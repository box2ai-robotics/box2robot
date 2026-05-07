#!/usr/bin/env bash
# 在 AutoDL 空白实例上一键装 box2robot_gpu_worker。
# 假设上游 manager 已通过 sftp 把项目源码 tar.gz 解压到 /root/box2robot_gpu_worker/
#
# 用法 (manager 自动 SSH 调用):
#   bash /root/box2robot_gpu_worker/scripts/install_on_autodl.sh \
#       --server https://robot.box2ai.com \
#       --cuda cu124
#
# 输出（manager 解析）:
#   ::B2R_BIND_CODE::123456    ← 拿到绑定码后给用户去 APP 输
#   ::B2R_DEVICE_ID::GPU-XXX
#   ::B2R_PHASE::done|err

set -e

# AutoDL 实例的 conda 默认装在 /root/miniconda3，但 SSH 非交互式 shell 不读 ~/.bashrc，
# 所以手动注入到 PATH。同时兜底 /opt/conda（部分镜像）和 /usr/local/bin。
for d in /root/miniconda3/bin /opt/conda/bin /usr/local/bin; do
  [[ -d "$d" && ":$PATH:" != *":$d:"* ]] && export PATH="$d:$PATH"
done

SERVER="https://robot.box2ai.com"
CUDA="cu124"
PROJECT_DIR="/root/box2robot_gpu_worker"
LOG="/root/b2r_install.log"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="$2"; shift 2;;
    --cuda)   CUDA="$2";   shift 2;;
    --project-dir) PROJECT_DIR="$2"; shift 2;;
    *) echo "[!] unknown arg: $1"; shift;;
  esac
done

phase() { echo "::B2R_PHASE::$1"; }
emit_bind_code() { echo "::B2R_BIND_CODE::$1"; }
emit_device_id() { echo "::B2R_DEVICE_ID::$1"; }

phase "init"
echo "[$(date +%T)] === Box2Robot GPU Worker 一键安装 ==="
echo "  server  : $SERVER"
echo "  cuda    : $CUDA"
echo "  project : $PROJECT_DIR"

# ---- Phase 1: 环境检测 ----
phase "probe"
echo "[$(date +%T)] [1/5] 环境检测"
nvidia-smi -L 2>&1 | head -1 || { echo "[!] nvidia-smi 不可用"; phase "err"; exit 1; }
# AutoDL 实例上 python 命令通常缺失，只有 python3。统一用 PY 变量
if command -v python >/dev/null 2>&1; then
  PY=python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "[!] 找不到 python/python3"; phase "err"; exit 1
fi
$PY --version
PIP="$PY -m pip"
$PIP --version >/dev/null 2>&1 || $PY -m ensurepip || true

cd "$PROJECT_DIR"
[[ -d lerobot ]] || {
  echo "[!] lerobot/ 子目录不存在 — 上游 tar 包没带 lerobot，尝试 git clone"
  git clone https://github.com/huggingface/lerobot.git lerobot \
    || git clone https://gitclone.com/github.com/huggingface/lerobot.git lerobot
  (cd lerobot && git checkout cb0a9449 || true)
}

# ---- Phase 2: PyTorch + LeRobot ----
phase "deps"
echo "[$(date +%T)] [2/5] 装 PyTorch ($CUDA) + LeRobot 依赖"
# 国内 pip 加速
export PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
# HuggingFace 镜像
export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}

if ! $PY -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
  echo "  → torch CUDA 版未装，开始安装"
  $PIP install torch torchvision torchaudio \
      --index-url "https://download.pytorch.org/whl/${CUDA}" \
      2>&1 | tee -a "$LOG"
fi
$PY -c "import torch; print(f'torch={torch.__version__} cuda={torch.cuda.is_available()}')"

# LeRobot 基础 + dataset 扩展（pi0/smolvla 等 VLA 模型必需）
if ! $PY -c "import lerobot" 2>/dev/null; then
  echo "  → 装 lerobot 基础包"
  (cd lerobot && $PIP install -e . --no-build-isolation) 2>&1 | tee -a "$LOG"
  echo "  → 装 lerobot[dataset]"
  $PIP install "lerobot[dataset] @ file:./lerobot" --no-build-isolation 2>&1 | tee -a "$LOG" || true
