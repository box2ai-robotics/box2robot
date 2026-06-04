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
#   ::B2R_PHASE::<name>        ← 阶段进入 (init/probe/deps/install/start_worker/wait_bind/autostart/done)
#   ::B2R_PHASE::failed:CODE   ← 失败 (CODE ∈ manager_server_protocol.md §9 错误码白名单)
#                                 manager 把 CODE 透传到 server provision_error_code 字段
# 协议: claude_md/manager_autodl_protocol.md §3.4
# 错误码白名单 (改这里必须同步 claude_md/manager_server_protocol.md §9):
#   MANAGER_INSTALL_FAIL     - probe/deps/install/autostart 任意一步失败
#   MANAGER_WORKER_TIMEOUT   - start_worker/wait_bind 超时
#   MANAGER_UNKNOWN          - 兜底 (set -e 触发的隐式失败)
#
# autodl-fs 共享盘策略 (region 内跨实例持久化):
#   /root/autodl-fs/data/box2robot-base-models  ← HF_HOME (HuggingFace 模型权重)
#   /root/autodl-fs/pip-cache                   ← PIP_CACHE_DIR (pip wheel 缓存)
# 第一台实例首次装 lerobot[diffusion]/[pi]/[smolvla] 等 extras 会下载 wheel 到 pip-cache,
# 之后同 region 任何实例 pip install 都从该 cache 离线装, 不再走网络. 跨 region 各装一份.

set -e

# v0.2 协议 §9: 兜底失败码 (set -e 触发的退出走这条)
# 各阶段开始前 update CURRENT_FAIL_CODE, 出错时由 trap emit
CURRENT_FAIL_CODE="MANAGER_UNKNOWN"
fail() {
  local code="${1:-$CURRENT_FAIL_CODE}"
  echo "::B2R_PHASE::failed:$code"
  exit 1
}
# set -e 隐式失败时也打 phase
trap 'rc=$?; if [[ $rc -ne 0 ]]; then echo "::B2R_PHASE::failed:$CURRENT_FAIL_CODE"; fi' EXIT

# AutoDL 实例的 conda 默认装在 /root/miniconda3，但 SSH 非交互式 shell 不读 ~/.bashrc，
# 所以手动注入到 PATH。同时兜底 /opt/conda（部分镜像）和 /usr/local/bin。
for d in /root/miniconda3/bin /opt/conda/bin /usr/local/bin; do
  [[ -d "$d" && ":$PATH:" != *":$d:"* ]] && export PATH="$d:$PATH"
done

# 铁律: 所有 box2robot worker 必须装到 conda env "b2r", 永远不装 base.
# base 会被 AutoDL 镜像自带的 lerobot/datasets/av 老版本污染, 导致 import 假成功但子模块缺失.
B2R_ENV_NAME="b2r"
B2R_PYTHON_VERSION="3.12"

SERVER="https://robot.box2ai.com"
CUDA="cu124"
PROJECT_DIR="/root/box2robot_gpu_worker"
LOG="/root/b2r_install.log"

# autodl-fs 共享盘根目录探测 (region 内跨实例持久化, 用于 HF_HOME + pip cache)
AUTODL_FS_ROOT=""
for d in /root/autodl-fs /autodl-fs; do
  [[ -d "$d" ]] && AUTODL_FS_ROOT="$d" && break
done

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

# AutoDL 学术加速 (仅本 SSH session 有效, 脚本退出自动失效)
# 加速 github.com / githubusercontent.com / huggingface.co
# 主路径仍走 PIP_INDEX_URL=清华源 + HF_ENDPOINT=hf-mirror, 学术加速作为兜底
# 详见 claude_md/manager_autodl_protocol.md §3.7 "网络代理与学术加速"
if [[ -f /etc/network_turbo ]]; then
  source /etc/network_turbo 2>/dev/null && \
    echo "[$(date +%T)] → AutoDL 学术加速已启用 (github + huggingface)" || \
    echo "[$(date +%T)] ⚠ /etc/network_turbo 加载失败, 继续 (走清华源/hf-mirror)"
else
  echo "[$(date +%T)] ⚠ 此实例无 /etc/network_turbo, 走清华源/hf-mirror 兜底"
fi

CURRENT_FAIL_CODE="MANAGER_INSTALL_FAIL"
phase "init"
echo "[$(date +%T)] === Box2Robot GPU Worker 一键安装 ==="
echo "  server  : $SERVER"
echo "  cuda    : $CUDA"
echo "  project : $PROJECT_DIR"

# ---- Phase 1: 环境检测 + 切到 conda env "b2r" ----
CURRENT_FAIL_CODE="MANAGER_INSTALL_FAIL"
phase "probe"
echo "[$(date +%T)] [1/5] 环境检测 + 准备 conda env '$B2R_ENV_NAME'"
nvidia-smi -L 2>&1 | head -1 || { echo "[!] nvidia-smi 不可用"; fail MANAGER_INSTALL_FAIL; }

