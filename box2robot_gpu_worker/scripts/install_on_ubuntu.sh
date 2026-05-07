#!/usr/bin/env bash
# Box2Robot GPU Worker — Ubuntu 22.04 一键安装
#
# 适用: 自己的 Ubuntu 22.04+ 服务器 / 工作站 (非 AutoDL, AutoDL 用 install_on_autodl.sh)
#
# 用法:
#   sudo bash install_on_ubuntu.sh                      # 全默认 (项目目录 = 脚本所在仓库)
#   sudo bash install_on_ubuntu.sh --server https://robot.box2ai.com
#   sudo bash install_on_ubuntu.sh --cuda cu121         # 老显卡用 CUDA 12.1
#   sudo bash install_on_ubuntu.sh --skip-deps          # 已装过依赖, 只配 systemd
#   sudo bash install_on_ubuntu.sh --uninstall          # 卸载
#
# 这个脚本会:
#   1) 检查 GPU / Python / pip
#   2) 创建 venv (默认) 或复用已有 conda/系统 Python
#   3) 装 PyTorch (CUDA) + LeRobot + box2robot_gpu_worker
#   4) 注册 systemd 服务 box2robot-gpu-worker (开机自启 + 崩溃重启)
#   5) 启动 worker, 等待绑定码并打印
#
# 完成后:
#   journalctl -u box2robot-gpu-worker -f       # 看实时日志
#   systemctl status box2robot-gpu-worker       # 查状态

set -e

# ---------- 默认值 ----------
SERVER="https://robot.box2ai.com"
CUDA="cu124"
PROJECT_DIR=""
PY_MODE="auto"                                  # auto / venv / conda / system
PY_BIN=""                                       # 由 PY_MODE 决定
SVC_NAME="box2robot-gpu-worker"
SVC_USER="root"
LOG_FILE="/var/log/${SVC_NAME}.log"
PIP_MIRROR="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
HF_MIRROR="${HF_ENDPOINT:-https://hf-mirror.com}"
SKIP_DEPS="no"
UNINSTALL="no"

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server)        SERVER="$2"; shift 2 ;;
    --cuda)          CUDA="$2"; shift 2 ;;
    --project-dir)   PROJECT_DIR="$2"; shift 2 ;;
    --py-mode)       PY_MODE="$2"; shift 2 ;;
    --user)          SVC_USER="$2"; shift 2 ;;
    --pip-mirror)    PIP_MIRROR="$2"; shift 2 ;;
    --hf-mirror)     HF_MIRROR="$2"; shift 2 ;;
    --skip-deps)     SKIP_DEPS="yes"; shift ;;
    --uninstall)     UNINSTALL="yes"; shift ;;
    -h|--help)       sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "[ERR] 未知参数: $1"; exit 1 ;;
  esac
done

# ---------- 检查 root ----------
[[ $EUID -eq 0 ]] || { echo "[ERR] 请用 sudo 或 root 运行"; exit 1; }

# ---------- 卸载分支 ----------
if [[ "$UNINSTALL" == "yes" ]]; then
  echo "[*] 卸载 $SVC_NAME ..."
  systemctl stop "$SVC_NAME" 2>/dev/null || true
  systemctl disable "$SVC_NAME" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SVC_NAME}.service"
  systemctl daemon-reload
  echo "[OK] 已卸载 (代码与依赖未删, 如需清理请手动 rm -rf venv)"
  exit 0
fi

# ---------- 定位项目目录 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PROJ="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$DEFAULT_PROJ}"

[[ -f "$PROJECT_DIR/setup.py" ]] || {
  echo "[ERR] 项目目录不对: $PROJECT_DIR (找不到 setup.py)"
  echo "      用 --project-dir /path/to/box2robot_gpu_worker 指定"
  exit 1
}

echo "================================================"
echo " Box2Robot GPU Worker — Ubuntu 22.04 一键安装"
echo "================================================"
echo " 项目目录 : $PROJECT_DIR"
echo " Server   : $SERVER"
echo " CUDA     : $CUDA"
echo " Py 模式  : $PY_MODE"
echo " 服务名   : $SVC_NAME"
echo " 运行用户 : $SVC_USER"
echo " pip 源   : $PIP_MIRROR"
echo " HF 镜像  : $HF_MIRROR"
echo "================================================"
echo

