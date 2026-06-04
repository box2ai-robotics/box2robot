# LeRobot 策略安装与配置指南

本文档覆盖 Box2Robot GPU Worker 可用的所有 LeRobot 策略的安装、配置和训练方法。

> 基于 LeRobot v0.5.2 (本地 `lerobot/` 目录), Python >= 3.12, PyTorch >= 2.7

---

## 目录

- [前置环境](#前置环境)
- [策略总览](#策略总览)
- [安装方法](#安装方法)
  - [ACT (已有)](#1-act-action-chunking-transformer)
  - [Diffusion Policy](#2-diffusion-policy)
  - [SmolVLA](#3-smolvla)
  - [Pi0 / Pi0.5](#4-pi0--pi05)
  - [GROOT (GR00T N1)](#5-groot)
  - [Wall-X](#6-wall-x)
  - [Multi-Task DiT](#7-multi-task-dit)
  - [SARM](#8-sarm)
  - [XVLA](#9-xvla)
  - [VQ-BeT / TDMPC](#10-vq-bet--tdmpc)
- [训练命令速查](#训练命令速查)
- [显存估算](#显存估算)
- [常见问题](#常见问题)

---

## 前置环境

所有策略共享同一个基础环境。如果你已按 `README.md` 完成安装，基础环境已就绪。

```bash
# 确认基础环境
conda activate b2r
python -c "import torch; print(f'PyTorch {torch.__version__}, CUDA {torch.cuda.is_available()}')"
python -c "import lerobot; print(f'LeRobot {lerobot.__version__}')"
```

如果 LeRobot 尚未安装：

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[training]" --no-build-isolation
cd ..
pip install -e .
```

`[training]` 包含 `dataset` + `accelerate` + `wandb`，是所有策略训练的基础依赖。

---

## AutoDL 实例：共享 pip cache（强烈推荐）

> ⚠️ **每次开新 AutoDL 实例都重新下载几 GB wheel 太浪费**。本节说明如何让同 region 的所有实例共享一份 pip wheel cache，避免重复下载。

### 原理

AutoDL 提供两个特殊存储：
| 路径 | 性质 | 用途 |
|---|---|---|
| `/root/autodl-tmp/` | 数据盘，**单实例独占**，扛 reboot | 项目代码（`workspace/box2robot/`） |
| `/root/autodl-fs/` | 网络盘，**同 region 跨实例共享**，扛实例销毁 | pip wheel cache + HF 模型权重 |

把 `PIP_CACHE_DIR` 指向 `autodl-fs/pip-cache`：第一台实例下载的 wheel 落到共享盘，**之后同 region 任何实例 `pip install` 都能命中缓存秒装**，跨 region 各装一份。

### 使用方法（任何新开的 AutoDL 实例）

**1. 一次性写入 `/etc/profile.d/b2r_env.sh`**（见下文"固化到 /etc/profile.d"），之后每次 login shell 自动生效

**2. 创建目录（首次）**：
```bash
mkdir -p /root/autodl-fs/pip-cache /root/autodl-fs/data/box2robot-base-models
```

**3. 装任意 lerobot extra, 命中 cache 秒装**：
```bash
# 重新 SSH 进来 / 跑 bash -lc 让 env 生效
pip install "lerobot[diffusion,pi,smolvla] @ file:./lerobot" --no-build-isolation
```

**临时 export（不写文件, 仅本 session 用）**：
```bash
export PIP_CACHE_DIR=/root/autodl-fs/pip-cache
export HF_HOME=/root/autodl-fs/data/box2robot-base-models
export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
export HF_ENDPOINT=https://hf-mirror.com
```

**固化到 `/etc/profile.d/b2r_env.sh`（推荐, login shell 自动加载, 比 `~/.bashrc` 更可靠）**：
```bash
cat > /etc/profile.d/b2r_env.sh <<'EOF'
# Box2Robot AutoDL 环境配置 (region-scoped 共享盘 + HF/pip 国内镜像)
# 自动加载: bash login shell 会 source /etc/profile -> /etc/profile.d/*.sh
[ -d /root/autodl-fs ] && {
    export PIP_CACHE_DIR=/root/autodl-fs/pip-cache
    export HF_HOME=/root/autodl-fs/data/box2robot-base-models
    export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
    export HF_ENDPOINT=https://hf-mirror.com
}
EOF
chmod 644 /etc/profile.d/b2r_env.sh
```

> **为什么不写 `~/.bashrc`**：paramiko/SSH 非交互式 exec_command 不会 source `~/.bashrc`（只会 source `/etc/profile` 链）。而 `bash -lc <cmd>` 或者交互式 ssh 都会走 `/etc/profile.d/*.sh`，所以这个位置最通用。
>
> **`HF_ENDPOINT=hf-mirror.com` 的必要性**：缺这个变量时, 任何要从 HuggingFace Hub 下东西的策略会卡死或失败：
> - **multi_task_dit** 实例化时下 `openai/clip-vit-base-patch16` (config + 权重)
> - **smolvla / pi0 / pi05 / wall_x / xvla / groot** 加载 base 权重 (lerobot 命名空间)
> - **lerobot 数据集**: `datasets.load_dataset("lerobot/...")`
> - **transformers tokenizer**: 任何 `AutoTokenizer.from_pretrained(...)` 调用
>
> 国内直连 huggingface.co 通常 [Errno 99] Cannot assign requested address 或超时 → 看起来像策略 bug，实际是网络。

> **AutoDL 实例没有 systemd**：开机自启只能走 `/root/onstart.sh`（AutoDL 镜像约定的开机钩子）或 `~/.bashrc`（仅 SSH 登录触发, 不可靠）。worker 长跑用 `nohup ... &` 后台跑 + 写 PID 到 `/root/b2r_worker.pid`，详见 `scripts/install_on_autodl.sh`。

### 西北B区已预热 cache（813 机）

`/root/autodl-fs/pip-cache/` (~7 GB, 536 个 wheel)，覆盖以下 extras：

```
lerobot[dataset,training,diffusion,pi,smolvla,multi_task_dit,wallx,xvla,sarm]
```

预热时间: 2026-05-09。包含 transformers 5.3.0、diffusers 0.35.2、peft 0.19.1、accelerate 1.13.0、torch 2.10.0+cu128、scipy、qwen-vl-utils、torchdiffeq、num2words、faker、matplotlib 等所有重型依赖的 wheel。

**已验证可 import 的策略**（在 813 机 `b2r` env 实测，2026-05-10）：

| 策略 | Import | 备注 |
|---|---|---|
| act, tdmpc, vqbet, sac, rtc | ✓ | 基础策略，无 extra 依赖 |
| diffusion | ✓ | `lerobot[diffusion]` |
| pi0, pi0_fast, pi05 | ✓ | `lerobot[pi]` |
| smolvla | ✓ | `lerobot[smolvla]` |
| multi_task_dit | ✓ | `lerobot[multi_task_dit]` |
| wall_x | ✓ | `lerobot[wallx]` |
| xvla | ✓ | `lerobot[xvla]` |
| groot | ✓ (顶层 import) | 实跑需 `flash-attn`（要按 GPU SM 编译，未预热） |
| **sarm** | ✗ | **`lerobot.policies.sarm` 模块在上游仓库不存在**，extra 装的是依赖（pydantic/faker/qwen-vl-utils），但策略代码未合入 |

### 一次性预热脚本

如果开了一台新 region 的实例，需要预热全套 extras 给同 region 后续实例用，跑：

```bash
cd box2robot_gpu_cloud_manager
conda activate b2r
python _setup_full_extras.py <实例名关键字>
```

脚本流程: 装 miniconda → 创建 b2r conda env → clone box2robot → clone lerobot → 装 torch cu124 → 装 lerobot 全套 extras（wheel 全落 autodl-fs）。耗时 30-60 分钟。

### 已知坑（避坑指南）

| 现象 | 根因 | 修复 |
|---|---|---|
| `Upload did not complete.` 创建 conda env 时 | conda 24.4 默认装 `anaconda-anon-usage` plugin，学术加速代理拦截 telemetry 上传导致 abort | `pip uninstall anaconda-anon-usage -y` |
| `JSONDecodeError: Expecting value` conda 拉 repodata 失败 | repodata cache 损坏（拿到空 JSON） | `conda clean -i -y && rm -rf /root/miniconda3/pkgs/cache/*.json` |
| `non-default solver backend (libmamba) but it was not recognized` | `CONDA_NO_PLUGINS` 关掉了 libmamba | `conda config --set solver classic` |
| `lerobot/pyproject.toml: No such file or directory` | `git clone --depth 1 box2robot.git` 没拉 lerobot submodule | `cd box2robot_gpu_worker && rm -rf lerobot && git clone https://github.com/huggingface/lerobot.git lerobot && cd lerobot && git checkout cb0a9449` |
| `torchaudio requires torch==2.6.0, but you have torch 2.10.0` | 装 lerobot 时 pip resolver 升级 torch 但没同步 torchaudio | 装完 lerobot 后再跑 `pip install torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128` 拉齐版本 |

---

## 策略总览

| 策略 | 类型 | 参数量 | 需要图像 | 最低显存 | 额外依赖 |
|------|------|--------|---------|---------|---------|
| **ACT** | Transformer | ~10M | 可选 | 6GB | 无 (基础依赖已含) |
| **Diffusion** | 扩散模型 | ~25M | 可选 | 8GB | diffusers |
| **SmolVLA** | VLA (视觉语言动作) | ~450M | **必须** | 16GB | transformers, num2words |
| **Pi0** | VLA (PaliGemma) | ~2.5B | **必须** | 24GB | transformers, scipy |
| **Pi0.5** | VLA (Pi0 增强) | ~2.5B | **必须** | 24GB | transformers, scipy |
| **GROOT** | VLA (GR00T N1) | ~1B+ | **必须** | 24GB+ | transformers, flash-attn, timm |
| **Wall-X** | VLA (Qwen2.5-VL) | ~1B+ | **必须** | 24GB+ | transformers, peft, torchdiffeq |
| **Multi-Task DiT** | DiT | ~100M+ | 可选 | 12GB | transformers, diffusers |
| **SARM** | VLA | ~500M+ | **必须** | 16GB+ | transformers, pydantic, faker |
| **XVLA** | VLA | ~500M+ | **必须** | 16GB+ | transformers |
| **VQ-BeT** | VQ-VAE + Transformer | ~20M | 可选 | 8GB | 无 |
| **TDMPC** | Model-Based RL | ~15M | 可选 | 8GB | 无 |

**需要图像** = 训练数据集中必须包含摄像头图像 (Box2Robot 的 `aligned_frames` 含 images)

---

## 安装方法

### 1. ACT (Action Chunking Transformer)

**已包含在基础安装中，无需额外操作。**

ACT 是 Box2Robot 的默认策略，不需要额外的 optional dependencies。

```bash
# 验证
python -c "from lerobot.policies.act import ACTPolicy; print('ACT OK')"
```

配置文件: `configs/act_box2robot.yaml`

---

### 2. Diffusion Policy

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[diffusion]" --no-build-isolation
```

这会安装:
- `diffusers >= 0.27.2`

```bash
# 验证
python -c "from lerobot.policies.diffusion import DiffusionPolicy; print('Diffusion OK')"
```

配置文件: `configs/diffusion_box2robot.yaml`

**训练示例:**

```bash
python -m lerobot.scripts.lerobot_train \
  --policy.type=diffusion \
  --dataset.repo_id=your_dataset \
  --training.num_steps=10000 \
  --training.batch_size=64 \
  --policy.n_action_steps=8 \
  --policy.horizon=16
```

---

### 3. SmolVLA

SmolVLA 是轻量级 VLA 模型 (SmolVLM2-500M + Action Expert，总参数 ~450M)，适合消费级 GPU 微调。

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[smolvla]" --no-build-isolation
```

这会安装:
- `transformers == 5.3.0`
- `num2words >= 0.5.14`
- `accelerate >= 1.7.0`

```bash
# 验证
python -c "from lerobot.policies.smolvla import SmolVLAPolicy; print('SmolVLA OK')"
```

配置文件: `configs/smolvla_box2robot.yaml`

**预训练权重:** `lerobot/smolvla_base` (HuggingFace Hub, 首次使用自动下载 ~900MB)

**训练示例 (微调):**

```bash
python -m lerobot.scripts.lerobot_train \
  --policy.type=smolvla \
  --policy.pretrained_path=lerobot/smolvla_base \
  --dataset.repo_id=your_dataset \
  --training.num_steps=30000 \
  --training.batch_size=16 \
  --policy.chunk_size=50
```

**显存优化 (显存不足时):**
- `--policy.freeze_vision_encoder=true` — 冻结视觉编码器，省 ~30% 显存
- `--policy.gradient_checkpointing=true` — 梯度检查点，省 ~40% 显存但慢 ~20%
- `--policy.dtype=bfloat16` — 混合精度 (需 Ampere+ GPU: RTX 30xx/40xx/A100)
- 减小 batch_size 到 8 或 4

---

### 4. Pi0 / Pi0.5

Pi0 基于 PaliGemma 2B + Gemma 300M Action Expert，是 Physical Intelligence 开源的通用机器人 VLA。
Pi0.5 是增强版，使用 AdaRMS 条件化和 200-token tokenizer。

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[pi]" --no-build-isolation
```

这会安装:
- `transformers == 5.3.0`
- `scipy >= 1.14.0`

```bash
# 验证 Pi0
python -c "from lerobot.policies.pi0 import PI0Policy; print('Pi0 OK')"

# 验证 Pi0.5
python -c "from lerobot.policies.pi05 import PI05Policy; print('Pi0.5 OK')"
```

配置文件: `configs/pi0_box2robot.yaml`

**预训练权重:**
- Pi0: `lerobot/pi0_base` (HuggingFace Hub, ~5GB)
- Pi0.5: `lerobot/pi0.5_base` (HuggingFace Hub, ~5GB)

**训练示例 (Pi0 微调):**

```bash
python -m lerobot.scripts.lerobot_train \
  --policy.type=pi0 \
  --policy.pretrained_path=lerobot/pi0_base \
  --dataset.repo_id=your_dataset \
  --training.num_steps=20000 \
  --training.batch_size=8 \
  --policy.chunk_size=50
```

**训练示例 (Pi0.5):**

```bash
python -m lerobot.scripts.lerobot_train \
  --policy.type=pi05 \
  --policy.pretrained_path=lerobot/pi0.5_base \
  --dataset.repo_id=your_dataset \
  --training.num_steps=20000 \
  --training.batch_size=8
```

**相对动作 (Relative Actions):** Pi0/Pi0.5 支持相对动作训练，模型学习偏移量而非绝对位置:

```bash
python -m lerobot.scripts.lerobot_train \
  --policy.type=pi0 \
  --policy.use_relative_actions=true \
  --policy.relative_exclude_joints='["gripper"]' \
  ...
```

**显存优化:**
- `--policy.train_expert_only=true` — 只训练 Action Expert (~300M)，冻结 PaliGemma，显存 ~12GB
- `--policy.gradient_checkpointing=true` — 省 ~40% 显存
- `--policy.dtype=bfloat16` — 混合精度
- 全量微调需要 40GB+ 显存 (A100)

---

### 5. GROOT

GROOT (GR00T N1) 是 NVIDIA 开源的通用机器人 VLA，需要特殊依赖 (flash-attn)。

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[groot]" --no-build-isolation
```

这会安装:
- `transformers == 5.3.0`
- `peft >= 0.18.0`
- `diffusers >= 0.27.2`
- `dm-tree >= 0.1.8`
- `timm >= 1.0.0`
- `decord >= 0.6.0` (仅 x86_64)
- `ninja >= 1.11.1`
- `flash-attn >= 2.5.9` (仅 Linux, **不支持 Windows**)

**flash-attn 安装注意:**

```bash
# Linux: pip 直接安装 (需要 CUDA toolkit 匹配)
pip install flash-attn --no-build-isolation

# Windows: flash-attn 官方不支持 Windows!
# 方案 1: 使用 WSL2 + Ubuntu
# 方案 2: 用预编译 wheel (社区提供, 不保证稳定):
#   pip install flash-attn --no-build-isolation --find-links https://github.com/bdashore3/flash-attention/releases
# 方案 3: 不用 GROOT (推荐 SmolVLA 或 Pi0 替代)
```

```bash
# 验证
python -c "from lerobot.policies.groot import GR00TPolicy; print('GROOT OK')"
```

---

### 6. Wall-X

Wall-X 基于 Qwen2.5-VL，结合 ODE (常微分方程) 进行动作生成。

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[wallx]" --no-build-isolation
```

这会安装:
- `transformers == 5.3.0`
- `peft >= 0.18.0`
- `scipy >= 1.14.0`
- `torchdiffeq >= 0.2.4`
- `qwen-vl-utils >= 0.0.11`

```bash
# 验证
python -c "from lerobot.policies.wall_x import WallXPolicy; print('Wall-X OK')"
```

---

### 7. Multi-Task DiT

Multi-Task Diffusion Transformer，结合 Transformer + 扩散模型做多任务学习。

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[multi_task_dit]" --no-build-isolation
```

这会安装:
- `transformers == 5.3.0`
- `diffusers >= 0.27.2`

```bash
# 验证
python -c "from lerobot.policies.multi_task_dit import MultiTaskDiTPolicy; print('Multi-Task DiT OK')"
```

---

### 8. SARM

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[sarm]" --no-build-isolation
```

这会安装:
- `transformers == 5.3.0`
- `pydantic >= 2.0.0`
- `faker >= 33.0.0`
- `matplotlib >= 3.10.3`
- `qwen-vl-utils >= 0.0.11`

```bash
# 验证
python -c "from lerobot.policies.sarm import SARMPolicy; print('SARM OK')"
```

---

### 9. XVLA

```bash
cd box2robot_gpu_worker/lerobot
pip install -e ".[xvla]" --no-build-isolation
```

这会安装:
- `transformers == 5.3.0`

```bash
# 验证
python -c "from lerobot.policies.xvla import XVLAPolicy; print('XVLA OK')"
```

---

### 10. VQ-BeT / TDMPC

这两个策略不需要额外依赖，基础安装已包含。

```bash
# 验证
python -c "from lerobot.policies.vqbet import VQBeTPolicy; print('VQ-BeT OK')"
python -c "from lerobot.policies.tdmpc import TDMPCPolicy; print('TDMPC OK')"
```

---

### 一键安装多个策略

```bash
cd box2robot_gpu_worker/lerobot

# 安装所有常用策略 (Diffusion + SmolVLA + Pi0)
pip install -e ".[training,diffusion,smolvla,pi]" --no-build-isolation

# 安装全部 (不含 GROOT，因为 flash-attn 问题)
pip install -e ".[training,diffusion,smolvla,pi,wallx,multi_task_dit,sarm,xvla]" --no-build-isolation
```

---

## 训练命令速查

所有训练都通过 `lerobot-train` 或 `python -m lerobot.scripts.lerobot_train` 启动。

### 通用参数

```bash
python -m lerobot.scripts.lerobot_train \
  --policy.type=<policy_type> \                   # act, diffusion, smolvla, pi0, pi05 ...
  --dataset.repo_id=<hf_dataset_id> \             # HuggingFace 数据集 ID
  --dataset.local_files_only=true \               # 仅用本地数据集 (不从 Hub 下载)
  --training.num_steps=<steps> \                  # 训练步数
  --training.batch_size=<bs> \                    # 批大小
  --training.lr=<lr> \                            # 学习率
  --output_dir=outputs/<name>                     # 输出目录
```

### 各策略推荐配置

**ACT (纯关节, 无图像):**
```bash
lerobot-train --policy.type=act \
  --dataset.repo_id=box2robot/my_dataset \
  --training.num_steps=10000 --training.batch_size=64 \
  --policy.chunk_size=20 --policy.n_obs_steps=2
```

**Diffusion (纯关节, 无图像):**
```bash
lerobot-train --policy.type=diffusion \
  --dataset.repo_id=box2robot/my_dataset \
  --training.num_steps=10000 --training.batch_size=64 \
  --policy.horizon=16 --policy.n_action_steps=8
```

**SmolVLA (需要图像):**
```bash
lerobot-train --policy.type=smolvla \
  --policy.pretrained_path=lerobot/smolvla_base \
  --dataset.repo_id=box2robot/my_dataset \
  --training.num_steps=30000 --training.batch_size=16 \
  --policy.chunk_size=50
```

**Pi0 (需要图像):**
```bash
lerobot-train --policy.type=pi0 \
  --policy.pretrained_path=lerobot/pi0_base \
  --dataset.repo_id=box2robot/my_dataset \
  --training.num_steps=20000 --training.batch_size=8 \
  --policy.chunk_size=50
```

---

## 显存估算

| 策略 | batch_size=4 | batch_size=8 | batch_size=16 | batch_size=32 | batch_size=64 |
|------|-------------|-------------|---------------|---------------|---------------|
| ACT | ~3GB | ~4GB | ~6GB | ~10GB | ~16GB |
| Diffusion | ~4GB | ~6GB | ~10GB | ~16GB | ~24GB |
| VQ-BeT | ~4GB | ~6GB | ~10GB | ~16GB | ~24GB |
| SmolVLA (expert-only) | ~8GB | ~12GB | ~18GB | OOM | OOM |
| SmolVLA (full) | ~14GB | ~20GB | OOM | OOM | OOM |
| Pi0 (expert-only) | ~10GB | ~14GB | ~22GB | OOM | OOM |
| Pi0 (full) | ~22GB | ~35GB | OOM | OOM | OOM |
| GROOT | ~20GB+ | ~30GB+ | OOM | OOM | OOM |

> OOM = 超出 24GB 显存。使用 gradient_checkpointing 可额外省 ~30-40%。
> bfloat16 混合精度约减半显存占用 (需 Ampere+ GPU)。

### GPU 推荐

| GPU | 显存 | 推荐策略 |
|-----|------|---------|
| RTX 3060 | 12GB | ACT, Diffusion, VQ-BeT |
| RTX 3090 / 4090 | 24GB | 以上 + SmolVLA (expert-only), Pi0 (expert-only) |
| A100 40GB | 40GB | 以上 + Pi0 (full), GROOT |
| A100 80GB / H100 | 80GB | 全部策略, 大 batch |

---

## 常见问题

### Q: transformers 版本冲突

LeRobot v0.5.2 锁定 `transformers==5.3.0`。如果你的环境中有其他版本:

```bash
pip install transformers==5.3.0
```

### Q: `ImportError: No module named 'diffusers'`

```bash
cd lerobot && pip install -e ".[diffusion]" --no-build-isolation
```

### Q: SmolVLA/Pi0 下载预训练权重很慢

设置 HuggingFace 镜像 (中国大陆):

```bash
# Linux/macOS
export HF_ENDPOINT=https://hf-mirror.com

# Windows (PowerShell)
$env:HF_ENDPOINT = "https://hf-mirror.com"

# 或使用 hf-mirror 工具
pip install hf-transfer
export HF_HUB_ENABLE_HF_TRANSFER=1
```

### Q: bfloat16 不支持

如果你的 GPU 不支持 bfloat16 (GTX 10xx/20xx):

```bash
# 改用 float16
--policy.dtype=float16
```

### Q: Windows 上 flash-attn 安装失败

flash-attn 官方不支持 Windows。影响的策略: GROOT。

替代方案:
1. 使用 WSL2 Ubuntu 环境
2. 改用 SmolVLA 或 Pi0 (不需要 flash-attn)

### Q: 如何用本地数据集训练

Box2Robot 通过 `convert.py` 将轨迹 JSON 转为 LeRobot v3 数据集格式:

```bash
# 1. 转换
b2r-convert --input cache/trajectories/ --output datasets/my_dataset

# 2. 训练 (指定本地路径)
lerobot-train --policy.type=act \
  --dataset.repo_id=datasets/my_dataset \
  --dataset.local_files_only=true \
  ...
```

### Q: 如何选择策略

```
纯关节 (无摄像头):
  简单任务 / 快速验证 → ACT (默认, 最稳定)
  需要更强泛化 → Diffusion Policy

有摄像头图像:
  消费级 GPU (12-24GB) → SmolVLA (推荐, 参数少效果好)
  高端 GPU (24-80GB) → Pi0 / Pi0.5
  多任务泛化 → GROOT / Wall-X (需要大量数据)
```
