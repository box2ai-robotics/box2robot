"""Box2Robot GPU Worker — LeRobot integration layer for Box2Robot platform."""
__version__ = "0.6.12"

# Servo normalization constants
STS_POS_MAX = 4095  # STS3215 encoder range
SC_POS_MAX = 1023   # SC09 encoder range
HW_POS_MAX = 4095   # Hiwonder HX (STS 兼容协议, 0-4095)
DEFAULT_FPS = 20    # Box2Robot recording sample rate

# 项目 ID → LeRobot 上游 policy.type 注册名 的映射.
# Server / 前端用 "gr00t" (带数字 0, 我们的 ID), LeRobot 上游注册名是 "groot" (字母 o).
# 不映射会导致 lerobot-train 报 'invalid choice: gr00t' (exit 2). worker 入口处统一归一化.
PROJECT_TO_LEROBOT_POLICY_TYPE = {
    "gr00t": "groot",
}


def normalize_model_type(model_type: str) -> str:
    """把项目 ID 归一化成 worker 内部 + LeRobot 上游使用的 policy.type. 未知值原样返回."""
    if not model_type:
        return model_type
    return PROJECT_TO_LEROBOT_POLICY_TYPE.get(model_type, model_type)


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
