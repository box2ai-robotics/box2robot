#!/usr/bin/env bash
# ============================================================
# Box2Robot GPU Worker — Linux/Ubuntu 一键安装脚本
#
# 用法:
#   bash scripts/setup_linux.sh
#   bash scripts/setup_linux.sh cu124    # 指定 CUDA 版本
#   bash scripts/setup_linux.sh cu128
#   bash scripts/setup_linux.sh cu118
#
# 前置: 已 clone lerobot 到 ./lerobot 目录, 已安装 conda
# ============================================================

set -e

echo
echo "=================================================="
echo "  Box2Robot GPU Worker — Linux 安装"
echo "=================================================="
echo

# --- Check conda ---
if ! command -v conda >/dev/null 2>&1; then
    echo "[错误] 未检测到 conda!"
    echo "请先安装 Miniconda: https://docs.conda.io/en/latest/miniconda.html"
    exit 1
fi

# --- Parse CUDA version arg ---
CUDA_VER="${1:-cu124}"
echo "[信息] CUDA 版本: $CUDA_VER"

case "$CUDA_VER" in
    cu118) TORCH_INDEX="https://download.pytorch.org/whl/cu118"; CUDA_LABEL="CUDA 11.8" ;;
    cu121) TORCH_INDEX="https://download.pytorch.org/whl/cu121"; CUDA_LABEL="CUDA 12.1" ;;
    cu124) TORCH_INDEX="https://download.pytorch.org/whl/cu124"; CUDA_LABEL="CUDA 12.4" ;;
    cu128) TORCH_INDEX="https://download.pytorch.org/whl/cu128"; CUDA_LABEL="CUDA 12.8" ;;
    *) echo "[错误] 不支持的 CUDA 版本: $CUDA_VER (支持: cu118/cu121/cu124/cu128)"; exit 1 ;;
esac

echo "[信息] PyTorch 索引: $TORCH_INDEX"
echo

# --- Activate conda's shell hook so `conda activate` works in script ---
eval "$(conda shell.bash hook)"

# --- Step 1: Create env ---
if conda env list | awk '{print $1}' | grep -qx "b2r"; then
    echo "[信息] conda 环境 'b2r' 已存在, 跳过创建"
else
    echo "[步骤 1/6] 创建 conda 环境 (Python 3.12)..."
    conda create -n b2r python=3.12 -y
fi

# --- Step 2: Activate ---
echo "[步骤 2/6] 激活环境..."
conda activate b2r

# --- Step 3: Install PyTorch with CUDA ---
echo
echo "[步骤 3/6] 安装 PyTorch ($CUDA_LABEL)..."
echo "  这一步需要下载约 2-3GB, 请耐心等待..."
echo
pip install torch torchvision torchaudio --index-url "$TORCH_INDEX"

# --- Verify GPU ---
echo
echo "[验证] 检查 GPU..."
python -c "import torch; g=torch.cuda.is_available(); n=torch.cuda.get_device_name(0) if g else 'N/A'; print(f'  CUDA: {g}  GPU: {n}  torch.version.cuda: {torch.version.cuda}')" || echo "[警告] GPU 验证失败, 继续安装..."

# --- Step 4: Install LeRobot base ---
echo
echo "[步骤 4/6] 安装 LeRobot 基础包..."
if [[ -f "lerobot/pyproject.toml" ]]; then
    pip install -e ./lerobot --no-build-isolation
else
    echo "[错误] lerobot 子目录不存在!"
    echo "请先 clone: git clone https://github.com/huggingface/lerobot.git lerobot"
    exit 1
fi

# --- Step 5: Install LeRobot[dataset] (av/datasets/torchcodec) ---
# pi0/pi05/smolvla 等 VLA 模型加载视频数据集时需要 av,
# 缺这一步训练时会报 "'av' is required but not installed"
echo
echo "[步骤 5/6] 安装 LeRobot 数据集依赖 (av, datasets, torchcodec)..."
echo "  缺这一步训练 pi0/pi05/smolvla 会崩"
if ! pip install "lerobot[dataset] @ file:./lerobot" --no-build-isolation; then
    echo "[警告] lerobot[dataset] 整体安装失败, 尝试逐个安装关键依赖..."
    pip install "av>=15.0.0,<16.0.0"
    pip install "datasets>=4.0.0,<5.0.0" "jsonlines>=4.0.0,<5.0.0"
    pip install "torchcodec>=0.3.0,<0.11.0" || echo "  (torchcodec 安装失败可忽略, av 已能解码常见视频)"
fi

# --- Step 6: Install GPU Worker ---
echo
echo "[步骤 6/6] 安装 Box2Robot GPU Worker..."
pip install -e . psutil

# --- Final check ---
echo
echo "=================================================="
echo "  安装完成! 运行诊断..."
echo "=================================================="
echo
python scripts/check_gpu.py

echo
echo "=================================================="
echo "  启动命令:"
echo "    conda activate b2r"
echo "    b2r-gpu --server https://robot.box2ai.com"
echo "=================================================="
echo
