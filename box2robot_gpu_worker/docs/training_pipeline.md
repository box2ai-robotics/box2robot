# Box2Robot GPU Worker 训练 / 推理 Pipeline 完全手册

> **核心目标**: 让 ACT / Diffusion Policy / SmolVLA / Pi0 / Pi0.5 / GR00T 的训练和推理在 multi-slot 并发场景下都稳定运行, 失败时精确诊断 + 给出修复指令.
>
> **适用版本**: gpu_worker v0.6.3+

---

## 0. 目录

1. [架构与并发模型](#1-架构与并发模型)
2. [模型对比矩阵](#2-模型对比矩阵)
3. [训练 Pipeline 详解](#3-训练-pipeline-详解)
4. [推理 Pipeline 详解](#4-推理-pipeline-详解)
5. [故障排查手册](#5-故障排查手册)
6. [显存预算与并发决策](#6-显存预算与并发决策)
7. [Multi-slot 并发安全](#7-multi-slot-并发安全)
8. [调试技巧](#8-调试技巧)
9. [常用命令速查](#9-常用命令速查)

---

## 1. 架构与并发模型

### 1.1 端到端流程

```
[APP 提交训练] → [Server SQLite atomic claim] → [GPU Worker poll-job]
                                                         ↓
                              ┌──────────────────────────┴─────┐
                              │                                │
                    Slot 1: 训练 ACT                  Slot 2: 推理 Pi05
                    (subprocess lerobot-train)        (主线程 model.select_action)
                              │                                │
                              └──→ 进度上报 ←─ 心跳保活 ─→ Server SQLite
                                                                ↓
                                           [APP 训练监控页 / 推理控制]
```

### 1.2 关键组件

| 组件 | 进程 / 线程 | 状态 |
|------|-----------|------|
| `b2r-gpu` 主进程 | 1 | 主循环 + 心跳 + slot 管理 |
| `_main_loop` | 主线程 | poll-job + 收割 + heartbeat |
| `_slot_runner` | daemon thread × `max_concurrent` | 每 thread 跑一个 `_process_job` |
| `lerobot-train` | subprocess × N | 训练进程, 各自独占 GPU 部分 |
| 推理 model | thread 内 | 加载 PyTorch model, 独立 GPU 实例 |

### 1.3 状态机

```
pending ─┬─ atomic claim ─→ downloading ─→ training ─┬─ completed
         │                                          ├─ failed
         │                                          ├─ cancelled (用户主动)
         │                                          ├─ paused (用户主动) → pending
         │                                          └─ interrupted (worker 离线) → training (心跳恢复)
         │                                                                       └─ pending (用户从 ckpt 恢复)
         └─ vram defer (worker 主动) ──→ pending (重新轮询)
```

---

## 2. 模型对比矩阵

### 2.1 总览

| 维度 | ACT | Diffusion | SmolVLA | Pi0 | Pi0.5 | GR00T |
|------|-----|-----------|---------|-----|-------|-------|
| **类型** | Transformer + VAE | DDPM Policy | VLM + Action Expert | PaliGemma + Expert | Pi0 强化版 | Eagle2 + Diffusion |
| **训练方式** | from-scratch | from-scratch | fine-tune base | fine-tune base | fine-tune base | fine-tune base |
| **参数量** | ~80M | ~150M | ~450M | ~3B | ~3B | ~3B |
| **支持 LoRA** | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ (lora_rank) |
| **需要图像** | 可选 | 可选 | ✅ | ✅ | ✅ | ✅ |
| **需要语言指令** | ❌ | ❌ | ✅ tokens | ✅ tokens | ✅ tokens | ✅ tokens |
| **base 模型** | - | - | `lerobot/smolvla_base` ~2GB | `lerobot/pi0_base` ~6GB | `lerobot/pi05_base` ~14GB | `nvidia/GR00T-N1.5-3B` ~6GB |
| **推荐显存** | 6GB | 10GB | 12GB | 16GB | 24GB | 24GB |
| **训练时长 (10k步)** | 5 min (4090) | 1 hr (4090) | 3 hr (4090, expert_only) | 4 hr (4090, expert_only) | 6 hr (4090, expert_only) | 5 hr (4090, lora_rank=16) |

### 2.2 数据格式差异

| 字段 | ACT | Diffusion | SmolVLA | Pi0 | Pi0.5 | GR00T |
|------|-----|-----------|---------|-----|-------|-------|
| `observation.state` 维度 | n_servos (6) | n_servos | n_servos→pad 到 32 (内部) | n_servos→pad 32 | n_servos→pad 32 | n_servos→pad 32 |
| `observation.images.*` key | `observation.images.top` | `observation.images.top` | base 期望 (top → 自动 rename) | base 期望 (top → cam_high) | base 期望 (top → exterior_1_left 或 base_0_rgb) | base 期望 |
| `action` 输出维度 | n_servos | n_servos | padded 32 → truncate n_servos | padded 32 → truncate | padded 32 → truncate | padded 32 → truncate |
| `task` 字段 | (有但模型不用) | (有但模型不用) | tokenize 进 prompt | tokenize 进 prompt | tokenize + state 离散化 | tokenize 进 prompt |
| Normalizer (state/action) | MEAN_STD | MEAN_STD | MEAN_STD | MEAN_STD | **QUANTILES** (q01/q99) | MIN_MAX |
| 视觉 normalize | ImageNet stats (mean/std) | ImageNet stats | IDENTITY (内部 SigLIP 处理) | IDENTITY | IDENTITY | IDENTITY |

### 2.3 优化器 / 调度器字段（lerobot CLI 命名空间）

| 前端字段 | ACT | Diffusion | SmolVLA | Pi0 | Pi0.5 | GR00T |
|----------|-----|-----------|---------|-----|-------|-------|
| `lr` (→ optimizer_lr) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `weight_decay` (→ optimizer_weight_decay) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `grad_clip_norm` (→ optimizer_grad_clip_norm) | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ |
| `scheduler_warmup_steps` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ (用 `warmup_ratio`) |
| `dtype` | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| `gradient_checkpointing` | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| `compile_model` | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ |
| `peft_enable` (LoRA) | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ (lora_rank) |

> Worker 在 `_add_policy_param` 用白名单 + 别名映射, 不在该模型 config 字段表里的会**静默跳过 + WARNING**, 不会让 `lerobot-train` 报 unrecognized arguments.

---

## 3. 训练 Pipeline 详解

### 3.1 主流程 (`worker.py`)

```
┌─ TrainingWorker.process_job(job_id) ─────────────────────────────┐
│ 1. _report_status("downloading")                                  │
│ 2. _get_job_info(job_id)  → model_type/batch_size/custom_params   │
│ 3. ds_fingerprint = md5(sorted(dataset_ids))[:12]                 │
│ 4. 数据下载 (with _get_file_lock("download_<fp>")):                │
│    - cache 命中 → 跳过                                            │
│    - cache 缺失 → _download_dataset(job_id) + _download_images    │
│ 5. _train_lerobot(...)  ─── 见 3.2                                │
│ 6. _report_status("completed", model_path)                        │
└────────────────────────────────────────────────────────────────────┘
```

### 3.2 `_train_lerobot` 核心步骤

```
┌─ 1. 路径计算 ───────────────────────────────────────────┐
│   ds_cache_dir = cache/ds_<fp>/         (共享, 节流量)  │
│   datasets_root = datasets/<job_id>-<fp[:8]>/  (per-job!) │
└──────────────────────────────────────────────────────────┘
            ↓
┌─ 2. with _get_file_lock("convert_<repo>"):              ┐
│   if dataset_marker.exists():                           │
│      cache hit, skip                                    │
│   else:                                                 │
│      convert(input=ds_dir, root=datasets_root)          │
│      → LeRobot v3 parquet + meta/info.json + stats.json │
│   if model_type == "pi05":                              │
│      _ensure_quantile_stats(...)  # 补 q01/q99           │
└─────────────────────────────────────────────────────────┘
            ↓
┌─ 3. 构造 lerobot-train 命令 ─────────────────────────────┐
│   cmd = [python, lerobot_train.py,                      │
│          --dataset.repo_id=<repo>,                      │
│          --dataset.root=<datasets_root>,                │
│          --steps=N --batch_size=N --output_dir=...]     │
│                                                          │
│   if is_vla:                                             │
│     cmd += [--policy.path=lerobot/<base>,               │
│             --rename_map={"top":<base第一个cam>}]        │
│     if peft_enable:                                      │
│       cmd += [--peft.method_type=LORA --peft.r=N]       │
│   else:                                                  │
│     cmd += [--policy.type=<model_type>]                 │
│                                                          │
│   for k, v in custom_params:                            │
│     _add_policy_param(cmd, model_type, k, v)            │
│     # 白名单 + 别名映射, 跳过不支持字段                   │
└──────────────────────────────────────────────────────────┘
            ↓
┌─ 4. subprocess.Popen(cmd, env=train_env) ───────────────┐
│   train_env 含 HF_HOME=base_model/ (训练/推理共享 ckpt)  │
│   for line in proc.stdout:                              │
│      解析 step:N loss:M → progress_cb → server          │
│      tail_lines.append(line)  # 保留最后 80 行          │
│      if _should_stop: proc.terminate()                  │
│   proc.wait()                                            │
│   if returncode != 0:                                    │
│      err = _diagnose_subprocess_error(returncode, tail) │
│      raise RuntimeError(err)  ─── 见 §5                  │
└──────────────────────────────────────────────────────────┘
```

### 3.3 各模型训练特殊处理

#### ACT
- `--policy.type=act` from-scratch
- `chunk_size > 1`: 自动设 `n_action_steps=1 + temporal_ensemble_coeff=0.01` (论文推荐)
- `kl_weight / use_vae / dim_model / n_heads / latent_dim` 透传

#### Diffusion
- `--policy.type=diffusion` from-scratch
- 用 `horizon` 不是 `chunk_size` (worker 自动转换)
- `num_train_timesteps / beta_schedule / prediction_type` 透传

#### SmolVLA
- `--policy.path=lerobot/smolvla_base` fine-tune
- 默认开 `freeze_vision_encoder=true + train_expert_only=true + train_state_proj=true` (官方推荐)
- `expert_width_multiplier / num_vlm_layers` 透传
- LoRA 推荐 `r=64 + lr=1e-3` (官方文档示例)

#### Pi0
- `--policy.path=lerobot/pi0_base` fine-tune
- 默认 `dtype=bfloat16 + gradient_checkpointing=true`
- aloha 数据训练, base 期望 `cam_high / cam_left_wrist / cam_right_wrist`
- `--rename_map={"observation.images.top": "observation.images.cam_high"}` (worker 自动)
- LoRA 推荐 `r=16 + lr=2.5e-4`

#### Pi0.5
- 跟 Pi0 类似, 但 base 是 droid 数据训的
- base 期望 `exterior_1_left / exterior_2_left / wrist_left` (不同 ckpt 可能是 `base_0_rgb` 等)
- **关键差异**: Normalization=`QUANTILES`, worker 自动调 `_ensure_quantile_stats` 给 dataset 补 q01/q99
- Tokenizer 长度 200 (Pi0 是 48), 支持 verbal instructions
- `tokenizer_max_length / empty_cameras / use_relative_actions` 高级字段

#### GR00T N1.5
- `--policy.path=nvidia/GR00T-N1.5-3B` fine-tune
- `tune_visual=false + tune_llm=false + tune_projector=true + tune_diffusion_model=true` 默认 (省显存)
- 用 `lora_rank=16` 直接走 GR00T 内置 LoRA (不是 lerobot peft pipeline)
- `warmup_ratio` 替代 `scheduler_warmup_steps`

---

## 4. 推理 Pipeline 详解

### 4.1 主流程 (`run_inference_server`)

```
┌─ 加载 model ──────────────────────────────────────────────┐
│ if adapter_config.json exists (LoRA):                     │
│   peft_cfg = PeftConfig.from_pretrained(ckpt)            │
│   base_path = peft_cfg.base_model_name_or_path           │
│   local_base = _resolve_hf_cache_path(base_path)         │
│   model = policy_cls.from_pretrained(local_base or base) │
│   model = PeftModel.from_pretrained(model, ckpt)         │
│   model = model.merge_and_unload()                        │
│ else:  # 全量训练 ckpt                                    │
│   model = policy_cls.from_pretrained(ckpt)               │
└────────────────────────────────────────────────────────────┘
            ↓
┌─ 加载 preprocessor (VLA only) ────────────────────────────┐
│ if is_vla:                                                │
│   _vla_pre, _vla_post = make_pre_post_processors(         │
│       policy_cfg=model.config,                            │
│       pretrained_path=ckpt_path)                          │
│   # 加载 6 步: Rename + Batch + Relative + Normalize +    │
│   #            StateTokenize + TextTokenize + Device      │
└────────────────────────────────────────────────────────────┘
            ↓
┌─ 推理循环 ────────────────────────────────────────────────┐
│ while not _should_stop():                                 │
│   state = _read_state(arm_device)                        │
│   img = _read_camera(cam_device)                         │
│   if is_vla:                                              │
│     raw = {"observation.state": state,                   │
│            "task": task_description,                     │
│            "observation.images.top": img}                │
│     obs = _vla_pre(raw)  # rename + tokenize + normalize │
│   else:                                                   │
│     obs = manual normalize + obs[OBS_STATE/cam] = ...    │
│   action = model.select_action(obs)                       │
│   action = action[..., :n_servos]  # truncate padded      │
│   action = _vla_post(action) if is_vla else manual unnorm│
│   action = _ema_smooth(action)  # 跨帧平滑                │
│   client.post(/api/device/<arm>/command, positions)       │
└────────────────────────────────────────────────────────────┘
```

### 4.2 各模型推理差异

| 模型 | obs 构造 | model 调用 | action 处理 |
|------|---------|-----------|-------------|
| ACT | manual normalize state + ImageNet 视觉 | `model.select_action(obs)` 走内部 queue | manual unnormalize + clamp |
| Diffusion | manual + 多 cam stack 到 `OBS_IMAGES` key | `model.predict_action_chunk(batch)` 后切 chunk | manual unnormalize 整 chunk |
| SmolVLA | `_vla_pre(raw)` 自动处理 | `model.select_action(obs)` | truncate 32→6 + `_vla_post` |
| Pi0 | 同 SmolVLA | 同 SmolVLA | 同 SmolVLA |
| Pi0.5 | 同 SmolVLA, **state 离散化进 prompt token** (Pi05 特有) | 同 SmolVLA | 同 SmolVLA |
| GR00T | `_vla_pre(raw)` 走 groot 专属 processor | `model.select_action` | `_vla_post` |

### 4.3 推理执行模式

| 模式 | 推理频率 | 执行频率 | 适用 |
|------|---------|---------|------|
| `original` | 每步 | 每步 (5Hz) | LeRobot 标准, 稳但慢 |
| `fixed` | 1Hz | 整 chunk 批量发 (20Hz) | 大 chunk_size, 一次推理多步执行 |
| `adaptive` | 1-5Hz | 自适应分块 (20Hz) | FAST-ACT 论文跳帧, 平衡速度精度 |
| `overlap` | 2Hz | 半 chunk 重叠 + EMA (20Hz) | 平滑过渡, 适合精细任务 |

---

## 5. 故障排查手册

### 5.1 启动阶段

#### `BLOCKED` `'av' is required but not installed`
- **原因**: lerobot.datasets/__init__.py 顶部 `require_package("av")` 抛错
- **修复**: `pip install av` 或 `pip install "lerobot[dataset] @ file:./lerobot"`
- **预案**: preflight `_check_dependencies` 启动时拦截

#### `BLOCKED` `No module named 'lerobot.datasets'`
- **原因**: 缺 `datasets` 库 (HF datasets, 不是数据集), lerobot.datasets/__init__.py 顶部 `require_package("datasets")` 抛错
- **修复**: `pip install datasets`
- **预案**: preflight 已升级 `datasets` 为 BLOCKED 级

#### `BLOCKED` `lerobot 未安装且本地子目录不存在`
- **原因**: lerobot 既没装到 site-packages, 子目录 `box2robot_gpu_worker/lerobot/` 也缺
- **修复**: `cd lerobot && pip install -e . --no-build-isolation` 或 `git submodule update --init`

#### `WARNING` `peft 未安装` (LoRA 训练时变 BLOCKED)
- **原因**: 准备开 LoRA 微调但没装 peft 库
- **修复**: `pip install peft accelerate`

#### `BLOCKED` `磁盘剩余空间 < 5GB`
- **原因**: HF base ckpt + dataset cache + outputs 总占用大
- **修复**: 清 `outputs/<旧 job>/` 或换 `--output` 目录到大盘

### 5.2 训练 subprocess 报错

#### `unrecognized arguments: --n_action_steps=50 --lr=...`
- **原因**: 早期版本 worker 没加 `--policy.` 前缀 (已修复)
- **现状**: worker.py 用 `_add_policy_param` 自动加前缀
- **如果再次出现**: 检查 worker.py 是不是回退到旧版本

#### `unrecognized arguments: --policy.grad_clip_norm`
- **原因**: ACT/Diffusion/GR00T 没有 `optimizer_grad_clip_norm` 字段, 但前端传过来了
- **现状**: `POLICY_FIELDS` 白名单过滤, 静默跳过 + WARNING
- **如果再次出现**: 该模型 config 字段表过期, 更新 worker `POLICY_FIELDS`

#### `'datasets' is required but not installed` (训练崩 exit 1)
- 同启动阶段, subprocess 内 import lerobot.datasets 触发
- worker `_diagnose_subprocess_error` 已识别并给修复指令

#### `quantile stats not found`
- **原因**: pi05 默认 QUANTILES normalization, 但 dataset 只有 mean/std stats
- **现状**: worker 训练 pi05 前自动调 `_ensure_quantile_stats` 补 q01/q99
- **如果还出现**: 检查 worker 日志有没有 `[PI05] Computing quantile stats` 行, 没有说明 augment 失败. 手动:
  ```bash
  python lerobot/src/lerobot/scripts/augment_dataset_quantile_stats.py \
      --repo-id=box2robot-<job_id>-<fp> \
      --root=datasets/box2robot-<job_id>-<fp>
  ```

#### `All image features are missing from the batch`
- **原因**: dataset 的 `observation.images.top` 跟 base config 期望 cam (cam_high/exterior_1_left/...) 不匹配
- **现状**: worker 自动加 `--rename_map={"top": "<base 第一个 cam>"}`
- **如果还出现**: 看日志有没有 `VLA rename_map: ... -> <base cam>` 行. 没有说明 `_get_base_visual_keys` 失败 (HF 下载 config 失败). 检查 HF 网络.

#### `tensor a (32) must match the size of tensor b (6)`
- **原因**: VLA model 输出 padded 到 max_action_dim=32, dataset stats 是 6 维, broadcast 失败
- **现状**: worker 推理 `_unnorm_action` 先 `at[..., :n_servos]` truncate
- **如果还出现**: 推理 worker.py 没拉新版, 重新 scp + 重启

#### `Could not load state dict ... model.safetensors not found`
- **原因**: ckpt 是 LoRA 格式 (含 `adapter_model.safetensors` 但无 `model.safetensors`)
- **现状**: worker 推理自动检测 `adapter_config.json` 存在 → 走 PEFT 加载
- **如果还出现**: 看日志有没有 `LoRA adapter detected ... loading via PEFT path`. 没有说明 worker 没拉新版.

#### `401 Unauthorized` (HF base 下载)
- **原因**: HuggingFace xet-server 鉴权失败 (可能是网络/token/cas-server 问题)
- **现状**: worker 启动设 `HF_HOME=box2robot_gpu_worker/base_model/`, 优先从本地 cache 加载
- **修复**:
  - 国内网络: `export HF_ENDPOINT=https://hf-mirror.com`
  - 私有模型: `huggingface-cli login`
  - 离线: 提前 `huggingface-cli download lerobot/pi05_base`

### 5.3 进程信号

#### exit code -9 / 137 (SIGKILL)
- **场景**: GPU OOM-killer 杀进程, 或 OS OOM
- **诊断**: tail_lines 含 `out of memory` / `cuda out of memory`
- **修复** (按优先级):
  1. 减 `batch_size` (通常翻半)
  2. 开 `gradient_checkpointing=true` (VLA 默认开)
  3. VLA 开 `train_expert_only=true` (省 30% 显存)
  4. 开 `peft_enable=true` (LoRA, 省 50% 显存)
  5. APP GPU 配置页调大 `max_concurrent` 反而别开 (会有更多 slot 争抢显存)
  6. 换更大显存 GPU

#### exit code -11 / 139 (SIGSEGV)
- **场景**: CUDA 驱动 / torch 版本不兼容; 或 numpy/transformers 版本问题
- **诊断**: 通常没有清晰 stdout 错误
- **修复**:
  - 跑 `python scripts/check_gpu.py` 验证 CUDA + torch 版本
  - `nvidia-smi` 看驱动版本是否支持 torch 编译的 CUDA
  - `pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124 --force-reinstall`

#### exit code -15 / 143 (SIGTERM)
- **场景**: worker 主动 terminate (用户取消 / pause)
- **正常**: worker 上报 status=cancelled/paused

### 5.4 状态机异常

#### Job 卡 `pending` 不被领取
1. 检查 GPU device 是否在线: server 日志 `[GPU] GPU-XXX heartbeat`
2. 检查 model_type 是否在 `supported_models`: APP GPU 配置页查看
3. 检查 job.gpu_device_id 是否预绑定其它设备 (不是当前 GPU)
4. 检查显存是否够: worker 日志 `[VRAM] BUDGET 拒绝 job ...`
5. 检查 max_concurrent: server 日志 `[SLOT] max_concurrent: 1 → ?`

#### Job 卡 `interrupted` 长时间不恢复
- **原因**: Worker 进程真死 (网络抖动早就该恢复)
- **诊断**: server `_check_stale_training_jobs` 把 GPU 离线 180s 的 training/downloading 标 interrupted
- **修复**: APP train-list 点 "从 Checkpoint 恢复" → status 推回 pending → worker 重新领取

#### Job `deploying` 卡死
- **原因**: 推理 worker 进程死了, 但 server 不知道 (旧版本)
- **现状**: server `_check_stale_training_jobs` 覆盖 deploying, 90s 离线后强制 → completed + 释放机械臂

### 5.5 Multi-slot 特有问题

#### `vram defer` 大量出现
```
[VRAM] BUDGET 拒绝 job xxx (model=pi05 batch=8)
    超出 max_vram_gb 预算 — 模型估算 14.0GB + 冗余 2.0GB,
    用户预算 24.0GB 已承诺给 1 个 slot (14.0GB), 剩 10.0GB.
```
- **原因**: 用户 max_concurrent 太大或 max_vram_gb 太小
- **修复**: APP GPU 配置页调小 max_concurrent (跑大模型时), 或调大 max_vram_gb

#### 同一 dataset 多 job 重复转换
- **现状**: lock + per-job datasets 目录, 转换是 per-job 独立的
- **如果觉得磁盘浪费**: 训练完手动 `rm -rf datasets/box2robot-<旧 job_id>-*`

---

## 6. 显存预算与并发决策

### 6.1 显存估算公式

```python
training_vram = base_vram + per_batch_vram * batch_size
                * (0.5 if peft_enable else 1.0)
                * (0.7 if pi0_train_expert_only else 1.0)
                * (0.7 if gradient_checkpointing else 1.0)
```

### 6.2 各模型 base + per_batch (worker 内置常量)

| 模型 | base_vram (GB) | per_batch (GB) | 推理 base (GB) |
|------|----------------|----------------|------------------|
| ACT | 2.0 | 0.05 | 2.0 |
| Diffusion | 3.0 | 0.05 | 3.0 |
| SmolVLA | 6.0 | 0.10 | 4.0 |
| Pi0 | 14.0 | 0.50 | 8.0 |
| Pi0.5 | 14.0 | 0.50 | 8.0 |
| GR00T | 10.0 | 0.20 | 6.0 |

### 6.3 决策双层

```python
ok = need_gb + 2GB(reserve) <= min(physical_free, max_vram_gb - committed_to_slots)
```

- **physical_free**: `torch.cuda.mem_get_info()` (其它进程占用也算)
- **max_vram_gb**: 用户 APP 设的预算 (默认 90% × 物理 vram)
- **committed**: 已分配给现有 slot 的 estimated_vram 总和

不够时 release 回 pending + error_msg 写明 blocker (`physical` 或 `budget`).

---

## 7. Multi-slot 并发安全

### 7.1 Race 保护点

| 资源 | 保护方式 |
|------|---------|
| `cache/ds_<fp>/` 数据下载 | `_get_file_lock("download_<fp>")`, 同 fp 串行 |
| `datasets/box2robot-<job_id>-*/` LeRobot 转换 | per-job 独立目录, 加 lock 防 lerobot 内部全局状态 |
| `meta/stats.json` quantile augment | per-job 独立, 跟 convert 同一 lock 内执行 |
| `_slots dict` | `_slots_lock` |
| `outputs/<job_id>/` ckpt | 唯一目录无冲突 |
| HF cache (`base_model/hub/`) | huggingface_hub 内部 fcntl lock |
| Server SQLite | WAL mode 多 connection 安全 |

### 7.2 状态一致性

| 路径 | 原子性保证 |
|------|----------|
| poll-job 抢任务 | `UPDATE WHERE status='pending'` rowcount==1 |
| 心跳 active_job_ids list | server 逐个 `_refresh_job_liveness` |
| 状态机迁移 | `_WORKER_ALLOWED_TRANSITIONS` 验证 |

---

## 8. 调试技巧

### 8.1 必装诊断工具

```bash
python scripts/check_gpu.py            # 完整体检 (依赖 + GPU + 网络)
python scripts/check_gpu.py --strict   # CI 模式, BLOCKED 退出码 1
nvidia-smi                              # 显存 + GPU 利用率
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv  # 谁占了显存
```

### 8.2 启用详细日志

```bash
# Worker 端
export PYTHONUNBUFFERED=1
b2r-gpu --server https://robot.box2ai.com 2>&1 | tee worker.log

# Subprocess 端 (lerobot-train) 的 stdout 已经被 worker 捕获并打印 [lerobot] 前缀
```

### 8.3 复现训练命令 (跳过 server, 直接调试)

worker 启动 subprocess 前会打印完整 cmd:

```
[CMD] 完整训练命令 (复制即可手动复现):
[CMD]   [0] /root/miniconda3/envs/b2r/bin/python3.12
[CMD]   [1] /root/.../lerobot/src/lerobot/scripts/lerobot_train.py
[CMD]   [2] --dataset.repo_id=box2robot-abc12345-ef53f6d2
[CMD]   [3] --dataset.root=/root/.../datasets/box2robot-abc12345-ef53f6d2
[CMD]   [4] --steps=10000
[CMD]   [5] --batch_size=8
...
```

直接复制粘贴到终端即可复现训练 (不通过 worker), 排查纯 lerobot 问题.

### 8.4 推理时 5 个检查点 (CHECK-1 ~ CHECK-5)

VLA 推理初始化会输出 4 个 checkpoint, 每次推理首帧再加 1 个:

| Check | 内容 | 用途 |
|-------|------|------|
| CHECK-1 | `model.config.input_features` / `output_features` / `max_state_dim` | 确认模型期望的 cam keys + state dim |
| CHECK-2 | preprocessor pipeline 步骤 + normalizer stats shape | 确认 stats 维度跟 dataset 一致 |
| CHECK-3 | raw obs (preprocessor 输入) shape | 确认 worker 喂的数据格式对 |
| CHECK-4 | processed batch (model 输入) | 确认 preprocessor 输出符合 model 期望 |
| CHECK-5 | action truncate 维度 | 确认 padded action 被切回 n_servos |

任一 check 跟预期不一致就是 bug, 把 dump 给 model 维护者排查.

### 8.5 dataset 验证

```bash
# 列出某 dataset 实际帧数 + features
python -c "
from lerobot.datasets import LeRobotDataset
ds = LeRobotDataset(repo_id='box2robot-abc-xxx', root='datasets/box2robot-abc-xxx')
print(ds.num_episodes, ds.meta.features.keys())
print(ds.meta.stats.get('observation.state'))
"
```

### 8.6 OOM 排查顺序

1. `nvidia-smi` 看是不是其它进程占了显存
2. worker 日志找 `[VRAM] BUDGET/PHYSICAL 拒绝` 看是预算还是物理满
3. subprocess 日志找 `out of memory` (具体 layer)
4. 减 `batch_size` (最有效)
5. 开 `gradient_checkpointing`
6. VLA 开 `train_expert_only`
7. 开 LoRA `peft_enable`
8. 调小 `chunk_size` / `n_action_steps`
9. 关 `compile_model` (它本身占额外显存)

---

## 9. 常用命令速查

### 9.1 worker 维护

```bash
# 启动
conda activate b2r
b2r-gpu --server https://robot.box2ai.com

# 自定义 HF cache 路径
b2r-gpu --hf-cache /root/autodl-tmp/.cache --server ...

# 多实例 (注意 device_id 会一样, 暂未支持 --instance-id)
# 推荐用 multi-slot (max_concurrent) 而不是多进程
```

### 9.2 清理磁盘

```bash
# 清单个 job 输出 (训完 + 下载完模型后可清)
rm -rf box2robot_gpu_worker/outputs/<job_id>/
rm -rf box2robot_gpu_worker/datasets/box2robot-<job_id>-*

# 清所有数据集 cache (谨慎, 下次训要重新下)
rm -rf box2robot_gpu_worker/cache/ds_*

# 清 HF base 模型 (需要重下 14GB+)
rm -rf box2robot_gpu_worker/base_model/hub/models--lerobot--pi05_base
```

### 9.3 手动 augment quantile stats (pi05 训前必备)

```bash
python box2robot_gpu_worker/lerobot/src/lerobot/scripts/augment_dataset_quantile_stats.py \
    --repo-id=box2robot-<job_id>-<fp[:8]> \
    --root=box2robot_gpu_worker/datasets/box2robot-<job_id>-<fp[:8]>
```

### 9.4 手动跑 lerobot-eval (验证训完的 ckpt)

```bash
python box2robot_gpu_worker/lerobot/src/lerobot/scripts/lerobot_eval.py \
    --policy.path=outputs/<job_id>/model/checkpoints/last/pretrained_model \
    --env.type=aloha \
    --eval.n_episodes=10
```

(目前 box2robot 不直接用这个, 推理走 worker `run_inference_server`)

---

## 10. 已知限制 / 待优化

| 项 | 现状 | 计划 |
|---|------|------|
| 同台机器多实例 worker | 不支持 (device_id hash 一致冲突) | `--instance-id` 后缀 |
| 训推共享 base 模型显存 | 不行 (训 subprocess + 推 thread 各加载一份 base, 各占 14GB) | model 共享需要重写 lerobot-train, 工程量大 |
| 实际 VRAM peak 反馈给估算表 | 静态表, 可能差 20% | 加数据收集点, 训完上报 actual peak |
| max_concurrent 用户手动设 | 是 | 按 max_vram_gb / 平均 base 自动建议 |
| HF gated 模型自动 token | 需 `huggingface-cli login` 一次 | preflight 加 token 检测 |
| GR00T LoRA via lora_rank | 走 GR00T 内置 (跟 lerobot peft 不一致) | 统一为 lerobot peft 接口 |

---

**最后更新**: 2026-05-08, gpu_worker v0.6.3
**维护**: 在调试中遇到新报错请追加到 §5 故障排查手册.
