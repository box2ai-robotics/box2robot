"""LingBot-VLA 4B 训练适配器 — 把 Box2Robot worker 参数翻译成 lingbot-vla 原生 yaml + subprocess 调用.

跟 worker._train_lerobot 平级的训练入口, 由 worker.process_job 在 model_type == "lingbot_vla" 时调.

关键差异 (跟 lerobot pipeline 比):
  - 走独立 b2r-vla conda env (lerobot v0.4.2, 跟 b2r env v0.5.2 隔离)
  - 训练入口是 lingbot-vla 自己的 `bash train.sh tasks/vla/train_lingbotvla.py <cfg>.yaml`,
    不是 `lerobot-train --policy.type=xxx`
  - 配置 yaml 结构跟 lerobot 完全不同 (model/data/train 三段, dataclass-based)
  - 必须提供 robot_config yaml (features 重映射 dataset 字段到 lingbot 内部 schema)

Phase 1 (本文件) 范围: 训练流程跑通 + step/loss 进度上报.
Phase 2 (后续): checkpoint 兼容 lerobot 格式 + 推理 deploy 适配 + autodl-fs 上传.

依赖: scripts/install_lingbot_vla_addon.sh 已在目标实例上跑过 (b2r-vla env + lingbot-vla repo 就位).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import yaml

logger = logging.getLogger("b2r.lingbot_vla_trainer")

# ============================================================================
# 路径约定 (跟 install_lingbot_vla_addon.sh 保持一致)
# ============================================================================
B2R_VLA_PYTHON = "/root/miniconda3/envs/b2r-vla/bin/python"
LINGBOT_VLA_REPO = "/root/autodl-tmp/workspace/box2robot/lingbot-vla"
LINGBOT_VLA_TRAIN_ENTRY = "tasks/vla/train_lingbotvla.py"
LINGBOT_VLA_TRAIN_SH = "train.sh"

# Box2Robot convert.py 输出的 image key (单相机, 跟 GR00T/pi0 一致写死)
B2R_IMAGE_KEY = "observation.images.wrist"

# LingBot-VLA 内部 schema 期望的 image key (SO-101 yaml 用 camera_top, robotwin yaml 用 camera_top/wrist)
LINGBOT_IMAGE_KEY = "observation.images.camera_top"


def train_lingbot_vla(
    ds_dir: str,
    model_dir: str,
    train_steps: int,
    batch_size: int,
    custom_params: dict,
    progress_cb: Callable[[int, int, dict], None],
    should_stop_cb: Callable[[], bool],
    n_servos: int,
    fps: int = 20,
) -> dict:
    """LingBot-VLA 训练主入口.

    Args:
        ds_dir: LeRobot v3 dataset 目录 (由 worker._train_lerobot 中的 convert.py 产出)
        model_dir: 训练输出目录 (model_dir/output/ 给 lingbot-vla, 收尾时整理到 model_dir/checkpoints/)
        train_steps: 总步数
        batch_size: micro_batch_size
        custom_params: 前端 advanced 参数 dict (key 跟 schemas/training-models.ts 一致)
        progress_cb: callback(step, total, metrics) 上报进度
        should_stop_cb: 返回 True 时主动停 (子进程发 SIGTERM)
        n_servos: 舵机数量 (自动检测维度, 单臂 6-DOF=6, 双臂=14)
        fps: 数据集帧率 (默认 20Hz)
    Returns:
        训练结果 dict (供 worker.process_job 后续上传 artifact 用)
    """
    # 0. 环境预检 (失败立即返回 — 没装 addon 时不要浪费 server 调度)
    _preflight_check()

    # 1. 数据集兼容性补丁 (LeRobot v0.5.2 写的 dataset 给 v0.4.2 读时, codebase_version 字段要兼容)
    _patch_dataset_codebase_version(ds_dir)

    # 2. 写一个 robot_config yaml (features 重映射, 把 box2robot dataset 字段映射成 lingbot 内部 schema)
    robot_config_path = Path(model_dir) / "robot_config.yaml"
    _write_robot_config(robot_config_path, n_servos)

    # 3. 写 norm_stats.json (lingbot-vla utils.py:91 assert 必填)
    #    从 lerobot dataset 自带 stats 算 mean/std/min/max, 转 lingbot-vla 期望格式
    norm_stats_path = Path(model_dir) / "norm_stats.json"
    _write_norm_stats(ds_dir, norm_stats_path, n_servos)

    # 4. 翻译 box2robot 训练参数 → lingbot-vla 原生 yaml
    train_config_path = Path(model_dir) / "train_config.yaml"
    output_dir = Path(model_dir) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_train_config(
        train_config_path, ds_dir, robot_config_path, output_dir,
        train_steps, batch_size, custom_params, n_servos,
        norm_stats_path=norm_stats_path,
    )

    # 4. subprocess 调 train.sh (用 b2r-vla env 的 python, 由 train.sh 内部 torchrun 启)
    result = _run_training(
        train_config_path, output_dir, train_steps,
        progress_cb, should_stop_cb,
    )

    # 5. 写 b2r_config.json (推理路径 worker.run_inference_server 必读, 用它判 model_type
    #    → 路由到 run_inference_lingbot_vla; 也供 inferencer 读 n_servos / pos_max).
    #    放在 model_dir/, 跟 _train_lerobot 同位置 (worker 读 model_dir/b2r_config.json).
    _write_b2r_config(
        Path(model_dir) / "b2r_config.json",
        n_servos=n_servos,
        custom_params=custom_params or {},
        train_steps=train_steps,
        result=result,
    )
    return result


def _write_b2r_config(path: Path, n_servos: int, custom_params: dict,
                     train_steps: int, result: dict) -> None:
    """供 worker.run_inference_server 读取 model_type/n_servos/pos_max/chunk_size 等元信息."""
    # use_length 默认 25 (lingbot-vla deploy/lingbot_vla_policy.py 默认值);
    # 用户可在前端 advanced 参数覆盖 (chunk_size 字段透传).
    use_length = int(custom_params.get("chunk_size") or custom_params.get("use_length") or 25)
    cfg = {
        "model_type": "lingbot_vla",
        "is_vla": True,
        "pos_max": 4095,
        "n_servos": n_servos,
        "use_vision": True,
        "chunk_size": use_length,
        "task_description": str(custom_params.get("task") or "manipulation task"),
        "train_steps": train_steps,
        "final_step": result.get("final_step"),
        "final_loss": result.get("final_loss"),
        "deploy_model_dir": result.get("deploy_model_dir"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("[LINGBOT-VLA] wrote b2r_config.json %s", path)


# ============================================================================
# 0. 环境预检
# ============================================================================
def _preflight_check() -> None:
    """检查 b2r-vla env + lingbot-vla repo 是否就位. 不就位直接 raise."""
    if not Path(B2R_VLA_PYTHON).is_file():
        raise RuntimeError(
            f"b2r-vla conda env 未安装: {B2R_VLA_PYTHON} 不存在. "
            f"请先在该实例上跑: bash scripts/install_lingbot_vla_addon.sh"
        )
    train_sh = Path(LINGBOT_VLA_REPO) / LINGBOT_VLA_TRAIN_SH
    train_py = Path(LINGBOT_VLA_REPO) / LINGBOT_VLA_TRAIN_ENTRY
    if not train_sh.is_file() or not train_py.is_file():
        raise RuntimeError(
            f"lingbot-vla 仓库未就位: {LINGBOT_VLA_REPO} 缺少 train.sh 或 {LINGBOT_VLA_TRAIN_ENTRY}. "
            f"请先在该实例上跑: bash scripts/install_lingbot_vla_addon.sh"
        )
    # 快速 import 检查 (~5s, 不算训练时间里)
    try:
        subprocess.run(
            [B2R_VLA_PYTHON, "-c", "import lingbotvla, torch, lerobot; assert torch.cuda.is_available()"],
            check=True, timeout=15, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"b2r-vla env import 失败: {e.stderr.decode()[:200]}")
    logger.info("[LINGBOT-VLA] 环境预检通过 (b2r-vla env + repo + cuda)")


# ============================================================================
# 1. 数据集 codebase_version 兼容性补丁
# ============================================================================
def _patch_dataset_codebase_version(ds_dir: str) -> None:
    """LeRobot 数据集 codebase_version 兼容性 patch.

    踩坑记录 (2026-05-27):
      - 之前以为 lerobot v0.4.2 期望 'v2.1', 设错了, 实际报错:
        BackwardCompatibilityError: The dataset is in 2.1 format. We introduced
        a new format since v3.0 which is not backward compatible with v2.1.
      - lingbot-vla `bash install.sh` 装的 lerobot v0.4.2 实际**已支持 v3.0 格式**,
        并且优先校验 v3.0+. 把字段设 "v2.1" 反而被拒.
      - 真正修复: codebase_version 必须 ≥ "v3.0" (跟 b2r env lerobot v0.5.2 写出来的一致即可).
    """
    info_path = Path(ds_dir) / "meta" / "info.json"
    if not info_path.is_file():
        logger.warning("[LINGBOT-VLA] meta/info.json 缺失, 跳过版本检查: %s", info_path)
        return
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[LINGBOT-VLA] info.json 解析失败 (%s), 跳过", e)
        return
    cur = str(info.get("codebase_version", ""))
    # 目标版本: v3.0 (lerobot v3 dataset format, b2r env v0.5+ / b2r-vla env v0.4.2+ 都接受).
    # 如果 cur 已经是 v3.x → noop. 否则强制 patch 成 v3.0.
    if cur.startswith("v3."):
        return  # 已兼容, noop
    backup = info_path.with_suffix(".json.bak")
    if not backup.exists():
        backup.write_text(json.dumps(info, indent=2), encoding="utf-8")
    info["codebase_version"] = "v3.0"
    info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
    logger.info("[LINGBOT-VLA] info.json codebase_version: %r → 'v3.0' (lerobot v3 format)", cur)


# ============================================================================
# 2.5. norm_stats.json 生成 (lingbot-vla utils.py:91 必填断言)
# ============================================================================
def _write_norm_stats(ds_dir: str, path: Path, n_servos: int) -> None:
    """从 LeRobot v3 dataset 算 stats 写 lingbot-vla 期望的 norm_stats.json.

    lingbot-vla 期望格式 (utils.py:99-104, Normalizer.__init__):
      {"norm_stats": {
        "action.arm.position":      {"mean": [...], "std": [...]},
        "action.effector.position": {"mean": [...], "std": [...]},
        "observation.state.arm.position":      {"mean": [...], "std": [...]},
        "observation.state.effector.position": {"mean": [...], "std": [...]},
      }}
    image features (camera_top 等) 走 identity, 不需要 stats.
    """
    import numpy as _np
    if path.exists():
        logger.info("[LINGBOT-VLA] norm_stats 已存在, 跳过: %s", path)
        return

    if n_servos == 6:
        slices = {"arm.position": (0, 5), "effector.position": (5, 6)}
    else:
        slices = {"arm.position": (0, n_servos)}

    # 直接读 LeRobot v3 dataset parquet 文件算 stats, 避开 LeRobotDataset 类
    # (它会尝试 HF API 验证 repo_id 导致 401 Repository Not Found).
    # LeRobot v3 结构: data/chunk-XXX/episode_*.parquet 含 observation.state + action 列.
    try:
        import pandas as _pd
    except ImportError as e:
        raise RuntimeError(f"无法 import pandas 算 norm_stats: {e}")

    data_dir = Path(ds_dir) / "data"
    parquet_files = sorted(data_dir.rglob("*.parquet"))
    if not parquet_files:
        raise RuntimeError(f"找不到 parquet 文件: {data_dir} (LeRobot v3 dataset 结构异常?)")

    states_list = []
    actions_list = []
    for pf in parquet_files:
        df = _pd.read_parquet(pf)
        if "observation.state" not in df.columns or "action" not in df.columns:
            raise RuntimeError(f"parquet {pf} 缺 observation.state 或 action 列: {list(df.columns)}")
        states_list.append(_np.array(df["observation.state"].tolist(), dtype=_np.float32))
        actions_list.append(_np.array(df["action"].tolist(), dtype=_np.float32))

    states = _np.concatenate(states_list, axis=0) if len(states_list) > 1 else states_list[0]
    actions = _np.concatenate(actions_list, axis=0) if len(actions_list) > 1 else actions_list[0]
    n_frames = states.shape[0]
    if n_frames == 0:
        raise RuntimeError(f"dataset 为空: {ds_dir}")
    logger.info("[LINGBOT-VLA] 算 norm_stats (n_frames=%d, n_servos=%d, %d parquet files)...",
                n_frames, n_servos, len(parquet_files))

    norm_stats = {}
    for key, (s0, s1) in slices.items():
        s_slice = states[:, s0:s1]
        a_slice = actions[:, s0:s1]
        norm_stats[f"observation.state.{key}"] = {
            "mean": s_slice.mean(axis=0).tolist(),
            "std": (s_slice.std(axis=0) + 1e-8).tolist(),
            "min": s_slice.min(axis=0).tolist(),
            "max": s_slice.max(axis=0).tolist(),
            "q01": _np.quantile(s_slice, 0.01, axis=0).tolist(),
            "q99": _np.quantile(s_slice, 0.99, axis=0).tolist(),
        }
        norm_stats[f"action.{key}"] = {
            "mean": a_slice.mean(axis=0).tolist(),
            "std": (a_slice.std(axis=0) + 1e-8).tolist(),
            "min": a_slice.min(axis=0).tolist(),
            "max": a_slice.max(axis=0).tolist(),
            "q01": _np.quantile(a_slice, 0.01, axis=0).tolist(),
            "q99": _np.quantile(a_slice, 0.99, axis=0).tolist(),
        }

    path.write_text(json.dumps({"norm_stats": norm_stats}, indent=2), encoding="utf-8")
    logger.info("[LINGBOT-VLA] norm_stats 写入 %s (keys=%s)", path, list(norm_stats.keys()))


# ============================================================================
# 2. Robot config yaml (features 重映射)
# ============================================================================
def _write_robot_config(path: Path, n_servos: int) -> None:
    """生成 robot_config yaml — 把 box2robot dataset 字段映射成 lingbot 内部 schema.

    单臂 6-DOF (5 关节 + 1 夹爪) 时跟子豪兄 SO-101 yaml 一致.
    多臂时自动算切片 (n_servos // 2 关节 + 1 夹爪 / 臂).
    """
    if n_servos == 6:
        # 单臂 SO-101 / BoxBot 标准布局
        arm_end = 5
        gripper_end = 6
        states = [
            {"observation.state.arm.position": {"origin_keys": [
                {"observation.state": {"start": 0, "end": arm_end}}]}},
            {"observation.state.effector.position": {"origin_keys": [
                {"observation.state": {"start": arm_end, "end": gripper_end}}]}},
        ]
        actions = [
            {"action.arm.position": {"origin_keys": [
                {"action": {"start": 0, "end": arm_end}}], "subtract_state": False}},
            {"action.effector.position": {"origin_keys": [
                {"action": {"start": arm_end, "end": gripper_end}}], "subtract_state": False}},
        ]
    else:
        # 其他维度 fallback: 全部当 arm 关节, 没 effector (后续可按需扩展)
        logger.warning("[LINGBOT-VLA] n_servos=%d 非标准 6, 全当 arm 关节处理", n_servos)
        states = [
            {"observation.state.arm.position": {"origin_keys": [
                {"observation.state": {"start": 0, "end": n_servos}}]}},
        ]
        actions = [
            {"action.arm.position": {"origin_keys": [
                {"action": {"start": 0, "end": n_servos}}], "subtract_state": False}},
        ]
    cfg = {
        "states": states,
        "actions": actions,
        "images": [
            {LINGBOT_IMAGE_KEY: {"origin_keys": B2R_IMAGE_KEY}},
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    logger.info("[LINGBOT-VLA] robot_config 写入 %s (n_servos=%d)", path, n_servos)


# ============================================================================
# 3. 训练 yaml (model + data + train 三段)
# ============================================================================
def _write_train_config(
    path: Path, ds_dir: str, robot_config_path: Path, output_dir: Path,
    train_steps: int, batch_size: int, custom_params: dict, n_servos: int,
    norm_stats_path: Path,
) -> None:
    """从 box2robot custom_params 翻译到 lingbot-vla 原生 yaml.

    重要修正 (2026-05-26 pipeline review 发现):
    1. lingbot-vla 没有 LoRA/peft 支持 — 删 use_lora/lora_rank 等错误字段.
       省显存正解: train.train_expert_only=true (冻结 VLM, 只训 action expert).
    2. robot_config 不走 robot_config_path, 而是 robot_config_root + data_name 拼.
       lingbotvla/data/vla_data/base_dataset.py:133: os.path.join(root, f'{name}.yaml')
    3. data.joints / data.cameras 必填 (FeatureInfo.update_info 期望).
       joints 写成 list of single-key dict: [{arm.position: 5}, {effector.position: 1}].
    4. data.norm_stats_file 必填 + JSON 必须存在 (utils.py:91 assert).
    5. data.norm_type 只接受 Literal[meanstd/bounds_99/minmax/identity],
       前端 bounds_99_woclip 是写错的值, 在这里 fallback.
    """
    cp = custom_params or {}

    # 单臂 6-DOF (5 关节 + 1 夹爪) → joints / cameras
    if n_servos == 6:
        joints = [{"arm.position": 5}, {"effector.position": 1}]
    else:
        joints = [{"arm.position": n_servos}]
    # box2robot convert.py 单相机, robot_config 重命名成 camera_top
    cameras = ["camera_top"]

    # norm_type 校验 (前端可能传 bounds_99_woclip / 其它 — fallback 到 meanstd)
    valid_norm = {"meanstd", "bounds_99", "minmax", "identity"}
    norm_type = cp.get("norm_type", "meanstd")
    if norm_type not in valid_norm:
        logger.warning("[LINGBOT-VLA] norm_type=%r 不在 %s, fallback meanstd", norm_type, valid_norm)
        norm_type = "meanstd"

    cfg = {
        "model": {
            "model_path": cp.get("model_path", "robbyant/lingbot-vla-4b"),
            "tokenizer_path": "Qwen/Qwen2.5-VL-3B-Instruct",
            "post_training": bool(cp.get("post_training", True)),
            "adanorm_time": bool(cp.get("adanorm_time", True)),
            "attn_implementation": "flash_attention_2",
            # NOTE: lingbot-vla ModelArguments 完全没有 lora/peft 字段!
            # 不要加 use_lora/lora_rank 等, 会被 ignored 而且误导用户以为 LoRA 生效.
        },
        "data": {
            "datasets_type": "vla",
            # lingbot-vla 用 root + name 拼路径: f"{robot_config_root}/{data_name}.yaml"
            "data_name": robot_config_path.stem,
            "robot_config_root": str(robot_config_path.parent),
            "train_path": str(ds_dir),
            "joints": joints,
            "cameras": cameras,
            "num_workers": 4,
            "norm_type": norm_type,
            "norm_stats_file": str(norm_stats_path),
            "img_size": 224,
        },
        "train": {
            "output_dir": str(output_dir),
            "data_parallel_mode": "fsdp2",
            "enable_full_shard": False,
            "module_fsdp_enable": True,
            "use_compile": bool(cp.get("use_compile", True)),
            "ulysses_parallel_size": 1,  # 单 GPU 训练
            # rmpad 关闭 (Qwen2-VL backbone 不支持 rmpad, 必须走 rmpad_with_pos_ids 路径)
            # 官方 real_load20000h.yaml 也设 false (训练入口 line 123 显式校验).
            "rmpad": False,
            "rmpad_with_pos_ids": False,
            # === 省显存关键 (代替 LoRA) ===
            # 4B 全量微调 ≥60GB VRAM 必 OOM. 必须冻结 VLM + 只训 action expert.
            "freeze_vision_encoder": bool(cp.get("freeze_vision_encoder", True)),
            "train_expert_only": bool(cp.get("train_expert_only", True)),
            # ============================
            "tokenizer_max_length": int(cp.get("tokenizer_max_length", 72)),
            # 维度 padding 上限 — **必须 ≥ 75** (跟 lingbot-vla 4B 预训练 head 硬编码对齐)
            # 同样的坑 GR00T 也有 (memory: error_groot_action_dim.md).
            # 改小会报: RuntimeError: The size of tensor a (8) must match tensor b (75)
            # 实际维度 (BoxBot 6-DOF) 会被 0-padding 到 75, 不影响精度只是浪费 attention.
            "max_action_dim": max(75, int(cp.get("max_action_dim", 75))),
            "max_state_dim": max(75, int(cp.get("max_state_dim", 75))),
            "lr": float(cp.get("lr", 5e-5)),
            "lr_decay_style": cp.get("lr_decay_style", "constant"),
            "micro_batch_size": int(batch_size),
            "gradient_accumulation_steps": int(cp.get("gradient_accumulation_steps", 1)),
            "max_steps": int(train_steps),
            "ckpt_manager": "dcp",
            "save_steps": int(cp.get("save_steps", 10000)),
            "save_epochs": 0,
            "enable_fp32": True,  # action expert 走 fp32 (官方默认), VLM 仍 bf16
            "enable_resume": True,
            # 推理时 deploy/lingbot_vla_policy.py:190 直接读 config.resize_imgs_with_padding,
            # PI0Config 没这个字段 → AttributeError. 必须在 yaml train 里给出, 让推理时的
            # missing_config_kwargs 把它注入到 config.__dict__. 同理把推理常访问的其它字段
            # 也补上 (deploy 端 not hasattr → 注入).
            "resize_imgs_with_padding": [224, 224],
            "adapt_to_pi_aloha": False,
            "use_delta_joint_actions_aloha": False,
            "train_state_proj": True,
            "num_steps": int(cp.get("num_denoising_steps", 10)),  # 推理 denoising 步数
        },
    }

    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
    logger.info("[LINGBOT-VLA] train_config 写入 %s (steps=%d batch=%d expert_only=%s freeze_vit=%s)",
                path, train_steps, batch_size,
                cfg["train"]["train_expert_only"], cfg["train"]["freeze_vision_encoder"])


# ============================================================================
# 3.5. dcp → safetensors 转换 (训练完成后跑, 供 deploy.lingbot_vla_policy 加载)
# ============================================================================
# 训练写 dcp 格式 (output/checkpoints/global_step_X/__*.distcp), 但推理 API
# (LingbotVLAServer.load_vla, deploy/lingbot_vla_policy.py:194-200) 用 safetensors
# + safe_open(file_path, framework='pt') 加载.
# 转换流程 (subprocess 走 b2r-vla env, 需要 import lingbotvla):
#   1. 找 output/checkpoints/global_step_X (取 step 最大的 / 或 'last')
#   2. ckpt_to_state_dict(dcp_path, output_dir, ckpt_manager='dcp')
#   3. safetensors.torch.save_file(state_dict, deploy_dir/model.safetensors)
#   4. 拷 train_config.yaml → deploy_dir/lingbotvla_cli.yaml (load_vla 要这个文件名)
#   5. 拷 norm_stats.json (训练 yaml 里 data.norm_stats_file 指向的)
#   6. 写 config.json (PreTrainedConfig.from_pretrained 需要)
_CONVERT_DCP_SCRIPT = '''
import sys, json, shutil, glob, os
from pathlib import Path

output_dir = Path(sys.argv[1])     # train output_dir (含 checkpoints/global_step_X)
deploy_dir = Path(sys.argv[2])     # 目标 deploy_model 目录
deploy_dir.mkdir(parents=True, exist_ok=True)
safetensors_path = deploy_dir / "model.safetensors"

if safetensors_path.exists():
    # 幂等恢复: safetensors 已写过, 跳过昂贵的 dcp → state_dict 转换
    print(f"[CONVERT] model.safetensors 已存在 ({safetensors_path.stat().st_size/1e9:.1f} GB), 跳过 dcp 转换", flush=True)
else:
    ckpt_root = output_dir / "checkpoints"
    ckpts = sorted(ckpt_root.glob("global_step_*"), key=lambda p: int(p.name.split("_")[-1]))
    if not ckpts:
        raise RuntimeError(f"No global_step_* checkpoint in {ckpt_root}")
    latest = ckpts[-1]
    print(f"[CONVERT] latest dcp: {latest}", flush=True)

    # 加载 dcp → state_dict (走 lingbot-vla 内置工具)
    sys.path.insert(0, "/root/autodl-tmp/workspace/box2robot/lingbot-vla")
    from lingbotvla.checkpoint.format_utils import ckpt_to_state_dict
    state_dict = ckpt_to_state_dict(str(latest), str(deploy_dir), ckpt_manager="dcp")
    print(f"[CONVERT] state_dict keys: {len(state_dict)}", flush=True)

    # 写 safetensors (需要 contiguous tensors)
    from safetensors.torch import save_file
    state_dict = {k: v.contiguous() if hasattr(v, "contiguous") else v for k, v in state_dict.items()}
    save_file(state_dict, str(safetensors_path))
    print(f"[CONVERT] saved {safetensors_path}", flush=True)

# 拷 train config → lingbotvla_cli.yaml (deploy load_vla 写死这个文件名)
train_cfg = output_dir.parent / "train_config.yaml"
if train_cfg.is_file():
    shutil.copy(train_cfg, deploy_dir / "lingbotvla_cli.yaml")
    print(f"[CONVERT] copied train_config → lingbotvla_cli.yaml", flush=True)

# 拷 norm_stats.json
norm_stats = output_dir.parent / "norm_stats.json"
if norm_stats.is_file():
    shutil.copy(norm_stats, deploy_dir / "norm_stats.json")
    print(f"[CONVERT] copied norm_stats.json", flush=True)

# 写 config.json 让 lerobot PreTrainedConfig.from_pretrained 能 load.
# 关键: 必须用 lerobot 的 draccus 风格 (discriminator key="type"), 不是 HF
# transformers 的 "model_type". LingbotVlaPolicy.QwenVLA_Config 继承自 PI0Config,
# 所以 type="pi0". 直接复制 HF cache 里 robbyant/lingbot-vla-4b 的 config.json,
# 那是 lingbot 官方的版本, 字段已对齐 (n_obs_steps/input_features/...).
# 训练时 lingbotvla_cli.yaml 的 model+train 字段会在 LingbotVLAServer.load_vla 内 merge 进来,
# 所以这里不再 patch 训练 override — 让运行时 yaml merge 处理就好.
config_json_path = deploy_dir / "config.json"
if not config_json_path.exists():
    # 在 HF_HOME 里找已下载的 lingbot-vla-4b config.json
    hf_home = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
    base_glob = glob.glob(
        f"{hf_home}/hub/models--robbyant--lingbot-vla-4b/snapshots/*/config.json")
    if base_glob:
        import shutil as _shutil
        _shutil.copy(base_glob[0], config_json_path)
        print(f"[CONVERT] copied base config.json from {base_glob[0]}", flush=True)
    else:
        # 兜底: 写一个最小化能 parse 的 (type=pi0 是 lingbot 继承自 PI0Config 的 lerobot type)
        config_data = {
            "type": "pi0",
            "n_obs_steps": 1,
            "input_features": {}, "output_features": {},
            "device": "cpu", "use_amp": False,
            "chunk_size": 50, "n_action_steps": 50,
            "max_state_dim": 75, "max_action_dim": 75,
            "empty_cameras": 0, "tokenizer_max_length": 72,
        }
        config_json_path.write_text(json.dumps(config_data, indent=2))
        print(f"[CONVERT] wrote fallback config.json {config_json_path}", flush=True)

print("[CONVERT] DONE", flush=True)
'''


_DEPLOY_REQUIRED = ("model.safetensors", "config.json", "lingbotvla_cli.yaml", "norm_stats.json")


def _convert_dcp_to_safetensors(output_dir: Path, deploy_dir: Path) -> None:
    """训练结束后把 dcp checkpoint 转成 safetensors + 拷配套文件到 deploy_model/.

    走 b2r-vla env subprocess (需要 import lingbotvla.checkpoint).
    超时 1800s (4B 模型 16GB state_dict 写盘到 autodl-fs NFS 实测 10-12min).

    幂等: subprocess 脚本内部按 deploy_dir 中已存在的文件跳过昂贵步骤 (safetensors gen 跳过),
    所以即使中途崩了, 重跑也能秒补漏掉的 config.json / 配套 yaml.
    """
    if deploy_dir.exists() and all((deploy_dir / f).exists() for f in _DEPLOY_REQUIRED):
        logger.info("[LINGBOT-VLA] deploy_model 已全就位, 跳过转换: %s", deploy_dir)
        return
    script_path = deploy_dir.parent / "_convert_dcp.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_CONVERT_DCP_SCRIPT, encoding="utf-8")

    logger.info("[LINGBOT-VLA] 开始 dcp → safetensors (output_dir=%s)...", output_dir)
    proc = subprocess.run(
        [B2R_VLA_PYTHON, str(script_path), str(output_dir), str(deploy_dir)],
        capture_output=True, text=True, timeout=1800,
    )
    for line in proc.stdout.splitlines():
        logger.info("[LINGBOT-VLA] %s", line)
    if proc.returncode != 0:
        logger.error("[LINGBOT-VLA] 转换失败 stderr: %s", proc.stderr[-1500:])
        raise RuntimeError(f"dcp → safetensors conversion failed (exit {proc.returncode})")


# ============================================================================
# 4. subprocess 调 train.sh + 解析进度
# ============================================================================
# lingbot-vla 实际 log 格式 (2026-05-28 实测确认):
#   tqdm:    "Step: 100/100 [02:13<00:00,  1.55it/s]"
#   logger:  "05/28/2026 01:56:31 - INFO - __main__ - Step 100/79, Epoch 2, Loss 0.2188, "
#                "VLA_Loss 0.2188, Depth_Loss 0.0000, GradNorm 0.6904, LR 5.00e-05, StepTime 0.529s"
# 注意:
#   - "Step 100/79" 第二个数是 epoch 内 batch 总数, 不是 max_steps. 不能用它做 total.
#   - "Step: 100/100" tqdm 风格的才是真实 progress (current/max_steps).
#   - Loss 后是空格不是冒号: "Loss 0.2188" 而非 "Loss: 0.2188".
import re
# 优先匹配 tqdm 风格 "Step: X/Y" (Y 是 max_steps), 再 fallback logger 风格 "Step X/Z"
_LOG_RE_STEP = re.compile(r"Step\s*:\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_LOG_RE_LOSS = re.compile(r"\bLoss[\s:=]+([\d.eE+-]+)", re.IGNORECASE)
_LOG_RE_TQDM = re.compile(r"(\d+)/(\d+)\s*\[")
# 训练成功 marker — torchrun cleanup 可能 hang 几分钟, 看到这些 marker 就主动结束 subprocess
_SUCCESS_MARKERS = (
    "Distributed checkpoint saved",
    "checkpoint saved at",
    "Reached max_steps",
)


def _run_training(
    train_config_path: Path, output_dir: Path, train_steps: int,
    progress_cb: Callable[[int, int, dict], None],
    should_stop_cb: Callable[[], bool],
) -> dict:
    """启动 train.sh subprocess + 监控 stdout 解析进度. 阻塞直到训练结束."""
    cmd = [
        "bash", LINGBOT_VLA_TRAIN_SH, LINGBOT_VLA_TRAIN_ENTRY, str(train_config_path),
    ]
    env = os.environ.copy()
    env["PATH"] = f"/root/miniconda3/envs/b2r-vla/bin:{env.get('PATH', '')}"
    env["TOKENIZERS_PARALLELISM"] = "false"
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env.setdefault("HF_HOME", "/root/autodl-fs/data/box2robot-base-models")
    env.setdefault("CUDA_VISIBLE_DEVICES", "0")  # 单卡
    # libtorchcodec 加载视频时 dlopen 系统 ld 路径的 libavutil.so.59 等. b2r-vla env 没装
    # system FFmpeg, 但 PyAV bundled FFmpeg 7 libs (libavutil-<hash>.so.59.x) 在 av.libs/.
    # 已用 ln -s 在 av.libs/ 建标准名 symlinks (libavutil.so.59 等), 这里把目录加入 LD_LIBRARY_PATH
    # 让 torchcodec dlopen 找到. 踩坑: 2026-05-28 训练失败 RuntimeError: Could not load libtorchcodec.
    av_libs = "/root/miniconda3/envs/b2r-vla/lib/python3.12/site-packages/av.libs"
    env["LD_LIBRARY_PATH"] = f"{av_libs}:{env.get('LD_LIBRARY_PATH', '')}"

    logger.info("[LINGBOT-VLA] 启动训练: %s (cwd=%s)", " ".join(cmd), LINGBOT_VLA_REPO)
    proc = subprocess.Popen(
        cmd, cwd=LINGBOT_VLA_REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, universal_newlines=True,
    )

    last_step = 0
    last_loss = None
    last_report_ts = 0.0
    start_ts = time.time()
    REPORT_INTERVAL = 5.0  # 至少每 5s 上报一次, 避免 server 误判 stale
    training_succeeded = False  # 看到 _SUCCESS_MARKERS 就置 True (跳过 fail 检测)
    # 收集 stdout 行 (滚动 buffer, 最后 grep fail markers).
    # train.sh 用 tee 吞 torchrun exit code → returncode 不可信 → 必须 grep log.
    collected_lines: list = []
    MAX_COLLECTED = 500  # 防 OOM: 最多保留 500 行尾 buffer

    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            # 实时透传 stdout 到 worker log
            print(f"[LINGBOT-VLA] {line}", flush=True)
            # 滚动 buffer 收集 (尾窗口, 防 OOM)
            collected_lines.append(line)
            if len(collected_lines) > MAX_COLLECTED:
                collected_lines.pop(0)

            # 解析步数
            step = None
            m = _LOG_RE_STEP.search(line)
            if m:
                step = int(m.group(1))
            else:
                m = _LOG_RE_TQDM.search(line)
                if m:
                    step = int(m.group(1))
            if step is not None:
                last_step = step
            # 解析 loss
            m = _LOG_RE_LOSS.search(line)
            if m:
                try:
                    last_loss = float(m.group(1))
                except ValueError:
                    pass

            # 节流上报 (避免 server 被 spam)
            now = time.time()
            if last_step > 0 and (now - last_report_ts) >= REPORT_INTERVAL:
                metrics = {
                    "loss": last_loss if last_loss is not None else 0.0,
                    "best_loss": last_loss if last_loss is not None else 0.0,
                    "steps_per_sec": last_step / max(1.0, now - start_ts),
                    "elapsed_sec": now - start_ts,
                }
                try:
                    progress_cb(last_step, train_steps, metrics)
                except Exception as e:
                    logger.warning("[LINGBOT-VLA] progress_cb 异常: %s", e)
                last_report_ts = now

            # 检查训练成功结束 marker — torchrun 的 NCCL destroy_process_group cleanup 经常
            # hang 几分钟, 卡住 worker.process_job. 看到这些 marker 就主动 terminate subprocess.
            if any(m in line for m in _SUCCESS_MARKERS):
                logger.info("[LINGBOT-VLA] 检测到训练成功 marker (%r), 主动结束 subprocess",
                            next(m for m in _SUCCESS_MARKERS if m in line))
                training_succeeded = True
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

            # 检查 stop 信号 (server 端用户点取消)
            if should_stop_cb():
                logger.warning("[LINGBOT-VLA] 收到 stop 信号, 终止训练")
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

        proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    # train.sh 用 `torchrun ... | tee log.txt` — tee 默认吞 torchrun 的 exit code.
    # bash 管道最后一个命令的 exit code 才是 $? = tee 永远 0 → proc.returncode 不可信.
    # 必须 grep lingbot-vla 进程实际抛出的 fail markers 才能正确判失败.
    # 踩坑 2026-05-27: 之前 dataset 加载 crash (BackwardCompatibilityError),
    # 但 proc.returncode==0 → trainer 误报 "训练完成 step=1/10000 loss=None".
    fail_markers = (
        "ChildFailedError",                          # torchrun 子进程崩
        "tasks/vla/train_lingbotvla.py FAILED",      # torchrun 失败 banner
        "exitcode: 1",                                # torchrun 子进程 exit 1
        "BackwardCompatibilityError",                # lerobot 数据集版本错
        "Traceback (most recent call last)",         # 任意 Python traceback
        "ValueError: Qwen2-VL does not support",     # rmpad 错
    )
    detected_fail = None
    for line in collected_lines:
        for m in fail_markers:
            if m in line:
                detected_fail = (m, line.strip()[:200])
                break
        if detected_fail:
            break

    # training_succeeded=True 时 (看到 checkpoint saved 等成功 marker), 跳过 fail 检测.
    # 因为 torchrun cleanup hang 时我们主动 terminate, returncode 会非 0 (SIGTERM=-15),
    # 但训练实际已成功. 必须靠 marker 区分.
    if not training_succeeded and (proc.returncode != 0 or detected_fail is not None):
        msg = f"lingbot-vla 训练失败"
        if detected_fail:
            msg += f" (marker={detected_fail[0]!r}, line={detected_fail[1]!r})"
        else:
            msg += f" (exit={proc.returncode})"
        raise RuntimeError(msg)

    logger.info("[LINGBOT-VLA] 训练结束 step=%d/%d loss=%s 用时=%.1fs",
                last_step, train_steps, last_loss, time.time() - start_ts)

    # 训练成功 → 把 dcp checkpoint 转成 safetensors 供 deploy.lingbot_vla_policy 加载.
    # 推理 API (LingbotVLAServer.load_vla) 要求 model_path 含: config.json + *.safetensors
    # + lingbotvla_cli.yaml + norm_stats.json. 训练写 dcp 在 output/checkpoints/global_step_X/
    # — 必须额外转换 (lingbot 自带 ckpt_to_state_dict 工具).
    deploy_model_dir = Path(output_dir).parent / "deploy_model"
    try:
        _convert_dcp_to_safetensors(output_dir=Path(output_dir), deploy_dir=deploy_model_dir)
        logger.info("[LINGBOT-VLA] dcp → safetensors 转换完成: %s", deploy_model_dir)
    except Exception as e:
        # 转换失败不阻塞 training 状态报告 (训练本身成功), 只 warn.
        # 推理时 worker 会发现 deploy_model/ 不存在并报清楚错.
        logger.warning("[LINGBOT-VLA] dcp → safetensors 转换失败 (训练已完成, 推理需重新转): %s", e)

    return {
        "model_type": "lingbot_vla",
        "final_step": last_step,
        "final_loss": last_loss,
        "output_dir": str(output_dir),
        "deploy_model_dir": str(deploy_model_dir) if deploy_model_dir.exists() else None,
        "elapsed_sec": time.time() - start_ts,
    }
