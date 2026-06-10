#!/usr/bin/env bash
# 在 AutoDL 实例（或任意 Linux 机器）上 **独立** 装 lingbot-vla 训练/推理环境。
#
# 跟 install_on_autodl.sh 关系:
#   - install_on_autodl.sh 装的是 b2r env (lerobot v0.5.2, 跑 act/diffusion/pi0/pi05/smolvla/gr00t)
#   - 本脚本装的是 b2r-vla env (lerobot v0.4.2 + lingbot-vla 4B + flash-attn), 跟 b2r env 完全隔离
#   - 两个 env 互不污染, worker.py 通过 subprocess 调对应 env 的 python
#
# 为什么独立 env?
#   lingbot-vla 锁死 lerobot v0.4.2 (install.sh 第一行写死), 跟 b2r env 的 v0.5.2 不兼容
#   (v0.5 把 LeRobotDataset 拆成 DatasetReader/DatasetWriter, transformers 升 v5).
#   强升必炸现有 6 个 model_type pipeline. 详见 claude_md/lingbot_vla_integration.md (待写)
#
# 用法 (manager 自动 SSH 调用 或 手动跑):
#   bash scripts/install_lingbot_vla_addon.sh                          # 默认全自动
#   bash scripts/install_lingbot_vla_addon.sh --cuda cu124             # 强制 cuda 版本
#   bash scripts/install_lingbot_vla_addon.sh --project-dir /xxx       # 指定 lingbot-vla clone 目录
#
# 输出 (沿用 install_on_autodl.sh 的 phase 协议, manager 可解析):
#   ::B2R_PHASE::<name>            阶段进入 (init/probe/env/torch/clone/install/verify/done)
#   ::B2R_PHASE::failed:CODE       失败 (CODE ∈ MANAGER_INSTALL_FAIL / MANAGER_UNKNOWN)
#
# 协议归属: 同 install_on_autodl.sh, 见 claude_md/manager_autodl_protocol.md §3.4
#
# 关键路径 (AutoDL 数据盘铁律, 扛 reboot 不丢):
#   /root/autodl-tmp/workspace/box2robot/lingbot-vla/   ← lingbot-vla 仓库 clone
#   /root/miniconda3/envs/b2r-vla/                       ← 独立 conda env (python 3.12 + torch 2.8 + cuda 12.x)
#   /root/autodl-fs/data/box2robot-base-models/          ← HF_HOME (跟 b2r env 共用, cache 按 repo_id 隔离不冲突)
#
# 网络加速 (沿用 install_on_autodl.sh 约定):
#   PIP_INDEX_URL  = https://pypi.tuna.tsinghua.edu.cn/simple
#   HF_ENDPOINT    = https://hf-mirror.com
#   学术加速        = source /etc/network_turbo (AutoDL 实例独有)

set -e

# ============================================================================
# 0. 协议: 失败码与 phase 输出
# ============================================================================
CURRENT_FAIL_CODE="MANAGER_UNKNOWN"
fail() {
  local code="${1:-$CURRENT_FAIL_CODE}"
  echo "::B2R_PHASE::failed:$code"
  exit 1
}
trap 'rc=$?; if [[ $rc -ne 0 ]]; then echo "::B2R_PHASE::failed:$CURRENT_FAIL_CODE"; fi' EXIT
phase() { echo "::B2R_PHASE::$1"; }

# ============================================================================
# 1. 配置
# ============================================================================
B2R_VLA_ENV="b2r-vla"
B2R_VLA_PYTHON="3.12"
LINGBOT_VLA_REPO="https://github.com/Robbyant/lingbot-vla.git"
LINGBOT_VLA_DIR_DEFAULT="/root/autodl-tmp/workspace/box2robot/lingbot-vla"
CUDA_VERSION=""   # 默认空 → 自动探测
LOG="/root/b2r_install_vla.log"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cuda)        CUDA_VERSION="$2";        shift 2;;
    --project-dir) LINGBOT_VLA_DIR_DEFAULT="$2"; shift 2;;
    --env)         B2R_VLA_ENV="$2";         shift 2;;
    -h|--help)
      grep -E '^#' "$0" | sed 's/^# \?//' | head -50
      exit 0;;
    *) echo "[!] unknown arg: $1"; shift;;
  esac
done

LINGBOT_VLA_DIR="$LINGBOT_VLA_DIR_DEFAULT"

