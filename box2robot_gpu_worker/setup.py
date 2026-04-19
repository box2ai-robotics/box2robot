from setuptools import setup, find_packages

setup(
    name="box2robot-gpu-worker",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "torch>=2.2",
        "numpy",
        "pyarrow",
        "httpx",
        "pyyaml",
    ],
    extras_require={
        "train": ["accelerate>=1.10", "wandb"],
    },
    entry_points={
        "console_scripts": [
            "b2r-gpu=box2robot_gpu_worker.gpu_worker:main",
            "box2robot-gpu=box2robot_gpu_worker.gpu_worker:main",
            "b2r-worker=box2robot_gpu_worker.worker:main",
        ],
    },
)