# ---------- Phase 1: 系统依赖 ----------
echo "[1/5] 检查系统环境"
nvidia-smi -L | head -1 || { echo "[ERR] nvidia-smi 不可用 — 请先装 NVIDIA 驱动"; exit 1; }

# 系统包 (静默装, 已装就跳过)
NEED_PKGS=()
command -v git    >/dev/null || NEED_PKGS+=(git)
command -v curl   >/dev/null || NEED_PKGS+=(curl)
command -v python3 >/dev/null || NEED_PKGS+=(python3)
dpkg -l python3-venv 2>/dev/null | grep -q ^ii || NEED_PKGS+=(python3-venv)
dpkg -l python3-pip  2>/dev/null | grep -q ^ii || NEED_PKGS+=(python3-pip)
dpkg -l build-essential 2>/dev/null | grep -q ^ii || NEED_PKGS+=(build-essential)

if [[ ${#NEED_PKGS[@]} -gt 0 ]]; then
  echo "  → 安装系统包: ${NEED_PKGS[*]}"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${NEED_PKGS[@]}"
fi

# ---------- Phase 2: 选择 Python 环境 ----------
echo "[2/5] 配置 Python 环境"

if [[ "$PY_MODE" == "auto" ]]; then
  if [[ -x /root/miniconda3/bin/python || -x /opt/conda/bin/python ]]; then
    PY_MODE="conda"
  else
    PY_MODE="venv"
  fi
  echo "  → 自动选择: $PY_MODE"
fi

case "$PY_MODE" in
  venv)
    VENV_DIR="$PROJECT_DIR/venv"
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
      echo "  → 创建 venv: $VENV_DIR"
      python3 -m venv "$VENV_DIR"
    fi
    PY_BIN="$VENV_DIR/bin"
    ;;
  conda)
    for d in /root/miniconda3/bin /opt/conda/bin; do
      [[ -x "$d/python" ]] && PY_BIN="$d" && break
    done
    [[ -n "$PY_BIN" ]] || { echo "[ERR] 没找到 conda, 改用 --py-mode venv"; exit 1; }
    ;;
  system)
    PY_BIN="$(dirname "$(command -v python3)")"
    ;;
  *)
    echo "[ERR] --py-mode 只能是 auto/venv/conda/system"; exit 1 ;;
esac

PY="$PY_BIN/python"
[[ -x "$PY" ]] || PY="$PY_BIN/python3"
PIP="$PY -m pip"
echo "  Python : $($PY --version) @ $PY"

# pip 源 + HF 镜像 (本次会话生效, 写入 service 持久化)
export PIP_INDEX_URL="$PIP_MIRROR"
export HF_ENDPOINT="$HF_MIRROR"
$PIP install -q --upgrade pip wheel setuptools

# ---------- Phase 3: 装依赖 ----------
if [[ "$SKIP_DEPS" == "yes" ]]; then
  echo "[3/5] 跳过依赖安装 (--skip-deps)"
else
  echo "[3/5] 安装 PyTorch ($CUDA) + LeRobot + worker"

  # PyTorch
  if ! $PY -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
    echo "  → 装 torch / torchvision / torchaudio (CUDA $CUDA)"
    $PIP install torch torchvision torchaudio \
        --index-url "https://download.pytorch.org/whl/${CUDA}"
  fi
  $PY -c "import torch; print(f'  torch={torch.__version__} cuda={torch.cuda.is_available()}')"

  # LeRobot (子目录或 git clone)
  cd "$PROJECT_DIR"
  if [[ ! -d lerobot ]]; then
    echo "  → 克隆 lerobot"
    git clone https://github.com/huggingface/lerobot.git lerobot \
      || git clone https://gitclone.com/github.com/huggingface/lerobot.git lerobot
    (cd lerobot && git checkout cb0a9449 || true)
  fi
  if ! $PY -c "import lerobot" 2>/dev/null; then
    echo "  → 装 lerobot 基础包 + dataset 扩展"
    (cd lerobot && $PIP install -e . --no-build-isolation)
    $PIP install "lerobot[dataset] @ file://$PROJECT_DIR/lerobot" --no-build-isolation || true
  fi

  # box2robot_gpu_worker
  echo "  → 装 box2robot_gpu_worker"
  $PIP install -e . --no-build-isolation
fi