# conda 路径注入 (非交互式 SSH 不读 ~/.bashrc)
for d in /root/miniconda3/bin /opt/conda/bin /usr/local/bin; do
  [[ -d "$d" && ":$PATH:" != *":$d:"* ]] && export PATH="$d:$PATH"
done

# AutoDL 学术加速 (github + huggingface 提速, 仅本 session 有效)
if [[ -f /etc/network_turbo ]]; then
  source /etc/network_turbo 2>/dev/null && \
    echo "[$(date +%T)] → AutoDL 学术加速已启用" || true
fi

# 国内镜像
export PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}

# autodl-fs 共享盘 (HF_HOME 跟 b2r env 共用, cache 按 repo_id 隔离)
AUTODL_FS_ROOT=""
for d in /root/autodl-fs /autodl-fs; do
  [[ -d "$d" ]] && AUTODL_FS_ROOT="$d" && break
done
if [[ -n "$AUTODL_FS_ROOT" ]]; then
  export HF_HOME=${HF_HOME:-$AUTODL_FS_ROOT/data/box2robot-base-models}
  export PIP_CACHE_DIR=${PIP_CACHE_DIR:-$AUTODL_FS_ROOT/pip-cache}
  mkdir -p "$HF_HOME" "$PIP_CACHE_DIR" 2>/dev/null || true
fi

CURRENT_FAIL_CODE="MANAGER_INSTALL_FAIL"
phase "init"
echo "[$(date +%T)] === LingBot-VLA Addon 安装 ==="
echo "  env       : $B2R_VLA_ENV (python=$B2R_VLA_PYTHON)"
echo "  lingbot-vla → $LINGBOT_VLA_DIR"
echo "  HF_HOME   = ${HF_HOME:-(本地默认)}"
echo "  PIP_CACHE = ${PIP_CACHE_DIR:-(本地默认)}"

# ============================================================================
# 2. Probe: 探测 nvidia / conda / CUDA 版本
# ============================================================================
phase "probe"
echo "[$(date +%T)] [1/6] 环境探测"

# nvidia-smi 探测 driver 支持的 CUDA 上限
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "[!] nvidia-smi 不可用 (没装 NVIDIA 驱动?)"; fail MANAGER_INSTALL_FAIL
fi
nvidia-smi -L | head -3

# 自动选 CUDA 版本 (匹配 PyTorch 官方 wheel 命名)
if [[ -z "$CUDA_VERSION" ]]; then
  DRIVER_CUDA=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1)
  if [[ -z "$DRIVER_CUDA" ]]; then
    echo "  ⚠ 无法探测 driver CUDA, 默认 cu124"; CUDA_VERSION="cu124"
  else
    DRIVER_MAJOR=$(echo "$DRIVER_CUDA" | cut -d. -f1)
    DRIVER_MINOR=$(echo "$DRIVER_CUDA" | cut -d. -f2)
    if [[ "$DRIVER_MAJOR" -lt 12 ]]; then
      echo "[!] driver CUDA=$DRIVER_CUDA < 12.0, lingbot-vla 要求 CUDA 12+"
      fail MANAGER_INSTALL_FAIL
    fi
    # PyTorch CUDA 是前向兼容的: cu128 在 driver CUDA 13.x 上能跑.
    # lingbot-vla 锁 torch 2.8.0, 这个版本最高 cu128 wheel, cu121 没有 torch 2.8.0.
    # 踩过: 803 机 driver CUDA=13.0, 原逻辑 fallback cu121 → ERROR "No matching distribution found for torch==2.8.0"
    if [[ "$DRIVER_MAJOR" -ge 13 ]]; then
      CUDA_VERSION="cu128"
    elif [[ "$DRIVER_MINOR" -ge 8 ]]; then CUDA_VERSION="cu128"
    elif [[ "$DRIVER_MINOR" -ge 4 ]]; then CUDA_VERSION="cu124"
    else CUDA_VERSION="cu121"
    fi
    echo "  driver CUDA=$DRIVER_CUDA → 选 PyTorch $CUDA_VERSION wheel"
  fi
fi

# conda 探测
CONDA_BIN=""
for c in /root/miniconda3/bin/conda /opt/conda/bin/conda; do
  [[ -x "$c" ]] && CONDA_BIN="$c" && break
done
[[ -z "$CONDA_BIN" ]] && { echo "[!] 找不到 conda"; fail MANAGER_INSTALL_FAIL; }
CONDA_ROOT="$(dirname $(dirname $CONDA_BIN))"
ENV_DIR="$CONDA_ROOT/envs/$B2R_VLA_ENV"

