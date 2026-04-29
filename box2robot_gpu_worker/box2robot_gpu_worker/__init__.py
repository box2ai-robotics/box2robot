"""Box2Robot GPU Worker — LeRobot integration layer for Box2Robot platform."""
__version__ = "0.6.1"

# Servo normalization constants
STS_POS_MAX = 4095  # STS3215 encoder range
SC_POS_MAX = 1023   # SC09 encoder range
HW_POS_MAX = 4095   # Hiwonder HX (STS 兼容协议, 0-4095)
DEFAULT_FPS = 20    # Box2Robot recording sample rate


def check_torch_cuda():
    """Check if PyTorch has CUDA support. Call before training/inference."""
    try:
        import torch
    except ImportError:
        raise RuntimeError(
            "PyTorch 未安装! 请先安装 CUDA 版本:\n"
            "  pip install torch torchvision torchaudio "
            "--index-url https://download.pytorch.org/whl/cu124"
        )

    if not torch.version.cuda:
        import warnings
        warnings.warn(
            "\n"
            "=" * 50 + "\n"
            "  你安装的是 CPU 版本的 PyTorch!\n"
            "  训练将使用 CPU (非常慢).\n\n"
            "  修复: pip uninstall torch torchvision torchaudio -y\n"
            "        pip install torch torchvision torchaudio \\\n"
            "            --index-url https://download.pytorch.org/whl/cu124\n"
            "=" * 50,
            stacklevel=2,
        )
        return False

    if not torch.cuda.is_available():
        import warnings
        warnings.warn(
            f"\n  PyTorch CUDA {torch.version.cuda} 已安装，但 GPU 不可用.\n"
            f"  请检查 NVIDIA 驱动版本 (运行 nvidia-smi).",
            stacklevel=2,
        )
        return False

    return True