# 找 b2r-gpu 入口
ENTRY="$PY_BIN/b2r-gpu"
[[ -x "$ENTRY" ]] || ENTRY="$PY_BIN/b2r-worker"
[[ -x "$ENTRY" ]] || { echo "[ERR] b2r-gpu / b2r-worker 入口不存在 (装失败?)"; exit 1; }
echo "  入口   : $ENTRY"

# ---------- Phase 4: 写 systemd service ----------
echo "[4/5] 注册 systemd 服务 $SVC_NAME"

SVC_FILE="/etc/systemd/system/${SVC_NAME}.service"
if [[ -f "$SVC_FILE" ]]; then
  systemctl stop "$SVC_NAME" 2>/dev/null || true
fi

touch "$LOG_FILE"
chown "$SVC_USER":"$SVC_USER" "$LOG_FILE" 2>/dev/null || true

# CUDA bin 路径 (训练可能要 nvcc)
CUDA_BIN=""
for d in /usr/local/cuda/bin /usr/local/cuda-12.4/bin /usr/local/cuda-12.1/bin; do
  [[ -d "$d" ]] && CUDA_BIN="$d:" && break
done

cat > "$SVC_FILE" <<EOF
[Unit]
Description=Box2Robot GPU Worker (b2r-gpu daemon)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=${PY_BIN}:${CUDA_BIN}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PYTHONUNBUFFERED=1
Environment=HF_ENDPOINT=$HF_MIRROR
Environment=PIP_INDEX_URL=$PIP_MIRROR
ExecStart=$ENTRY --server $SERVER
Restart=always
RestartSec=10
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SVC_FILE"
systemctl daemon-reload
systemctl enable "$SVC_NAME" >/dev/null
echo "  ✓ 已写入 $SVC_FILE"

# 杀掉前台残留 worker (避免 systemd 起冲突)
pkill -f "$(basename "$ENTRY") --server" 2>/dev/null || true
sleep 1

# ---------- Phase 5: 启动 + 等绑定码 ----------
echo "[5/5] 启动 worker, 等待绑定码 (最多 90s)"
> "$LOG_FILE"
systemctl restart "$SVC_NAME"
sleep 2

if ! systemctl is-active --quiet "$SVC_NAME"; then
  echo "[ERR] 服务启动失败:"
  journalctl -u "$SVC_NAME" -n 40 --no-pager
  exit 1
fi

DEVICE_ID=""
BIND_CODE=""
ALREADY_BOUND=""
for i in $(seq 1 90); do
  sleep 1
  [[ -z "$DEVICE_ID" ]] && DEVICE_ID=$(grep -oE "GPU-[A-F0-9]{12}" "$LOG_FILE" 2>/dev/null | head -1 || true)
  if grep -qE "绑定码|bind[_ ]code" "$LOG_FILE" 2>/dev/null; then
    BIND_CODE=$(grep -oE "[0-9]{6}" "$LOG_FILE" | head -1)
  fi
  if grep -qE "已绑定|activated|Already bound|GPU Worker 已就绪|Worker .* ready" "$LOG_FILE" 2>/dev/null; then
    ALREADY_BOUND="yes"
  fi
  [[ -n "$BIND_CODE" || -n "$ALREADY_BOUND" ]] && break
done

# ---------- 总结 ----------
echo
echo "================ 完成 ================"
echo " 服务名     : $SVC_NAME (开机自启 ✓)"
echo " 项目目录   : $PROJECT_DIR"
echo " Python    : $PY"
echo " 入口      : $ENTRY"
echo " 日志      : $LOG_FILE"
echo " Device ID : ${DEVICE_ID:-(等下次心跳出现)}"
if [[ -n "$BIND_CODE" ]]; then
  echo
  echo " ★ 绑定码  : $BIND_CODE"
  echo "   去 APP \"GPU 配置\" 输入此 6 位绑定码完成激活"
elif [[ -n "$ALREADY_BOUND" ]]; then
  echo " 状态      : 已绑定 (之前激活过)"
else
  echo " 状态      : 90s 内未拿到绑定码, 查看日志:"
  echo "             tail -50 $LOG_FILE"
fi
echo
echo " 常用命令:"
echo "   systemctl status $SVC_NAME      # 查状态"
echo "   systemctl restart $SVC_NAME     # 重启"
echo "   journalctl -u $SVC_NAME -f      # 实时日志"
echo "   tail -f $LOG_FILE               # 文件日志"
echo "   sudo bash $0 --uninstall        # 卸载"
echo "======================================"