# ============================================================================
# 3. Env: 建独立 conda env
#    AutoDL 镜像默认 .condarc 含清华 pkgs/free/ 仓库, 但 anaconda 2017 后已废弃
#    该仓库, 清华镜像偶尔返回非 JSON 内容导致 conda create 报
#    "RuntimeError: Unable to read repodata JSON file ... pkgs/free/noarch".
#    踩过: 803 机 2026-05-26. 修复: 删 free 行 + clean cache. POLICY_INSTALL_GUIDE.md 已记.
# ============================================================================
phase "env"
echo "[$(date +%T)] [2/6] 准备 conda env '$B2R_VLA_ENV'"
if [[ ! -d "$ENV_DIR" ]]; then
  # 预清理: 防 AutoDL conda free 仓库 repodata 坑
  if [[ -f /root/.condarc ]] && grep -q 'pkgs/free' /root/.condarc; then
    echo "  → 检测到 .condarc 含废弃的 pkgs/free 仓库, 自动删除 (防 repodata JSON 错误)"
    sed -i '/pkgs\/free/d' /root/.condarc
  fi
  "$CONDA_BIN" clean --index-cache -y 2>&1 | tail -3 || true
  echo "  → 创建 env (python=$B2R_VLA_PYTHON)"
  "$CONDA_BIN" create -n "$B2R_VLA_ENV" "python=$B2R_VLA_PYTHON" -y 2>&1 | tee -a "$LOG"
else
  echo "  → env 已存在, 复用"
fi
PY="$ENV_DIR/bin/python"
PIP="$PY -m pip"
[[ -x "$PY" ]] || { echo "[!] $PY 不存在"; fail MANAGER_INSTALL_FAIL; }
$PY --version
$PIP install -q --upgrade pip setuptools wheel 2>&1 | tee -a "$LOG" || true

# ============================================================================
# 4. Torch: 必须先装 CUDA 版 torch (lingbot-vla README 漏了这步, 不显式装会拉 CPU torch)
# ============================================================================
phase "torch"
echo "[$(date +%T)] [3/6] 装 PyTorch 2.8.0 ($CUDA_VERSION)"
if $PY -c "import torch; assert torch.cuda.is_available(); assert torch.__version__.startswith('2.8')" 2>/dev/null; then
  echo "  → torch 2.8 CUDA 版已装, 跳过"
else
  # lingbot-vla README 锁 torch 2.8.0, 严格按这个版本装
  $PIP install torch==2.8.0 torchvision torchaudio \
      --index-url "https://download.pytorch.org/whl/${CUDA_VERSION}" \
      2>&1 | tee -a "$LOG"
fi
$PY -c "import torch; print(f'  torch={torch.__version__} cuda={torch.version.cuda} available={torch.cuda.is_available()}')"

# ============================================================================
# 5. Clone: 拉 lingbot-vla 官方仓库 (含 submodules)
#    AutoDL 学术加速 (proxy) 跟 git protocol v2 不兼容, 默认 clone 会报
#    "fatal: expected flush after ref listing". 强制 HTTP/1.1 + 大 postBuffer
#    解决. 踩过: 803 机 2026-05-26.
# ============================================================================
phase "clone"
echo "[$(date +%T)] [4/6] Clone lingbot-vla → $LINGBOT_VLA_DIR"
mkdir -p "$(dirname $LINGBOT_VLA_DIR)"
# 强制 HTTP/1.1 兼容 AutoDL 学术加速 (写 --global, 之后 git pull / submodule 都受益)
git config --global http.version HTTP/1.1 2>/dev/null || true
git config --global http.postBuffer 524288000 2>/dev/null || true
if [[ -d "$LINGBOT_VLA_DIR/.git" ]]; then
  echo "  → 已存在, git pull + submodule update"
  (cd "$LINGBOT_VLA_DIR" && git pull --ff-only && git submodule update --init --recursive) 2>&1 | tee -a "$LOG" || true
else
  # 学术加速失败时兜底走 gitclone.com (gitclone 偶尔 502, 再 fallback 走原 URL 关代理)
  git clone --recurse-submodules "$LINGBOT_VLA_REPO" "$LINGBOT_VLA_DIR" 2>&1 | tee -a "$LOG" \
    || git clone --recurse-submodules "https://gitclone.com/github.com/Robbyant/lingbot-vla.git" "$LINGBOT_VLA_DIR" 2>&1 | tee -a "$LOG" \
    || { unset http_proxy https_proxy; git clone --recurse-submodules "$LINGBOT_VLA_REPO" "$LINGBOT_VLA_DIR" 2>&1 | tee -a "$LOG"; }
