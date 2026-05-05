"""GPU/CUDA/dependency diagnostic script.

Usage:
    conda activate b2r
    python scripts/check_gpu.py
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    print("=" * 60)
    print(" Box2Robot GPU Worker — Diagnostic")
    print("=" * 60)
    ok = True

    # 1. Python version
    print(f"\n[1] Python: {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 12):
        print("    [WARN] 推荐 Python >= 3.12")

    # 2. PyTorch + CUDA
    try:
        import torch
        print(f"\n[2] PyTorch: {torch.__version__}")
        print(f"    CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"    CUDA build: {torch.version.cuda}")
            print(f"    GPU: {torch.cuda.get_device_name(0)}")
            free_b, total_b = torch.cuda.mem_get_info()
            print(f"    VRAM: free {free_b/1e9:.1f} / total {total_b/1e9:.1f} GB")
            if free_b / 1e9 < 6:
                print("    [WARN] 空闲显存 < 6GB — 训练 ACT 可能 OOM。请关闭其它占用 GPU 的程序。")
        else:
            print("    [ERROR] CUDA 不可用. 你装的可能是 CPU 版 PyTorch.")
            print("    Fix: pip uninstall torch torchvision torchaudio -y")
            print("         pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124")
            ok = False
    except ImportError:
        print("\n[2] PyTorch: NOT INSTALLED")
        ok = False

    # 3. 核心依赖
    print("\n[3] Required packages:")
    for pkg in ["datasets", "huggingface_hub", "safetensors", "draccus",
                "numpy", "PIL", "httpx", "pyarrow"]:
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", "?")
            print(f"    {pkg:20s} OK ({ver})")
        except ImportError:
            print(f"    {pkg:20s} MISSING")
            ok = False

    # 4. LeRobot
    print("\n[4] LeRobot:")
    try:
        import lerobot
        print(f"    lerobot package: {getattr(lerobot, '__version__', '?')}")
    except ImportError:
        local = Path(__file__).parent.parent / "lerobot" / "src" / "lerobot"
        if local.is_dir():
            print(f"    [WARN] lerobot 未通过 pip 安装, 但本地 submodule 存在: {local}")
            print(f"           Fix: cd lerobot && pip install -e . --no-build-isolation")
        else:
            print("    [ERROR] lerobot 未安装且 submodule 未拉取.")
            print("           Fix: git submodule update --init --recursive")
            ok = False

    # 5. nvidia-smi
    print("\n[5] nvidia-smi:")
    import shutil, subprocess
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used",
                 "--format=csv,noheader"],
                timeout=5, text=True,
            )
            for line in out.strip().splitlines():
                print(f"    {line}")
        except Exception as e:
            print(f"    [WARN] nvidia-smi 调用失败: {e}")
    else:
        print("    [WARN] nvidia-smi not in PATH (NVIDIA driver 未安装?)")

    # 6. 项目路径长度
    print("\n[6] Project path:")
    root = Path(__file__).parent.parent.resolve()
    print(f"    {root}  ({len(str(root))} chars)")
    if sys.platform == "win32" and len(str(root)) > 90:
        print(f"    [WARN] 路径偏深, 训练产物可能超过 Windows 260 字符限制.")
        print(f"           建议项目移到更短路径 (如 D:\\b2r\\)")

    print("\n" + "=" * 60)
    print(" Result:", "PASS" if ok else "FAIL — 按上述提示修复")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
