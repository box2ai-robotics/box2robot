#!/usr/bin/env python3
"""
GPU 环境诊断 + 完整依赖体检 — 检查 PyTorch CUDA 配置 + LeRobot 训练栈.

Usage:
    python scripts/check_gpu.py            # 输出体检报告
    python scripts/check_gpu.py --strict   # 关键依赖缺失时返回 exit code 1 (CI 用)
"""
import argparse
import importlib
import platform
import subprocess
import sys


# ── 依赖矩阵: (import_name, pip_name, level, fix_cmd, purpose) ──
# level: "critical"  — 缺则 worker 完全跑不了
#        "vla"       — 缺则 VLA (pi0/pi05/smolvla) 训练崩 ('av' is required ...)
#        "optional"  — 加速/可视化, 不影响功能
DEPENDENCIES = [
    # critical: worker 主体
    ("numpy",       "numpy",      "critical", "pip install numpy",      "数值计算"),
    ("httpx",       "httpx",      "critical", "pip install httpx",      "HTTP 客户端 (与 server 通信)"),
    ("pyarrow",     "pyarrow",    "critical", "pip install pyarrow",    "LeRobot 数据集格式"),
    ("yaml",        "pyyaml",     "critical", "pip install pyyaml",     "配置文件解析"),
    ("psutil",      "psutil",     "critical", "pip install psutil",     "硬件状态采集"),
    ("torch",       "torch",      "critical", "见 CUDA 版本指南 (本脚本上方输出)", "深度学习"),
    ("lerobot",     "lerobot",    "critical", "cd lerobot && pip install -e . --no-build-isolation", "训练框架"),

    # vla: 训练 VLA 模型必需 (pi0/pi05/smolvla)
    ("av",          "av",         "vla",
     'pip install "av>=15.0.0,<16.0.0"   # 或 pip install "lerobot[dataset] @ file:./lerobot"',
     "视频解码 (lerobot 加载图像/视频数据集时调用)"),
    ("datasets",    "datasets",   "vla",
     'pip install "lerobot[dataset] @ file:./lerobot" --no-build-isolation',
     "HuggingFace datasets (parquet 加载)"),
    ("transformers","transformers","vla",
     "pip install -e .[vla]   # 或 pip install transformers accelerate",
     "VLA 主干 (paligemma 等)"),
    ("accelerate",  "accelerate", "vla",
     "pip install accelerate",
     "分布式训练 + 大模型加载"),

    # optional
    ("torchcodec",  "torchcodec", "optional",
     'pip install "torchcodec>=0.3.0,<0.11.0"',
     "新版视频解码 (Windows 需要 torch>=2.8, 失败可忽略, av 已能解码)"),
    ("wandb",       "wandb",      "optional", "pip install wandb",      "训练日志可视化"),
]


class Report:
    def __init__(self):
        self.lines = []
        self.critical_missing = []
        self.vla_missing = []
        self.optional_missing = []
        self.gpu_ok = False

    def add(self, line: str = ""):
        self.lines.append(line)

    def summary(self) -> str:
        out = ["", "=" * 60, "  体检结果", "=" * 60]
        if self.critical_missing:
            out.append(f"  [严重] 关键依赖缺失 {len(self.critical_missing)} 个: "
                       + ", ".join(self.critical_missing))
            out.append("    Worker 无法启动, 必须修复!")
        else:
            out.append("  [OK] 关键依赖齐全, Worker 可以启动")

        if self.vla_missing:
            out.append(f"  [警告] VLA 依赖缺失 {len(self.vla_missing)} 个: "
                       + ", ".join(self.vla_missing))
            out.append("    pi0/pi05/smolvla 训练会崩 ('av' is required ...)")
            out.append("    一键修复: pip install \"lerobot[dataset] @ file:./lerobot\" --no-build-isolation")
        else:
            out.append("  [OK] VLA 依赖齐全, 可以训练 pi0/pi05/smolvla")

        if self.optional_missing:
            out.append(f"  [提示] 可选依赖缺失 {len(self.optional_missing)} 个: "
                       + ", ".join(self.optional_missing) + " (不影响主流程)")

        if self.gpu_ok:
            out.append("  [OK] GPU 可用")
        else:
            out.append("  [严重] GPU 不可用, 详见上方诊断")

        out.append("=" * 60)
        return "\n".join(out)


def check_python(rep: Report):
    rep.add("[Python]")
    ver = sys.version_info
    rep.add(f"  版本: {ver.major}.{ver.minor}.{ver.micro}")
    rep.add(f"  路径: {sys.executable}")
    if (ver.major, ver.minor) != (3, 12):
        rep.add(f"  [警告] 推荐 Python 3.12, 当前 {ver.major}.{ver.minor} 可能与 lerobot/torchcodec 不兼容")
    rep.add(f"  OS: {platform.system()} {platform.release()} ({platform.machine()})")