fi
[[ -d "$LINGBOT_VLA_DIR/.git" ]] || { echo "[!] clone 全部失败"; fail MANAGER_INSTALL_FAIL; }

# ============================================================================
# 6. Install: 跑官方 install.sh 的等价步骤 (用 b2r-vla env 的 pip)
#    注意: 不直接 bash install.sh, 因为它假设当前 active env 是 lingbotvla,
#    我们显式用 $PIP 走 b2r-vla env 更可控.
# ============================================================================
phase "install"
echo "[$(date +%T)] [5/6] 装 lerobot v0.4.2 + lingbot-vla + flash-attn"
cd "$LINGBOT_VLA_DIR"

# 6.1 lerobot v0.4.2 (lingbot-vla 锁死, 跟 b2r env 的 v0.5.2 隔离)
if ! $PY -c "import lerobot; assert lerobot.__version__.startswith('0.4')" 2>/dev/null; then
  echo "  → 装 lerobot v0.4.2"
  $PIP install "https://github.com/huggingface/lerobot/archive/refs/tags/v0.4.2.tar.gz" \
    --no-build-isolation 2>&1 | tee -a "$LOG"
fi

# 6.2 lingbot-vla 本体
echo "  → 装 lingbot-vla 本体 (-e .)"
$PIP install -e . --no-build-isolation 2>&1 | tee -a "$LOG"

# 6.3 lingbot-depth + MoGe 视觉子模块 (--no-deps 避免重装 torch)
if [[ -d "lingbotvla/models/vla/vision_models/lingbot-depth" ]]; then
  echo "  → 装 lingbot-depth (--no-deps)"
  $PIP install -e ./lingbotvla/models/vla/vision_models/lingbot-depth/ --no-deps 2>&1 | tee -a "$LOG" || true
fi
if [[ -d "lingbotvla/models/vla/vision_models/MoGe" ]]; then
  echo "  → 装 MoGe"
  $PIP install -e ./lingbotvla/models/vla/vision_models/MoGe/ 2>&1 | tee -a "$LOG" || true
fi

# 6.4 flash-attn 2.8.3
# 历史教训 (803 机 2026-05-26):
#   - 直接 pip install flash-attn==2.8.3 走 setup.py: 它会先 guess 一个 prebuilt wheel URL,
#     下载到 pip-cache (/autodl-fs/data/pip-cache/), 然后 mv 到 build tmpdir → 跨 NFS 分区,
#     报 "Invalid cross-device link" → 退回 source build → 缺 nvcc → 编译失败 → 全流程失败.
#   - 解决: 直接 wget prebuilt wheel 到 $TMP (本地盘) 再 pip install 这个文件.
#     wheel 名: flash_attn-2.8.3+cu12torch2.8cxx11abiTRUE-cp312-cp312-linux_x86_64.whl
#     来源: https://github.com/Dao-AILab/flash-attention/releases/tag/v2.8.3
#     cxx11abi 默认 TRUE (PyTorch >= 2.0 全用 cxx11 ABI), 不匹配再 fallback FALSE.
if ! $PY -c "import flash_attn; assert flash_attn.__version__.startswith('2.8')" 2>/dev/null; then
  FA_VER="2.8.3"
  PY_TAG="cp312"
  TORCH_TAG="torch2.8"
  CUDA_TAG="cu12"
  WHEEL_DIR="/root"
  for ABI_TAG in cxx11abiTRUE cxx11abiFALSE; do
    WHEEL_NAME="flash_attn-${FA_VER}+${CUDA_TAG}${TORCH_TAG}${ABI_TAG}-${PY_TAG}-${PY_TAG}-linux_x86_64.whl"
    WHEEL_URL="https://github.com/Dao-AILab/flash-attention/releases/download/v${FA_VER}/${WHEEL_NAME}"
    WHEEL_PATH="${WHEEL_DIR}/${WHEEL_NAME}"
    if [[ -f "$WHEEL_PATH" ]] || wget -q "$WHEEL_URL" -O "$WHEEL_PATH"; then
      [[ -s "$WHEEL_PATH" ]] || { rm -f "$WHEEL_PATH"; continue; }
      echo "  → 下载/复用 prebuilt wheel: $WHEEL_NAME ($(du -h $WHEEL_PATH | cut -f1))"
      if $PIP install "$WHEEL_PATH" --no-build-isolation 2>&1 | tee -a "$LOG"; then
        break
      fi
    else
      rm -f "$WHEEL_PATH"
    fi
  done
  if ! $PY -c "import flash_attn" 2>/dev/null; then
    # prebuilt wheel 全失败 → 退回 source build (要 nvcc, 慢)
    if ! command -v nvcc >/dev/null 2>&1 && ! [[ -x "$ENV_DIR/bin/nvcc" ]]; then
      echo "  → prebuilt wheel 失败, 装 conda cuda-nvcc 后 source build (~2-5 分钟)"
      "$CONDA_BIN" install -n "$B2R_VLA_ENV" -c nvidia cuda-nvcc -y 2>&1 | tee -a "$LOG" || true
      export PATH="$ENV_DIR/bin:$PATH"
    fi
    echo "  → 编译 flash-attn==${FA_VER} (耗时 10-30 分钟)"
    $PIP install flash-attn==${FA_VER} --no-build-isolation 2>&1 | tee -a "$LOG" \
      || { echo "[!] flash-attn 装失败 (prebuilt + source 都失败)"; fail MANAGER_INSTALL_FAIL; }
  fi