fi

# ---- Phase 3: box2robot_gpu_worker ----
phase "install"
echo "[$(date +%T)] [3/5] 装 box2robot_gpu_worker"
$PIP install -e . --no-build-isolation 2>&1 | tee -a "$LOG"
which b2r-worker || which b2r-gpu || { echo "[!] b2r-worker / b2r-gpu 入口缺失"; phase "err"; exit 1; }

# ---- Phase 4: 启动 worker，拿绑定码 ----
phase "start_worker"
echo "[$(date +%T)] [4/5] 启动 b2r-worker（后台）"
ENTRY=$(which b2r-gpu || which b2r-worker)
WORKER_LOG="/root/b2r_worker.log"
> "$WORKER_LOG"
nohup "$ENTRY" --server "$SERVER" >"$WORKER_LOG" 2>&1 &
WORKER_PID=$!
echo "  worker pid=$WORKER_PID, log=$WORKER_LOG"

# 等绑定码 / 已激活 token 出现（最多 90s）
phase "wait_bind"
DEVICE_ID=""
BIND_CODE=""
TOKEN=""
for i in $(seq 1 90); do
  sleep 1
  # b2r-worker 启动时会打印 device_id + 绑定码 / 或 activated token
  if grep -qE "GPU-[A-F0-9]{12}" "$WORKER_LOG" 2>/dev/null; then
    DEVICE_ID=$(grep -oE "GPU-[A-F0-9]{12}" "$WORKER_LOG" | head -1)
  fi
  if grep -qE "绑定码|bind[_ ]code" "$WORKER_LOG" 2>/dev/null; then
    BIND_CODE=$(grep -oE "[0-9]{6}" "$WORKER_LOG" | head -1)
  fi
  if grep -qE "已绑定|activated|status.*activated|Already bound|GPU Worker 已就绪|Worker .* ready" "$WORKER_LOG" 2>/dev/null; then
    TOKEN="OK"
  fi
  [[ -n "$BIND_CODE" || -n "$TOKEN" ]] && break
done

if [[ -n "$DEVICE_ID" ]]; then emit_device_id "$DEVICE_ID"; fi
if [[ -n "$BIND_CODE" ]]; then
  emit_bind_code "$BIND_CODE"
elif [[ -n "$TOKEN" ]]; then
  echo "  worker 已绑定（之前激活过），无需新绑定码"
else
  echo "[!] 90s 内未拿到绑定码，最近日志:"
  tail -30 "$WORKER_LOG"
  phase "err"
  exit 1
fi

# ---- Phase 5: 注册 systemd 自启动 ----
phase "autostart"
echo "[$(date +%T)] [5/5] 注册 systemd 用户级自启动"
mkdir -p /root/.config/systemd/user
cat > /root/.config/systemd/user/box2robot-worker.service <<EOF
[Unit]
Description=Box2Robot GPU Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/box2robot_gpu_worker
ExecStart=$ENTRY --server $SERVER
Environment=HF_ENDPOINT=$HF_ENDPOINT
Restart=always
RestartSec=10
StandardOutput=append:/root/b2r_worker.log
StandardError=append:/root/b2r_worker.log

[Install]
WantedBy=default.target
EOF
# AutoDL 实例没 user systemd, 用 nohup + bashrc 兜底
if ! systemctl --user daemon-reload 2>/dev/null; then
  echo "  ⓘ user systemd 不可用，用 ~/.bashrc 自启动"
  if ! grep -q "b2r-worker" /root/.bashrc 2>/dev/null; then
    cat >> /root/.bashrc <<EOF

# Box2Robot GPU Worker auto-start
if ! pgrep -f "$(basename $ENTRY)" > /dev/null; then
    nohup $ENTRY --server $SERVER > /root/b2r_worker.log 2>&1 &
fi
EOF
  fi
else
  systemctl --user enable box2robot-worker 2>/dev/null || true
fi

phase "done"
echo "[$(date +%T)] === 完成 ==="
echo "  device_id : $DEVICE_ID"
echo "  bind_code : ${BIND_CODE:-(已绑定)}"
echo "  worker pid: $WORKER_PID"
echo "  worker log: $WORKER_LOG"
