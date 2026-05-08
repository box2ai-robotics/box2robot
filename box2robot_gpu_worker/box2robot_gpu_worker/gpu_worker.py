"""
GPU Worker — 注册为算力设备, 绑定后自动领取训练任务.

Usage:
    conda activate b2r
    b2r-gpu --server https://robot.box2ai.com

    # 指定输出目录
    b2r-gpu --server https://robot.box2ai.com --output outputs
"""
import argparse
import json
import logging
import os
import platform
import shutil
import sys
import time
from pathlib import Path

import httpx
import torch

from box2robot_gpu_worker import __version__ as WORKER_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("lerobot.datasets.video_utils").setLevel(logging.WARNING)  # torchcodec fallback warning
logger = logging.getLogger("b2r-gpu")


def _setup_file_logging() -> None:
    """无论交互式启动还是 nohup, 都把 b2r-gpu 日志落到 ~/.b2r-gpu/worker.log
    (10MB × 5 滚转), 方便事后通过 cloud manager SSH 查看. 不影响 stdout."""
    try:
        from logging.handlers import RotatingFileHandler
        log_dir = Path.home() / ".b2r-gpu"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "worker.log"
        handler = RotatingFileHandler(
            str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
        # 加到 root, 这样所有子 logger (b2r-gpu / b2r / lerobot 等) 都会写入
        root = logging.getLogger()
        # 防止重复添加 (b2r-gpu 进程被 reload 时)
        already = any(getattr(h, "_b2r_file_log", False) for h in root.handlers)
        if not already:
            handler._b2r_file_log = True  # type: ignore[attr-defined]
            root.addHandler(handler)
            logger.info("[LOG] file logging → %s", log_file)
    except Exception as e:
        # 文件 log 失败不影响主流程, 只是少了一个排错手段
        logger.warning("[LOG] file logging setup failed: %s", e)


_setup_file_logging()


def get_hw_info() -> dict:
    """Collect local hardware information (static specs)."""
    info = {
        "gpu_name": "N/A",
        "vram_gb": 0,
        "ram_gb": 0,
        "disk_free_gb": 0,
        "os": f"{platform.system()} {platform.release()}",
        "cuda_version": "",
        "python_version": platform.python_version(),
        "torch_version": torch.__version__,
    }

    # GPU
    if torch.cuda.is_available():
        info["gpu_name"] = torch.cuda.get_device_name(0)
        info["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1)
        info["cuda_version"] = torch.version.cuda or ""

    # RAM
    try:
        import psutil
        info["ram_gb"] = round(psutil.virtual_memory().total / 1024**3, 1)
    except ImportError:
        pass

    # Disk
    try:
        total, used, free = shutil.disk_usage(Path.cwd())
        info["disk_free_gb"] = round(free / 1024**3, 1)
    except Exception:
        pass

    return info


def _find_nvidia_smi() -> str:
    """Find nvidia-smi path (Windows needs full path, Linux/Ubuntu is in PATH)."""
    import subprocess
    # Linux/macOS: usually in PATH
    if platform.system() != "Windows":
        return "nvidia-smi"
    # Windows: try common locations
    for prog_dir in [os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramW6432", r"C:\Program Files")]:
        smi = os.path.join(prog_dir, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")
        if os.path.isfile(smi):
            return smi
    # Fallback: hope it's in PATH
    return "nvidia-smi"


# Cache nvidia-smi path at module load
_NVIDIA_SMI = _find_nvidia_smi()

# Pre-warm psutil cpu_percent (first call with interval=0 always returns 0)
try:
    import psutil as _psutil
    _psutil.cpu_percent(interval=None)
except Exception:
    pass


def get_usage_stats() -> dict:
    """Collect real-time resource usage. Works on Windows + Ubuntu."""
    import subprocess
    stats = {"cpu_pct": 0, "ram_used_gb": 0, "vram_used_gb": 0, "gpu_pct": 0}

    # CPU + RAM (psutil, cross-platform)
    try:
        import psutil
        stats["cpu_pct"] = round(psutil.cpu_percent(interval=None), 1)
        mem = psutil.virtual_memory()
        stats["ram_used_gb"] = round(mem.used / 1024**3, 1)
    except ImportError:
        pass

    # GPU utilization + VRAM used (nvidia-smi, system-level, not just PyTorch)
    if torch.cuda.is_available():
        try:
            result = subprocess.run(
                [_NVIDIA_SMI,
                 "--query-gpu=utilization.gpu,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                creationflags=0x08000000 if platform.system() == "Windows" else 0,  # CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split("\n")[0].split(",")
                if len(parts) >= 2:
                    stats["gpu_pct"] = int(parts[0].strip())
                    stats["vram_used_gb"] = round(int(parts[1].strip()) / 1024, 1)
        except Exception:
            # Fallback: PyTorch memory (only captures PyTorch allocations)
            stats["vram_used_gb"] = round(torch.cuda.memory_reserved(0) / 1024**3, 1)

    # Disk (refresh)
    try:
        _, _, free = shutil.disk_usage(Path.cwd())
        stats["disk_free_gb"] = round(free / 1024**3, 1)
    except Exception:
        pass

    return stats


def _preflight(server_url: str, output_dir: Path) -> bool:
    """启动前完整自检 — 依赖 + 系统资源 + 网络 + 写入权限.

    每次 b2r-gpu 启动都跑一遍, 输出统一的 [READY] / [WARNING] / [BLOCKED] 报告:
    - BLOCKED: 关键问题, worker 无法启动 → 返回 False, sys.exit(1)
    - WARNING: 可启动但部分功能受影响 (如 VLA 训练依赖缺) → 仅警告
    - READY: 所有项通过

    检查项:
    1. Python 依赖 (av/numpy/torch/lerobot/...)
    2. CUDA-torch 兼容
    3. 磁盘空间 (HF 缓存 + base ckpt 下载需要 ~30GB)
    4. HF Hub 连通性 (下载 base 模型依赖)
    5. Server URL 连通性
    6. 写入权限 (outputs/ datasets/ cache/ HF cache)
    """
    issues = []  # [(level, category, msg, fix_hint)]

    # ===== 1. Python 依赖 =====
    _check_dependencies(issues)
    # ===== 2. CUDA-torch 兼容 =====
    _check_cuda_torch(issues)
    # ===== 3. 磁盘空间 =====
    _check_disk_space(issues, output_dir)
    # ===== 4. 网络 =====
    _check_hf_hub(issues)
    _check_server_url(issues, server_url)
    # ===== 5. 写入权限 =====
    _check_write_permissions(issues, output_dir)

    # ===== 输出统一报告 =====
    blocked = [i for i in issues if i[0] == "BLOCKED"]
    warnings_ = [i for i in issues if i[0] == "WARNING"]

    print()
    print("=" * 60)
    print("  Box2Robot GPU Worker — 启动自检")
    print("=" * 60)
    if not issues:
        print("  [READY] 全部 6 项检查通过, 可以启动")
        print("=" * 60)
        return True

    if warnings_:
        print(f"  [WARNING] {len(warnings_)} 项可启动但受限:")
        for _, cat, msg, hint in warnings_:
            print(f"    - {cat}: {msg}")
            if hint:
                print(f"      修复: {hint}")

    if blocked:
        print(f"  [BLOCKED] {len(blocked)} 项致命, worker 无法启动:")
        for _, cat, msg, hint in blocked:
            print(f"    - {cat}: {msg}")
            if hint:
                print(f"      修复: {hint}")
        print()
        print("  完整体检: python scripts/check_gpu.py")
        print("  一键修复 (Windows): scripts\\setup_windows.bat")
        print("  一键修复 (Linux):   bash scripts/setup_linux.sh")
        print("=" * 60)
        return False

    print("=" * 60)
    return True


def _check_dependencies(issues: list):
    """Python 依赖 import 检查."""
    import importlib
    # (import_name, pip_pkg, hard_fail, fix_hint)
    deps = [
        ("numpy",        "numpy",        True,  "pip install numpy"),
        ("httpx",        "httpx",        True,  "pip install httpx"),
        ("pyarrow",      "pyarrow",      True,  "pip install pyarrow"),
        ("yaml",         "pyyaml",       True,  "pip install pyyaml"),
        ("psutil",       "psutil",       True,  "pip install psutil"),
        ("lerobot",      "lerobot",      True,
         "cd lerobot && pip install -e . --no-build-isolation"),
        ("av",           "av",           True,
         'pip install "av>=15.0.0,<16.0.0"   # 或 pip install "lerobot[dataset] @ file:./lerobot"'),
        # datasets: lerobot.datasets/__init__.py 顶部强制 require_package("datasets"),
        # 缺它 import lerobot.datasets 就抛 "No module named 'lerobot.datasets'".
        # ACT/Diffusion 也用 lerobot.datasets 加载训练数据 → 升级到 BLOCKED 级.
        ("datasets",     "datasets",     True,
         'pip install datasets    # 或 pip install "lerobot[dataset] @ file:./lerobot"'),
        # VLA-only, 仅 warning
        ("transformers", "transformers", False, "pip install transformers accelerate"),
        ("accelerate",   "accelerate",   False, "pip install accelerate"),
        # peft: LoRA 微调依赖. 仅当用户在 APP 训练页开启 'LoRA 微调' 才需要;
        # 不开启则不影响. 启动时只 WARNING 不 BLOCK.
        ("peft",         "peft",         False,
         'pip install peft accelerate    # 启用 LoRA 微调 (顶层 --peft.method_type=LORA)'),
    ]
    for import_name, pip_pkg, hard_fail, fix_hint in deps:
        try:
            importlib.import_module(import_name)
        except ImportError:
            level = "BLOCKED" if hard_fail else "WARNING"
            cat = "依赖" if hard_fail else "VLA 依赖"
            issues.append((level, cat, f"{pip_pkg} 未安装", fix_hint))


def _check_cuda_torch(issues: list):
    """CUDA 驱动版本 vs torch 编译 CUDA 是否兼容."""
    if not torch.cuda.is_available():
        compiled_cuda = getattr(torch.version, "cuda", None)
        if compiled_cuda is None:
            issues.append((
                "BLOCKED", "GPU",
                "PyTorch 是 CPU build, GPU 不可用",
                "pip install torch torchvision torchaudio "
                "--index-url https://download.pytorch.org/whl/cu124",
            ))
        else:
            issues.append((
                "WARNING", "GPU",
                f"torch 编译 CUDA {compiled_cuda} 但运行时 GPU 不可用 (驱动太旧?)",
                "运行 nvidia-smi 检查驱动版本; 必要时升级",
            ))


def _check_disk_space(issues: list, output_dir: Path):
    """磁盘空间 — HF 缓存 + base ckpt 下载需要 ~30GB."""
    import shutil as _sh
    try:
        _, _, free = _sh.disk_usage(str(output_dir.resolve()))
        free_gb = free / 1024**3
        if free_gb < 5:
            issues.append((
                "BLOCKED", "磁盘",
                f"剩余空间 {free_gb:.1f}GB, 不够基本运行 (要 5GB+)",
                "清理磁盘或更换 --output 目录",
            ))
        elif free_gb < 30:
            issues.append((
                "WARNING", "磁盘",
                f"剩余空间 {free_gb:.1f}GB, VLA 模型 (pi0 ~10GB / pi05 ~10GB) 可能装不下",
                "腾出 30GB+ 推荐",
            ))
    except Exception as e:
        issues.append(("WARNING", "磁盘", f"无法检测: {e}", ""))


def _check_hf_hub(issues: list):
    """HF Hub 连通性 — pi0/pi05/smolvla base 都从 HF 下载."""
    import urllib.request
    import urllib.error
    try:
        # HEAD 请求, 不下载数据, 5 秒超时
        req = urllib.request.Request("https://huggingface.co", method="HEAD")
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        issues.append((
            "WARNING", "HF Hub",
            f"无法访问 huggingface.co ({e}), VLA 训练下载 base 会失败",
            "检查网络/代理; 国内可设 HF_ENDPOINT=https://hf-mirror.com",
        ))


def _check_server_url(issues: list, server_url: str):
    """Server URL 连通性 — worker 必须能连 server."""
    import urllib.request
    import urllib.error
    if not server_url:
        issues.append(("BLOCKED", "Server", "未指定 --server URL", ""))
        return
    try:
        req = urllib.request.Request(server_url.rstrip("/"), method="HEAD")
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.HTTPError,) as e:
        # 4xx/5xx 也算可达 (server 在线只是 HEAD 不被支持)
        if 400 <= e.code < 600:
            return
        issues.append((
            "BLOCKED", "Server",
            f"无法访问 {server_url}: HTTP {e.code}",
            "检查 URL 是否正确; server 是否在运行",
        ))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        issues.append((
            "BLOCKED", "Server",
            f"无法连接 {server_url}: {e}",
            "检查网络 / server 地址 / 端口防火墙",
        ))


def _check_write_permissions(issues: list, output_dir: Path):
    """写入权限 — outputs/ datasets/ cache/ HF cache 都要可写."""
    import os as _os
    pkg_root = Path(__file__).parent.parent
    paths = [
        ("output_dir", output_dir),
        ("datasets", pkg_root / "datasets"),
        ("cache", pkg_root / "cache"),
        ("HF cache", Path(_os.path.expanduser("~/.cache/huggingface"))),
    ]
    for label, p in paths:
        try:
            p.mkdir(parents=True, exist_ok=True)
            test_file = p / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
        except Exception as e:
            issues.append((
                "BLOCKED", "写入权限",
                f"{label} ({p}) 不可写: {e}",
                f"chmod +w {p}; 或用其它 --output 目录",
            ))


class GPUWorker:
    """GPU Worker: register → bind → poll jobs → train → report."""

    def __init__(self, server_url: str, output_dir: str = "outputs"):
        self.server_url = server_url.rstrip("/")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(timeout=30)
        self.device_id = ""
        self.token = ""
        self.hw_info = get_hw_info()

        # === Multi-slot 并发支持 (v0.6.3+) ===
        # _slots: job_id → {"thread": Thread, "job": dict, "action": str, "started_at": ts,
        #                   "estimated_vram_gb": float}
        # 显存允许时多个 slot 同时跑训练 + 推理, server 看到一个 device 多个 active job.
        # 资源上限 (_max_vram_gb / _max_ram_gb): 用户在 APP GPU 配置页设, 默认 90%/50%,
        # 既保护系统留余量, 也让 worker 不超出用户预算 (即使物理 free 还多).
        import threading as _t
        self._slots: dict = {}
        self._slots_lock = _t.Lock()
        self._max_concurrent: int = 1
        self._max_vram_gb: float = 0.0   # 0 = 走物理上限 (兼容老 server 没下发)
        self._max_ram_gb: float = 0.0

        # 兼容: 旧字段 _active_job_id 不再使用 (心跳改上报 active_job_ids list).
        # 保留 attribute 以防 _process_job 等老代码引用; 实际逻辑迁移到 _slots.
        self._active_job_id: str = ""

        # Hardening H: 心跳里 server 下发的 should_stop_jobs (set of job_id 字符串).
        # 由 _heartbeat 维护; 当前主要做日志/调试用 (实际 stop 走 progress→409 通道).
        self._h_stop_set: set = set()

    def run(self):
        """Main loop: activate → wait for bind → poll jobs → train."""
        # Step 0: 完整启动自检 — 依赖 + GPU + 磁盘 + 网络 + 写入权限
        # BLOCKED 项任意一个不通过 → 直接退出, 给出修复指令.
        # WARNING 项 (如 VLA 依赖缺失) 仅警告, ACT/Diffusion 训练不受影响.
        if not _preflight(self.server_url, self.output_dir):
            sys.exit(1)

        self._print_banner()

        # Step 1: Activate (register with server)
        result = self._activate()
        if not result:
            return

        if result["status"] == "need_bind":
            bind_code = result["bind_code"]
            self.device_id = result["device_id"]
            print()
            print("=" * 50)
            print(f"  绑定码: {bind_code}")
            print(f"  设备ID: {self.device_id}")
            print()
            print("  请在 APP 中输入绑定码完成绑定")
            print("  (等待绑定中...)")
            print("=" * 50)
            print()

            # Poll until bound
            if not self._wait_for_bind():
                print("绑定超时 (5分钟)")
                return

        elif result["status"] == "activated":
            self.device_id = result["device_id"]
            self.token = result["token"]
            logger.info("Already bound: %s", self.device_id)

        print()
        logger.info("GPU Worker 已就绪, 开始监听训练任务...")
        logger.info("设备: %s | GPU: %s | VRAM: %.1fGB",
                     self.device_id, self.hw_info["gpu_name"], self.hw_info["vram_gb"])
        print()

        # Step 2: Main loop — heartbeat + poll jobs
        self._main_loop()

    def _print_banner(self):
        print()
        print("=" * 50)
        print(f"  Box2Robot GPU Worker v{WORKER_VERSION}")
        print(f"  Server: {self.server_url}")
        print(f"  GPU: {self.hw_info['gpu_name']}")
        print(f"  VRAM: {self.hw_info['vram_gb']} GB")
        print(f"  RAM: {self.hw_info['ram_gb']} GB")
        print(f"  Disk: {self.hw_info['disk_free_gb']} GB free")
        print(f"  PyTorch: {self.hw_info['torch_version']}")
        print(f"  CUDA: {self.hw_info['cuda_version'] or 'N/A'}")
        print("=" * 50)

        # Warn if GPU not available
        if not torch.cuda.is_available():
            print()
            print("!" * 50)
            print("  WARNING: GPU 不可用!")
            cuda_ver = torch.version.cuda
            if not cuda_ver:
                print("  原因: 安装的是 CPU 版本的 PyTorch")
                print()
                print("  修复: 重新安装 CUDA 版本的 PyTorch:")
                print("    pip uninstall torch torchvision torchaudio -y")
                print("    pip install torch torchvision torchaudio \\")
                print("        --index-url https://download.pytorch.org/whl/cu124")
            else:
                print(f"  PyTorch 编译的 CUDA 版本: {cuda_ver}")
                print("  可能原因: NVIDIA 驱动版本太旧")
                print("  请运行 nvidia-smi 检查驱动版本")
            print()
            print("  诊断工具: python scripts/check_gpu.py")
            print("!" * 50)
            print()

    def _activate(self) -> dict:
        try:
            from box2robot_gpu_worker.protocol import API_VERSION as _API_VERSION
            payload = {**self.hw_info, "fw_version": WORKER_VERSION, "api_version": _API_VERSION}
            r = self.client.post(f"{self.server_url}/api/gpu/activate", json=payload)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("Server connection failed: %s", e)
            return {}

    def _wait_for_bind(self, timeout=300) -> bool:
        """Poll server until device is bound."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(3)
            try:
                # Re-activate to check if bound
                r = self.client.post(f"{self.server_url}/api/gpu/activate", json=self.hw_info)
                r.raise_for_status()
                data = r.json()
                if data.get("status") == "activated":
                    self.device_id = data["device_id"]
                    self.token = data["token"]
                    logger.info("绑定成功!")
                    return True
            except Exception:
                pass
            remaining = int(timeout - (time.time() - t0))
            print(f"\r  等待绑定... ({remaining}s)", end="", flush=True)
        print()
        return False

    def _main_loop(self):
        """Multi-slot 并发主循环.

        v0.6.3+: 一个 worker 进程同时跑 max_concurrent 个 job (训练 / 推理混跑).
        从 server /api/gpu/config 拉 max_concurrent (用户在 APP GPU 配置页设),
        每空一个 slot 就 poll 一次, 在显存允许范围内并发处理.

        线程模型:
        - 主循环 (本函数): 心跳 / poll / 收割已完成 slot
        - 每个 job 一个 daemon thread (_slot_runner) 跑 _process_job
        - _slots dict 用 _slots_lock 保护
        - 心跳带 active_job_ids 列表, server 逐个刷 liveness
        """
        heartbeat_interval = 10
        poll_interval = 5
        upgrade_interval = 60
        config_refresh_interval = 30   # 用户改 max_concurrent 后 30s 内生效
        last_heartbeat = 0
        last_upgrade_check = 0
        last_config_refresh = 0

        # 启动时拉一次配置 (得到 max_concurrent 默认值)
        self._refresh_concurrent_config()

        try:
            while True:
                now = time.time()

                if now - last_heartbeat >= heartbeat_interval:
                    self._heartbeat()
                    last_heartbeat = now
                if now - last_upgrade_check >= upgrade_interval:
                    self._check_upgrade()
                    last_upgrade_check = now
                if now - last_config_refresh >= config_refresh_interval:
                    self._refresh_concurrent_config()
                    last_config_refresh = now

                # 收割已完成的 slot (slot_runner finally 已 pop, 这里兜底清残留)
                self._reap_finished_slots()

                # 检查空槽位 + 领取任务: 每个空槽 poll 一次, server 端 atomic claim 防重
                with self._slots_lock:
                    free_slots = max(0, self._max_concurrent - len(self._slots))
                claimed = 0
                vram_blocked = False
                for _ in range(free_slots):
                    job, action, resume_step = self._poll_job()
                    if not job:
                        break  # 没任务可领, 后续空 slot 也无意义
                    # === VRAM 决策: 物理 free + 用户 budget 双限制 ===
                    need_gb = self._estimate_job_vram(job, action)
                    ok, vinfo = self._has_enough_vram(need_gb)
                    if not ok:
                        # 区分被哪一边卡住, 给用户精准提示 (会写到 job.error_msg)
                        if vinfo["blocker"] == "budget":
                            reason = (
                                f"超出 max_vram_gb 预算 — 模型估算 {need_gb:.1f}GB + "
                                f"冗余 {self.VRAM_RESERVE_GB:.1f}GB, 用户预算 "
                                f"{vinfo['max_vram_gb']:.1f}GB 已承诺给 {len(self._slots)} 个 slot "
                                f"({vinfo['slots_committed_gb']:.1f}GB), 剩 "
                                f"{vinfo['budget_remaining_gb']:.1f}GB. "
                                f"建议: APP GPU 配置页调大 max_vram_gb, 或减小 batch_size, "
                                f"或开 LoRA / gradient_checkpointing."
                            )
                        else:
                            reason = (
                                f"GPU 物理显存不够 — 估算 {need_gb:.1f}GB + 冗余 "
                                f"{self.VRAM_RESERVE_GB:.1f}GB > 实际空闲 "
                                f"{vinfo['physical_free_gb']:.1f}GB / 总 "
                                f"{vinfo['physical_total_gb']:.1f}GB. "
                                f"建议: 等当前 slot 跑完, 或终止其它占用进程."
                            )
                        logger.warning("[VRAM] %s 拒绝 job %s (model=%s batch=%s)",
                                        vinfo["blocker"].upper(),
                                        job["id"], job.get("model_type"),
                                        job.get("batch_size"))
                        logger.warning("    %s", reason)
                        self._release_job_to_pending(job["id"], reason)
                        vram_blocked = True
                        break  # 等 slot 释放再试, 不再 poll 下一个
                    self._spawn_slot(job, action, resume_step,
                                      estimated_vram_gb=need_gb)
                    claimed += 1
                # 没领到任务 (server 没活 / vram 不够) → 慢轮询, 别空转
                if not claimed:
                    time.sleep(poll_interval if not vram_blocked else poll_interval * 2)

        except KeyboardInterrupt:
            print("\nWorker stopped. 等待 active job 退出...")
            self._wait_slots_exit(timeout=30)

    # =========== 显存预估 + 决策 (v0.6.3+) ============
    # 拿到 job 后, 基于 model_type / batch_size / 训练参数 (LoRA / expert_only /
    # gradient_checkpointing) 估算它跑起来的显存峰值, 跟当前 GPU 空闲显存比较, 留 2GB 冗余.
    # 装不下就主动 release 回 server (status downloading → pending), 等 slot 释放再领.

    VRAM_RESERVE_GB = 2.0   # 必留冗余 (PyTorch 内部 allocator 抖动 / cuda kernel 占用)

    # 训练静态 base 显存 (GB) — 模型参数 + 优化器 state, 不含 batch
    _VRAM_BASE_TRAIN = {
        "act": 2.0, "diffusion": 3.0,
        "smolvla": 6.0,
        "pi0": 14.0, "pi0_fast": 14.0, "pi05": 14.0,
        "groot": 10.0,
    }
    # 每个 batch sample 的额外显存 (GB) — activations + gradients
    _VRAM_PER_BATCH_TRAIN = {
        "act": 0.05, "diffusion": 0.05,
        "smolvla": 0.10,
        "pi0": 0.50, "pi0_fast": 0.50, "pi05": 0.50,
        "groot": 0.20,
    }
    # 推理静态显存 — base 加载好就 ok, 没有 gradient
    _VRAM_BASE_INFER = {
        "act": 2.0, "diffusion": 3.0,
        "smolvla": 4.0,
        "pi0": 8.0, "pi0_fast": 8.0, "pi05": 8.0,
        "groot": 6.0,
    }

    @classmethod
    def _estimate_job_vram(cls, job: dict, action: str) -> float:
        """估算 job 跑起来的显存峰值 (GB).

        粗略表 (实际跟 image resolution / num_workers / chunk_size 相关), 但已经够
        给"能不能加塞"做决策. 推理走 _VRAM_BASE_INFER (无训练增量); 训练走
        base + per_batch * batch_size, 然后乘上 LoRA / expert_only / grad_ckpt 的折扣.
        """
        model_type = (job.get("model_type") or "act").lower()
        batch_size = int(job.get("batch_size") or 32)
        custom = job.get("custom_params") or {}
        if isinstance(custom, str):
            try:
                import json as _j
                custom = _j.loads(custom)
            except Exception:
                custom = {}

        if action == "inference":
            return cls._VRAM_BASE_INFER.get(model_type, 4.0)

        base = cls._VRAM_BASE_TRAIN.get(model_type, 4.0)
        per_batch = cls._VRAM_PER_BATCH_TRAIN.get(model_type, 0.05)

        def _flag(key, default="false"):
            v = str(custom.get(key, default)).lower()
            return v in ("true", "1", "yes", "on")

        # LoRA 微调: 只训 adapter, base 权重 frozen, 显存约 50%
        if _flag("peft_enable"):
            base *= 0.5
            per_batch *= 0.5
        # expert_only (pi0/pi05): 冻结 VLM, 显存约 70%
        elif model_type in ("pi0", "pi0_fast", "pi05") and _flag("train_expert_only"):
            base *= 0.7
            per_batch *= 0.7
        # SmolVLA 默认就 train_expert_only=true (config 默认), 已经是低显存版本

        # gradient checkpointing: VLA 默认开, 用计算时间换显存约 30%
        gc_default = "true" if model_type in ("pi0", "pi0_fast", "pi05", "smolvla") else "false"
        if _flag("gradient_checkpointing", gc_default):
            per_batch *= 0.7

        return base + per_batch * batch_size

    def _has_enough_vram(self, need_gb: float) -> tuple[bool, dict]:
        """检查显存是否够装 need_gb 的新 job.

        双层判断 (取严格):
        1. 物理空闲 (torch.cuda.mem_get_info): GPU 真实剩余, 受外部进程占用影响
        2. 用户预算 (max_vram_gb - 已分配给现有 slot 的总和): 用户在 APP 设的上限,
           即使物理还有更多空闲也不超出. 默认 max_vram_gb = 物理 vram * 0.9.

        返回 (是否够, info dict 含决策细节). info 用于报错日志和 release reason.
        """
        info = {
            "need_gb": need_gb,
            "reserve_gb": self.VRAM_RESERVE_GB,
            "physical_free_gb": 0.0,
            "physical_total_gb": 0.0,
            "max_vram_gb": self._max_vram_gb,
            "slots_committed_gb": 0.0,
            "budget_remaining_gb": 0.0,
            "blocker": "",   # 'physical' / 'budget' / '' (ok)
        }
        # 已承诺给现有 slot 的预算 (estimated_vram 总和)
        with self._slots_lock:
            info["slots_committed_gb"] = sum(
                float(s.get("estimated_vram_gb", 0)) for s in self._slots.values()
            )
        # 物理实测
        try:
            import torch
            if torch.cuda.is_available():
                free_b, total_b = torch.cuda.mem_get_info()
                info["physical_free_gb"] = free_b / (1024 ** 3)
                info["physical_total_gb"] = total_b / (1024 ** 3)
        except Exception as e:
            logger.warning("[VRAM] mem_get_info failed: %s", e)
            info["physical_free_gb"] = float("inf")  # 检测不了就放行
        # 用户预算上限 (max_vram_gb=0 表示无限制)
        if self._max_vram_gb > 0:
            info["budget_remaining_gb"] = max(0.0, self._max_vram_gb - info["slots_committed_gb"])
        else:
            info["budget_remaining_gb"] = float("inf")
        # 必须同时满足两个限制
        usable = min(info["physical_free_gb"], info["budget_remaining_gb"]) - self.VRAM_RESERVE_GB
        ok = need_gb <= usable
        if not ok:
            if info["budget_remaining_gb"] <= info["physical_free_gb"]:
                info["blocker"] = "budget"
            else:
                info["blocker"] = "physical"
        return ok, info

    def _release_job_to_pending(self, job_id: str, reason: str):
        """显存装不下时, 把 claim 的 job (status=downloading) 推回 pending.

        走 server 的 update_status 接口 (downloading → pending 已加进状态机).
        失败时 worker 这边会跳过这个 job 等下次循环, server 那边 stale check
        180s 后会自动判定 GPU 离线挂起 interrupted.
        """
        try:
            r = self.client.post(
                f"{self.server_url}/api/training/jobs/{job_id}/status",
                json={"status": "pending", "error_msg": f"VRAM-defer: {reason}",
                      "key": ""},
                timeout=5,
            )
            if r.status_code == 200:
                logger.info("[VRAM] released job %s back to pending: %s", job_id, reason)
            else:
                logger.warning("[VRAM] release %s failed: HTTP %d %s",
                                job_id, r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("[VRAM] release %s exception: %s", job_id, e)

    # =========== Multi-slot 并发: slot 管理 ============

    def _refresh_concurrent_config(self):
        """从 server 拉运行时配置 (max_concurrent + 资源上限). 30s 一次, 用户改完 30s 内生效."""
        try:
            r = self.client.get(
                f"{self.server_url}/api/gpu/config",
                params={"device_id": self.device_id, "token": self.token},
                timeout=5,
            )
            r.raise_for_status()
            self._apply_config(r.json(), source="poll")
        except Exception:
            pass

    def _apply_config(self, cfg: dict, source: str = "?"):
        """应用 server 下发的 config (max_concurrent / max_vram_gb / max_ram_gb).

        来源 source: 'poll' = 30s 主动拉; 'heartbeat' = 心跳响应 (10s, 更快).
        改动只在变化时打 log, 同源重复 cfg 不刷屏.
        """
        if not isinstance(cfg, dict):
            return
        new_mc = cfg.get("max_concurrent")
        if new_mc is not None:
            new_mc = max(1, int(new_mc))
            if new_mc != self._max_concurrent:
                logger.info("[SLOT] max_concurrent: %d → %d (来源: %s)",
                             self._max_concurrent, new_mc, source)
                self._max_concurrent = new_mc
        new_vram = cfg.get("max_vram_gb")
        if new_vram is not None:
            new_vram = float(new_vram)
            if new_vram > 0 and abs(new_vram - self._max_vram_gb) > 0.05:
                logger.info("[SLOT] max_vram_gb: %.1f → %.1f GB (来源: %s)",
                             self._max_vram_gb, new_vram, source)
                self._max_vram_gb = new_vram
        new_ram = cfg.get("max_ram_gb")
        if new_ram is not None:
            new_ram = float(new_ram)
            if new_ram > 0 and abs(new_ram - self._max_ram_gb) > 0.05:
                logger.info("[SLOT] max_ram_gb: %.1f → %.1f GB (来源: %s)",
                             self._max_ram_gb, new_ram, source)
                self._max_ram_gb = new_ram

    def _spawn_slot(self, job: dict, action: str, resume_step, estimated_vram_gb: float = 0):
        """启动一个 daemon thread 跑 job, 加进 _slots (含 vram 估算用于后续决策)."""
        import threading as _t
        job_id = job["id"]
        with self._slots_lock:
            if job_id in self._slots:
                logger.warning("[SLOT] job %s already in slot, skip", job_id)
                return
            thread = _t.Thread(
                target=self._slot_runner,
                args=(job, action, resume_step),
                daemon=True,
                name=f"slot-{job_id}",
            )
            self._slots[job_id] = {
                "thread": thread,
                "job": job,
                "action": action,
                "started_at": time.time(),
                "estimated_vram_gb": estimated_vram_gb,
            }
            active = len(self._slots)
            committed = sum(float(s.get("estimated_vram_gb", 0)) for s in self._slots.values())
        thread.start()
        logger.info("[SLOT] started job=%s action=%s vram_est=%.1fGB (active %d/%d, "
                     "committed %.1f/%.1fGB)",
                     job_id, action, estimated_vram_gb, active, self._max_concurrent,
                     committed, self._max_vram_gb or self.hw_info.get("vram_gb", 0))

    def _slot_runner(self, job: dict, action: str, resume_step):
        """单 slot 入口, 复用现有 _process_job. 完成 / 异常都从 _slots 移除."""
        job_id = job["id"]
        try:
            self._process_job(job, action, resume_step)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error("[SLOT] job %s failed in slot: %s", job_id, e, exc_info=True)
        finally:
            with self._slots_lock:
                self._slots.pop(job_id, None)
                active = len(self._slots)
            logger.info("[SLOT] finished job=%s (active %d/%d)",
                         job_id, active, self._max_concurrent)

    def _reap_finished_slots(self):
        """兜底清理已经死掉但还在 _slots 里的条目."""
        with self._slots_lock:
            dead = [jid for jid, info in self._slots.items()
                     if not info["thread"].is_alive()]
            for jid in dead:
                self._slots.pop(jid, None)

    def _wait_slots_exit(self, timeout: int = 30):
        """KeyboardInterrupt 时等所有 slot 优雅退出."""
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            with self._slots_lock:
                if not self._slots:
                    return
                remain = list(self._slots.keys())
            logger.info("等待 %d 个 slot 退出: %s", len(remain), remain)
            _time.sleep(2)

    def _heartbeat(self):
        """Send heartbeat with updated hw info + real-time usage + active job ids.

        v0.6.3+: 上报 active_job_ids 列表 (multi-slot). 旧字段 active_job_id (单个)
        保留 兼容老 server, 取列表第一个.
        """
        usage = get_usage_stats()
        # Update disk_free in hw_info
        if "disk_free_gb" in usage:
            self.hw_info["disk_free_gb"] = usage.pop("disk_free_gb")

        # api_version: v1.0+ 协议版本声明 (见 protocol.py); 老 server 会忽略
        from box2robot_gpu_worker.protocol import API_VERSION as _API_VERSION
        payload = {
            "api_version": _API_VERSION,
            "device_id": self.device_id,
            "token": self.token,
            "fw_version": WORKER_VERSION,
            "gpu_info": self.hw_info,
            "usage": usage,
        }
        # 上报当前活跃 job_ids list (multi-slot). 旧 server 还会看 active_job_id 单字段,
        # 取第一个兼容. 没活跃 slot 则两个都不传.
        with self._slots_lock:
            active_ids = list(self._slots.keys())
        if active_ids:
            payload["active_job_ids"] = active_ids
            payload["active_job_id"] = active_ids[0]

        try:
            r = self.client.post(f"{self.server_url}/api/gpu/heartbeat", json=payload)
            # 心跳响应里可能带 server 下发的 config (用户在 APP 改完, 1 心跳周期生效)
            if r.status_code == 200:
                try:
                    body = r.json()
                    if isinstance(body, dict) and "config" in body:
                        self._apply_config(body["config"], source="heartbeat")
                    # Hardening H: server 对账下发"该停的 job 列表".
                    # 实际停止信号仍走 progress→409 (worker.py progress_cb 已处理).
                    # 这里维护一个 stop set, 一是日志/告警, 二是清理已经死掉的 slot 占位.
                    stop_list = body.get("should_stop_jobs") if isinstance(body, dict) else None
                    if isinstance(stop_list, list) and stop_list:
                        stop_ids = {item.get("job_id", "") for item in stop_list
                                    if isinstance(item, dict)}
                        self._h_stop_set = stop_ids  # 公开给其他模块/调试用
                        with self._slots_lock:
                            for jid in list(self._slots.keys()):
                                if jid not in stop_ids:
                                    continue
                                slot = self._slots.get(jid)
                                thread = slot.get("thread") if isinstance(slot, dict) else None
                                if thread is not None and not thread.is_alive():
                                    # 线程已死但 slot 没清 → 直接清掉, 释放 vram 配额
                                    self._slots.pop(jid, None)
                                    logger.warning("[H-RECONCILE] Cleaned dead slot for job=%s "
                                                   "(server says terminal, thread dead)", jid)
                                else:
                                    # 线程还活着: progress 通道已经会让它停, 这里只告警
                                    logger.warning("[H-RECONCILE] Server says job=%s is terminal, "
                                                   "waiting for slot thread to exit via progress 409", jid)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Heartbeat failed: %s", e)

    def _check_upgrade(self):
        """Check server for new Worker version."""
        try:
            r = self.client.get(f"{self.server_url}/api/gpu/upgrade/check", params={
                "current_version": WORKER_VERSION,
                "device_id": self.device_id,
                "token": self.token,
            })
            r.raise_for_status()
            data = r.json()
            if data.get("available"):
                new_ver = data.get("version", "?")
                changelog = data.get("changelog", "")
                size = data.get("size", 0)
                logger.info("=" * 40)
                logger.info("发现新版本: v%s → v%s (%d KB)",
                             WORKER_VERSION, new_ver, size // 1024)
                if changelog:
                    logger.info("更新日志: %s", changelog)
                self._apply_upgrade(data)
        except Exception:
            pass  # Silently skip if server unreachable

    def _apply_upgrade(self, info: dict):
        """Download and apply upgrade package."""
        try:
            logger.info("下载升级包...")
            r = self.client.get(f"{self.server_url}/api/gpu/upgrade/download", params={
                "device_id": self.device_id,
                "token": self.token,
            })
            r.raise_for_status()

            # Save to temp file
            import zipfile
            import tempfile
            upgrade_dir = self.output_dir / "_upgrades"
            upgrade_dir.mkdir(parents=True, exist_ok=True)
            pkg_path = upgrade_dir / info.get("filename", "upgrade.zip")
            with open(pkg_path, "wb") as f:
                f.write(r.content)
            logger.info("升级包已下载: %s (%d KB)", pkg_path, len(r.content) // 1024)

            # Extract if zip
            if str(pkg_path).endswith(".zip"):
                extract_dir = upgrade_dir / "extracted"
                if extract_dir.exists():
                    shutil.rmtree(extract_dir)
                with zipfile.ZipFile(pkg_path) as zf:
                    zf.extractall(extract_dir)
                logger.info("已解压到: %s", extract_dir)

                # Check if there's a setup.py or pyproject.toml → pip install
                for root_dir in [extract_dir] + list(extract_dir.iterdir()):
                    if (root_dir / "setup.py").exists() or (root_dir / "pyproject.toml").exists():
                        logger.info("安装升级包: pip install -e %s", root_dir)
                        import subprocess
                        result = subprocess.run(
                            [sys.executable, "-m", "pip", "install", "-e", str(root_dir), "--quiet"],
                            capture_output=True, text=True,
                        )
                        if result.returncode == 0:
                            installed_ver = info.get("version", "?")
                            logger.info("升级安装成功 (v%s)", installed_ver)
                            self._maybe_restart_after_upgrade(new_ver=installed_ver)
                        else:
                            logger.error("升级安装失败: %s", result.stderr[:200])
                        break
            else:
                logger.info("非 zip 格式, 跳过自动安装. 请手动处理: %s", pkg_path)

            logger.info("=" * 40)
        except Exception as e:
            logger.error("升级失败: %s", e)

    def _maybe_restart_after_upgrade(self, new_ver: str = "?"):
        """升级后尝试自动重启 worker. 有活跃任务时跳过, 等任务结束后人工重启.

        Linux/macOS/Windows 都用 os.execv 替换当前进程, 子训练进程会随父死亡 (Linux 默认行为).
        所以必须确保当前没有活跃训练任务, 否则训练会被中断 (虽然 server 端 600s 后会标 interrupted
        让 worker 重新领取从 ckpt 续训, 但仍然浪费时间).
        """
        import os
        try:
            with self._slots_lock:
                n_slots = len(self._slots)
        except Exception:
            n_slots = 0
        if n_slots > 0:
            logger.warning(
                "升级到 v%s 已就位, 但有 %d 个活跃训练任务 — 暂不自动重启. "
                "任务结束后请手动重启 worker (`pkill -f b2r-gpu` 然后重启), "
                "或下次 worker 空闲时心跳检测会再次触发自动重启.",
                new_ver, n_slots)
            self._pending_restart = True  # 留个 flag, 主循环空闲时检查
            return
        logger.info("=" * 40)
        logger.info(">>> Worker 即将自动重启加载新版本 v%s ...", new_ver)
        logger.info("=" * 40)
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            logger.error("自动重启失败: %s. 请手动重启 worker.", e)

    def _poll_job(self) -> tuple:
        """Check for pending training or inference jobs. Returns (job, action, resume_from_step)."""
        try:
            r = self.client.get(f"{self.server_url}/api/gpu/poll-job",
                params={"device_id": self.device_id, "token": self.token})
            r.raise_for_status()
            data = r.json()
            return data.get("job"), data.get("action", "train"), data.get("resume_from_step")
        except Exception:
            return None, "train", None

    # _start_bg_heartbeat / _stop_bg_heartbeat 已废弃 (v0.6.3+):
    # 多 slot 模式下主循环已经 10s 一次心跳并上报 active_job_ids list,
    # 每个 slot 不再需要独立 heartbeat thread (避免 N 个线程重复刷 + 竞争).
    # 删除以避免误用.

    def _process_job(self, job: dict, action: str = "train", resume_from_step: int = None):
        """Route to training or inference based on action.

        v0.6.3+ multi-slot: 这个函数现在跑在 _slot_runner thread 内, _slots dict 已记录
        active job. 主循环 _main_loop 10s 一次心跳已上报 active_job_ids list, 不再需要
        per-slot 的 bg_heartbeat (多 slot 会开 N 个 thread 重复刷, 浪费).
        """
        job_id = job["id"]
        logger.info("=" * 40)

        try:
            if action == "inference":
                deploy_info = job.get("deploy_info", {})
                arm_id = deploy_info.get("arm_device_id", "")
                logger.info("推理部署: %s → 机械臂 %s (model=%s)",
                             job_id, arm_id, job["model_type"])
                self._run_inference(job, arm_id)
            else:
                if resume_from_step:
                    logger.info("恢复训练: %s (从 step %d, model=%s, steps=%d)",
                                 job_id, resume_from_step, job["model_type"], job["train_steps"])
                else:
                    logger.info("领取训练: %s (model=%s, steps=%d)",
                                 job_id, job["model_type"], job["train_steps"])
                from box2robot_gpu_worker.worker import TrainingWorker
                worker = TrainingWorker(
                    self.server_url,
                    pairing_key=job.get("pairing_key", ""),
                    output_dir=str(self.output_dir),
                )
                worker.process_job(job_id, resume_from_step=resume_from_step)
        except KeyboardInterrupt:
            logger.info("训练被手动中断 (Ctrl+C): %s", job_id)
            # 扫描已保存的 checkpoints，上报给 server (用绝对路径)
            model_dir = str((self.output_dir / job_id / "model").resolve())
            from box2robot_gpu_worker.worker import TrainingWorker
            ckpts = TrainingWorker._scan_checkpoints(model_dir)
            # 通过 status 通道一并上报 checkpoint 列表 (progress 通道在 cancelled 后会 409)
            self._report_status(job_id, "cancelled",
                                error_msg=f"Worker 手动停止 (已保存 {len(ckpts)} 个 checkpoint)" if ckpts else "Worker 手动停止",
                                model_path=model_dir if ckpts else None,
                                checkpoints=ckpts or None)
            raise  # 继续向上传播，退出 worker
        # _slot_runner finally 已 pop 掉 _slots 里的条目 + 主循环心跳已上报新 list,
        # 不需要在这里管 _active_job_id / bg_heartbeat.

        logger.info("任务完成: %s", job_id)
        logger.info("=" * 40)
        logger.info("继续监听下一个任务...")

    def _run_inference(self, job: dict, arm_device_id: str):
        """Load trained model and run inference loop against remote arm."""
        deploy_info = job.get("deploy_info", {})
        checkpoint_step = deploy_info.get("checkpoint_step")

        model_path = job.get("model_path", "")
        if not model_path:
            model_path = str((self.output_dir / job["id"] / "model").resolve())

        # 路径不存在时的兜底: DB 里可能存的是相对路径 (老 worker 写入)，或者来自其他 worker。
        # 用本机 output_dir 重建一次绝对路径，能救回本机已训好的模型。
        if not Path(model_path).exists():
            fallback = (self.output_dir / job["id"] / "model").resolve()
            if fallback.exists():
                logger.warning("model_path 不存在 (%s)，回退到本机 output_dir: %s", model_path, fallback)
                model_path = str(fallback)

        # 如果指定了 checkpoint_step, 定位到具体的 checkpoint 目录
        # LeRobot v3 用 6 位零填充目录名 (e.g. checkpoints/000200/), 老格式是 str(step).
        # 两种都试一遍, 兼容历史数据.
        if checkpoint_step is not None:
            ckpt_root = Path(model_path) / "checkpoints"
            candidates = [
                ckpt_root / f"{int(checkpoint_step):06d}" / "pretrained_model",
                ckpt_root / str(checkpoint_step) / "pretrained_model",
            ]
            ckpt_subdir = next((c for c in candidates if c.exists()), None)
            if ckpt_subdir is not None:
                logger.info("使用 checkpoint step %d: %s", checkpoint_step, ckpt_subdir)
                model_path = str(ckpt_subdir)
            else:
                logger.warning("Checkpoint %d 不存在 (试过 %s), 使用默认模型路径",
                               checkpoint_step, [str(c) for c in candidates])

        if not Path(model_path).exists():
            logger.error("模型不存在: %s (job=%s, output_dir=%s)",
                         model_path, job["id"], self.output_dir.resolve())
            # 推理部署阶段模型缺失 → 训练成果完好, 仅本次部署失败
            # 必须用 "completed" 而非 "failed", 否则训练任务会被永久标记为失败
            self._report_status(job["id"], "completed",
                                error_msg=f"推理部署失败: 模型路径不存在 {model_path}")
            return

        # 不改状态! 保持 deploying, run_inference_server 的 _should_stop 依赖此状态

        # 查找关联的摄像头设备
        camera_id = deploy_info.get("camera_device_id", "")

        execution_mode = deploy_info.get("execution_mode", "original")
        chunk_params = deploy_info.get("chunk_params", {})
        logger.info("执行模式: %s, 参数: %s", execution_mode, chunk_params)

        ctrl_c = False
        try:
            from box2robot_gpu_worker.worker import run_inference_server
            run_inference_server(
                model_dir=model_path,
                server_url=self.server_url,
                device_id=arm_device_id,
                token="",
                fps=20,
                camera_id=camera_id,
                job_id=job["id"],
                execution_mode=execution_mode,
                chunk_params=chunk_params,
            )
        except KeyboardInterrupt:
            ctrl_c = True
            logger.info("推理已停止 (Ctrl+C)")
        except Exception as e:
            logger.error("推理失败: %s", e)
            # 推理部署失败 → 恢复为 completed (训练结果完好), 仅把错误信息上报
            # 不能用 "failed", 否则训练任务会被标记为彻底失败、无法再次部署
            self._report_status(job["id"], "completed", error_msg=f"推理部署失败: {e}")
            # 释放可能已开启的力矩 / 摄像头流 (不再重复上报状态, 避免覆盖 error_msg)
            if arm_device_id:
                try:
                    self.client.post(f"{self.server_url}/api/device/{arm_device_id}/command",
                                     json={"torque": False})
                except Exception:
                    pass
            if camera_id:
                try:
                    self.client.post(f"{self.server_url}/api/camera/{camera_id}/stream/mode",
                                     json={"mode": "idle"})
                except Exception:
                    pass
            return

        # 停止推理 → 通知 Server 释放力矩 + 恢复 completed
        self._stop_inference_on_server(job["id"], arm_device_id, camera_id)

        if ctrl_c:
            raise KeyboardInterrupt  # 向上传播, 退出 Worker

    def _stop_inference_on_server(self, job_id: str, arm_id: str = "", camera_id: str = ""):
        """通知 Server 停止推理: 更新状态 + 释放力矩 + 摄像头回 idle"""
        # 1. 更新状态为 completed
        self._report_status(job_id, "completed")
        # 2. 额外确保力矩释放 (run_inference_server 已做一次, 这里是 server 侧冗余保障)
        if arm_id:
            try:
                self.client.post(f"{self.server_url}/api/device/{arm_id}/command",
                                 json={"torque": False})
                logger.info("力矩已释放: %s", arm_id)
            except Exception:
                pass
        if camera_id:
            try:
                self.client.post(f"{self.server_url}/api/camera/{camera_id}/stream/mode",
                                 json={"mode": "idle"})
            except Exception:
                pass

    def _report_status(self, job_id: str, status: str, error_msg: str = None,
                       model_path: str = None, checkpoints: list = None):
        try:
            data = {"status": status, "key": ""}
            if error_msg:
                data["error_msg"] = error_msg
            if model_path:
                data["model_path"] = model_path
            if checkpoints:
                data["checkpoints"] = checkpoints
            self.client.post(f"{self.server_url}/api/training/jobs/{job_id}/status", json=data)
        except Exception:
            pass

    def _report_progress(self, job_id: str, step: int, total: int, metrics: dict):
        try:
            self.client.post(f"{self.server_url}/api/training/jobs/{job_id}/progress",
                             json={"step": step, "total_steps": total, "metrics": metrics, "key": ""})
        except Exception:
            pass


def _setup_hf_cache(hf_cache_arg: str | None = None):
    """统一 HuggingFace cache 路径到项目内 base_model/, 训练/推理共享.

    pi05_base 约 14GB, smolvla_base 约 2GB, pi0_base 约 6GB. 不指定 cache 默认在
    `~/.cache/huggingface` (家目录), AutoDL 系统盘小易满, 且 worker 跨设备/重装时
    cache 散落各处. 统一放在项目内 box2robot_gpu_worker/base_model/ 后:
    - 训练 subprocess + 推理 共用一份 base ckpt
    - 重装环境/迁移设备时 base_model/ 跟代码一起带走 (或单独管理)
    - 第一次下载后, 后续训练/推理秒加载

    优先级:
    1. CLI --hf-cache 显式指定 → 用它
    2. 已设 HF_HOME 环境变量 → 不覆盖 (尊重用户)
    3. 默认: 项目内 box2robot_gpu_worker/base_model/

    HuggingFace cache 实际结构: <HF_HOME>/hub/models--<org>--<name>/snapshots/<sha>/
    例 base_model 下找 pi05_base ckpt:
       box2robot_gpu_worker/base_model/hub/models--lerobot--pi05_base/snapshots/<sha>/
    """
    import os
    if hf_cache_arg:
        os.environ["HF_HOME"] = os.path.expanduser(hf_cache_arg)
        logger.info("[HF_HOME] CLI override: %s", os.environ["HF_HOME"])
        return
    if os.environ.get("HF_HOME"):
        logger.info("[HF_HOME] already set: %s (用户配置, 不覆盖)", os.environ["HF_HOME"])
        return
    # 默认: 项目内 box2robot_gpu_worker/base_model/
    # gpu_worker.py 在 box2robot_gpu_worker/box2robot_gpu_worker/gpu_worker.py,
    # 上 2 级是项目根 box2robot_gpu_worker/.
    pkg_dir = Path(__file__).resolve().parent
    project_root = pkg_dir.parent
    base_model_dir = project_root / "base_model"
    try:
        base_model_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("[HF_HOME] cannot create %s: %s, falling back to ~/.cache/huggingface",
                        base_model_dir, e)
        return
    os.environ["HF_HOME"] = str(base_model_dir)
    logger.info("[HF_HOME] 设为项目内 base_model 目录: %s", base_model_dir)
    logger.info("    (训练/推理共享 base ckpt, 第一次下载后秒加载)")
    # 检测老 cache 提示用户迁移
    legacy_caches = [
        os.path.expanduser("~/.cache/huggingface/hub"),
        "/root/autodl-tmp/.cache/hub",
    ]
    target_hub = base_model_dir / "hub"
    for legacy in legacy_caches:
        if os.path.isdir(legacy) and not target_hub.exists():
            logger.info("[HF_HOME] 检测到老 cache: %s", legacy)
            logger.info("    建议迁移避免重复下载: ln -sfn %s %s", legacy, target_hub)
            logger.info("    或拷贝 (占空间): cp -r %s %s", legacy, target_hub)
            break


def _cmd_check_version(_args):
    from box2robot_gpu_worker.ota import print_check_version
    return print_check_version()


def _cmd_check_deps(_args):
    from box2robot_gpu_worker.ota import print_check_deps
    return print_check_deps()


def _cmd_upgrade(args):
    from box2robot_gpu_worker.ota import print_upgrade, DEFAULT_SERVER_URL
    return print_upgrade(
        force=getattr(args, "force", False),
        auto_restart=not getattr(args, "no_restart", False),
        server_url=getattr(args, "server", None) or DEFAULT_SERVER_URL,
    )


def _passive_version_check():
    """Worker 启动时被动跑一次版本检查 (失败/慢都不阻塞主流程).

    仅打印日志; 用户在 stdout 看到提示即可, 不主动升级.
    """
    try:
        from box2robot_gpu_worker.ota import check_version
        info = check_version(timeout=3.0)
    except Exception as e:
        logger.debug("[OTA] passive check skipped: %s", e)
        return
    if info.get("status") == "older":
        logger.warning(
            "=" * 60 + "\n"
            "  ⚠ Box2Robot GPU Worker 有新版本可用!\n"
            f"  本地: v{info['local']}    GitHub: v{info['remote']}\n"
            f"  升级: b2r-gpu upgrade   (一键 git pull + pip install -e .)\n"
            f"  仓库: {info['repo_url']}\n"
            + "=" * 60
        )
    elif info.get("status") == "current":
        logger.info("[OTA] 已是最新版本 v%s", info["local"])


def main():
    parser = argparse.ArgumentParser(description="Box2Robot GPU Worker")
    sub = parser.add_subparsers(dest="cmd")

    # 默认 (无子命令) 跑 daemon — 保持原行为, 向下兼容
    parser.add_argument("--server", "-s", type=str, default="https://robot.box2ai.com",
                        help="Server URL (default: https://robot.box2ai.com)")
    parser.add_argument("--output", "-o", type=str, default="outputs",
                        help="Output directory for models")
    parser.add_argument("--hf-cache", type=str, default=None,
                        help="HuggingFace cache 目录 (例: /root/autodl-tmp/.cache). "
                             "默认自动检测 AutoDL 数据盘, 否则用 ~/.cache/huggingface. "
                             "训练 + 推理共享同一份 base 模型, 避免重复下载.")
    parser.add_argument("--no-version-check", action="store_true",
                        help="启动时跳过 GitHub 版本检查 (离线环境)")

    # 子命令: check-version / check-deps / upgrade
    p_cv = sub.add_parser("check-version", help="检查 GitHub 上是否有新版本")
    p_cv.set_defaults(func=_cmd_check_version)

    p_cd = sub.add_parser("check-deps", help="检测核心 + 可选依赖完整性 (含 pip check)")
    p_cd.set_defaults(func=_cmd_check_deps)

    p_up = sub.add_parser("upgrade",
        help="一键升级: 优先 server zip, fallback git pull. 默认完成后自动重启.")
    p_up.add_argument("--force", action="store_true",
                      help="即使版本相同也强制跑 (hotfix 同版本号场景)")
    p_up.add_argument("--no-restart", action="store_true",
                      help="升级完成后不自动重启 (默认会 os.execv 替换当前进程)")
    p_up.set_defaults(func=_cmd_upgrade)

    args = parser.parse_args()

    # 子命令分发
    if args.cmd:
        return args.func(args)

    # 默认: 跑 daemon
    # 启动早期就设 HF_HOME, 后续 import torch / lerobot / transformers 才能读到
    _setup_hf_cache(args.hf_cache)

    # 被动版本检查 (失败不阻塞)
    if not args.no_version_check:
        _passive_version_check()

    worker = GPUWorker(args.server, args.output)
    worker.run()


if __name__ == "__main__":
    main()
