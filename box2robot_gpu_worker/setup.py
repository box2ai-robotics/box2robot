from setuptools import setup, find_packages

# NOTE: PyTorch must be installed BEFORE this package, with CUDA support.
# Default `pip install torch` installs CPU-only on Windows!
#
# Correct installation:
#   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
#   pip install -e ./lerobot --no-build-isolation
#   pip install 'lerobot[dataset] @ ./lerobot'   # video deps (av/datasets/pyarrow/torchcodec)
#   pip install -e .
#
# Or use the automated scripts:
#   scripts/setup_windows.bat   (Windows)
#   scripts/setup_linux.sh      (Linux/Ubuntu)

setup(
    name="box2robot-gpu-worker",
    version="0.6.3",
    packages=find_packages(),
    python_requires=">=3.12",
    install_requires=[
        # torch is NOT listed here — it must be pre-installed with CUDA.
        # Listing torch here causes pip to pull the CPU-only version on Windows.
        "numpy",
        "pyarrow",
        "httpx",
        "pyyaml",
        "psutil",
        # av: 视频解码 (lerobot 加载图像/视频数据集时必需,
        # 否则 VLA 模型 (pi0/pi05/smolvla) 会在数据加载阶段崩溃。
        # 等价于 `pip install 'lerobot[dataset]'` 中的 av-dep)
        "av>=15.0.0,<16.0.0",
    ],
    extras_require={
        "train": ["accelerate>=1.10", "wandb"],
        "smolvla": ["transformers>=4.57", "accelerate>=1.10"],
        "pi": ["transformers>=4.52", "accelerate>=1.10"],
        "vla": ["transformers>=4.57", "accelerate>=1.10"],
    },
    entry_points={
        "console_scripts": [
            "b2r-gpu=box2robot_gpu_worker.gpu_worker:main",
            "box2robot-gpu=box2robot_gpu_worker.gpu_worker:main",
            "b2r-worker=box2robot_gpu_worker.worker:main",
        ],
    },
)