# 找 conda 二进制 (AutoDL 镜像默认 /root/miniconda3, 兜底 /opt/conda)
CONDA_BIN=""
for c in /root/miniconda3/bin/conda /opt/conda/bin/conda; do
  [[ -x "$c" ]] && CONDA_BIN="$c" && break
done
if [[ -z "$CONDA_BIN" ]]; then
  echo "[!] 找不到 conda — AutoDL 镜像异常?"; fail MANAGER_INSTALL_FAIL
fi
CONDA_ROOT="$(dirname $(dirname $CONDA_BIN))"
B2R_ENV_DIR="$CONDA_ROOT/envs/$B2R_ENV_NAME"

# 创建 b2r env (如果不存在). python=3.12 锁定版本.
if [[ ! -d "$B2R_ENV_DIR" ]]; then
  echo "  → 创建 conda env '$B2R_ENV_NAME' (python=$B2R_PYTHON_VERSION)"
  "$CONDA_BIN" create -n "$B2R_ENV_NAME" "python=$B2R_PYTHON_VERSION" -y 2>&1 | tee -a "$LOG"
else
  echo "  → conda env '$B2R_ENV_NAME' 已存在, 复用"
fi

# 后续所有 PY/PIP 都走 b2r env, 永远不污染 base
PY="$B2R_ENV_DIR/bin/python"
PIP="$PY -m pip"
[[ -x "$PY" ]] || { echo "[!] $PY 不存在 — env 创建失败"; fail MANAGER_INSTALL_FAIL; }
echo "  → Python: $PY"
$PY --version
$PIP --version >/dev/null 2>&1 || $PY -m ensurepip || true

cd "$PROJECT_DIR"
[[ -d lerobot ]] || {
  echo "[!] lerobot/ 子目录不存在 — 上游 tar 包没带 lerobot，尝试 git clone"
  git clone https://github.com/huggingface/lerobot.git lerobot \
    || git clone https://gitclone.com/github.com/huggingface/lerobot.git lerobot
  (cd lerobot && git checkout cb0a9449 || true)
}

# ---- Phase 2: PyTorch + LeRobot ----
CURRENT_FAIL_CODE="MANAGER_INSTALL_FAIL"
phase "deps"
echo "[$(date +%T)] [2/5] 装 PyTorch ($CUDA) + LeRobot 依赖"
# 国内 pip 加速
export PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
# HuggingFace 镜像
export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}
# autodl-fs 共享盘 (region 内跨实例持久化):
#   HF_HOME       → base 权重缓存 (pi0/pi05 14GB+, 跨 worker 复用)
#   PIP_CACHE_DIR → pip wheel 缓存 (lerobot[diffusion]/[pi]/[smolvla] 等 extras
#                   首次实例下载, 之后所有实例从共享盘秒装, 不再走网络)
if [[ -n "$AUTODL_FS_ROOT" ]]; then
  export HF_HOME=${HF_HOME:-$AUTODL_FS_ROOT/data/box2robot-base-models}
  export PIP_CACHE_DIR=${PIP_CACHE_DIR:-$AUTODL_FS_ROOT/pip-cache}
  mkdir -p "$HF_HOME" "$PIP_CACHE_DIR" 2>/dev/null || true
  echo "  HF_HOME       = $HF_HOME"
  echo "  PIP_CACHE_DIR = $PIP_CACHE_DIR (跨实例 wheel 缓存)"
else
  echo "  ⚠ autodl-fs 不可用, pip cache 走本地默认 (~/.cache/pip), 跨实例不共享"
fi

if ! $PY -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
  echo "  → torch CUDA 版未装，开始安装"
  $PIP install torch torchvision torchaudio \
      --index-url "https://download.pytorch.org/whl/${CUDA}" \
      2>&1 | tee -a "$LOG"
fi
$PY -c "import torch; print(f'torch={torch.__version__} cuda={torch.cuda.is_available()}')"

