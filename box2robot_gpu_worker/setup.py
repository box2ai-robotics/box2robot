from setuptools import setup, find_packages

# NOTE: PyTorch must be installed BEFORE this package, with CUDA support.
# Default `pip install torch` installs CPU-only on Windows!
#
# Correct installation:
#   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
#   pip install -e .
#
# Or use the automated script:
#   scripts/setup_windows.bat

setup(
    name="box2robot-gpu-worker",
    version="0.6.1",
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