fi

# ============================================================================
# 6.5. 给 b2r env 也补上 WS 客户端依赖 (推理时 worker 在 b2r env, 走 WS 连 b2r-vla 子进程)
#      websockets / msgpack / typing_extensions — lingbot-vla 自带 deploy/websocket_client_policy
#      用到它们, 但 b2r env 默认没装 (b2r 主路径只用 lerobot/transformers).
#      装到 b2r 不影响 b2r-vla, 同时 import lingbot-vla 的 msgpack_numpy.py 需要 msgpack.
# ============================================================================
B2R_PIP="/root/miniconda3/envs/b2r/bin/pip"
if [[ -x "$B2R_PIP" ]]; then
  phase "client_deps"
  echo "[$(date +%T)] [5.5/6] 给 b2r env 装 WS 客户端依赖 (websockets/msgpack/typing_extensions)"
  CURRENT_FAIL_CODE="MANAGER_INSTALL_FAIL"
  "$B2R_PIP" install -q --index-url "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
      websockets "msgpack>=1.0.0" "typing_extensions>=4.0" \
      || echo "[!] b2r env WS 依赖装失败 (推理时再补也不晚)"
  echo "  ✓ b2r env WS 依赖就绪"
else
  echo "[!] 跳过 b2r env WS 依赖 (b2r env 不存在: $B2R_PIP)"
fi

# ============================================================================
# 7. Verify: 导入测试 + 打印版本
# ============================================================================
phase "verify"
echo "[$(date +%T)] [6/6] 验证安装"
$PY <<'PYEOF' || fail MANAGER_INSTALL_FAIL
import sys
print(f"  python      = {sys.version.split()[0]}")
import torch
print(f"  torch       = {torch.__version__} (cuda={torch.version.cuda}, available={torch.cuda.is_available()})")
import lerobot
print(f"  lerobot     = {lerobot.__version__}")
try:
    import lingbotvla
    print(f"  lingbotvla  = {getattr(lingbotvla, '__version__', '(no __version__)')}")
except ImportError as e:
    print(f"[!] lingbotvla import failed: {e}"); sys.exit(1)
try:
    import flash_attn
    print(f"  flash_attn  = {flash_attn.__version__}")
except ImportError as e:
    print(f"[!] flash_attn import failed: {e}"); sys.exit(1)
assert lerobot.__version__.startswith("0.4"), f"lerobot 版本异常: {lerobot.__version__}, 应该是 0.4.x"
print("  ✓ 所有关键依赖 import 成功")
PYEOF

phase "done"
echo "[$(date +%T)] === LingBot-VLA Addon 安装完成 ==="
echo ""
echo "  env path  : $ENV_DIR"
echo "  python    : $PY"
echo "  lingbot-vla: $LINGBOT_VLA_DIR"
echo ""
echo "  下一步 (训练示例):"
echo "    conda activate $B2R_VLA_ENV"
echo "    cd $LINGBOT_VLA_DIR"
echo "    bash train.sh tasks/vla/train_lingbotvla.py configs/vla/real_load20000h.yaml"
echo ""
echo "  worker.py 集成时通过 subprocess 调:"
echo "    $PY -m torch.distributed.run --nproc-per-node=N $LINGBOT_VLA_DIR/tasks/vla/train_lingbotvla.py <cfg>"