# LeRobot 基础 + 全部 policy extras
# 历史教训 (2026-05-10): 之前只装 [dataset,training], 然后训 pi0 / smolvla / diffusion / gr00t
# 各自缺包都要 hot-fix 一次:
#   - lerobot[pi]: transformers (pi0/pi05 用)
#   - lerobot[smolvla]: transformers (smolvla 用, 跟 pi 共享)
#   - lerobot[diffusion]: diffusers
#   - lerobot[gr00t]: peft, dm-tree (注: lerobot pyproject.toml 没列 dm-tree 在 extras 里, 单独装)
#   - lerobot[peft]: peft (LoRA 微调用)
# 改成默认装全套, 一次到位. pip-cache 共享盘命中后秒装, 没显著延长 setup 时间.
# 用 import accelerate 判断, 因为 import lerobot 顶层 success 不代表 training extras 装好.
if ! $PY -c "import lerobot, accelerate, transformers, diffusers, peft, tree" 2>/dev/null; then
  echo "  → 装 lerobot 基础包"
  (cd lerobot && $PIP install -e . --no-build-isolation) 2>&1 | tee -a "$LOG"
  echo "  → 装 lerobot[dataset,training,pi,smolvla,diffusion,peft,gr00t]"
  $PIP install "lerobot[dataset,training,pi,smolvla,diffusion,peft,gr00t] @ file:./lerobot" --no-build-isolation 2>&1 | tee -a "$LOG" || true
  # GR00T 用的 dm-tree (Google DeepMind tree util) 不在 lerobot extras 里, 单独装
  echo "  → 装 dm-tree (GR00T 依赖, lerobot extras 漏列)"
  $PIP install dm-tree 2>&1 | tee -a "$LOG" || true
fi

# ---- Phase 3: box2robot_gpu_worker ----
CURRENT_FAIL_CODE="MANAGER_INSTALL_FAIL"
phase "install"
echo "[$(date +%T)] [3/5] 装 box2robot_gpu_worker (env=$B2R_ENV_NAME)"
$PIP install -e . --no-build-isolation 2>&1 | tee -a "$LOG"
# entry 必须在 b2r env 的 bin/ 下, 不接受 base 的同名命令
B2R_ENTRY=""
for cand in "$B2R_ENV_DIR/bin/b2r-gpu" "$B2R_ENV_DIR/bin/b2r-worker"; do
  [[ -x "$cand" ]] && B2R_ENTRY="$cand" && break
done
if [[ -z "$B2R_ENTRY" ]]; then
  echo "[!] b2r-worker / b2r-gpu 入口在 $B2R_ENV_DIR/bin/ 下不存在"; fail MANAGER_INSTALL_FAIL
fi
echo "  → entry: $B2R_ENTRY"

# ---- Phase 4: 启动 worker，拿绑定码 ----
CURRENT_FAIL_CODE="MANAGER_WORKER_TIMEOUT"
phase "start_worker"
echo "[$(date +%T)] [4/5] 启动 b2r-worker（后台, env=$B2R_ENV_NAME）"
ENTRY="$B2R_ENTRY"
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
  fail MANAGER_WORKER_TIMEOUT
fi

# ---- Phase 5: 系统级 systemd 自启动（实例重启自动起 worker）----
# autostart 失败不算致命 (worker 已经启动了), 仅打 warning, 不 fail
CURRENT_FAIL_CODE="MANAGER_INSTALL_FAIL"
phase "autostart"
echo "[$(date +%T)] [5/5] 注册系统级 systemd 自启动"
PY_BIN=$(dirname "$ENTRY")
cat > /etc/systemd/system/box2robot-worker.service <<EOF
[Unit]
Description=Box2Robot GPU Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/box2robot_gpu_worker
Environment=PATH=$PY_BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=HF_ENDPOINT=$HF_ENDPOINT
Environment=HF_HOME=$HF_HOME
Environment=PIP_INDEX_URL=$PIP_INDEX_URL
Environment=PIP_CACHE_DIR=${PIP_CACHE_DIR:-/root/.cache/pip}
ExecStart=$ENTRY --server $SERVER
Restart=always
RestartSec=10
StandardOutput=append:/root/b2r_worker.log
StandardError=append:/root/b2r_worker.log

[Install]
WantedBy=multi-user.target
EOF

# AutoDL 实例的 systemd 通常可用；如果不可用退化到 ~/.bashrc
if systemctl daemon-reload 2>/dev/null; then
  systemctl enable box2robot-worker 2>&1 | head -3
  # 如果之前 nohup 起了 worker，先 kill 掉避免 systemd 起冲突
  pkill -f "$(basename $ENTRY)" 2>/dev/null || true
  sleep 1
  systemctl restart box2robot-worker 2>&1 | head -3
  echo "  ✓ systemd 系统级自启动已配置 (开机自动起)"
  systemctl is-active box2robot-worker || true
else
  echo "  ⚠ systemd 不可用，回退到 ~/.bashrc 自启（仅 SSH 登录时触发）"
  if ! grep -q "b2r-worker" /root/.bashrc 2>/dev/null; then
    cat >> /root/.bashrc <<EOF
# Box2Robot GPU Worker auto-start (fallback)
if ! pgrep -f "$(basename $ENTRY)" > /dev/null; then
    nohup $ENTRY --server $SERVER > /root/b2r_worker.log 2>&1 &
fi
EOF
  fi
fi

phase "done"
echo "[$(date +%T)] === 完成 ==="
echo "  device_id : $DEVICE_ID"
echo "  bind_code : ${BIND_CODE:-(已绑定)}"
echo "  worker pid: $WORKER_PID"
echo "  worker log: $WORKER_LOG"
