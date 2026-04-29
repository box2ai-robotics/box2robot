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

    def run(self):
        """Main loop: activate → wait for bind → poll jobs → train."""
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
            payload = {**self.hw_info, "fw_version": WORKER_VERSION}
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
        """Heartbeat + poll for jobs + check upgrades."""
        heartbeat_interval = 10  # seconds
        poll_interval = 5  # seconds
        upgrade_interval = 60  # seconds
        last_heartbeat = 0
        last_upgrade_check = 0

        try:
            while True:
                now = time.time()

                # Heartbeat
                if now - last_heartbeat >= heartbeat_interval:
                    self._heartbeat()
                    last_heartbeat = now

                # Check upgrade
                if now - last_upgrade_check >= upgrade_interval:
                    self._check_upgrade()
                    last_upgrade_check = now

                # Poll for jobs
                job, action, resume_step = self._poll_job()
                if job:
                    self._process_job(job, action, resume_step)
                else:
                    time.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\nWorker stopped.")

    def _heartbeat(self):
        """Send heartbeat with updated hw info + real-time usage."""
        usage = get_usage_stats()
        # Update disk_free in hw_info
        if "disk_free_gb" in usage:
            self.hw_info["disk_free_gb"] = usage.pop("disk_free_gb")

        try:
            self.client.post(f"{self.server_url}/api/gpu/heartbeat", json={
                "device_id": self.device_id,
                "token": self.token,
                "fw_version": WORKER_VERSION,
                "gpu_info": self.hw_info,
                "usage": usage,
            })
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
                            logger.info("升级安装成功! 请重启 Worker 生效 (v%s)", info.get("version"))
                        else:
                            logger.error("升级安装失败: %s", result.stderr[:200])
                        break
            else:
                logger.info("非 zip 格式, 跳过自动安装. 请手动处理: %s", pkg_path)

            logger.info("=" * 40)
        except Exception as e:
            logger.error("升级失败: %s", e)

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

    def _start_bg_heartbeat(self):
        """Start background heartbeat thread (keeps GPU device online during blocking tasks)."""
        import threading
        self._bg_heartbeat_stop = False
        def _hb_loop():
            while not self._bg_heartbeat_stop:
                self._heartbeat()
                for _ in range(100):  # 10s = 100 * 0.1s (check stop flag frequently)
                    if self._bg_heartbeat_stop:
                        break
                    time.sleep(0.1)
        self._bg_heartbeat_thread = threading.Thread(target=_hb_loop, daemon=True)
        self._bg_heartbeat_thread.start()

    def _stop_bg_heartbeat(self):
        """Stop background heartbeat thread."""
        self._bg_heartbeat_stop = True
        if hasattr(self, '_bg_heartbeat_thread'):
            self._bg_heartbeat_thread.join(timeout=2)

    def _process_job(self, job: dict, action: str = "train", resume_from_step: int = None):
        """Route to training or inference based on action."""
        job_id = job["id"]
        logger.info("=" * 40)

        # Start background heartbeat (keeps GPU online during blocking training)
        self._start_bg_heartbeat()

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
            # 扫描已保存的 checkpoints，上报给 server
            model_dir = str(self.output_dir / job_id / "model")
            from box2robot_gpu_worker.worker import TrainingWorker
            ckpts = TrainingWorker._scan_checkpoints(model_dir)
            if ckpts:
                # 上报 checkpoint 列表
                self._report_progress(job_id, ckpts[-1], job.get("train_steps", 0),
                                      {"checkpoints": ckpts, "log": f"训练中断，已保存 {len(ckpts)} 个 checkpoint"})
            self._report_status(job_id, "cancelled",
                                error_msg=f"Worker 手动停止 (已保存 {len(ckpts)} 个 checkpoint)" if ckpts else "Worker 手动停止",
                                model_path=model_dir if ckpts else None)
            raise  # 继续向上传播，退出 worker
        finally:
            self._stop_bg_heartbeat()

        logger.info("任务完成: %s", job_id)
        logger.info("=" * 40)
        logger.info("继续监听下一个任务...")

    def _run_inference(self, job: dict, arm_device_id: str):
        """Load trained model and run inference loop against remote arm."""
        deploy_info = job.get("deploy_info", {})
        checkpoint_step = deploy_info.get("checkpoint_step")

        model_path = job.get("model_path", "")
        if not model_path:
            model_path = str(self.output_dir / job["id"] / "model")

        # 如果指定了 checkpoint_step, 定位到具体的 checkpoint 目录
        if checkpoint_step is not None:
            ckpt_subdir = Path(model_path) / "checkpoints" / str(checkpoint_step) / "pretrained_model"
            if ckpt_subdir.exists():
                logger.info("使用 checkpoint step %d: %s", checkpoint_step, ckpt_subdir)
                model_path = str(ckpt_subdir)
            else:
                logger.warning("Checkpoint %d 不存在, 使用默认模型路径", checkpoint_step)

        if not Path(model_path).exists():
            logger.error("模型不存在: %s", model_path)
            self._report_status(job["id"], "failed", error_msg=f"模型路径不存在: {model_path}")
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
            self._report_status(job["id"], "failed", error_msg=str(e))
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

    def _report_status(self, job_id: str, status: str, error_msg: str = None, model_path: str = None):
        try:
            data = {"status": status, "key": ""}
            if error_msg:
                data["error_msg"] = error_msg
            if model_path:
                data["model_path"] = model_path
            self.client.post(f"{self.server_url}/api/training/jobs/{job_id}/status", json=data)
        except Exception:
            pass

    def _report_progress(self, job_id: str, step: int, total: int, metrics: dict):
        try:
            self.client.post(f"{self.server_url}/api/training/jobs/{job_id}/progress",
                             json={"step": step, "total_steps": total, "metrics": metrics, "key": ""})
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Box2Robot GPU Worker")
    parser.add_argument("--server", "-s", type=str, default="https://robot.box2ai.com",
                        help="Server URL (default: https://robot.box2ai.com)")
    parser.add_argument("--output", "-o", type=str, default="outputs",
                        help="Output directory for models")
    args = parser.parse_args()

    worker = GPUWorker(args.server, args.output)
    worker.run()


if __name__ == "__main__":
    main()
