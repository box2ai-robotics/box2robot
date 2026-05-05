#!/usr/bin/env bash
# Box2Robot GPU Worker — Linux/macOS 一键安装脚本
# 用法: bash scripts/setup_linux.sh [cu118 | cu121 | cu124 | cu128]
# 默认 CUDA 12.4
set -e

CUDA_VER="${1:-cu124}"

echo "============================================================"
echo " Box2Robot GPU Worker setup (Linux, $CUDA_VER)"
echo "============================================================"

# 1. 检查 conda
if ! command -v conda &>/dev/null; then
    echo "[ERROR] 未找到 conda. 请先安装 Miniconda."
    echo "  https://docs.conda.io/en/latest/miniconda.html"
    exit 1
fi

# 让 conda activate 在脚本内可用
source "$(conda info --base)/etc/profile.d/conda.sh"

# 2. 创建环境
echo
echo "[1/6] Creating conda environment 'b2r' (python 3.12)..."
if conda env list | awk '{print $1}' | grep -qx "b2r"; then
    echo "  environment 'b2r' already exists, skipping."
else
    conda create -n b2r python=3.12 -y
fi

# 3. 激活
echo
echo "[2/6] Activating 'b2r'..."
conda activate b2r

# 4. PyTorch
echo
echo "[3/6] Installing PyTorch ($CUDA_VER)..."
pip install torch torchvision torchaudio --index-url "https://download.pytorch.org/whl/$CUDA_VER"

# 5. 验证 GPU
echo
echo "[4/6] Verifying GPU..."
python - <<'PY'
import torch
assert torch.cuda.is_available(), "CUDA not available"
print(f"  GPU: {torch.cuda.get_device_name(0)}")
print(f"  VRAM: {round(torch.cuda.get_device_properties(0).total_memory/1e9, 1)} GB")
PY

# 6. LeRobot submodule
echo
echo "[5/6] Installing LeRobot from submodule..."
if [ ! -f "lerobot/setup.py" ] && [ ! -f "lerobot/pyproject.toml" ]; then
    echo "[ERROR] lerobot/ submodule 未拉取."
    echo "  请在仓库根目录执行: git submodule update --init --recursive"
    exit 1
fi
( cd lerobot && pip install -e . --no-build-isolation )

# 7. GPU Worker
echo
echo "[6/6] Installing GPU Worker + dependencies..."
pip install -e .

# 8. preflight
echo
echo "Running preflight check..."
python -m box2robot_gpu_worker.preflight || echo "[WARN] preflight 报告了问题, 但 install 已完成."

echo
echo "============================================================"
echo " Setup complete."
echo "============================================================"
echo
echo "Next:"
echo "  conda activate b2r"
echo "  b2r-gpu --server https://robot.box2ai.com"
echo