def check_nvidia_smi(rep: Report):
    rep.add("")
    rep.add("[NVIDIA 驱动]")
    smi_path = _find_nvidia_smi()
    try:
        result = subprocess.run(
            [smi_path, "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000 if platform.system() == "Windows" else 0,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                rep.add(f"  GPU: {parts[0]}")
                rep.add(f"  驱动: {parts[1]}")
                rep.add(f"  显存: {parts[2]}")
            rep.add("  nvidia-smi: OK")
        else:
            rep.add(f"  nvidia-smi 执行失败: {result.stderr.strip()}")
    except FileNotFoundError:
        rep.add("  nvidia-smi 未找到! 请安装 NVIDIA 驱动")
        rep.add("  下载: https://www.nvidia.com/download/index.aspx")
    except Exception as e:
        rep.add(f"  nvidia-smi 异常: {e}")


def check_torch(rep: Report):
    rep.add("")
    rep.add("[PyTorch]")
    try:
        import torch
        rep.add(f"  torch 版本: {torch.__version__}")
        cuda_available = torch.cuda.is_available()
        cuda_version = getattr(torch.version, "cuda", None)
        rep.add(f"  CUDA 编译版本: {cuda_version or 'None (CPU-only build!)'}")
        rep.add(f"  torch.cuda.is_available(): {cuda_available}")
        if cuda_available:
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                rep.add(f"  GPU {i}: {props.name} ({props.total_memory / 1024**3:.1f} GB)")
            rep.gpu_ok = True
        else:
            rep.add("  [严重] GPU 不可用!")
            if cuda_version is None:
                rep.add("  原因: 装的是 CPU 版本 PyTorch")
                rep.add("  修复: pip uninstall torch torchvision torchaudio -y")
                rep.add("        pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124")
            else:
                rep.add(f"  原因: PyTorch 要求 CUDA {cuda_version}, 但驱动可能太旧")
                rep.add("  修复: 升级 NVIDIA 驱动, 或用更低版本的 cuXXX")
    except ImportError:
        rep.add("  [严重] PyTorch 未安装!")
        rep.add("  修复: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124")


def check_dependencies(rep: Report):
    rep.add("")
    rep.add("[依赖矩阵]")
    rep.add(f"  {'包':<14} {'状态':<10} {'级别':<10} 说明")
    rep.add(f"  {'-'*14} {'-'*10} {'-'*10} {'-'*30}")
    for import_name, pip_name, level, fix_cmd, purpose in DEPENDENCIES:
        if import_name == "torch":
            continue  # torch 已在 check_torch 里详细处理
        try:
            mod = importlib.import_module(import_name)
            ver = getattr(mod, "__version__", "?")
            status = f"OK {ver}"
            level_tag = level
        except ImportError:
            status = "缺失"
            level_tag = f"[{level.upper()}]"
            if level == "critical":
                rep.critical_missing.append(pip_name)
            elif level == "vla":
                rep.vla_missing.append(pip_name)
            else:
                rep.optional_missing.append(pip_name)
        rep.add(f"  {import_name:<14} {status:<10} {level_tag:<10} {purpose}")

    # 给出缺失依赖的修复指令
    missing_with_fix = []
    for import_name, pip_name, level, fix_cmd, purpose in DEPENDENCIES:
        if import_name == "torch":
            continue
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing_with_fix.append((pip_name, level, fix_cmd))
    if missing_with_fix:
        rep.add("")
        rep.add("[缺失依赖修复指令]")
        for pip_name, level, fix_cmd in missing_with_fix:
            rep.add(f"  ({level}) {pip_name}:")
            rep.add(f"    {fix_cmd}")


def check_box2robot(rep: Report):
    rep.add("")
    rep.add("[Box2Robot GPU Worker]")
    try:
        import box2robot_gpu_worker
        ver = getattr(box2robot_gpu_worker, "__version__", "?")
        rep.add(f"  版本: {ver}")
    except ImportError:
        rep.add("  未安装! 修复: pip install -e .")
        rep.critical_missing.append("box2robot-gpu-worker")


def _find_nvidia_smi() -> str:
    import os
    if platform.system() != "Windows":
        return "nvidia-smi"
    for prog_dir in [os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramW6432", r"C:\Program Files")]:
        smi = os.path.join(prog_dir, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")
        if os.path.isfile(smi):
            return smi
    return "nvidia-smi"


def run_check(strict: bool = False) -> int:
    """Run all checks. Returns exit code (0 = OK, 1 = critical missing in strict mode)."""
    print("=" * 60)
    print("  Box2Robot GPU Worker — 环境诊断 + 依赖体检")
    print("=" * 60)
    rep = Report()
    check_python(rep)
    check_nvidia_smi(rep)
    check_torch(rep)
    check_dependencies(rep)
    check_box2robot(rep)
    print("\n".join(rep.lines))
    print(rep.summary())
    if strict and (rep.critical_missing or not rep.gpu_ok):
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="关键依赖缺失或 GPU 不可用时返回 exit code 1")
    args = parser.parse_args()
    sys.exit(run_check(strict=args.strict))


if __name__ == "__main__":
    main()
