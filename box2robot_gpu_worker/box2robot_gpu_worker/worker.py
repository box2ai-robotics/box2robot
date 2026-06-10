"""
Training Worker — connects to Box2Robot server, downloads dataset, trains, reports progress.

Usage:
    # Process a specific job from the server
    b2r-worker --server https://robot.box2ai.com --job-id abc123 --key my-secret

    # Run as a polling worker (checks for pending jobs)
    b2r-worker --server https://robot.box2ai.com --key my-secret --poll
"""
import argparse
import json
import logging
import sys
import time
from pathlib import Path

import httpx

from box2robot_gpu_worker import normalize_model_type as _normalize_model_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("box2robot.worker")


def _setup_file_logging() -> None:
    """落 ~/.b2r-gpu/worker.log (10MB × 5 滚转), b2r-worker 独立启动时也保留持久日志.
    与 gpu_worker._setup_file_logging 共用同一文件 + 同一去重 flag, 同进程不重复加 handler."""
    try:
        from logging.handlers import RotatingFileHandler
        log_dir = Path.home() / ".b2r-gpu"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "worker.log"
        root = logging.getLogger()
        if any(getattr(h, "_b2r_file_log", False) for h in root.handlers):
            return
        handler = RotatingFileHandler(
            str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
        handler._b2r_file_log = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        logger.info("[LOG] file logging → %s", log_file)
    except Exception as e:
        logger.warning("[LOG] file logging setup failed: %s", e)


_setup_file_logging()


# === Multi-slot race protection (v0.6.3+) ===
# 多个 GPU slot 并发跑同一 dataset 时, 数据下载 / LeRobot convert / quantile augment
# 三个阶段都会写共享磁盘路径 (cache/ds_<fp>, datasets/<repo>, stats.json), 不加锁
# 会撞文件冲突 / 重复下载. 这里用 dict<key, Lock> per-fingerprint 串行数据准备,
# 准备完之后训练 subprocess 各自跑, dataset 文件 read-only 安全.
import threading as _threading
_FILE_LOCKS_LOCK = _threading.Lock()
_FILE_LOCKS: dict = {}

def _get_file_lock(key: str) -> "_threading.Lock":
    """获取或创建一把 per-key 锁. key 可以是 fingerprint, repo_id 等."""
    with _FILE_LOCKS_LOCK:
        if key not in _FILE_LOCKS:
            _FILE_LOCKS[key] = _threading.Lock()
        return _FILE_LOCKS[key]


# === error_msg 脱敏 ===
# 上报给 server / 透传给前端的错误消息里不能出现 cloud 供应商绝对路径,
# 替换成中性占位符 <cloud-storage> / <workspace>, 保留 job_id 之后的尾段方便排错.
# 仅作用于 error_msg 字符串, 不动 model_path (推理需要真实路径).
import re as _re

_PATH_REDACT_RULES = (
    (_re.compile(r'/(?:root/)?autodl-fs(?:/data)?/box2robot-outputs/pool-default'), '<cloud-storage>'),
    (_re.compile(r'/mnt/box2robot-outputs/pool-default'),                            '<cloud-storage>'),
    (_re.compile(r'/(?:root/)?autodl-fs(?:/data)?'),                                 '<cloud-storage>'),
    (_re.compile(r'/(?:root/)?autodl-tmp/workspace/box2robot'),                      '<workspace>'),
    (_re.compile(r'/(?:root/)?autodl-tmp'),                                          '<workspace>'),
)


def _sanitize_error_path(text):
    """把错误消息里暴露 cloud 供应商的绝对路径替换成中性占位符. 非字符串原样返回."""
    if not isinstance(text, str) or not text:
        return text
    for pattern, repl in _PATH_REDACT_RULES:
        text = pattern.sub(repl, text)
    return text


class TrainingWorker:
    """Connects to Box2Robot server, trains models, reports progress."""

    def __init__(self, server_url: str, pairing_key: str = "", output_dir: str = "outputs"):
        self.server_url = server_url.rstrip("/")
        self.pairing_key = pairing_key
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(timeout=60)
        self._should_stop = False
        self._should_pause = False

    @staticmethod
    def _ds_fingerprint(ids: list) -> str:
        """统一的数据集指纹 — process_job 和 _train_lerobot 都用这个, 防止两端口径不一致.

        ids: dataset_ids (来自 server job 元数据) 或 trajectory id 列表 — 内容相同.
        正常情况下两者完全一致; 若 server 删了某条轨迹, 仍以 server 给的 dataset_ids
        为准 (process_job 用 dataset_ids → 下载/校验缓存 → 把 fingerprint 透传给 _train_lerobot).
        """
        import hashlib
        return hashlib.md5("_".join(sorted(ids)).encode()).hexdigest()[:12]

    def process_job(self, job_id: str, resume_from_step: int = None):
        """Download dataset, train, report progress, complete.

        Args:
            resume_from_step: If set, resume training from this checkpoint step
                              (uses LeRobot --resume --checkpoint_path)
        """
        self._should_stop = False
        self._should_pause = False
        logger.info("Processing job: %s%s", job_id,
                     f" (resume from step {resume_from_step})" if resume_from_step else "")
        self._report_status(job_id, "downloading")

        # 1. 先获取 job 信息 (轻量, 不含轨迹帧数据)
        job_info = self._get_job_info(job_id)
        if not job_info:
            self._report_status(job_id, "failed", error_msg="Failed to get job info")
            return

        # 入口归一化: 把前端 "gr00t" 转成 LeRobot 上游的 "groot", 避免 lerobot-train
        # invalid choice. 项目其他 ID (act/diffusion/pi0/pi05/smolvla/...) 原样直通.
        model_type = _normalize_model_type(job_info.get("model_type", "act"))
        train_steps = job_info.get("train_steps", 10000)
        batch_size = job_info.get("batch_size", 64)
        chunk_size = job_info.get("chunk_size", 1)
        custom_params = job_info.get("custom_params", {})
        if isinstance(custom_params, str):
            custom_params = json.loads(custom_params) if custom_params else {}
        dataset_ids = job_info.get("dataset_ids", [])
        if isinstance(dataset_ids, str):
            dataset_ids = json.loads(dataset_ids) if dataset_ids else []

        # 2. 用 dataset_ids 算特征码, 强校验本地缓存完整性
        ds_fingerprint = self._ds_fingerprint(dataset_ids)
        ds_cache_dir = Path(__file__).parent.parent / "cache" / f"ds_{ds_fingerprint}"
        ds_dir = ds_cache_dir / "dataset"
        img_base = ds_cache_dir / "images"

        # === Multi-slot race protection: 同 fingerprint 多 thread 串行下载 ===
        # 不加锁两个 slot 都看 cache 不完整 → 都下载 → 互相覆盖 traj_*.json.
        # 加锁后第一个进入的下载完, 第二个看 cache 已 complete 直接 hit.
        with _get_file_lock(f"download_{ds_fingerprint}"):
            # 完整性校验 (锁内重新检查): 文件数 == len(dataset_ids), 防止上次中途崩溃留下的半截缓存被误命中
            cached_jsons = sorted(ds_dir.glob("traj_*.json")) if ds_dir.is_dir() else []
            cache_complete = (
                len(dataset_ids) > 0
                and len(cached_jsons) == len(dataset_ids)
            )

            if cache_complete:
                logger.info("[CACHE HIT] fp=%s trajs=%d dir=%s",
                             ds_fingerprint, len(cached_jsons), ds_cache_dir)
                trajectories = []
                for f in cached_jsons:
                    with open(f) as fh:
                        trajectories.append(json.load(fh))
                has_any_images = img_base.is_dir() and any(img_base.iterdir())
                if has_any_images:
                    n_img_dirs = sum(1 for d in img_base.iterdir() if d.is_dir())
                    logger.info("[CACHE HIT] images=%d dirs", n_img_dirs)
            else:
                # 缓存未命中或不完整 — 下载完整数据集
                if ds_dir.is_dir() and cached_jsons:
                    logger.warning("[CACHE STALE] fp=%s have %d files, expect %d → 重新下载",
                                    ds_fingerprint, len(cached_jsons), len(dataset_ids))
                else:
                    logger.info("[CACHE MISS] fp=%s → 下载数据集", ds_fingerprint)
                dataset = self._download_dataset(job_id)
                if not dataset:
                    self._report_status(job_id, "failed", error_msg="Failed to download dataset")
                    return
                trajectories = dataset.get("trajectories", [])
                if not trajectories:
                    self._report_status(job_id, "failed", error_msg="No trajectories in dataset")
                    return
                # 保存到缓存
                ds_dir.mkdir(parents=True, exist_ok=True)
                has_any_images = False
                for i, traj in enumerate(trajectories):
                    with open(ds_dir / f"traj_{i:04d}.json", "w") as f:
                        json.dump(traj, f)
                    img_url = traj.get("image_download_url")
                    if img_url:
                        traj_id = traj.get("id", f"traj_{i:04d}")
                        img_dir = img_base / traj_id
                        if self._download_images(img_url, img_dir):
                            has_any_images = True
                logger.info("Dataset saved to %s", ds_cache_dir)

        logger.info("Dataset: %d trajectories, model=%s, steps=%d (fp=%s)",
                     len(trajectories), model_type, train_steps, ds_fingerprint)

        # 2. Preprocessing + Train
        self._report_progress(job_id, 0, train_steps, {
            "phase": "preprocessing",
            "message": f"数据集下载完成: {len(trajectories)} 条轨迹" + (f", {sum(1 for d in img_base.iterdir() if d.is_dir())} 组图像" if has_any_images else ""),
        })
        self._report_status(job_id, "training")
        # 用绝对路径，避免 worker 重启 / cwd 变化后 Path(model_path).exists() 失败
        model_dir = str((self.output_dir / job_id / "model").resolve())

        # 残留清理: 共享盘 (autodl-fs / NFS) 上同 job_id 的 model/ 目录可能因上次训练
        # 被 kill / OOM / 实例销毁而残留. lerobot configs/train.py:145 会因目录已存在
        # 且 resume=false 抛 FileExistsError. 这里只在"非 resume" 路径下清理, resume
        # 路径走 _train_lerobot 内部 checkpoint 检查, 不动 model_dir.
        if not resume_from_step:
            try:
                _md = Path(model_dir)
                if _md.is_dir():
                    import shutil as _shutil
                    _shutil.rmtree(_md, ignore_errors=True)
                    logger.info("[CLEAN] removed stale model_dir: %s", model_dir)
            except Exception as e:
                logger.warning("[CLEAN] stale model_dir cleanup failed: %s", e)

        # 每次 progress 上报都让 server 知道 model_dir + 当前 checkpoint 列表,
        # 这样训练中途任何时刻断电/崩溃, server 都已经记下"这个 job 的模型在哪、有哪些可用 ckpt".
        # 不依赖 cancel/pause/done 路径才上报, 防止 OOM / 突然 kill 时来不及汇报.
        _last_reported_ckpts: list = []

        def progress_cb(step, total, metrics):
            if self._should_stop or self._should_pause:
                return
            # 顺带带上当前 checkpoints + model_path. 只在 ckpt 列表变化时塞进去,
            # 避免每条 progress 都重复 IO.
            try:
                ckpts_now = self._scan_checkpoints(model_dir)
            except Exception:
                ckpts_now = []
            nonlocal _last_reported_ckpts
            if ckpts_now and ckpts_now != _last_reported_ckpts:
                metrics = dict(metrics or {})
                metrics["checkpoints"] = ckpts_now
                metrics["model_path"] = model_dir  # 仅作为元数据; server 写 DB 主要在 status 通道
                _last_reported_ckpts = ckpts_now
                # 顺手用 status 通道写一次 model_path, 让 DB model_path 即使中途异常也有值
                try:
                    self._report_status(job_id, "training",
                                        model_path=model_dir,
                                        checkpoints=ckpts_now)
                except Exception:
                    pass
            resp = self._report_progress(job_id, step, total, metrics)
            if resp and resp.get("should_stop"):
                logger.warning("Server requested stop")
                self._should_stop = True
            elif resp and resp.get("should_pause"):
                logger.warning("Server requested pause — will save checkpoint and stop")
                self._should_pause = True

        try:
            # 训练入口分发:
            #   - lingbot_vla → 独立 b2r-vla env (lerobot v0.4.2 + lingbot-vla 仓库),
            #                   走 lingbot_vla_trainer (subprocess 调 train.sh)
            #   - 其他 (ACT/Diffusion/VLA) → _train_lerobot (lerobot-train CLI)
            # 一机一模型互斥靠 max_concurrent=1 自动保证 (subprocess 占 slot 其他 job 排队).
            if model_type == "lingbot_vla":
                result = self._train_lingbot_vla(
                    trajectories, model_dir, train_steps, batch_size,
                    custom_params, progress_cb, ds_fingerprint,
                )
            else:
                # 先把 Box2Robot JSON 轨迹转成 LeRobot v3 dataset, 再调 lerobot-train subprocess.
                result = self._train_lerobot(
                    trajectories, model_type, model_dir,
                    train_steps, batch_size, chunk_size, custom_params, progress_cb,
                    resume_from_step=resume_from_step,
                    ds_fingerprint=ds_fingerprint,
                )

            if self._should_stop:
                # 取消前先扫描已保存的 checkpoint 并随 status 一起上报
                # （progress 通道在 status=cancelled 时会被 409 拒收，必须走 status）
                ckpts = self._scan_checkpoints(model_dir)
                self._report_status(job_id, "cancelled",
                                    model_path=model_dir if ckpts else None,
                                    checkpoints=ckpts or None)
                logger.info("Training cancelled. Checkpoints: %s", ckpts)
                return

            if self._should_pause:
                ckpts = self._scan_checkpoints(model_dir)
                self._report_status(job_id, "paused",
                                    model_path=model_dir if ckpts else None,
                                    checkpoints=ckpts or None)
                logger.info("Training paused at checkpoint. Checkpoints: %s", ckpts)
                return

            # 3. Complete
            self._report_status(job_id, "completed", model_path=model_dir)
            logger.info("Training complete: %s", model_dir)
            logger.info("Results: %s", json.dumps(result, indent=2))
            # Hardening E: 把训练好的模型 tar.gz 推到 server, 哪怕 worker 实例销毁
            # (AutoDL 关机) 也能在另一台机器上下载来跑推理.
            try:
                self._upload_model_artifact(job_id, model_dir)
            except Exception as e:
                logger.warning("[E] Model upload failed (训练已完成不影响 status): %s", e)

        except Exception as e:
            logger.error("Training failed: %s", e, exc_info=True)
            # 异常前也扫一次，部分 checkpoint 已落盘则保留可推理能力
            try:
                ckpts = self._scan_checkpoints(model_dir)
            except Exception:
                ckpts = []
            self._report_status(job_id, "failed", error_msg=str(e),
                                model_path=model_dir if ckpts else None,
                                checkpoints=ckpts or None)

    # VLA models that fine-tune from pretrained base (vision-language-action)
    VLA_MODELS = {"smolvla", "pi0", "pi0_fast", "pi05"}
    # Default pretrained base for each VLA model (HuggingFace Hub)
    VLA_PRETRAINED = {
        "smolvla": "lerobot/smolvla_base",
        "pi0": "lerobot/pi0_base",
        "pi0_fast": "lerobot/pi0_fast_base",
        "pi05": "lerobot/pi05_base",
    }
    # Box2Robot dataset 当前的图像 key (convert.py 写死了; 单相机)
    DATASET_VISION_KEY = "observation.images.wrist"

    # 已知 VLA base 期望的相机 key (用于 _get_base_visual_keys 离线/网络失败兜底).
    # 数据来源: 各 base 的 config.json input_features. 第一个 key 是主视角,
    # rename_map 把我们的 'observation.images.wrist' 映射到这里; 其余 cam 会被
    # modeling 自动 -1 填充 (siglip empty camera).
    KNOWN_BASE_VISUAL_KEYS = {
        "lerobot/pi05_base": [
            "observation.images.base_0_rgb",
            "observation.images.left_wrist_0_rgb",
            "observation.images.right_wrist_0_rgb",
        ],
        "lerobot/pi05_droid": [
            "observation.images.exterior_1_left",
            "observation.images.exterior_2_left",
            "observation.images.wrist_left",
        ],
        "lerobot/pi0_base": [
            "observation.images.cam_high",
            "observation.images.cam_left_wrist",
            "observation.images.cam_right_wrist",
        ],
        "lerobot/pi0_fast_base": [
            "observation.images.cam_high",
            "observation.images.cam_left_wrist",
            "observation.images.cam_right_wrist",
        ],
        "lerobot/smolvla_base": [
            "observation.images.top",
        ],
    }

    # 哪些模型 STATE/ACTION 默认用 QUANTILES normalization → 需要 dataset 有 q01/q99 stats.
    # 这个集合从 lerobot 各 policy config 的 normalization_mapping 读取得来:
    #   pi05/configuration_pi05.py: STATE/ACTION = QUANTILES
    #   pi0/configuration_pi0.py:   STATE/ACTION = MEAN_STD
    #   smolvla/configuration_smolvla.py: STATE/ACTION = MEAN_STD
    QUANTILE_NORM_MODELS = {"pi05"}

    # 前端 schema 扁平 key → 真实 lerobot policy config 字段名 别名映射.
    # 大多数前端 key 跟 config 字段同名 (chunk_size, n_action_steps, kl_weight, ...),
    # 这里只列名字不一致的.
    PARAM_ALIASES = {
        "lr": "optimizer_lr",
        "weight_decay": "optimizer_weight_decay",
        "grad_clip_norm": "optimizer_grad_clip_norm",
    }

    # 各 policy 实际暴露的 dataclass 字段集合 (从 lerobot/policies/<m>/configuration_<m>.py 抽出).
    # 用作 CLI 参数白名单 — 前端 schema 通用字段 (如 GRAD_CLIP/SCHED_WARMUP) 在某些 model
    # 不存在 (ACT/Diffusion/GR00T 没 grad_clip_norm; ACT/GR00T 没 scheduler_warmup_steps;
    # SmolVLA/GR00T 没 dtype...). 不过滤就会撞 lerobot draccus "unrecognized arguments"
    # 让训练 exit code 2.
    POLICY_FIELDS = {
        "act": {
            "n_obs_steps", "chunk_size", "n_action_steps",
            "vision_backbone", "pretrained_backbone_weights",
            "replace_final_stride_with_dilation", "pre_norm",
            "dim_model", "n_heads", "dim_feedforward", "feedforward_activation",
            "n_encoder_layers", "n_decoder_layers",
            "use_vae", "latent_dim", "n_vae_encoder_layers",
            "temporal_ensemble_coeff", "dropout", "kl_weight",
            "optimizer_lr", "optimizer_weight_decay", "optimizer_lr_backbone",
        },
        "diffusion": {
            "n_obs_steps", "horizon", "n_action_steps", "drop_n_last_frames",
            "arch",
            "vision_backbone", "resize_shape", "crop_ratio", "crop_shape", "crop_is_random",
            "pretrained_backbone_weights", "use_group_norm",
            "spatial_softmax_num_keypoints", "use_separate_rgb_encoder_per_camera",
            "down_dims", "kernel_size", "n_groups",
            "diffusion_step_embed_dim", "use_film_scale_modulation",
            "n_layers", "n_heads", "n_emb",
            "p_drop_emb", "p_drop_attn", "n_cond_layers", "causal_attn",
            "noise_scheduler_type", "num_train_timesteps", "beta_schedule",
            "beta_start", "beta_end", "prediction_type", "clip_sample", "clip_sample_range",
            "num_inference_steps", "compile_model", "compile_mode", "do_mask_loss_for_padding",
            "optimizer_lr", "optimizer_betas", "optimizer_eps", "optimizer_weight_decay",
            "scheduler_name", "scheduler_warmup_steps",
        },
        "smolvla": {
            "n_obs_steps", "chunk_size", "n_action_steps",
            "max_state_dim", "max_action_dim",
            "resize_imgs_with_padding", "empty_cameras",
            "adapt_to_pi_aloha", "use_delta_joint_actions_aloha",
            "tokenizer_max_length", "num_steps", "use_cache",
            "freeze_vision_encoder", "train_expert_only", "train_state_proj",
            "optimizer_lr", "optimizer_betas", "optimizer_eps",
            "optimizer_weight_decay", "optimizer_grad_clip_norm",
            "scheduler_warmup_steps", "scheduler_decay_steps", "scheduler_decay_lr",
            "vlm_model_name", "load_vlm_weights", "add_image_special_tokens",
            "attention_mode", "prefix_length", "pad_language_to",
            "num_expert_layers", "num_vlm_layers", "self_attn_every_n_layers",
            "expert_width_multiplier", "min_period", "max_period",
            "compile_model", "compile_mode",
        },
        "pi0": {
            "paligemma_variant", "action_expert_variant", "dtype",
            "n_obs_steps", "chunk_size", "n_action_steps",
            "max_state_dim", "max_action_dim",
            "num_inference_steps",
            "time_sampling_beta_alpha", "time_sampling_beta_beta",
            "time_sampling_scale", "time_sampling_offset",
            "min_period", "max_period",
            "use_relative_actions", "relative_exclude_joints",
            "image_resolution", "empty_cameras",
            "gradient_checkpointing", "compile_model", "compile_mode",
            "freeze_vision_encoder", "train_expert_only",
            "optimizer_lr", "optimizer_betas", "optimizer_eps",
            "optimizer_weight_decay", "optimizer_grad_clip_norm",
            "scheduler_warmup_steps", "scheduler_decay_steps", "scheduler_decay_lr",
            "tokenizer_max_length",
        },
        "groot": {
            "n_obs_steps", "chunk_size", "n_action_steps",
            "max_state_dim", "max_action_dim",
            "image_size", "base_model_path", "tokenizer_assets_repo",
            "embodiment_tag",
            "tune_llm", "tune_visual", "tune_projector", "tune_diffusion_model",
            "lora_rank", "lora_alpha", "lora_dropout", "lora_full_model",
            "optimizer_lr", "optimizer_betas", "optimizer_eps", "optimizer_weight_decay",
            "warmup_ratio",
            "video_backend", "balance_dataset_weights", "balance_trajectory_weights",
            "dataset_paths", "dataloader_num_workers",
        },
    }
    # pi0_fast / pi05 字段集合跟 pi0 一致 (lerobot 上游设计如此, 都用 OpenPI 移植)
    POLICY_FIELDS["pi0_fast"] = POLICY_FIELDS["pi0"]
    POLICY_FIELDS["pi05"] = POLICY_FIELDS["pi0"]

    # 需要 tuple[int, int] 的字段 — 前端可能传 int 或 [int,int], _add_policy_param 自动 wrap.
    # 不处理的话 draccus 会抛 DecodingError("`image_size`: ... 'int' has no len()") 让训练 exit 1.
    # (model_type, real_key) → 维度 (2 = [H,W], 3 = [H,W,C])
    # 注意 model_type key 用 worker 内部归一化后的值: "gr00t" (前端) → "groot" (lerobot 上游)
    # 见 _normalize_model_type / __init__.py:14, 这里必须用 "groot" 才能匹配!
    _TUPLE_INT_FIELDS = {
        ("groot", "image_size"): 2,         # GrootConfig.image_size: tuple[int, int]
        ("diffusion", "crop_shape"): 2,     # DiffusionConfig.crop_shape: tuple[int, int] | None
        ("diffusion", "resize_shape"): 2,   # DiffusionConfig.resize_shape: tuple[int, int]
    }

    @classmethod
    def _wrap_tuple_int(cls, model_type: str, real_key: str, value):
        """前端传 int (如 224) 时, 把 value 自动转成 '[N,N]' (draccus 能解析的 JSON list).
        非 tuple 字段直接返回 value 不动.
        """
        dim = cls._TUPLE_INT_FIELDS.get((model_type, real_key))
        if dim is None:
            return value
        # 已经是 list/tuple 的, 转成 JSON 风格字符串让 draccus 解析
        if isinstance(value, (list, tuple)):
            try:
                items = [int(x) for x in value]
                if len(items) == dim:
                    return "[" + ",".join(str(x) for x in items) + "]"
            except (ValueError, TypeError):
                return value
            return value
        # 标量 (int / "224") → 复制成 dim 维
        try:
            v = int(str(value).strip())
            wrapped = "[" + ",".join([str(v)] * dim) + "]"
            logger.info("[%s] auto-wrap %s: %r → %s (tuple[int]*%d)",
                        model_type.upper(), real_key, value, wrapped, dim)
            return wrapped
        except (ValueError, TypeError):
            return value

    def _add_policy_param(cls, cmd: list, model_type: str, key: str, value) -> bool:
        """加 --policy.{key}={value} 到 cmd, 但只在该 model 实际支持时.

        前端 schema 可能给所有模型加了通用字段 (grad_clip_norm/dtype/...), 但有些 model
        config 没暴露这些字段 — 直接传会撞 draccus 'unrecognized arguments' 让训练 exit 2.
        本函数:
        1. 把前端 key 通过 PARAM_ALIASES 映射到真实 config 字段名 (如 lr→optimizer_lr)
        2. 用 POLICY_FIELDS[model_type] 校验, 不在白名单的静默跳过 + warning
        3. 对 tuple[int] 类字段 (如 gr00t.image_size) 自动 wrap (前端传 int 时复制成 [N,N])
        4. 通过则 append --policy.{真实字段}={value}

        Returns True if added, False if skipped.
        """
        real_key = cls.PARAM_ALIASES.get(key, key)
        fields = cls.POLICY_FIELDS.get(model_type)
        # 自动 wrap tuple 字段 (gr00t.image_size 等) — 在 white-list 检查前做, 让 wrap 后的值也能透传
        value = cls._wrap_tuple_int(model_type, real_key, value)
        if fields is None:
            # 未知 model_type — 兜底放行, 让 lerobot 自己报错
            cmd.append(f"--policy.{real_key}={value}")
            return True
        if real_key not in fields:
            logger.warning(
                "[%s] Skip param '%s' (→ '%s'): not in %s policy config (white-list)",
                model_type.upper(), key, real_key, model_type,
            )
            return False
        cmd.append(f"--policy.{real_key}={value}")
        return True

    def _ensure_quantile_stats(self, repo_id: str, datasets_root: Path, model_type: str):
        """如果模型用 QUANTILES normalization, 给 dataset 补算 q01/q99 stats.

        LeRobotDataset.create() 默认只算 mean/std/min/max, 不算 quantile.
        Pi05 的 normalizer 加载时会找 q01/q99 → 找不到崩.

        正解: 调上游 lerobot/scripts/augment_dataset_quantile_stats.py 的核心逻辑
        给本地 dataset 补算 stats (跳过其中的 push_to_hub 步骤 — 我们不上传 HF).
        如果 stats 已存在, has_quantile_stats() 短路跳过, 重复训练无开销.
        """
        if model_type not in self.QUANTILE_NORM_MODELS:
            return
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent / "lerobot" / "src"))
            from lerobot.scripts.augment_dataset_quantile_stats import (
                has_quantile_stats, compute_quantile_stats_for_dataset,
            )
            from lerobot.datasets import LeRobotDataset, write_stats

            logger.info("[%s] Checking dataset quantile stats (required for QUANTILES normalization)...",
                         model_type.upper())
            ds = LeRobotDataset(repo_id=repo_id, root=datasets_root)
            if has_quantile_stats(ds.meta.stats):
                logger.info("Dataset already has quantile stats, skip.")
                return

            logger.info("Computing quantile stats (q01/q10/q50/q90/q99) for %d episodes...",
                         ds.num_episodes)
            new_stats = compute_quantile_stats_for_dataset(ds)
            ds.meta.stats = new_stats
            write_stats(new_stats, ds.meta.root)
            logger.info("Quantile stats written to %s/meta/stats.json", ds.meta.root)
        except Exception as e:
            # 不让 quantile 计算失败阻塞训练 — 如果失败, 仍可继续 (lerobot 会在 normalizer
            # 加载时给出明确报错, 用户能看到), 但记录 warning 让用户知道.
            logger.warning("Failed to compute quantile stats for %s: %s. "
                            "Pi05 训练可能崩 (找不到 q01/q99). "
                            "可手动: python lerobot/src/lerobot/scripts/augment_dataset_quantile_stats.py "
                            "--repo-id=%s --root=%s",
                            repo_id, e, repo_id, datasets_root)

    @staticmethod
    def _diagnose_subprocess_error(returncode: int, tail_lines: list) -> str:
        """根据 subprocess 退出码 + 最后几行 stdout 给出友好错误描述.

        Linux 信号: -N (Python) 或 128+N (POSIX). 关键信号:
          -2  / 130 = SIGINT  (Ctrl+C 或父进程转发的中断)
          -9  / 137 = SIGKILL (大概率 OOM-killer 杀的, 也可能用户 kill -9)
          -11 / 139 = SIGSEGV (内存越界, 通常是 CUDA 驱动/torch 不兼容)
          -15 / 143 = SIGTERM (被外部 terminate)
          1         = 通用错误 (Python 异常等)
        """
        # 1. 信号类错误 — 直接返回, 不再走关键字扫描.
        # 原因: 进程被强杀时 Python traceback 末尾常出现 "lerobot.datasets" / "datasets is required"
        # 这种字符串, 会被关键字扫描误判成"缺 av/datasets 库", 给用户错误的修复建议.
        # 信号类终止本质是外部干预, 不是依赖问题, 直接返回明确的描述.
        if returncode in (-2, 130):
            return (f"训练失败 (exit code {returncode})\n"
                    "训练子进程被 SIGINT 终止 (Ctrl+C)\n"
                    "通常是 Worker 进程被用户手动中断 (Ctrl+C / kill -2 / 关闭终端).")
        if returncode in (-9, 137):
            return (f"训练失败 (exit code {returncode})\n"
                    "训练子进程被 SIGKILL 终止\n"
                    "可能原因: OOM-killer (系统内存不足) / 用户 kill -9 / 容器被强制回收.")
        if returncode in (-11, 139):
            return (f"训练失败 (exit code {returncode})\n"
                    "训练子进程段错误 (SIGSEGV)\n"
                    "通常是 CUDA 驱动 / torch 版本不兼容, 或 GPU 硬件异常.")
        if returncode in (-15, 143):
            return (f"训练失败 (exit code {returncode})\n"
                    "训练子进程被 SIGTERM 终止 (外部 kill)\n"
                    "通常是 Worker 进程被关闭 / 系统重启 / kill 命令.")

        # 2. 关键字扫描 (最后 80 行 stdout) — 仅在非信号类终止时进行
        sig_msg = ""
        joined = "\n".join(tail_lines).lower()
        kw_hint = ""
        if "out of memory" in joined or "cuda out of memory" in joined:
            kw_hint = (
                "GPU 显存不足 (OOM). 解决方案:\n"
                "  - 减小 batch_size (当前任务批大小过大)\n"
                "  - VLA 模型加 --policy.gradient_checkpointing=true (默认已开)\n"
                "  - VLA 加 --policy.train_expert_only=true 冻结 VLM 主干\n"
                "  - 换更大显存的 GPU (pi05 推荐 16GB+)"
            )
        elif "modulenotfounderror" in joined or "importerror" in joined:
            # 提取模块名 — peft / lerobot.datasets / 其它分别给针对性指令
            import re as _re
            m = _re.search(r"(?:no module named|cannot import name)\s+'?([^'\s]+)'?", joined)
            mod = m.group(1) if m else "依赖库"
            mod_lower = mod.lower()
            if "peft" in mod_lower:
                kw_hint = (
                    "LoRA 微调缺 PEFT 库. 解决方案:\n"
                    "  - pip install peft accelerate\n"
                    "  - 或装 lerobot 全套: pip install \"lerobot[peft] @ file:./lerobot\" --no-build-isolation\n"
                    "  - 不想用 LoRA: APP 训练页关闭 'LoRA 微调' 开关"
                )
            elif "lerobot.datasets" in mod_lower or ("'datasets' is required" in joined):
                # lerobot/src/lerobot/datasets/__init__.py 顶部 require_package("datasets")
                # require_package("av"), 缺 datasets 或 av 都会让 lerobot.datasets import 失败.
                kw_hint = (
                    "lerobot.datasets 加载失败 — 缺 HuggingFace datasets 库或 av (PyAV).\n"
                    "  - pip install datasets av\n"
                    "  - 或装全套: pip install \"lerobot[dataset] @ file:./lerobot\" --no-build-isolation\n"
                    "  - 验证: python -c \"import datasets, av; print(datasets.__version__, av.__version__)\""
                )
            elif "av" == mod_lower or "'av' is required" in joined:
                kw_hint = (
                    "缺 PyAV (视频解码). 解决: pip install \"av>=15.0.0,<16.0.0\""
                )
            else:
                kw_hint = (
                    f"缺少 Python 依赖: {mod}. 解决方案:\n"
                    f"  - 跑一遍体检: python scripts/check_gpu.py\n"
                    f"  - 重装 dataset 依赖: pip install \"lerobot[dataset] @ file:./lerobot\" --no-build-isolation\n"
                    f"  - 或重装 worker: cd box2robot_gpu_worker && pip install -e . --upgrade"
                )
        elif "filenotfounderror" in joined and "checkpoint" in joined:
            kw_hint = "找不到 checkpoint 文件 — resume 路径可能错误, 或上次训练未保存任何 ckpt."
        elif "huggingfacehub" in joined and ("timeout" in joined or "connection" in joined):
            kw_hint = (
                "HuggingFace Hub 下载失败 (网络问题). 解决方案:\n"
                "  - 检查网络 / 代理\n"
                "  - 国内可设 HF_ENDPOINT=https://hf-mirror.com 后重启 worker"
            )
        elif "config.json not found on the huggingface hub" in joined:
            # lerobot policies.py:202 把 LocalEntryNotFoundError (offline+无缓存) 包成这条
            # 误导性消息; 真正原因是 HF_HUB_OFFLINE=1 + base 模型未预下载. 主进程的
            # _ensure_vla_base_cached 应该已经预下了, 走到这里说明该方法被跳过或网络一直挂.
            kw_hint = (
                "VLA base 模型未预下载到 HF cache, 训练子进程离线模式下找不到 config.json.\n"
                "  - 主 worker 进程网络是否通: curl -I $HF_ENDPOINT\n"
                "  - AutoDL: source /etc/network_turbo 启用学术加速\n"
                "  - 国内: 设 HF_ENDPOINT=https://hf-mirror.com\n"
                "  - 手动预下: huggingface-cli download lerobot/smolvla_base\n"
                "  - 检查 HF_HOME 磁盘空间 (smolvla ~2GB / pi0 ~6GB / pi05 ~14GB)"
            )
        elif ("we couldn't connect to" in joined and "huggingface" in joined) \
                or ("oserror" in joined and "couldn't find them in the cached files" in joined):
            # transformers/utils/hub.py 在 OFFLINE=1 + cache 缺失时的报错文案.
            # 多见于 SmolVLA 的 VLM 主干 (HuggingFaceTB/SmolVLM2-500M-Video-Instruct)
            # 没被预下到 cache. _ensure_vla_base_cached 会同时拉 base + VLM 依赖,
            # 走到这里说明 VLM_DEPS 没覆盖到, 或主进程 HF 不通.
            kw_hint = (
                "transformers 加载模型时找不到 cache 又联不上 HF.\n"
                "  通常是 SmolVLA 的 VLM 主干 (SmolVLM2-500M-Video-Instruct) 没预下载.\n"
                "  - 主 worker 进程网络是否通: curl -I $HF_ENDPOINT\n"
                "  - AutoDL: source /etc/network_turbo\n"
                "  - 手动预下 VLM: huggingface-cli download HuggingFaceTB/SmolVLM2-500M-Video-Instruct\n"
                "  - 检查 HF_HOME 磁盘空间 (SmolVLM2 ~1GB)"
            )
        elif "all image features are missing" in joined:
            kw_hint = (
                "数据集图像 key 跟模型期望不一致. 应该被自动 rename_map 修复, "
                "若仍报错请检查 worker 日志中 'VLA rename_map' 行."
            )
        elif "quantile" in joined:
            kw_hint = (
                "Quantile stats 缺失 (pi05 用 QUANTILES normalization). "
                "应被自动 augment 修复, 若仍报错可手动跑: "
                "python lerobot/src/lerobot/scripts/augment_dataset_quantile_stats.py "
                "--repo-id=<repo> --root=<datasets_root>"
            )
        # === LoRA / PEFT 相关报错 ===
        elif "target modules" in joined and ("not found" in joined or "no modules" in joined):
            kw_hint = (
                "LoRA target_modules 配置错误 — 指定的层在 base 模型里不存在.\n"
                "  - 留空 (peft_target_modules='') 让 lerobot 用 policy 默认\n"
                "  - 或正则匹配真实层名后缀, 例: '.*\\.q_proj|.*\\.v_proj'\n"
                "  - SmolVLA 默认: q_proj/v_proj of lm_expert + state/action proj"
            )
        elif "peftconfig" in joined or ("invalid" in joined and "peft" in joined):
            kw_hint = (
                "PEFT 配置错误. 解决方案:\n"
                "  - peft_method_type 必须是 LORA (大写), 其它方法 (PREFIX_TUNING/IA3) 暂不支持\n"
                "  - peft_r 必须是正整数 (4~256 之间)\n"
                "  - 关 LoRA 试试: APP 训练页关闭 'LoRA 微调' 开关"
            )
        elif "lora" in joined and ("rank" in joined or "alpha" in joined) and "error" in joined:
            kw_hint = (
                "LoRA rank/alpha 参数异常.\n"
                "  - peft_r 推荐 8/16/32/64 (8 的倍数), 当前值可能太大\n"
                "  - 8GB GPU r ≤ 16; 24GB GPU r ≤ 64"
            )
        elif "adapter_config" in joined or ("loading" in joined and "adapter" in joined):
            kw_hint = (
                "PEFT adapter checkpoint 加载失败 (推理或 resume 时).\n"
                "  - 检查 model_path 含 adapter_config.json 和 adapter_model.safetensors\n"
                "  - LoRA fine-tune 的 ckpt 不能直接当 base 模型加载, 需要先 merge_and_unload"
            )

        # 3. 拼最终信息: 关键字提示 + 最后 15 行原始日志
        # (信号类终止已在第 1 步直接 return, 这里 sig_msg 必空, 不再拼接)
        # P2-10 (2026-05-21): tail 从 5 行扩到 15 行 — 真正能看到 traceback 顶部
        # File "xxx.py", line N, in <fn> 等 root cause 行; 之前 5 行只显示尾部
        # "RuntimeError: xxx" 没有调用栈, 用户看不出真因.
        parts = [f"训练失败 (exit code {returncode})"]
        if kw_hint:
            parts.append(kw_hint)
        if tail_lines:
            tail = tail_lines[-15:]
            # 行数标识 (让用户知道这是 stderr 尾部, 不是完整 log)
            label = f"最后 {len(tail)} 行 stderr (完整 log 在 worker 端):"
            parts.append(label + "\n  " + "\n  ".join(tail))
        return "\n".join(parts)

    @staticmethod
    def _preflight_rename_map(pretrained_path: str, rename_map_dict: dict, lerobot_src: Path) -> None:
        """启动训练前预检 rename_map 是否真能生效.

        分两步在子进程里跑 (避免污染 worker 进程的 import):
        1. draccus 解析 cfg.rename_map (用我们传的 --rename_map=... arg)
        2. PolicyProcessorPipeline.from_pretrained(..., overrides) 后,
           检查 rename_observations_processor 步骤的 rename_map 是不是我们想要的

        任一失败 → 大声 logger.error 但不抛 (训练继续, 让真实错误暴露具体细节).
        """
        import os as _os
        import subprocess as _sp
        rename_str = json.dumps(rename_map_dict)
        logger.info("[PREFLIGHT] 开始 rename_map 预检 (base=%s)", pretrained_path)

        # Step 1: draccus dict[str,str] 解析
        py = "\n".join([
            "import json, draccus",
            "from dataclasses import dataclass, field",
            "from typing import Dict",
            "@dataclass",
            "class _T:",
            "    rename_map: Dict[str, str] = field(default_factory=dict)",
            f"args=[{json.dumps('--rename_map=' + rename_str)}]",
            "cfg=draccus.parse(config_class=_T, args=args)",
            "print('[PREFLIGHT-DRACCUS] cfg.rename_map =', json.dumps(cfg.rename_map))",
        ])
        env = {**_os.environ, "PYTHONUNBUFFERED": "1"}
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
        env.setdefault("HF_DATASETS_OFFLINE", "1")
        if lerobot_src.exists():
            old_pp = env.get("PYTHONPATH", "")
            sep = ";" if _os.name == "nt" else ":"
            env["PYTHONPATH"] = (
                f"{lerobot_src}{sep}{old_pp}" if old_pp else str(lerobot_src)
            )
        try:
            r = _sp.run([sys.executable, "-c", py], capture_output=True, text=True,
                        env=env, timeout=30)
            for line in (r.stdout or "").splitlines():
                logger.info("[PREFLIGHT] %s", line)
            for line in (r.stderr or "").splitlines():
                if line.strip():
                    logger.warning("[PREFLIGHT-stderr] %s", line)
            if r.returncode != 0:
                logger.error("[PREFLIGHT] draccus 解析失败 (rc=%d) — 真训练时 cfg.rename_map "
                             "也会丢失, 必然走到 'All image features are missing'!",
                             r.returncode)
                return
        except Exception as e:
            logger.warning("[PREFLIGHT] draccus 检查跳过: %s", e)

        # Step 2: 用 from_pretrained + overrides 实际加载 preprocessor, 看 rename 步骤
        # 是不是真的拿到我们的 rename_map. 这一步会下载 (如果未缓存) policy_preprocessor.json,
        # 比较慢但是最直接的验证.
        py2 = "\n".join([
            "import json",
            # 必须先 import policy 模块, 否则 pi05_prepare_state_tokenizer_processor_step
            # 等专属 step 不会注册到 ProcessorStepRegistry, from_pretrained 会 KeyError
            "import lerobot.policies.pi05.processor_pi05  # noqa",
            "import lerobot.policies.pi0.processor_pi0  # noqa",
            "import lerobot.policies.pi0_fast.processor_pi0_fast  # noqa",
            "import lerobot.policies.smolvla.processor_smolvla  # noqa",
            "from lerobot.processor.pipeline import PolicyProcessorPipeline",
            "from lerobot.processor import batch_to_transition, transition_to_batch",
            f"overrides = {{'rename_observations_processor': {{'rename_map': {json.dumps(rename_map_dict)}}}}}",
            "pp = PolicyProcessorPipeline.from_pretrained(",
            f"    pretrained_model_name_or_path={json.dumps(pretrained_path)},",
            "    config_filename='policy_preprocessor.json',",
            "    overrides=overrides,",
            "    to_transition=batch_to_transition,",
            "    to_output=transition_to_batch,",
            ")",
            "for s in pp.steps:",
            "    cls_name = type(s).__name__",
            "    if cls_name == 'RenameObservationsProcessorStep':",
            "        print('[PREFLIGHT-RENAME-STEP] rename_map =', json.dumps(s.rename_map))",
            "    else:",
            "        print('[PREFLIGHT-STEP]', cls_name)",
            # 真正模拟 dataloader 给的 batch, 跑一遍 preprocessor, 看 key 是否被改了
            "import torch",
            "fake_batch = {",
            "    'observation.images.wrist': torch.zeros(3, 480, 640, dtype=torch.uint8),",
            "    'observation.state': torch.zeros(6),",
            "    'task': 'preflight test',",
            "    'action': torch.zeros(6),",
            "}",
            "try:",
            "    out = pp(fake_batch)",
            "    print('[PREFLIGHT-AFTER-PREPROCESS] keys =', sorted(out.keys()))",
            "except Exception as e:",
            "    print('[PREFLIGHT-AFTER-PREPROCESS] ERROR:', type(e).__name__, str(e)[:200])",
        ])
        try:
            r = _sp.run([sys.executable, "-c", py2], capture_output=True, text=True,
                        env=env, timeout=120)
            for line in (r.stdout or "").splitlines():
                if "[PREFLIGHT" in line:
                    logger.info("[PREFLIGHT] %s", line)
            for line in (r.stderr or "").splitlines():
                if line.strip() and ("Warning" not in line):
                    logger.warning("[PREFLIGHT-stderr] %s", line[:200])
            if r.returncode != 0:
                logger.error("[PREFLIGHT] from_pretrained+overrides 失败 (rc=%d) — "
                             "真训练时也会失败. stderr 末尾: %s",
                             r.returncode, (r.stderr or "")[-500:])
        except Exception as e:
            logger.warning("[PREFLIGHT] from_pretrained 检查跳过: %s", e)
        logger.info("[PREFLIGHT] 完成")

    @classmethod
    def _get_base_visual_keys(cls, pretrained_path: str) -> list:
        """获取 base 期望的相机 key 列表 (用于自动构造 --rename_map).

        优先级:
        1. 本地路径 → 读 config.json
        2. HF Hub 下载 config.json (在线节点)
        3. KNOWN_BASE_VISUAL_KEYS 硬编码兜底 (离线节点 / HF 不可达)

        VLA base 的相机 key 通常和我们 Box2Robot dataset 的 'observation.images.wrist'
        不一样, 直接训会抛 "All image features are missing from the batch". 拿到 base
        期望的 key 列表后, 外层用 --rename_map 把 dataset 的 wrist 映射到 base 第一个 cam,
        其余 base cam 会被 modeling.prepare_images 自动用 -1 填充 (siglip empty camera).

        Returns: list of visual key names. 全部失败才返回 [].
        """
        import os as _os
        # 1+2: 文件 / HF 下载
        try:
            if _os.path.isdir(pretrained_path):
                cfg_path = _os.path.join(pretrained_path, "config.json")
                if not _os.path.isfile(cfg_path):
                    logger.warning("Local base path %s has no config.json", pretrained_path)
                    cfg_path = None
            else:
                # HF hub repo_id like "lerobot/pi05_base"
                from huggingface_hub import hf_hub_download
                cfg_path = hf_hub_download(repo_id=pretrained_path, filename="config.json")
            if cfg_path:
                with open(cfg_path, encoding="utf-8") as f:
                    base_cfg = json.load(f)
                feats = base_cfg.get("input_features", {}) or {}
                visual_keys = [k for k, v in feats.items() if (v or {}).get("type") == "VISUAL"]
                if visual_keys:
                    return visual_keys
        except Exception as e:
            logger.warning("Failed to read base visual keys from %s: %s", pretrained_path, e)

        # 3: 硬编码兜底 — 关键场景: 离线 GPU 节点拿不到 HF
        fallback = cls.KNOWN_BASE_VISUAL_KEYS.get(pretrained_path)
        if fallback:
            logger.warning(
                "Using hardcoded fallback visual keys for %s: %s",
                pretrained_path, fallback,
            )
            return list(fallback)
        return []

    # VLA base 间接依赖的 VLM/backbone repo (lerobot/xxx_base 之外还要从 HF 拉的)
    # smolvla = SmolVLM2 + action expert; base 包只有 expert, VLM 走单独 repo.
    # pi0/pi05 的 gemma 权重已经塞在 base 包里, 不需要额外拉.
    VLA_VLM_DEPS = {
        "lerobot/smolvla_base": ["HuggingFaceTB/SmolVLM2-500M-Video-Instruct"],
    }

    @staticmethod
    def _hf_snapshot_or_raise(repo_id: str, *, allow_patterns: list, label: str) -> str:
        """主进程在线状态拉一个 HF repo snapshot. 临时撤销 OFFLINE env, 失败抛
        RuntimeError 让上层把 job 标 failed + 给清晰错误.
        """
        import os as _os
        from huggingface_hub import snapshot_download
        prev_offline = _os.environ.pop("HF_HUB_OFFLINE", None)
        prev_tf_offline = _os.environ.pop("TRANSFORMERS_OFFLINE", None)
        prev_ds_offline = _os.environ.pop("HF_DATASETS_OFFLINE", None)
        hf_home = _os.environ.get("HF_HOME") or _os.path.expanduser("~/.cache/huggingface")
        endpoint = _os.environ.get("HF_ENDPOINT", "https://huggingface.co")
        logger.info("[VLA-CACHE] %s 预下: repo=%s endpoint=%s HF_HOME=%s",
                    label, repo_id, endpoint, hf_home)
        try:
            local_dir = snapshot_download(
                repo_id=repo_id,
                allow_patterns=allow_patterns,
            )
            logger.info("[VLA-CACHE] %s OK → %s", label, local_dir)
            return local_dir
        except Exception as e:
            logger.error("[VLA-CACHE] %s snapshot_download 失败: %s", label, e)
            raise RuntimeError(
                f"{label} '{repo_id}' 下载失败: {e}\n"
                f"  HF_ENDPOINT={endpoint}\n"
                f"  HF_HOME={hf_home}\n"
                f"  解决方案:\n"
                f"  1. 检查 GPU 节点网络 (curl -I {endpoint})\n"
                f"  2. AutoDL 实例: source /etc/network_turbo 启用学术加速\n"
                f"  3. 国内可设 HF_ENDPOINT=https://hf-mirror.com 后重启 worker\n"
                f"  4. 手动预下: huggingface-cli download {repo_id}"
            ) from e
        finally:
            if prev_offline is not None:
                _os.environ["HF_HUB_OFFLINE"] = prev_offline
            if prev_tf_offline is not None:
                _os.environ["TRANSFORMERS_OFFLINE"] = prev_tf_offline
            if prev_ds_offline is not None:
                _os.environ["HF_DATASETS_OFFLINE"] = prev_ds_offline

    @classmethod
    def _ensure_vla_base_cached(cls, pretrained_path: str) -> None:
        """确保 VLA base (config.json + 权重 + tokenizer + VLM 主干) 已下载到 HF cache.

        训练子进程会被强制设 HF_HUB_OFFLINE=1 (避免训练中途调 model_info() 崩),
        所以必须在主 worker 进程 (在线) 把 base 整套预下到 cache. 否则首次训练
        SmolVLA / Pi0 时子进程立刻抛:
            FileNotFoundError: config.json not found on the HuggingFace Hub in lerobot/smolvla_base
            OSError: We couldn't connect to 'https://huggingface.co' to load the files...
        实际是 LocalEntryNotFoundError (offline + 不在 cache) 被 lerobot/transformers
        包装成看起来像"Hub 上没这个文件 / 没网"的误导性错误.

        本地路径 (含 / 或 \\) 跳过. HF repo_id 才下载. snapshot_download 已缓存
        会立刻返回 (秒级).

        SmolVLA 特殊处理: 还要拉 VLM 主干 (HuggingFaceTB/SmolVLM2-500M-Video-Instruct).
        smolvla_base 只有 action expert 权重, 训练时 transformers 还要从 vlm_model_name
        拉 VLM tokenizer + config + 权重, 不预下也会因 OFFLINE=1 崩.
        """
        import os as _os
        if not pretrained_path:
            return
        # 本地绝对/相对路径 (含 config.json 的目录) 不需要拉
        if _os.path.isabs(pretrained_path) or _os.sep in pretrained_path \
                or _os.path.isdir(pretrained_path):
            logger.info("[VLA-CACHE] 本地路径, 跳过 snapshot_download: %s", pretrained_path)
            return
        try:
            from huggingface_hub import snapshot_download  # noqa: F401  (检测可用性)
        except Exception as e:
            logger.warning("[VLA-CACHE] huggingface_hub 不可用: %s — 子进程在线模式兜底", e)
            return

        # Step 1: 拉 VLA base 包本身 (action expert 权重 + config + tokenizer)
        cls._hf_snapshot_or_raise(
            pretrained_path,
            allow_patterns=[
                "*.json", "*.safetensors", "*.bin", "*.model",
                "tokenizer*", "*.txt", "*.py",
            ],
            label="VLA base",
        )

        # Step 2: 读 base 的 config.json, 找 vlm_model_name (smolvla 必有, 其它 VLA 可能没有)
        vlm_deps = list(cls.VLA_VLM_DEPS.get(pretrained_path, []))
        try:
            from huggingface_hub import hf_hub_download
            cfg_path = hf_hub_download(repo_id=pretrained_path, filename="config.json")
            with open(cfg_path, encoding="utf-8") as f:
                base_cfg = json.load(f)
            dynamic_vlm = base_cfg.get("vlm_model_name", "")
            if dynamic_vlm and dynamic_vlm not in vlm_deps:
                vlm_deps.append(dynamic_vlm)
        except Exception as e:
            # base 没 vlm_model_name 字段 (pi0/pi05) 或读不到 - 走硬编码 deps 即可
            logger.debug("[VLA-CACHE] 读 base config.json 拿 vlm_model_name 失败: %s", e)

        # Step 3: 拉所有间接依赖 (VLM 主干 + tokenizer)
        for vlm_repo in vlm_deps:
            cls._hf_snapshot_or_raise(
                vlm_repo,
                # VLM 主干: 配置 + 权重 + tokenizer; 不下示例图省带宽
                allow_patterns=[
                    "*.json", "*.safetensors", "*.bin", "*.model",
                    "tokenizer*", "*.txt", "vocab*", "merges*", "added_tokens*",
                    "special_tokens*", "preprocessor_config*",
                ],
                label="VLM 主干",
            )

    @classmethod
    def _ensure_groot_eagle_assets(cls, assets_repo: str) -> None:
        """GR00T 训练时 lerobot 内部 ensure_eagle_cache_ready 会从 HF 下 11 个
        tokenizer/processor assets, 但子进程 HF_HUB_OFFLINE=1 下不动. 这里主进程
        (在线) 预下到 $HF_HOME/lerobot/<assets_repo>/.

        路径计算 (跟 lerobot/utils/constants.py + groot/groot_n1.py 对齐):
            HF_LEROBOT_HOME = HF_HOME / "lerobot"
            cache_dir       = HF_LEROBOT_HOME / assets_repo
                            = $HF_HOME/lerobot/lerobot/eagle2hg-processor-groot-n1p5/
        (路径里 lerobot/lerobot/ 重复是 lerobot 的 cache 子目录 + repo_id 含 'lerobot/' org 前缀, 不是 bug)
        """
        import os as _os
        if not assets_repo:
            return
        try:
            from huggingface_hub import hf_hub_download
        except Exception as e:
            logger.warning("[GROOT-EAGLE] huggingface_hub 不可用: %s", e)
            return
        hf_home = _os.environ.get("HF_HOME") or _os.path.expanduser("~/.cache/huggingface")
        target_dir = _os.path.join(hf_home, "lerobot", assets_repo)
        # cache 已完整 → 跳过下载, 但 **仍要走 config.json patch** (旧 cache 可能是 unpatched
        # 的 flash_attention_2 版本; 早期 return 会让推理路径永远拿不到 patch).
        cache_ready = (_os.path.isfile(_os.path.join(target_dir, "config.json"))
                       and _os.path.isfile(_os.path.join(target_dir, "vocab.json")))
        if cache_ready:
            logger.info("[GROOT-EAGLE] cache 已完整, 跳过预下: %s", target_dir)
        _os.makedirs(target_dir, exist_ok=True)
        # 临时撤销 OFFLINE env (主进程必须能联网)
        prev_offline = _os.environ.pop("HF_HUB_OFFLINE", None)
        prev_tf_offline = _os.environ.pop("TRANSFORMERS_OFFLINE", None)
        endpoint = _os.environ.get("HF_ENDPOINT", "https://huggingface.co")
        _os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # 大文件走 LFS, 防 401
        # 跟 lerobot/policies/groot/utils.py::ensure_eagle_cache_ready 必下清单一致
        ASSETS = [
            "vocab.json", "merges.txt", "added_tokens.json", "chat_template.json",
            "special_tokens_map.json", "config.json", "generation_config.json",
            "preprocessor_config.json", "processor_config.json", "tokenizer_config.json",
        ]
        logger.info("[GROOT-EAGLE] 预下 eagle assets: repo=%s endpoint=%s target=%s",
                    assets_repo, endpoint, target_dir)
        try:
            for fname in ASSETS:
                dst = _os.path.join(target_dir, fname)
                if _os.path.isfile(dst):
                    continue
                try:
                    hf_hub_download(repo_id=assets_repo, filename=fname,
                                    repo_type="model", local_dir=target_dir)
                    logger.info("[GROOT-EAGLE]   OK %s", fname)
                except Exception as e:
                    # 个别文件不存在 (如 tokenizer.json 在某些版本是 404) → SKIP, 不阻塞
                    logger.warning("[GROOT-EAGLE]   SKIP %s: %s", fname,
                                   type(e).__name__)
            # patch config.json: HF 上 _attn_implementation="flash_attention_2", 但 worker
            # 环境通常没装 flash_attn (装编译麻烦, 需 nvcc + torch 版本严格对齐).
            # 改成 "sdpa" (PyTorch 内置 Scaled Dot-Product Attention, 所有 GPU 都支持).
            # 外层 + text_config + vision_config 三处都要 patch (transformers 在 PreTrainedModel
            # 加载时 check 外层; Eagle25VL 内部加载 Qwen2 / Siglip 时 check 嵌套子配置).
            cfg_path = _os.path.join(target_dir, "config.json")
            if _os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, encoding="utf-8") as f:
                        cfg = json.load(f)
                    patched = False
                    if cfg.get("_attn_implementation") == "flash_attention_2":
                        cfg["_attn_implementation"] = "sdpa"
                        patched = True
                    tc = cfg.get("text_config")
                    if isinstance(tc, dict) and tc.get("_attn_implementation") == "flash_attention_2":
                        tc["_attn_implementation"] = "sdpa"
                        patched = True
                    vc = cfg.get("vision_config")
                    if isinstance(vc, dict) and vc.get("_attn_implementation") == "flash_attention_2":
                        vc["_attn_implementation"] = "sdpa"
                        patched = True
                    if patched:
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            json.dump(cfg, f, ensure_ascii=False, indent=2)
                        logger.info("[GROOT-EAGLE] patched config.json: "
                                    "_attn_implementation flash_attention_2 → sdpa "
                                    "(worker 没装 flash_attn, sdpa 是 PyTorch 内置兜底)")
                except Exception as e:
                    logger.warning("[GROOT-EAGLE] patch config.json 失败: %s", e)
            # === 2026-05-21 P2: patch HF dynamic modules 路径 ===
            # transformers 用 trust_remote_code=True 加载 GR00T 时, 真正 import 的不是
            # vendor 也不是 snapshot cache, 而是 ${HF_HOME}/modules/transformers_modules/
            # <repo_safe>/ 下的 .py 文件 (HF 把 - 转成 _hyphen_, / 转 _ 后写到这).
            # 这些文件是 HF 上的原版 (flash_attention_2 硬编码), 之前的 patch 全部走偏.
            # 这里用本地 vendor 已 patched 的两个 .py 覆盖过去, idempotent.
            try:
                cls._patch_hf_modules_groot_attn(hf_home)
            except Exception as e:
                logger.warning("[GROOT-EAGLE] patch HF modules 失败: %s", e)
        finally:
            if prev_offline is not None:
                _os.environ["HF_HUB_OFFLINE"] = prev_offline
            if prev_tf_offline is not None:
                _os.environ["TRANSFORMERS_OFFLINE"] = prev_tf_offline

    @classmethod
    def _patch_hf_modules_groot_attn(cls, hf_home: str) -> None:
        """把本地 vendor 已 patched 的 configuration_eagle2_5_vl.py +
        modeling_eagle2_5_vl.py 拷贝到 HF dynamic modules 路径
        (${HF_HOME}/modules/transformers_modules/<eagle2*groot*>/).

        transformers 用 trust_remote_code=True 加载 GR00T 时, 从这个路径动态
        import .py — 这才是真正生效的源码. 之前修 vendor / snapshot cache 都不生效
        (transformers 不读那两个路径).

        idempotent: 每次启动都跑, 文件内容相同则 noop; pyc cache 会清掉.

        Args:
            hf_home: HF_HOME 路径, modules 目录在 $hf_home/modules/transformers_modules/
        """
        import os as _os
        import shutil as _shutil
        modules_root = _os.path.join(hf_home, "modules", "transformers_modules")
        if not _os.path.isdir(modules_root):
            logger.info("[GROOT-EAGLE] HF modules 路径不存在, 跳过 patch (transformers "
                        "尚未首次加载该模型): %s", modules_root)
            return
        # 找匹配 eagle2*groot* 的子目录 (HF 把 lerobot/eagle2hg-processor-groot-n1p5
        # 转成 eagle2hg_hyphen_processor_hyphen_groot_hyphen_n1p5; 可能多个版本共存)
        candidates = [d for d in _os.listdir(modules_root)
                      if "eagle2" in d.lower() and "groot" in d.lower()
                      and _os.path.isdir(_os.path.join(modules_root, d))]
        if not candidates:
            logger.info("[GROOT-EAGLE] HF modules 下没找到 eagle2*groot* 目录, 跳过")
            return
        # vendor patched .py 源路径 (worker.py 同 git repo 下的 lerobot submodule)
        # worker.py: <repo>/box2robot_gpu_worker/box2robot_gpu_worker/worker.py
        # vendor:    <repo>/box2robot_gpu_worker/lerobot/src/lerobot/policies/groot/eagle2_hg_model/
        worker_file = _os.path.abspath(__file__)
        repo_subroot = _os.path.dirname(_os.path.dirname(worker_file))  # .../box2robot_gpu_worker
        vendor_dir = _os.path.join(repo_subroot, "lerobot", "src", "lerobot",
                                    "policies", "groot", "eagle2_hg_model")
        files_to_copy = ["configuration_eagle2_5_vl.py", "modeling_eagle2_5_vl.py"]
        for fname in files_to_copy:
            src = _os.path.join(vendor_dir, fname)
            if not _os.path.isfile(src):
                logger.warning("[GROOT-EAGLE] vendor patched 源文件不存在, 跳过: %s", src)
                continue
            # 简单检查 vendor 文件已 patched (含 sdpa 字串)
            try:
                with open(src, encoding="utf-8") as f:
                    head = f.read(8192)
                if "sdpa" not in head:
                    logger.warning("[GROOT-EAGLE] vendor %s 看起来未 patched (无 sdpa "
                                   "字串), 跳过覆盖防破坏", fname)
                    continue
            except Exception as e:
                logger.warning("[GROOT-EAGLE] 读 vendor %s 异常: %s", fname, e)
                continue
            for cand in candidates:
                dst = _os.path.join(modules_root, cand, fname)
                # 已是 patched 版 (内容相同) → noop, 不刷时间戳避免触发 transformers
                # 重 import 检查机制
                try:
                    if _os.path.isfile(dst):
                        with open(dst, encoding="utf-8") as f:
                            dst_head = f.read(8192)
                        if dst_head == head:
                            continue  # 已经是 patched 版
                    # 备份再覆盖 (备份文件名固定, 每次启动覆盖同一个 .bak 防累积)
                    if _os.path.isfile(dst):
                        try:
                            _shutil.copy2(dst, dst + ".orig.bak")
                        except Exception:
                            pass
                    _shutil.copy2(src, dst)
                    logger.info("[GROOT-EAGLE] 覆盖 HF modules .py → %s", dst)
                    # 清 pyc cache (旧编译产物会让 Python 跳过 .py 直接用)
                    pycache = _os.path.join(modules_root, cand, "__pycache__")
                    if _os.path.isdir(pycache):
                        try:
                            _shutil.rmtree(pycache)
                            logger.info("[GROOT-EAGLE] 清 pyc cache: %s", pycache)
                        except Exception as e:
                            logger.warning("[GROOT-EAGLE] 清 pyc cache 失败: %s", e)
                except Exception as e:
                    logger.warning("[GROOT-EAGLE] 覆盖 %s 失败: %s", dst, e)

    def _train_lingbot_vla(self, trajectories, model_dir,
                            train_steps, batch_size, custom_params, progress_cb,
                            ds_fingerprint: str = None):
        """LingBot-VLA 4B 训练入口 — 走独立 b2r-vla env + lingbot-vla 仓库.

        Pipeline:
          1. 复用 process_job 已下到 cache/ds_<fp>/dataset/ 的轨迹 JSON
          2. 调 convert.py 转 LeRobot v3 dataset (输出到 cache/ds_<fp>/lingbot_dataset/)
          3. 调 lingbot_vla_trainer.train_lingbot_vla (subprocess 走 b2r-vla env)

        前置条件: 目标实例必须跑过 scripts/install_lingbot_vla_addon.sh (装 b2r-vla env + repo).
        没装直接 raise (manager 收到失败 stage, server 标 failed, 用户看 error_msg).
        """
        import os as _os
        from pathlib import Path as _Path
        from box2robot_gpu_worker.convert import convert as _convert
        from box2robot_gpu_worker.lingbot_vla_trainer import train_lingbot_vla as _train_lvla

        if not ds_fingerprint:
            ds_fingerprint = self._ds_fingerprint([t.get("id", "") for t in trajectories])
        ds_cache_dir = _Path(__file__).parent.parent / "cache" / f"ds_{ds_fingerprint}"
        traj_json_dir = ds_cache_dir / "dataset"           # process_job 已写入 traj_*.json
        img_base = ds_cache_dir / "images"
        lingbot_ds_dir = ds_cache_dir / "lingbot_dataset"  # convert 输出目录 (跟 _train_lerobot 的 datasets/ 区分)

        # Step 1: convert → LeRobot v3 (lingbot-vla 要求 v3 格式)
        has_images = img_base.is_dir() and any(img_base.iterdir())
        task_desc = str(custom_params.get("task") or "manipulation task")
        if not lingbot_ds_dir.exists() or not (lingbot_ds_dir / "meta" / "info.json").is_file():
            logger.info("[LINGBOT-VLA] convert → %s (images=%s, task=%r)",
                        lingbot_ds_dir, has_images, task_desc[:60])
            # LeRobotDataset.create(root=...) 的 root 是**完整 dataset 路径**, 不是 parent.
            # convert.py:218 直接传给 lerobot, 它内部 mkdir(root, exist_ok=False) 要求 root 不存在.
            # 之前传 .parent 会让 obj.root = cache/ds_<fp>/ (已存在的 traj 目录) → FileExistsError.
            _convert(
                input_path=traj_json_dir,
                repo_id=lingbot_ds_dir.name,
                task_description=task_desc,
                root=lingbot_ds_dir,   # 完整 dataset 路径
                images_dir=img_base if has_images else None,
                use_videos=has_images,  # 有图就用 video 存储 (lingbot-vla 期望 mp4)
                video_codec="h264",
            )
        else:
            logger.info("[LINGBOT-VLA] dataset 已存在, 跳过 convert: %s", lingbot_ds_dir)

        # Step 2: 检测 n_servos (从第一条 traj)
        n_servos = 6  # SO-101 默认
        try:
            first_traj = trajectories[0]
            first_frame = first_traj["frames"][0]
            n_servos = len({p["id"] for p in first_frame["positions"]})
        except Exception as e:
            logger.warning("[LINGBOT-VLA] n_servos 检测失败 (%s), 用默认 %d", e, n_servos)

        # Step 3: 调 trainer (阻塞)
        progress_cb(0, train_steps, {"phase": "training",
                                     "message": f"启动 lingbot-vla 训练 (n_servos={n_servos})"})
        result = _train_lvla(
            ds_dir=str(lingbot_ds_dir),
            model_dir=model_dir,
            train_steps=train_steps,
            batch_size=batch_size,
            custom_params=custom_params,
            progress_cb=progress_cb,
            should_stop_cb=lambda: self._should_stop,
            n_servos=n_servos,
        )
        return result

    def _train_lerobot(self, trajectories, model_type, model_dir,
                       train_steps, batch_size, chunk_size, custom_params, progress_cb,
                       resume_from_step: int = None,
                       ds_fingerprint: str = None):
        """Train using LeRobot (ACT/Diffusion/SmolVLA/Pi0/etc).

        Pipeline:
        1. Convert Box2Robot JSON trajectories → LeRobot v3 dataset (with images if available)
        2. Call lerobot-train via subprocess (draccus CLI, most reliable)
        3. Model saved to model_dir/checkpoints/last/pretrained_model/

        VLA models (smolvla, pi0, pi0_fast, pi05) fine-tune from pretrained base with:
        - --policy.path for pretrained weights (instead of --policy.type for from-scratch)
        - bfloat16 dtype + gradient checkpointing for memory efficiency
        - Frozen vision encoder + expert-only training (configurable)
        """
        import hashlib as _hashlib
        import os as _os
        import subprocess
        import shutil as _shutil
        from box2robot_gpu_worker.convert import convert

        is_vla = model_type in self.VLA_MODELS

        # 优先用 process_job 透传过来的指纹 (与 dataset_ids 同源, 防止 server 删轨迹后口径错位).
        # 仅在 fingerprint 缺失 (旧调用路径) 时退回用 trajectories.id 现算.
        if not ds_fingerprint:
            ds_fingerprint = self._ds_fingerprint([t.get("id", "") for t in trajectories])
        # === 下载 cache 共享 (按 fp), LeRobot dataset 按稳定变体共享 ===
        # 原始下载缓存: cache/ds_<fp>/
        # 转换后 dataset: datasets/box2robot-<fp>-<storage>-f<fps>-t<task_hash>/
        # 注意 task 会写入 LeRobot dataset, 所以 task 不同必须分开缓存.
        ds_cache_dir = Path(__file__).parent.parent / "cache" / f"ds_{ds_fingerprint}"
        ds_dir = ds_cache_dir / "dataset"
        img_dir = ds_cache_dir / "images"

        # Step 1: Convert to LeRobot format
        has_images = img_dir.is_dir() and any(img_dir.iterdir())
        task_description = str(custom_params.get("task") or "manipulation task")

        def _bool_data_param(*keys: str, default: str = "false") -> bool:
            for key in keys:
                value = custom_params.get(key)
                if value not in (None, ""):
                    return str(value).lower() in ("true", "1", "yes", "on")
            return str(default).lower() in ("true", "1", "yes", "on")

        use_videos = has_images and _bool_data_param(
            "use_videos",
            "dataset_use_videos",
            default=_os.environ.get("B2R_USE_VIDEOS", "true"),
        )
        video_codec = str(
            custom_params.get("video_codec")
            or custom_params.get("dataset_video_codec")
            or _os.environ.get("B2R_VIDEO_CODEC", "h264")
        )
        video_backend = (
            custom_params.get("video_backend")
            or custom_params.get("dataset_video_backend")
            or _os.environ.get("B2R_VIDEO_BACKEND")
            or ("pyav" if use_videos else "")
        )
        video_backend = str(video_backend) if video_backend else None
        fps = 20
        storage_key = f"video-{video_codec.lower()}" if use_videos else "image"
        storage_key = "".join(c if c.isalnum() else "-" for c in storage_key).strip("-")
        task_hash = _hashlib.md5(task_description.encode("utf-8")).hexdigest()[:8]
        repo_id = f"box2robot-{ds_fingerprint[:8]}-{storage_key}-f{fps}-t{task_hash}"
        datasets_root = Path(__file__).parent.parent / "datasets" / repo_id
        dataset_marker = datasets_root / "meta" / "info.json"

        def _compatible_legacy_dataset(candidate: Path) -> bool:
            info_path = candidate / "meta" / "info.json"
            if not info_path.is_file():
                return False
            try:
                with open(info_path) as f:
                    info = json.load(f)
                if int(info.get("fps", fps)) != fps:
                    return False
                visual = (info.get("features") or {}).get(self.DATASET_VISION_KEY)
                if has_images:
                    if not visual:
                        return False
                    if use_videos:
                        vinfo = visual.get("info") or {}
                        if visual.get("dtype") != "video":
                            return False
                        if str(vinfo.get("video.codec", "")).lower() != video_codec.lower():
                            return False
                    elif visual.get("dtype") != "image":
                        return False
                tasks_path = candidate / "meta" / "tasks.parquet"
                if tasks_path.is_file():
                    import pandas as _pd
                    tasks = _pd.read_parquet(tasks_path)
                    task_values = {str(x) for x in list(tasks.index)}
                    if "task" in tasks.columns:
                        task_values.update(str(x) for x in tasks["task"].dropna().tolist())
                    if task_description not in task_values:
                        return False
                return True
            except Exception as e:
                logger.warning("[CACHE] legacy dataset compatibility check failed for %s: %s", candidate, e)
                return False

        def _link_compatible_legacy_dataset() -> bool:
            datasets_parent = datasets_root.parent
            # 兼容旧命名: box2robot-<job_id>-<fp8>
            legacy_pattern = f"box2robot-*-{ds_fingerprint[:8]}"
            for candidate in sorted(datasets_parent.glob(legacy_pattern),
                                    key=lambda p: p.stat().st_mtime,
                                    reverse=True):
                if candidate == datasets_root or not candidate.is_dir():
                    continue
                if not _compatible_legacy_dataset(candidate):
                    continue
                if datasets_root.exists() or datasets_root.is_symlink():
                    return dataset_marker.exists()
                try:
                    datasets_root.symlink_to(candidate, target_is_directory=True)
                    logger.info("[CACHE MIGRATE] linked stable dataset %s -> %s", datasets_root, candidate)
                except Exception as e:
                    logger.warning("[CACHE MIGRATE] symlink failed (%s), copying %s -> %s",
                                   e, candidate, datasets_root)
                    _shutil.copytree(candidate, datasets_root, symlinks=True)
                    logger.info("[CACHE MIGRATE] copied stable dataset %s from %s", datasets_root, candidate)
                return dataset_marker.exists()
            return False

        # VLA models require camera images
        if is_vla and not has_images:
            raise ValueError(
                f"{model_type.upper()} 是视觉语言动作模型，需要摄像头图像数据。"
                f"请使用带图像的数据集，或改用 ACT/Diffusion 等纯状态模型。"
            )

        # === Multi-slot race protection: 同 repo_id 多 thread 串行 convert + augment ===
        # 不加锁两个 slot 都看 dataset_marker 不存在 → 都调 LeRobotDataset.create →
        # 写 parquet 互相覆盖. 加锁后第一个进入的转完, 第二个看 marker 存在直接 hit.
        # 同样保护 quantile augment (写 stats.json) 不被同时改.
        with _get_file_lock(f"convert_{repo_id}"):
            # 锁内重新检查 marker — 第一个 thread 转完后第二个进锁会看到已存在
            if dataset_marker.exists():
                logger.info("[CACHE HIT] LeRobot dataset %s 已存在, 跳过转换", repo_id)
                if progress_cb:
                    progress_cb(0, train_steps, {"phase": "converting", "message": "数据集已缓存, 跳过转换"})
            elif _link_compatible_legacy_dataset():
                logger.info("[CACHE HIT] LeRobot dataset %s 由旧缓存迁移命中, 跳过转换", repo_id)
                if progress_cb:
                    progress_cb(0, train_steps, {"phase": "converting", "message": "数据集已缓存, 跳过转换"})
            else:
                logger.info(
                    "Converting to LeRobot format (vision=%s, videos=%s, codec=%s)...",
                    has_images,
                    use_videos,
                    video_codec,
                )
                if progress_cb:
                    progress_cb(0, train_steps, {"phase": "converting", "message": "转换为 LeRobot 数据集格式..."})
                convert(
                    input_path=ds_dir,
                    repo_id=repo_id,
                    task_description=task_description,
                    fps=fps,
                    images_dir=img_dir if has_images else None,
                    root=datasets_root,
                    use_videos=use_videos,
                    video_codec=video_codec,
                )
            logger.info("LeRobot dataset ready: %s", datasets_root)

            # Step 1.5: 模型特定的 dataset 后处理
            # pi05 用 QUANTILES normalization, 需要 dataset 有 q01/q99 stats.
            # 在同一锁内做 augment, 避免 stats.json 写冲突.
            if model_type in self.QUANTILE_NORM_MODELS:
                if progress_cb:
                    progress_cb(0, train_steps, {
                        "phase": "augmenting",
                        "message": f"为 {model_type.upper()} 补算 quantile stats (q01/q99)...",
                    })
                self._ensure_quantile_stats(repo_id, datasets_root, model_type)

        if progress_cb:
            progress_cb(0, train_steps, {"loss": 0})

        # Step 2: Train via lerobot CLI
        # 启动方式优先级:
        # 1. python -m lerobot.scripts.lerobot_train — 首选, 保留 package 上下文,
        #    relative import 才能解析 (直接 python lerobot_train.py 会
        #    "attempted relative import with no known parent package")
        # 2. pip 注册的 console script `lerobot-train` (pip install -e . 后生成)
        # 本地 lerobot/src 存在时, 通过 PYTHONPATH 让 -m 找到包 (见下方 train_env)
        lerobot_src = Path(__file__).parent.parent / "lerobot" / "src"
        import importlib.util
        import shutil as _sh
        if importlib.util.find_spec("lerobot") is not None or lerobot_src.exists():
            cmd = [sys.executable, "-m", "lerobot.scripts.lerobot_train"]
        else:
            console_bin = _sh.which("lerobot-train")
            if console_bin:
                cmd = [console_bin]
            else:
                raise FileNotFoundError(
                    "lerobot 未安装且本地子目录不存在。"
                    "请 cd lerobot && pip install -e . --no-build-isolation, "
                    "或确保 box2robot_gpu_worker/lerobot/ 子目录完整。"
                )

        def _int_train_param(*keys: str, default: int) -> int:
            for key in keys:
                value = custom_params.get(key)
                if value not in (None, ""):
                    return int(value)
            return int(default)

        num_workers = _int_train_param(
            "num_workers",
            "dataloader_num_workers",
            default=int(_os.environ.get("B2R_NUM_WORKERS", "4")),
        )
        num_workers = max(0, num_workers)
        prefetch_factor = _int_train_param("prefetch_factor", default=4)
        persistent_workers = str(custom_params.get("persistent_workers", "true")).lower() in (
            "true",
            "1",
            "yes",
            "on",
        )

        cmd += [
            f"--dataset.repo_id={repo_id}",
            f"--dataset.root={datasets_root}",
            f"--steps={train_steps}",
            f"--batch_size={batch_size}",
            f"--num_workers={num_workers}",
            f"--output_dir={model_dir}",
            "--policy.push_to_hub=false",
            "--wandb.enable=false",
            f"--save_freq={max(100, min(5000, train_steps // 5))}",
            "--log_freq=1",
        ]
        if use_videos and video_backend:
            cmd.append(f"--dataset.video_backend={video_backend}")
        if num_workers > 0:
            cmd += [
                f"--prefetch_factor={prefetch_factor}",
                f"--persistent_workers={str(persistent_workers).lower()}",
            ]

        if is_vla:
            # VLA: fine-tune from pretrained base
            pretrained_path = custom_params.get(
                "pretrained_path",
                self.VLA_PRETRAINED.get(model_type, f"lerobot/{model_type}_base"),
            )
            cmd.append(f"--policy.path={pretrained_path}")

            # 预下载 VLA base 到 HF cache (主进程在线状态), 否则训练子进程因
            # HF_HUB_OFFLINE=1 + cache 缺 config.json/权重立刻挂. snapshot_download
            # 已缓存即刻返回; 失败抛 RuntimeError, 上层把 job 标 failed.
            self._ensure_vla_base_cached(pretrained_path)

            # === 图像 key 适配 (rename_map) ===
            # base 训练时用了不同数据集 (pi0_base=aloha, pi05_base=droid, smolvla=...),
            # input_features 里的 cam 命名跟我们 Box2Robot dataset 的 'observation.images.wrist'
            # 对不上, 不处理会抛 "All image features are missing from the batch".
            #
            # 解法 (官方 rename_map.mdx): dataset 第一个 cam 映射到 base 第一个 cam,
            # 其余 base cam 由 pi0/pi05 modeling.prepare_images 自动 -1 填充 (siglip empty).
            #
            # 用户可通过 custom_params['rename_map'] 显式覆盖 (JSON string).
            logger.info("=" * 60)
            logger.info("[RENAME-MAP] 构建 VLA rename_map (model=%s base=%s)",
                        model_type, pretrained_path)
            user_rename = custom_params.get("rename_map")
            rename_map_dict = None
            if user_rename:
                # 用户显式指定 (来自前端高级选项); 透传
                if isinstance(user_rename, str):
                    rename_str = user_rename
                    try:
                        rename_map_dict = json.loads(user_rename)
                    except Exception as e:
                        logger.error("[RENAME-MAP] 用户指定的 rename_map 不是合法 JSON: %s (%s)",
                                     user_rename, e)
                else:
                    rename_map_dict = user_rename
                    rename_str = json.dumps(user_rename)
                cmd.append(f"--rename_map={rename_str}")
                logger.info("[RENAME-MAP] 来源: user custom_params")
                logger.info("[RENAME-MAP] dict: %s", rename_map_dict)
                logger.info("[RENAME-MAP] CLI arg: --rename_map=%s", rename_str)
            else:
                base_visual_keys = self._get_base_visual_keys(pretrained_path)
                logger.info("[RENAME-MAP] base_visual_keys (从 base config.json 读取): %s",
                            base_visual_keys)
                if base_visual_keys:
                    # 按语义匹配 base key, 不要盲目选第一个 (一般是 base/exterior).
                    # 用户数据集是 wrist 单相机, 强行映射到 base_0_rgb (主视角) 会导致
                    # pi05 把腕部抓取动作的视角理解成"远景全景"语义错位 → 推理动作错乱.
                    # 优先级: 数据集 key 含 wrist → base 含 wrist 的; 含 top/front → base
                    # 含 base/front 的; 都不匹配 → 第一个 (旧行为兜底).
                    ds_key_lower = self.DATASET_VISION_KEY.lower()
                    chosen_base_key = None
                    if "wrist" in ds_key_lower:
                        # 优先 right_wrist (主导手), 然后 left_wrist, 然后任何含 wrist 的
                        for k in base_visual_keys:
                            if "right_wrist" in k.lower():
                                chosen_base_key = k
                                break
                        if not chosen_base_key:
                            for k in base_visual_keys:
                                if "left_wrist" in k.lower():
                                    chosen_base_key = k
                                    break
                        if not chosen_base_key:
                            for k in base_visual_keys:
                                if "wrist" in k.lower():
                                    chosen_base_key = k
                                    break
                    elif any(s in ds_key_lower for s in ("top", "front", "exterior", "base")):
                        for k in base_visual_keys:
                            if any(s in k.lower() for s in ("base_", "front", "exterior")):
                                chosen_base_key = k
                                break
                    if not chosen_base_key:
                        chosen_base_key = base_visual_keys[0]
                        logger.info("[RENAME-MAP] 语义匹配未命中, fallback 到第一个 base cam")
                    else:
                        logger.info("[RENAME-MAP] 语义匹配命中: %s 含 'wrist' → 选 %s",
                                    self.DATASET_VISION_KEY, chosen_base_key)

                    rename_map_dict = {self.DATASET_VISION_KEY: chosen_base_key}
                    rename_str = json.dumps(rename_map_dict)
                    cmd.append(f"--rename_map={rename_str}")
                    n_padded = max(0, len(base_visual_keys) - 1)
                    logger.info("[RENAME-MAP] 来源: 自动生成 (按语义匹配 base cam)")
                    logger.info("[RENAME-MAP] dict: %s", rename_map_dict)
                    logger.info("[RENAME-MAP] CLI arg: --rename_map=%s", rename_str)
                    logger.info("[RENAME-MAP] %s -> %s (base 共 %d 个 cam; %d 个会被 -1 填充)",
                                self.DATASET_VISION_KEY, chosen_base_key,
                                len(base_visual_keys), n_padded)
                else:
                    logger.error(
                        "[RENAME-MAP] 无法获取 base visual keys (%s); 训练大概率会因 "
                        "'All image features are missing' 失败. 可在 custom_params 里手动指定 rename_map.",
                        pretrained_path,
                    )

            # === use_relative_actions 与 pretrained 互斥 ===
            # lerobot_train.py 当 use_relative_actions=true + 有 pretrained_path + 不 resume 时,
            # 会把 processor_pretrained_path 设成 None (warning: "Building processors from current
            # policy config"), 导致 preprocessor 从头建, 我们的 rename_map override 全部丢失,
            # batch 里仍然是 'observation.images.wrist' → 训练第一步报 "All image features are missing".
            # 这里强制剥掉, 让 lerobot 走 from_pretrained + override 路径.
            ura_val = str(custom_params.get("use_relative_actions", "")).lower()
            if ura_val in ("true", "1", "yes"):
                logger.warning(
                    "[RENAME-MAP] 检测到 custom_params.use_relative_actions=%s — VLA fine-tune "
                    "from pretrained 时此选项会让 lerobot 重建 preprocessor 并丢弃 rename_map, "
                    "训练会立刻挂在 'All image features are missing'. 已自动剥掉此选项.",
                    custom_params.get("use_relative_actions"),
                )
                # 从已经追加的 cmd 里移除 (worker 在前面 _add_policy_param 时可能已经加进去)
                cmd[:] = [a for a in cmd if not a.startswith("--policy.use_relative_actions")]
                # 防止后续 custom_params 透传循环再加回来 (整个 key 删掉, 走 PI05Config 默认值 False)
                custom_params = {k: v for k, v in custom_params.items()
                                 if k != "use_relative_actions"}

            # 预检 (preflight): 在启动训练前验证 draccus 能正确解析 --rename_map,
            # 且 from_pretrained + overrides 后, rename 步骤里的 rename_map 跟我们传的一致.
            # 任何一步失败都说明真训练时也会失败 — 提前暴露问题, 不浪费 GPU 时间.
            if rename_map_dict:
                self._preflight_rename_map(pretrained_path, rename_map_dict, lerobot_src)
            logger.info("=" * 60)

            # 注: pi05 默认 normalization=QUANTILES (用 q01/q99 鲁棒缩放, 比 mean/std
            # 抗异常值好). 这里我们 *不* 改模型 normalization, 而是在 dataset 准备阶段
            # (_ensure_quantile_stats) 给 dataset 补算 quantile stats, 让 pi05 用它原生设计.
            # 用户仍可通过 custom_params['normalization_mapping'] 显式覆盖.

            # 显存优化默认值 — 走白名单, 不支持的 model 静默跳过 (如 SmolVLA 没 dtype/
            # gradient_checkpointing 字段, GR00T 也没 dtype)
            self._add_policy_param(cmd, model_type, "dtype",
                                    custom_params.get("dtype", "bfloat16"))
            self._add_policy_param(cmd, model_type, "gradient_checkpointing",
                                    custom_params.get("gradient_checkpointing", "true"))

            # === 显存预算预检 (B) — 不让用户白等几分钟加载完才发现 OOM ===
            # pi0 / pi05 / pi0_fast 是 4B 参数; 全量微调时:
            #   weights (bf16) 8GB + grad (bf16) 8GB + Adam 一阶矩 (fp32) 16GB + 二阶矩 16GB
            #   ≈ 48GB (光优化器状态), 加 activations + KV cache 实际要 60-80GB.
            # 已知在 4080 SUPER 32GB / vGPU-32GB 上 100% OOM (实测 1d4fc93f 任务 25s 后崩).
            # smolvla 450M 全量微调约 8-12GB, 32GB 能跑.
            def _truthy(v) -> bool:
                return str(v).lower() in ("true", "1", "yes", "on")
            def _falsy(v) -> bool:
                return str(v).lower() in ("false", "0", "no", "off", "")
            if model_type in ("pi0", "pi0_fast", "pi05"):
                # 用户显式传 false (没让默认 true 兜底) → 全量微调路径
                fve = custom_params.get("freeze_vision_encoder", "true")
                teo = custom_params.get("train_expert_only", "true")
                # peft (LoRA) 启用时不需要这检查 (LoRA 只训百万参数, 全量"骨架"冻结)
                peft_on = _truthy(custom_params.get("peft_enable", ""))
                if not peft_on and _falsy(fve) and _falsy(teo):
                    # 拿当前 GPU vram (worker self.hw_info 在子类里; 用 nvidia-smi 现查兜底)
                    vram_gb = 0
                    try:
                        import subprocess as _sp
                        r = _sp.run(["nvidia-smi", "--query-gpu=memory.total",
                                     "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, timeout=5)
                        if r.returncode == 0:
                            vram_gb = int(r.stdout.strip().splitlines()[0]) // 1024
                    except Exception:
                        pass
                    REQUIRED_GB = 60
                    if vram_gb and vram_gb < REQUIRED_GB:
                        msg = (
                            f"{model_type.upper()} 全量微调 (freeze_vision_encoder=false + "
                            f"train_expert_only=false) 需 ≥ {REQUIRED_GB}GB VRAM (4B 参数 "
                            f"+ Adam 一阶/二阶矩 ~48GB), 当前 GPU 仅 {vram_gb}GB.\n"
                            f"\n"
                            f"建议 (任选其一):\n"
                            f"  1. (推荐) 关闭全量微调: freeze_vision_encoder=true + "
                            f"train_expert_only=true (worker 默认值, ~16GB 即可)\n"
                            f"  2. 改用 LoRA 微调: peft_enable=true peft_method_type=LORA peft_r=16\n"
                            f"  3. 换更大显存 GPU (H100/A100 80G)\n"
                            f"\n"
                            f"如确认 GPU 显存足够 (运维强制), 可在 custom_params 加 "
                            f"'override_vram_budget=true' 跳过本检查."
                        )
                        if not _truthy(custom_params.get("override_vram_budget", "")):
                            raise RuntimeError(f"[VRAM-BUDGET] {msg}")
                        else:
                            logger.warning("[VRAM-BUDGET] 全量微调 vram 预算超限 (need %dGB, "
                                           "have %dGB), 但 override_vram_budget=true 强制继续",
                                           REQUIRED_GB, vram_gb)

            # 所有 VLA: freeze vision encoder + train expert only 默认开 (省显存).
            # 不开的话: pi0 (4B) / pi05 (4B) / smolvla (450M) 训练时全 forward 4B/450M
            # 加 Adam 二阶矩 → 4080 SUPER 32GB 都装不下 (实测 OOM, 见 fix_history).
            # 用户想全量微调可在 custom_params 显式传 false 覆盖.
            if model_type in ("smolvla", "pi0", "pi0_fast", "pi05"):
                self._add_policy_param(cmd, model_type, "freeze_vision_encoder",
                                        custom_params.get("freeze_vision_encoder", "true"))
                self._add_policy_param(cmd, model_type, "train_expert_only",
                                        custom_params.get("train_expert_only", "true"))
            # SmolVLA 独有: train_state_proj (pi0/pi05 没这字段)
            if model_type == "smolvla":
                self._add_policy_param(cmd, model_type, "train_state_proj",
                                        custom_params.get("train_state_proj", "true"))
            # Pi0/Pi05: 可选 compile_model
            if model_type in ("pi0", "pi0_fast", "pi05"):
                if custom_params.get("compile_model"):
                    self._add_policy_param(cmd, model_type, "compile_model",
                                            custom_params["compile_model"])
            # VLA chunk_size/n_action_steps: use model defaults (50) unless explicitly overridden
            if chunk_size > 1 and custom_params.get("override_chunk_size"):
                self._add_policy_param(cmd, model_type, "chunk_size", chunk_size)
                self._add_policy_param(cmd, model_type, "n_action_steps", chunk_size)
        else:
            # ACT/Diffusion/GR00T 等: train from scratch
            cmd.append(f"--policy.type={model_type}")
            cmd.append(f"--policy.repo_id=box2robot/{repo_id}")
            # GR00T 训练需要 lerobot/eagle2hg-processor-groot-n1p5 的 11 个
            # tokenizer/processor assets. lerobot 内部的 ensure_eagle_cache_ready
            # 会在子进程里 hf_hub_download 这些文件, 但 worker 子进程 HF_HUB_OFFLINE=1
            # 拉不动 → ValueError("Unrecognized model in .../eagle2hg-processor-groot-n1p5").
            # 这里在主进程 (在线) 预下到 $HF_HOME/lerobot/<assets_repo>/, 让子进程离线
            # 模式下 AutoConfig.from_pretrained 能读到.
            if model_type == "groot":
                custom_repo = custom_params.get("tokenizer_assets_repo",
                                                "lerobot/eagle2hg-processor-groot-n1p5")
                self._ensure_groot_eagle_assets(custom_repo)
                # 强制 max_state_dim/max_action_dim 跟 GR00T-N1.5-3B 预训练 head 对齐.
                # 预训练 head 硬编码 action_dim=32 / state_dim=64 (action_head_cfg). 用户实际
                # action 维度可以小 (BoxBot 6/7/8 关节), 但要 0-padding 到 32/64 才匹配 head shape.
                # 不强制就崩在 validate_inputs: action.shape[2] != self.action_dim.
                GROOT_REQUIRED_STATE_DIM = 64
                GROOT_REQUIRED_ACTION_DIM = 32
                user_state = int(custom_params.get("max_state_dim", 0) or 0)
                user_action = int(custom_params.get("max_action_dim", 0) or 0)
                if user_state and user_state < GROOT_REQUIRED_STATE_DIM:
                    logger.warning("[GROOT] max_state_dim=%d 太小, 强制设为 %d (跟预训练 head 对齐)",
                                   user_state, GROOT_REQUIRED_STATE_DIM)
                    custom_params["max_state_dim"] = GROOT_REQUIRED_STATE_DIM
                if user_action and user_action < GROOT_REQUIRED_ACTION_DIM:
                    logger.warning("[GROOT] max_action_dim=%d 太小, 强制设为 %d (跟预训练 head 对齐)",
                                   user_action, GROOT_REQUIRED_ACTION_DIM)
                    custom_params["max_action_dim"] = GROOT_REQUIRED_ACTION_DIM
                # 用户没传时也补默认 (不依赖前端 schema 改)
                if "max_state_dim" not in custom_params:
                    custom_params["max_state_dim"] = GROOT_REQUIRED_STATE_DIM
                if "max_action_dim" not in custom_params:
                    custom_params["max_action_dim"] = GROOT_REQUIRED_ACTION_DIM
            if chunk_size > 1:
                # Diffusion 用 horizon 不是 chunk_size; 其它走 chunk_size
                if model_type == "diffusion":
                    self._add_policy_param(cmd, model_type, "horizon", chunk_size)
                else:
                    self._add_policy_param(cmd, model_type, "chunk_size", chunk_size)
                # n_action_steps=1 + temporal_ensemble 是 ACT 推荐用法 (每步推一次,
                # EMA 平滑), 不适用于其它模型. GR00T 默认 50; Diffusion 用户通过
                # advancedParams 自己设.
                if model_type == "act":
                    self._add_policy_param(cmd, model_type, "n_action_steps", 1)
                    self._add_policy_param(cmd, model_type, "temporal_ensemble_coeff", 0.01)

        # Resume from checkpoint (暂停后恢复训练)
        # LeRobot v3 用 6 位零填充目录名 (000200), 老格式是 str(step). 两种都试.
        # 注意: lerobot 的 CLI 接口是 `--resume=true --config_path=<train_config.json>`,
        # 不接受 `--checkpoint_path` (它是 configs/train.py 里 field(init=False) 的内部字段).
        # lerobot 从 config_path 反推 checkpoint_path (policy_dir.parent).
        if resume_from_step:
            ckpt_root = Path(model_dir) / "checkpoints"
            candidates = [
                ckpt_root / f"{int(resume_from_step):06d}",
                ckpt_root / str(resume_from_step),
            ]
            ckpt_path = next((c for c in candidates if c.exists()), None)
            if ckpt_path is not None:
                train_config = ckpt_path / "pretrained_model" / "train_config.json"
                if not train_config.is_file():
                    raise FileNotFoundError(
                        f"Resume checkpoint {ckpt_path.name} exists but missing "
                        f"pretrained_model/train_config.json (incomplete save). "
                        f"Try an earlier checkpoint or start a new job."
                    )
                cmd.append("--resume=true")
                cmd.append(f"--config_path={train_config}")
                logger.info("Resuming from checkpoint: %s (step %d) via config_path",
                            ckpt_path, resume_from_step)
            else:
                # 显式失败, 不再静默 fallback 到从头训练 — 后者会因 model_dir 已存在
                # 触发 lerobot FileExistsError, 给用户的报错指向不明.
                raise FileNotFoundError(
                    f"Resume requested at step {resume_from_step} but checkpoint not found. "
                    f"Searched: {[str(c.name) for c in candidates]}. "
                    f"Checkpoint may have been cleaned up; please start a new training job."
                )
        # === PEFT (LoRA) 处理 — 顶层 --peft.* 命名空间, 不是 --policy.* ===
        # lerobot 的 PEFT 通过顶层 PeftConfig 配置 (lerobot/configs/default.py:PeftConfig).
        # 默认 None (不启用); 一旦传任意 --peft.xxx 字段, lerobot 自动 wrap policy with PEFT.
        # 仅 VLA (pi0/pi05/smolvla 等含 LM 的模型) 支持; ACT/Diffusion 不支持 (忽略警告).
        # 安装依赖: pip install 'lerobot[peft]' (即 peft + accelerate)
        peft_enable = str(custom_params.get("peft_enable", "")).lower() in (
            "true", "1", "yes", "on",
        )
        if peft_enable and is_vla:
            method = str(custom_params.get("peft_method_type", "LORA")).upper()
            rank = int(custom_params.get("peft_r", 16))
            cmd.append(f"--peft.method_type={method}")
            cmd.append(f"--peft.r={rank}")
            tm = custom_params.get("peft_target_modules")
            if tm:
                cmd.append(f"--peft.target_modules={tm}")
            ftm = custom_params.get("peft_full_training_modules")
            if ftm:
                cmd.append(f"--peft.full_training_modules={ftm}")
            logger.info("[%s] PEFT enabled: method=%s r=%d", model_type.upper(), method, rank)
        elif peft_enable and not is_vla:
            logger.warning("[%s] PEFT requested but only VLA models support it, ignored",
                            model_type.upper())

        # Custom params 透传 — 通过 _add_policy_param 走白名单 + 别名映射, 不支持的
        # key 静默跳过 + warning, 避免某些字段在某些 model 不存在导致 lerobot CLI
        # "unrecognized arguments" 让训练 exit code 2.
        _handled_keys = {"task", "pretrained_path", "dtype", "gradient_checkpointing",
                         "freeze_vision_encoder", "train_expert_only", "train_state_proj",
                         "compile_model", "override_chunk_size", "rename_map",
                         "normalization_mapping",
                         "num_workers", "dataloader_num_workers", "prefetch_factor",
                         "persistent_workers",
                         "use_videos", "dataset_use_videos", "video_codec",
                         "dataset_video_codec", "video_backend", "dataset_video_backend",
                         # chunk_size / n_action_steps / horizon 在主分支已处理 (传 server
                         # 选定的统一值), 不再从 custom_params 透传以免重复
                         "chunk_size", "n_action_steps", "horizon",
                         # peft_* 已在上面以顶层 --peft.* 形式处理, 不要再走 --policy.*
                         "peft_enable", "peft_method_type", "peft_r",
                         "peft_target_modules", "peft_full_training_modules",
                         # 前端 split H/W 字段, 下方合并成 resize_shape/crop_shape 后再透传
                         "resize_shape_h", "resize_shape_w",
                         "crop_shape_h", "crop_shape_w"}

        # 前端 schema 把 resize_shape / crop_shape 拆成 H/W 两个 int 字段以改善 UX,
        # 这里合并回 LeRobot config 要求的 [H, W] (0 视为未设置).
        def _merge_hw(h_key: str, w_key: str, target: str):
            h = custom_params.get(h_key)
            w = custom_params.get(w_key)
            try:
                h_i = int(h) if h not in (None, "", "null") else 0
                w_i = int(w) if w not in (None, "", "null") else 0
            except (TypeError, ValueError):
                return
            if h_i > 0 and w_i > 0 and target not in custom_params:
                custom_params[target] = [h_i, w_i]
                logger.info("[DIFFUSION] merge %s=%d + %s=%d → %s=[%d,%d]",
                            h_key, h_i, w_key, w_i, target, h_i, w_i)

        if model_type == "diffusion":
            _merge_hw("resize_shape_h", "resize_shape_w", "resize_shape")
            _merge_hw("crop_shape_h", "crop_shape_w", "crop_shape")

        for k, v in custom_params.items():
            if k in _handled_keys:
                continue
            self._add_policy_param(cmd, model_type, k, v)

        logger.info("LeRobot train cmd: %s %s", model_type.upper(), " ".join(cmd[-6:]))
        # 完整 cmd dump — 出问题时直接复制粘贴可复现
        logger.info("=" * 60)
        logger.info("[CMD] 完整训练命令 (复制即可手动复现):")
        for i, arg in enumerate(cmd):
            logger.info("[CMD]   [%d] %s", i, arg)
        logger.info("=" * 60)

        # Run with real-time stdout forwarding for progress
        import os as _os
        train_env = {**_os.environ, "PYTHONUNBUFFERED": "1"}
        # HF_HOME 由 gpu_worker.py 启动时设, subprocess 自动继承.
        # base 模型 (pi05_base ~14GB) 只在 cache 第一次下载, 之后训练/推理共用.
        _hf_home = train_env.get("HF_HOME", _os.path.expanduser("~/.cache/huggingface"))
        logger.info("[HF_HOME] subprocess 继承: %s (base 模型缓存共享)", _hf_home)
        # 只设置 HF_HOME 不等于离线: transformers/huggingface_hub 仍可能为了
        # tokenizer metadata 调 model_info(). GPU 节点无外网时必须让子进程硬走本地缓存.
        train_env.setdefault("HF_HUB_OFFLINE", "1")
        train_env.setdefault("TRANSFORMERS_OFFLINE", "1")
        train_env.setdefault("HF_DATASETS_OFFLINE", "1")
        logger.info(
            "[HF_OFFLINE] HF_HUB_OFFLINE=%s TRANSFORMERS_OFFLINE=%s HF_DATASETS_OFFLINE=%s",
            train_env.get("HF_HUB_OFFLINE"),
            train_env.get("TRANSFORMERS_OFFLINE"),
            train_env.get("HF_DATASETS_OFFLINE"),
        )
        if lerobot_src.exists():
            # 本地子目录优先：把 lerobot/src 注入 PYTHONPATH, 让 -m 解析到本地包
            old_pp = train_env.get("PYTHONPATH", "")
            sep = ";" if _os.name == "nt" else ":"
            train_env["PYTHONPATH"] = (
                f"{lerobot_src}{sep}{old_pp}" if old_pp else str(lerobot_src)
            )
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(Path(__file__).parent.parent),
            env=train_env,
        )

        last_report_step = 0
        last_report_time = 0
        last_ckpt_set: set = set()  # 已上报过的 checkpoint 集合
        # 保留最后 N 行 stdout, 子进程崩时用于诊断 (OOM / ImportError / etc.)
        from collections import deque
        tail_lines: deque = deque(maxlen=80)
        import re
        # Match INFO log: "step:10 smpl:80 loss:41.394 grdn:627.730"
        # 注意: lerobot MetricsTracker.__str__ 用 format_big_number(step, precision=0),
        # step >= 1000 会变成 "step:1K" / "step:50K" / "step:1.2M" — 必须把 K/M/B/T/Q 后缀也接住,
        # 否则 pi0 / 大步数训练 (步数 1000+) 全程没法上报 step.
        metrics_re = re.compile(
            r'\bstep:([\d.]+[KMBTQ]?)\b.*?\bloss:([\d.e+-]+)\b'
        )
        # Match tqdm progress: "Training:  15%|...| 150/10000 [01:23<..."
        tqdm_re = re.compile(r'Training:\s+\d+%\|.*\|\s*(\d+)/(\d+)\s+\[')

        def _parse_big_num(s: str) -> int:
            """反解析 lerobot format_big_number — '1K' → 1000, '1.2M' → 1200000."""
            s = s.strip()
            if not s:
                return 0
            mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 10**12, "Q": 10**15}
            if s[-1] in mult:
                return int(float(s[:-1]) * mult[s[-1]])
            return int(float(s))

        # ===== stdout 心跳监控 (P0 #1) =====
        # 之前直接 for line in proc.stdout 阻塞读, 子进程卡死 (CUDA / 死锁 / IO 死等) 时
        # 主进程跟着无限阻塞, server 看 worker heartbeat 还在 → job 永远 training 不动.
        # 改成: 独立 reader 线程把 stdout 推 queue, 主循环每 10s 检查一次, 连续
        # STDOUT_STUCK_S 没新行 → kill + report failed.
        import queue as _queue
        import threading as _threading
        STDOUT_STUCK_S = 600  # 10 分钟无 stdout 视为卡死 (大模型首步 forward 可能 ~5 分钟)
        _line_q: "_queue.Queue[str | None]" = _queue.Queue(maxsize=10000)

        def _stdout_pump():
            try:
                for raw in proc.stdout:
                    _line_q.put(raw)
            finally:
                _line_q.put(None)  # EOF sentinel

        _reader = _threading.Thread(target=_stdout_pump, daemon=True)
        _reader.start()

        last_output_at = time.time()
        # 主动 server-check: lerobot 卡在 base 模型加载 (VLA pi05 14GB ~3min) 时不上报 progress,
        # progress→409 cancel 信号收不到, 孤儿 subprocess 一直占显存 (实测 13:23 启动→14:18 还在跑).
        # 修法: stdout 超时分支每 30s 主动 GET /jobs/{id} 看 status, cancelled/paused 就 set flag.
        _SERVER_CHECK_INTERVAL_S = 30
        _last_server_check = time.time()
        eof = False
        while not eof:
            try:
                line = _line_q.get(timeout=10)
            except _queue.Empty:
                line = ""  # 超时 sentinel
                idle_s = time.time() - last_output_at
                if idle_s > STDOUT_STUCK_S:
                    logger.warning("[STUCK] training subprocess no stdout for %.0fs, killing pid=%s",
                                   idle_s, proc.pid)
                    try:
                        proc.terminate()
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"训练子进程卡死 ({int(idle_s)}s 无 stdout, "
                        f"可能 OOM / CUDA 死锁 / 数据加载阻塞)。"
                        f"最后日志: {' '.join(list(tail_lines)[-3:])[:300]}")
                # 主动 server-check (每 30s 一次, 短 timeout 不卡循环) — 防孤儿
                if (not self._should_stop and not self._should_pause
                        and time.time() - _last_server_check > _SERVER_CHECK_INTERVAL_S):
                    _last_server_check = time.time()
                    try:
                        _r = self.client.get(
                            f"{self.server_url}/api/training/jobs/{job_id}",
                            timeout=3.0)
                        if _r.status_code == 200:
                            _j = _r.json() or {}
                            _st = _j.get("status", "")
                            if _st == "cancelled":
                                logger.warning(
                                    "[SERVER-CHECK] job %s status=cancelled, triggering stop "
                                    "(progress 通道收不到 cancel, 兜底主动 poll 发现)", job_id)
                                self._should_stop = True
                            elif _st == "paused":
                                logger.warning(
                                    "[SERVER-CHECK] job %s status=paused, triggering pause "
                                    "(progress 通道收不到, 兜底主动 poll 发现)", job_id)
                                self._should_pause = True
                    except Exception as _e:
                        logger.debug("[SERVER-CHECK] failed (will retry next interval): %s", _e)
                # 响应停止/暂停信号 (跟原 for-loop 内逻辑一致)
                if self._should_stop or self._should_pause:
                    reason = "paused" if self._should_pause else "cancelled"
                    logger.info("Stopping LeRobot subprocess (user %s, idle path)", reason)
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                continue
            if line is None:
                eof = True
                break
            last_output_at = time.time()
            line = line.strip()
            if not line:
                continue
            tail_lines.append(line)
            is_tqdm = "Training:" in line and "%" in line
            print(f"  [lerobot] {line}")

            # 检查停止/暂停信号 → 终止子进程
            if self._should_stop or self._should_pause:
                reason = "paused" if self._should_pause else "cancelled"
                logger.info("Stopping LeRobot subprocess (user %s)", reason)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

            if not progress_cb:
                continue

            # 步数解析: tqdm 优先 (有精确 cur/total), metrics_re 兜底.
            # 关键 bug (2026-05-10 fix): lerobot format_big_number(step, precision=0) 把
            # step >= 1000 round 成 "step:1K"/"step:3K", 即便 step=2606 也显示 "step:3K".
            # 之前先解析 metrics_re → step 直接被当成 3000 上报 → server 提前判完成,
            # 真训练还差几百步. 修: 同一行先取 tqdm 准确 step, 再用 metrics 取 loss.
            tqdm_step: int = 0
            if is_tqdm:
                tm = tqdm_re.search(line)
                if tm:
                    try:
                        tqdm_step = int(tm.group(1))
                    except Exception:
                        tqdm_step = 0
            m = metrics_re.search(line)
            if m:
                try:
                    metrics_step = _parse_big_num(m.group(1))
                    loss = float(m.group(2))
                    # 优先用 tqdm 精确 step (没 tqdm 才用 metrics K-rounded — 它仍单调递增)
                    real_step = tqdm_step if tqdm_step > 0 else metrics_step
                    if real_step > last_report_step:
                        metrics = {"loss": loss}
                        for kv in re.findall(r'(\w+):([\d.e+-]+)', line):
                            if kv[0] not in ("step", "smpl", "ep"):
                                try:
                                    metrics[kv[0]] = float(kv[1])
                                except ValueError:
                                    pass
                        metrics["log"] = line
                        progress_cb(real_step, train_steps, metrics)
                        last_report_step = real_step
                except Exception:
                    pass
            elif tqdm_step > 0 and tqdm_step > last_report_step:
                # 纯 tqdm 行 (没 metrics, 例如 dataloader 阶段) — 仍上报 step 进度
                try:
                    progress_cb(tqdm_step, train_steps, {"log": line})
                    last_report_step = tqdm_step
                except Exception:
                    pass
            # 其他重要行 (WARNING/ERROR/INFO 但非 metrics)
            elif any(k in line for k in ("WARNING", "ERROR", "Creating", "End of", "Checkpoint", "Start")):
                # 过滤已知预期但每次都打的 noise (避免吓用户):
                #   - lerobot 默认 device='mps' (Apple Silicon), 没 mps 时 fallback cuda — 正常行为, 每次启动打 2 次
                # 别的 WARNING (Vision embedding key / quantile stats / etc) 仍透传, 它们是有用信息
                LERROR_NOISE_KEYWORDS = (
                    "Device 'mps' is not available",
                    "Switching to 'cuda'",
                )
                if any(noise in line for noise in LERROR_NOISE_KEYWORDS):
                    continue   # 静默丢弃, 不上报 server
                report_metrics: dict = {"log": line}
                # Checkpoint 保存事件 — 扫描并上报 checkpoint 列表
                if "Checkpoint" in line:
                    ckpt_steps = self._scan_checkpoints(model_dir)
                    if ckpt_steps and set(ckpt_steps) != last_ckpt_set:
                        report_metrics["checkpoints"] = ckpt_steps
                        last_ckpt_set = set(ckpt_steps)
                        logger.info("Checkpoints available: %s", ckpt_steps)
                progress_cb(last_report_step, train_steps, report_metrics)

        proc.wait()
        if proc.returncode != 0 and not self._should_stop and not self._should_pause:
            err_msg = self._diagnose_subprocess_error(proc.returncode, list(tail_lines))
            raise RuntimeError(err_msg)

        # Find the pretrained model path
        ckpt_dir = Path(model_dir) / "checkpoints" / "last" / "pretrained_model"
        if not ckpt_dir.exists():
            ckpt_dirs = sorted(Path(model_dir).glob("checkpoints/*/pretrained_model"))
            if ckpt_dirs:
                ckpt_dir = ckpt_dirs[-1]

        # 最终 checkpoint 列表上报
        final_ckpts = self._scan_checkpoints(model_dir)
        if final_ckpts and progress_cb:
            progress_cb(train_steps, train_steps, {"checkpoints": final_ckpts})
        logger.info("Training complete. Model: %s, checkpoints: %s", ckpt_dir, final_ckpts)

        # Save config for inference (写到 model_dir 和 checkpoint 目录)
        Path(model_dir).mkdir(parents=True, exist_ok=True)
        config_path = Path(model_dir) / "b2r_config.json"
        import json as _json
        inference_config = {
            "model_type": model_type,
            "is_vla": is_vla,
            "pos_max": 4095,
            "use_vision": has_images,
            "lerobot_dataset": repo_id,
            "lerobot_checkpoint": str(ckpt_dir),
            "chunk_size": chunk_size,
            "n_servos": len(trajectories[0]["frames"][0]["positions"]) if trajectories else 6,
            "task_description": task_description,
        }
        with open(config_path, "w") as f:
            _json.dump(inference_config, f, indent=2)

        return {"model_dir": model_dir, "model_type": model_type, "checkpoint": str(ckpt_dir)}

    @staticmethod
    def _scan_checkpoints(model_dir: str) -> list:
        """扫描 model_dir/checkpoints/ 下已保存的 checkpoint 步数列表"""
        ckpt_root = Path(model_dir) / "checkpoints"
        if not ckpt_root.exists():
            return []
        steps = []
        for d in ckpt_root.iterdir():
            if d.is_dir() and d.name.isdigit():
                # 确认 pretrained_model 目录存在 (checkpoint 完整)
                if (d / "pretrained_model").exists():
                    steps.append(int(d.name))
        return sorted(steps)

    def _download_images(self, url: str, dest_dir: Path) -> bool:
        """Download and extract image zip from server."""
        import io
        import zipfile
        try:
            full_url = url if url.startswith("http") else f"{self.server_url}{url}"
            r = self.client.get(full_url, timeout=120)
            r.raise_for_status()
            dest_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                zf.extractall(dest_dir)
            count = len(list(dest_dir.glob("*.jpg")))
            logger.info("  Downloaded %d images → %s", count, dest_dir)
            return count > 0
        except Exception as e:
            logger.warning("  Image download failed: %s", e)
            return False

    def _get_job_info(self, job_id: str) -> dict:
        """获取 job 元信息 (不含轨迹帧数据, 轻量)"""
        try:
            url = f"{self.server_url}/api/training/jobs/{job_id}"
            params = {"worker": "1"}
            if self.pairing_key:
                params["key"] = self.pairing_key
            r = self.client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("Get job info failed: %s", e)
            return {}

    def _download_dataset(self, job_id: str) -> dict:
        try:
            url = f"{self.server_url}/api/training/jobs/{job_id}/dataset"
            params = {}
            if self.pairing_key:
                params["key"] = self.pairing_key
            r = self.client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("Download failed: %s", e)
            return {}

    def _report_progress(self, job_id: str, step: int, total: int, metrics: dict) -> dict:
        url = f"{self.server_url}/api/training/jobs/{job_id}/progress"
        payload = {"step": step, "total_steps": total, "metrics": metrics, "key": self.pairing_key}
        # checkpoint 列表或最终进度: 重试; 普通进度: 不重试
        has_checkpoint = "checkpoints" in metrics
        max_retries = 3 if has_checkpoint else 1
        for attempt in range(max_retries):
            try:
                r = self.client.post(url, json=payload)
                if r.status_code == 409:
                    try:
                        body = r.json()
                    except Exception:
                        body = {}
                    if body.get("should_pause"):
                        return {"should_pause": True}
                    return {"should_stop": True}
                r.raise_for_status()
                # Hardening F: 上报成功 → 顺便 flush 之前积压的 checkpoint 报告
                if has_checkpoint:
                    self._flush_pending_checkpoint_reports(job_id)
                return r.json()
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning("Progress report failed (retry %d/%d): %s", attempt + 1, max_retries, e)
                    time.sleep(3)
                else:
                    logger.warning("Progress report failed: %s", e)
        # Hardening F: 重要报告 (含 checkpoints 列表) 全部重试失败 → 落盘, 等下次成功上报时再 flush.
        if has_checkpoint:
            self._persist_pending_report(job_id, payload)
        return {}

    def _pending_dir(self) -> "Path":
        from pathlib import Path
        d = self.output_dir / "_pending_reports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _persist_pending_report(self, job_id: str, payload: dict):
        """Hardening F: progress 重试用尽后, 把 checkpoint 报告存到磁盘,
        下次同一 job 的 progress 成功上报时一并 flush. 防止网络抖动让
        cancel/pause 的 checkpoint 列表永久丢失.
        """
        try:
            import uuid
            f = self._pending_dir() / f"{job_id}__{int(time.time())}__{uuid.uuid4().hex[:6]}.json"
            with open(f, "w", encoding="utf-8") as fp:
                json.dump(payload, fp)
            logger.warning("[F] Persisted pending progress to %s", f)
        except Exception as e:
            logger.warning("[F] Persist pending failed: %s", e)

    def _flush_pending_checkpoint_reports(self, job_id: str):
        """Hardening F: 见 _persist_pending_report. 在下次成功 progress 时尝试重发."""
        try:
            d = self._pending_dir()
            files = sorted(d.glob(f"{job_id}__*.json"))
            for f in files:
                try:
                    with open(f, "r", encoding="utf-8") as fp:
                        payload = json.load(fp)
                    url = f"{self.server_url}/api/training/jobs/{job_id}/progress"
                    r = self.client.post(url, json=payload)
                    if r.status_code in (200, 409):
                        f.unlink()
                        logger.info("[F] Flushed pending report %s (status=%d)", f.name, r.status_code)
                    else:
                        logger.warning("[F] Flush returned %d, keeping %s", r.status_code, f.name)
                except Exception as e:
                    logger.warning("[F] Flush failed for %s: %s", f, e)
        except Exception as e:
            logger.debug("[F] Flush sweep error: %s", e)

    def _report_status(self, job_id: str, status: str,
                       error_msg: str = None, model_path: str = None,
                       checkpoints: list = None):
        url = f"{self.server_url}/api/training/jobs/{job_id}/status"
        data = {"status": status, "key": self.pairing_key}
        if error_msg:
            data["error_msg"] = _sanitize_error_path(error_msg)
        if model_path:
            data["model_path"] = model_path
        if checkpoints:
            # 通过 status 通道随状态一起持久化 checkpoint 列表
            # （progress 通道在 cancelled/paused 时会被 server 409 拒收）
            data["checkpoints"] = checkpoints
        # 关键状态 (completed/failed/cancelled) 失败时重试
        is_terminal = status in ("completed", "failed", "cancelled")
        max_retries = 5 if is_terminal else 1
        for attempt in range(max_retries):
            try:
                r = self.client.post(url, json=data)
                r.raise_for_status()
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 3 * (attempt + 1)
                    logger.warning("Status report failed (retry %d/%d in %ds): %s",
                                   attempt + 1, max_retries, wait, e)
                    time.sleep(wait)
                else:
                    logger.error("Status report FAILED after %d retries: %s", max_retries, e)

    def _upload_model_artifact(self, job_id: str, model_dir: str):
        """Hardening E: 训练完成后把模型 tar.gz 推到 server.

        打包 model_dir/checkpoints/last/pretrained_model/ (LeRobot 标准产物);
        若不存在则退化为整个 model_dir. 计算 sha256, 通过 multipart 上传到
        /api/training/jobs/{job_id}/upload-model. 失败仅警告 (训练已完成 status
        已 commit, 不影响主流程).
        """
        import hashlib
        import tarfile
        import tempfile
        from pathlib import Path

        m = Path(model_dir)
        if not m.exists():
            logger.warning("[E] model_dir 不存在, 跳过上传: %s", model_dir)
            return
        # LeRobot 标准产物目录
        canonical = m / "checkpoints" / "last" / "pretrained_model"
        target = canonical if canonical.exists() else m

        # 打包到临时文件
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                tar.add(str(target), arcname="model")
            size = Path(tmp_path).stat().st_size
            # sha256
            h = hashlib.sha256()
            with open(tmp_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            sha = h.hexdigest()
            logger.info("[E] Model packed: %s → %.1f MB sha=%s",
                        target, size / 1024 / 1024, sha[:12])

            # multipart 上传
            url = f"{self.server_url}/api/training/jobs/{job_id}/upload-model"
            with open(tmp_path, "rb") as f:
                files = {"file": (f"{job_id}.tar.gz", f, "application/gzip")}
                fields = {"sha256": sha}
                # httpx 支持 data + files
                r = self.client.post(
                    url,
                    files=files,
                    data=fields,
                    headers={"X-Pairing-Key": self.pairing_key or ""},
                    timeout=600.0,  # 大模型可能上传几分钟
                )
            if r.status_code == 200:
                logger.info("[E] Model uploaded to server: job=%s size=%d", job_id, size)
            else:
                logger.warning("[E] Upload returned %d: %s", r.status_code, r.text[:200])
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


def _resolve_hf_cache_path(repo_id: str) -> str | None:
    """检查 HF cache 里是否已下载 repo_id 的 snapshot, 返回本地路径或 None.

    HuggingFace cache 结构 (HF_HOME 指向 cache 根目录):
        <HF_HOME>/hub/models--<org>--<name>/snapshots/<commit_sha>/<files...>
        <HF_HOME>/hub/models--<org>--<name>/refs/main  (含最新 commit_sha)

    例 lerobot/pi05_base + HF_HOME=/root/autodl-tmp/.cache:
        /root/autodl-tmp/.cache/hub/models--lerobot--pi05_base/snapshots/9e55186.../

    返回该路径让 from_pretrained(local_path) 跳过 hub API, 避免:
    - 401 Unauthorized (xet server 鉴权偶发失败)
    - 网络超时 (国内连 huggingface.co)
    - 重复下载浪费带宽
    """
    import os as _os
    if "/" not in repo_id or _os.path.isdir(repo_id):
        return None  # 已是本地路径
    hf_home = _os.environ.get("HF_HOME") or _os.path.expanduser("~/.cache/huggingface")
    org, name = repo_id.split("/", 1)
    repo_dir = Path(hf_home) / "hub" / f"models--{org}--{name}"
    if not repo_dir.is_dir():
        return None
    # 找 refs/main 指向的 commit
    ref_file = repo_dir / "refs" / "main"
    snapshot_dir = None
    if ref_file.is_file():
        commit = ref_file.read_text().strip()
        candidate = repo_dir / "snapshots" / commit
        if candidate.is_dir():
            snapshot_dir = candidate
    if snapshot_dir is None:
        # refs/main 不存在或 commit 目录缺失, 拿最新 snapshot 兜底
        snapshots_root = repo_dir / "snapshots"
        if snapshots_root.is_dir():
            snaps = [d for d in snapshots_root.iterdir() if d.is_dir()]
            if snaps:
                snapshot_dir = sorted(snaps, key=lambda p: p.stat().st_mtime)[-1]
    if snapshot_dir is None:
        return None
    # 验证关键文件存在 (config.json + 至少一个 safetensors)
    if not (snapshot_dir / "config.json").is_file():
        return None
    has_weights = any(snapshot_dir.glob("*.safetensors")) or any(snapshot_dir.glob("*.bin"))
    if not has_weights:
        return None
    return str(snapshot_dir)


def _resolve_hf_snapshot_path(repo_id: str, required_files: tuple[str, ...] = ()) -> str | None:
    """返回 HF cache snapshot 路径，不要求权重文件，用于 tokenizer/processor 等轻量资源."""
    import os as _os
    if "/" not in repo_id:
        return str(repo_id) if _os.path.isdir(repo_id) else None
    if _os.path.isdir(repo_id):
        path = Path(repo_id)
        if all((path / f).is_file() for f in required_files):
            return str(path)
        return None

    hf_home = _os.environ.get("HF_HOME") or _os.path.expanduser("~/.cache/huggingface")
    org, name = repo_id.split("/", 1)
    repo_dir = Path(hf_home) / "hub" / f"models--{org}--{name}"
    if not repo_dir.is_dir():
        return None

    ref_file = repo_dir / "refs" / "main"
    snapshot_dir = None
    if ref_file.is_file():
        candidate = repo_dir / "snapshots" / ref_file.read_text().strip()
        if candidate.is_dir():
            snapshot_dir = candidate
    if snapshot_dir is None:
        snapshots_root = repo_dir / "snapshots"
        if snapshots_root.is_dir():
            snaps = [d for d in snapshots_root.iterdir() if d.is_dir()]
            if snaps:
                snapshot_dir = sorted(snaps, key=lambda p: p.stat().st_mtime)[-1]
    if snapshot_dir is None:
        return None
    if required_files and not all((snapshot_dir / f).is_file() for f in required_files):
        return None
    return str(snapshot_dir)


def run_inference_server(model_dir: str, server_url: str, device_id: str,
                         token: str = "", pos_max: int = 4095, fps: int = 20,
                         camera_id: str = "", chunk_size: int = 20,
                         job_id: str = "", execution_mode: str = "original",
                         chunk_params: dict = None):
    """Inference loop with selectable execution strategy.

    execution_mode:
      original  — LeRobot default: select_action per step, single command (~5Hz)
      fixed     — Full chunk execution: predict once, execute all steps (~1Hz infer, 20Hz exec)
      adaptive  — FAST-ACT skip: predict, analyze consistency, execute N steps (1-5Hz infer, 20Hz exec)
      overlap   — Sliding window: execute chunk/2, overlap with temporal ensemble (~2Hz infer, 20Hz exec)

    Pipeline (双缓冲):
      GPU: read state+cam → predict chunk(20步) → send batch → predict next chunk...
      ARM: receive batch → PlaybackTask execute → request more → receive next batch...

    ACT/Diffusion/VLA: lerobot policy 原生 chunk 输出 (chunk_size actions per inference)
    """
    import os as _os
    import io
    import numpy as np
    import torch
    from PIL import Image

    # 推理路径在 worker 主进程内加载 VLA processor/tokenizer, 不经过训练 subprocess.
    # 只设置 HF_HOME 不会阻止 transformers/huggingface_hub 查 model_info(), 离线节点会炸.
    _hf_home = _os.environ.get("HF_HOME", _os.path.expanduser("~/.cache/huggingface"))
    _os.environ.setdefault("HF_HUB_OFFLINE", "1")
    _os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    _os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    logger.info(
        "[HF_OFFLINE] inference HF_HOME=%s HF_HUB_OFFLINE=%s TRANSFORMERS_OFFLINE=%s HF_DATASETS_OFFLINE=%s",
        _hf_home,
        _os.environ.get("HF_HUB_OFFLINE"),
        _os.environ.get("TRANSFORMERS_OFFLINE"),
        _os.environ.get("HF_DATASETS_OFFLINE"),
    )

    logger.info("Loading model from %s", model_dir)
    # b2r_config.json 总是落在顶层 model_dir/ 下 (worker 训练时写). 但 model_dir 实参
    # 可能是深路径 .../checkpoints/000200/pretrained_model — 向上回溯到包含 b2r_config.json
    # 的目录, 避免误用 default config 把 pos_max 等关键参数走到默认值.
    config_path = Path(model_dir) / "b2r_config.json"
    if not config_path.exists():
        for p in Path(model_dir).resolve().parents:
            cand = p / "b2r_config.json"
            if cand.exists():
                config_path = cand
                break

    use_vision = False
    model_type = "act"
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        pos_max = config.get("pos_max", pos_max)
        use_vision = config.get("use_vision", False)
        model_type = config.get("model_type", "act")
        chunk_size = config.get("chunk_size", chunk_size)
    else:
        config = {"n_servos": 6, "pos_max": pos_max}

    # VLA models use LeRobot's built-in preprocessing (VLM handles images internally)
    is_vla = config.get("is_vla", model_type in ("smolvla", "pi0", "pi0_fast", "pi05"))
    task_description = config.get("task_description", "manipulation task")

    # === LingBot-VLA 4B: 走独立 b2r-vla env subprocess + WS, 不经 lerobot factory ===
    # 架构跟 lerobot policies 完全不同 (Qwen2.5-VL + flow-matching), 用 lingbot-vla 官方
    # 推理服务 (deploy.lingbot_vla_policy WS). 见 lingbot_vla_inferencer.py.
    if model_type == "lingbot_vla":
        from box2robot_gpu_worker.lingbot_vla_inferencer import run_inference_lingbot_vla
        logger.info("[INFER] dispatching to lingbot_vla inferencer (model_dir=%s)", model_dir)
        return run_inference_lingbot_vla(
            model_dir=model_dir, server_url=server_url, device_id=device_id,
            token=token, pos_max=pos_max, fps=fps, camera_id=camera_id,
            chunk_size=chunk_size, job_id=job_id, execution_mode=execution_mode,
            chunk_params=chunk_params,
        )

    # LeRobot policy — 解析 model_dir 到具体的 pretrained_model 目录.
    # model_dir 可能传进来三种形态:
    #   (a) 顶层 model_dir, 内含 checkpoints/<step>/pretrained_model/  → glob 找 last
    #   (b) 直接是 pretrained_model 目录 (gpu_worker 指定 checkpoint_step 时)
    #   (c) checkpoints/<step>/ 但少 pretrained_model 后缀 → 补上
    ckpt_path = config.get("lerobot_checkpoint", "")
    if not ckpt_path or not Path(ckpt_path).exists():
        m = Path(model_dir)
        if (m / "config.json").exists():
            # 形态 (b): model_dir 自己就是 pretrained_model
            ckpt_path = str(m)
        elif (m / "pretrained_model" / "config.json").exists():
            # 形态 (c): model_dir 是 checkpoints/<step>/, 加一层
            ckpt_path = str(m / "pretrained_model")
        else:
            # 形态 (a): 顶层 model_dir, glob 找最新 checkpoint
            ckpt_dirs = sorted(m.glob("checkpoints/*/pretrained_model"))
            ckpt_path = str(ckpt_dirs[-1]) if ckpt_dirs else ""
    if not ckpt_path or not (Path(ckpt_path) / "config.json").exists():
        raise FileNotFoundError(f"No pretrained_model found in {model_dir}")

    logger.info("Loading LeRobot %s from %s", model_type.upper(), ckpt_path)
    sys.path.insert(0, str(Path(__file__).parent.parent / "lerobot" / "src"))
    from lerobot.policies.factory import get_policy_class
    from safetensors.torch import load_file as _load_sf

    # GR00T: 兜底 patch HF cache 里的 config.json (_attn_implementation flash_attention_2 → sdpa).
    # 即使 lerobot/.../groot/utils.py 的 patch 没生效 (例如 cache 已存在 → hf_hub_download 跳过下载
    # 不会刷新文件), 这里再过一次确保 worker 没装 flash_attn 也能加载 GR00T.
    if model_type == "groot":
        TrainingWorker._ensure_groot_eagle_assets(
            "lerobot/eagle2hg-processor-groot-n1p5")

    policy_cls = get_policy_class(model_type)

    # === LoRA / PEFT 格式自动检测 ===
    # 用户开 LoRA 微调训练时, lerobot 保存的 ckpt 不含 model.safetensors,
    # 而是 adapter_config.json + adapter_model.safetensors (PEFT 格式).
    # 直接 policy_cls.from_pretrained 会"加载 config + 跳过权重", 退化成随机初始化模型.
    # 检测到 adapter_config.json 时走 PEFT 路径: 先加载 base 再贴 adapter, 最后 merge.
    _adapter_cfg_file = Path(ckpt_path) / "adapter_config.json"
    if _adapter_cfg_file.exists():
        logger.info("LoRA adapter detected at %s — loading via PEFT path", ckpt_path)
        try:
            from peft import PeftConfig, PeftModel
        except ImportError:
            raise RuntimeError(
                "ckpt 是 LoRA 格式但 worker 缺 peft 库. "
                "解决: pip install peft accelerate"
            )
        peft_cfg = PeftConfig.from_pretrained(str(ckpt_path))
        base_path = getattr(peft_cfg, "base_model_name_or_path", "") or ""
        if not base_path:
            # 兜底: 用 model_type 对应的 HF base
            VLA_BASE_FALLBACK = {
                "pi0": "lerobot/pi0_base", "pi0_fast": "lerobot/pi0_fast_base",
                "pi05": "lerobot/pi05_base", "smolvla": "lerobot/smolvla_base",
            }
            base_path = VLA_BASE_FALLBACK.get(model_type, f"lerobot/{model_type}_base")
            logger.warning("adapter_config 没记录 base_model_name_or_path, 用 fallback: %s", base_path)
        # === 优先从本地 HF cache 加载, 避免每次都走网络 401/慢下载 ===
        # HF_HOME (gpu_worker.py 启动时设) 指向 cache 根目录, 实际 ckpt 落在
        # <HF_HOME>/hub/models--<org>--<name>/snapshots/<commit>/. 我们检测它,
        # 命中就用本地路径加载 (skip hub API 调用).
        local_base = _resolve_hf_cache_path(base_path)
        if local_base:
            logger.info("Loading PEFT base from local HF cache: %s", local_base)
            base_path = local_base
        else:
            import os as _os
            logger.info("Loading PEFT base from HuggingFace Hub: %s "
                         "(下载到 %s, 训练/推理共享 cache)",
                         base_path, _os.environ.get("HF_HOME", "~/.cache/huggingface"))
        model = policy_cls.from_pretrained(base_path)
        logger.info("Applying LoRA adapter from %s", ckpt_path)
        model = PeftModel.from_pretrained(model, str(ckpt_path))
        # merge_and_unload: 把 LoRA 权重合并进 base, 返回普通 model. 推理速度 = 全量微调.
        # 不 merge 推理也能跑但慢一些 (要走 PEFT forward hook).
        try:
            model = model.merge_and_unload()
            logger.info("LoRA adapter merged into base model (merge_and_unload)")
        except Exception as e:
            logger.warning("merge_and_unload 失败 (%s), 保持 PeftModel wrapper, 推理稍慢", e)
    else:
        # 全量训练 ckpt — 直接加载
        model = policy_cls.from_pretrained(ckpt_path)
    model.eval()
    if torch.cuda.is_available():
        model = model.cuda()
    if hasattr(model, "reset"):
        model.reset()

    # 加载 MEAN_STD 归一化参数
    # VLA models handle normalization differently — check if preprocessor files exist
    _ckpt = Path(ckpt_path)
    _has_manual_norm = False
    _state_mean = _state_std = _action_mean = _action_std = None
    _pre_file = _ckpt / "policy_preprocessor_step_3_normalizer_processor.safetensors"
    _post_file = _ckpt / "policy_postprocessor_step_0_unnormalizer_processor.safetensors"
    if _pre_file.exists() and _post_file.exists():
        _pre = _load_sf(str(_pre_file))
        _post = _load_sf(str(_post_file))
        if "observation.state.mean" in _pre and "action.mean" in _post:
            _has_manual_norm = True
            _state_mean = _pre["observation.state.mean"]
            _state_std = _pre["observation.state.std"]
            _action_mean = _post["action.mean"]
            _action_std = _post["action.std"]
            if torch.cuda.is_available():
                _state_mean, _state_std = _state_mean.cuda(), _state_std.cuda()
                _action_mean, _action_std = _action_mean.cuda(), _action_std.cuda()

    logger.info("%s model loaded (chunk=%d, vision=%s, vla=%s, GPU=%s)",
                 model_type.upper(), chunk_size, use_vision, is_vla, torch.cuda.is_available())

    # === 检查点 1: dump model.config.input_features (诊断 state/image shape 错配) ===
    try:
        cfg_input = getattr(model.config, "input_features", {}) or {}
        logger.info("[CHECK-1] model.config.input_features (%d entries):", len(cfg_input))
        for _k, _ft in cfg_input.items():
            _t = getattr(_ft, "type", None)
            _t = _t.value if hasattr(_t, "value") else str(_t)
            _s = getattr(_ft, "shape", None)
            logger.info("    %s = type=%s shape=%s", _k, _t, _s)
        cfg_output = getattr(model.config, "output_features", {}) or {}
        logger.info("[CHECK-1] model.config.output_features (%d entries):", len(cfg_output))
        for _k, _ft in cfg_output.items():
            _t = getattr(_ft, "type", None)
            _t = _t.value if hasattr(_t, "value") else str(_t)
            _s = getattr(_ft, "shape", None)
            logger.info("    %s = type=%s shape=%s", _k, _t, _s)
        logger.info("[CHECK-1] max_state_dim=%s, max_action_dim=%s",
                     getattr(model.config, "max_state_dim", None),
                     getattr(model.config, "max_action_dim", None))
    except Exception as _e:
        logger.warning("[CHECK-1] dump input_features failed: %s", _e)

    # === VLA 推理: 加载 lerobot 完整 preprocessor/postprocessor pipeline ===
    # VLA 模型 (pi0/pi05/smolvla) 推理需要的处理远不止 image key rename:
    #   1. RenameObservations  — wrist → 模型期望的 cam (base_0_rgb 等)
    #   2. AddBatchDimension   — 加 batch 维
    #   3. RelativeActions     — 相对动作转换 (use_relative_actions=true 时)
    #   4. NormalizerProcessor — 用训练时 dataset stats (q01/q99 for pi05) 归一化 state/action
    #   5. Pi05PrepareStateTokenizer — pi05 把 state 离散化进 token
    #   6. TokenizerProcessor  — PaliGemma tokenizer 把 'task' 字符串 → observation.language.tokens
    #   7. DeviceProcessor     — 移到 GPU
    # 之前 worker 手动构造 batch 跳过这些, 导致缺 language.tokens / normalize 不一致 / state 没离散化等.
    # 正解: 用 lerobot make_pre_post_processors 加载训练时保存的完整 pipeline (含 stats).
    _vla_pre = _vla_post = None
    _vision_key = "observation.images.wrist"  # ACT/Diffusion 默认 (跟 dataset 一致, 它们 from-scratch)
    if is_vla:
        try:
            from lerobot.policies import make_pre_post_processors
            preprocessor_overrides = {}
            local_tokenizer = _resolve_hf_snapshot_path(
                "google/paligemma-3b-pt-224",
                required_files=("tokenizer.json", "tokenizer_config.json"),
            )
            if local_tokenizer:
                preprocessor_overrides["tokenizer_processor"] = {
                    "tokenizer_name": local_tokenizer,
                }
                logger.info("VLA tokenizer resolved from local HF cache: %s", local_tokenizer)
            else:
                logger.warning(
                    "VLA tokenizer local cache not found; tokenizer_processor will use its saved tokenizer_name"
                )
            _vla_pre, _vla_post = make_pre_post_processors(
                policy_cfg=model.config,
                pretrained_path=ckpt_path,
                preprocessor_overrides=preprocessor_overrides,
            )
            logger.info("VLA preprocessor/postprocessor loaded from %s", ckpt_path)
            # === 检查点 2: dump preprocessor steps + normalizer stats shape ===
            try:
                _steps = getattr(_vla_pre, "steps", []) or []
                logger.info("[CHECK-2] preprocessor pipeline %d steps:", len(_steps))
                for _i, _st in enumerate(_steps):
                    _name = getattr(_st, "name", _st.__class__.__name__)
                    logger.info("    [%d] %s", _i, _name)
                    # 重点 dump NormalizerProcessor 的 stats shape
                    if "Normalizer" in _name or "normalizer" in str(_st.__class__.__name__).lower():
                        _stats = getattr(_st, "stats", None) or {}
                        for _fk, _fs in _stats.items():
                            shapes = {kk: tuple(vv.shape) if hasattr(vv, "shape") else type(vv).__name__
                                       for kk, vv in (_fs or {}).items()}
                            logger.info("        [normalizer stats] %s: %s", _fk, shapes)
                    # rename_map
                    if "Rename" in _name:
                        rmap = getattr(_st, "rename_map", None)
                        if rmap:
                            logger.info("        [rename_map] %s", rmap)
            except Exception as _e:
                logger.warning("[CHECK-2] preprocessor dump failed: %s", _e)
            # preprocessor 内部 RenameObservations 自动处理图像 key 映射, 不需要手动 _vision_key.
            # 但保留 _vision_key 作输入端 dataset key (preprocessor 期望我们用 dataset 时的 key).
        except Exception as e:
            logger.warning("Failed to load VLA preprocessor: %s. Falling back to manual key rename "
                            "(no normalize/no tokenize → 推理质量会差, pi05 会缺 language.tokens 直接崩)",
                            e)
            # Fallback: 手动 rename image key (上一版逻辑), 缺 tokenizer 仍会崩
            try:
                img_feats = getattr(model.config, "image_features", None) or {}
                if img_feats:
                    _vision_key = next(iter(img_feats.keys()))
                    logger.info("Fallback inference image key: '%s'", _vision_key)
            except Exception:
                pass

    client = httpx.Client(base_url=server_url, timeout=10,
                          headers={"Authorization": f"Bearer {token}"} if token else {})

    logger.info("Inference: %s @ %dHz (chunk=%d, mode=%s) → %s",
                 device_id, fps, chunk_size, execution_mode, server_url)
    if camera_id:
        logger.info("Camera: %s", camera_id)
        try:
            client.post(f"/api/camera/{camera_id}/stream/mode", json={"mode": "inference"})
        except Exception:
            pass
    logger.info("Press Ctrl+C to stop")

    # 开启力矩
    try:
        client.post(f"/api/device/{device_id}/command", json={"torque": True})
    except Exception:
        pass

    n_servos = config.get("n_servos", 6)
    step_count = 0
    last_stop_check = time.time()  # 首次检查延迟 5 秒, 给模型加载后的初始化留缓冲
    interval = 1.0 / fps

    # EMA action smoothing — 抑制 ACT 预测噪声 + 舵机背隙抖动
    # alpha=1.0 关闭(原值直通); alpha 越小越平滑, 0.3 经验值 (新值30%+历史70%)
    _ema_alpha = float((chunk_params or {}).get("ema_alpha", 0.3))
    _ema_alpha = max(0.0, min(1.0, _ema_alpha))
    _ema_state = None
    logger.info("EMA smoothing alpha=%.2f (1.0=off)", _ema_alpha)

    # ===== 推理停止信号: 主循环只读 flag, HTTP 查询丢到后台线程 =====
    # 关键设计: 推理主循环每个 tick 必须 < 50ms (≥20Hz), 任何 HTTP 调用都不能在主循环里同步等.
    # 老版 _should_stop 同步调 /check-inference, 一次网络抖动整个循环卡 60s → 机械臂不动.
    # 新版: 后台线程每 5s polling, 主循环只 atomic 读 _stop_flag, 永远不卡.
    # 默认"继续推理", 只有 server 明确说 stop 才停, 网络挂了不影响推理流畅性 (符合
    # "用户不主动停就一直跑" 原则).
    import threading as _th_inf
    _stop_flag = False
    _stop_lock = _th_inf.Lock()
    _stop_reason = ""

    def _stop_poller():
        """后台线程: 每 5s 查 server 是否要停推理. 失败默认继续 (网络挂时不误停)."""
        nonlocal _stop_flag, _stop_reason
        while True:
            try:
                if not job_id:
                    time.sleep(5)
                    continue
                r = client.get(f"/api/training/jobs/{job_id}/check-inference", timeout=3.0)
                if r.status_code == 200:
                    data = r.json()
                    should_stop = False
                    reason = ""
                    if "should_stop" in data:
                        should_stop = bool(data.get("should_stop"))
                        reason = data.get("stop_reason") or "server requested"
                    else:
                        # 旧 server 兼容
                        if not data.get("running", True):
                            should_stop, reason = True, "server: not running"
                        elif not data.get("arm_online", True):
                            should_stop, reason = True, "arm offline"
                    if should_stop:
                        with _stop_lock:
                            _stop_flag = True
                            _stop_reason = reason
                        logger.info("[STOP-POLLER] 收到停止信号: %s", reason)
                        return
            except Exception as e:
                # 网络抖动/server 重启 → 静默继续, 不误停推理 (用户不主动停就一直跑)
                logger.debug("[STOP-POLLER] check 失败 (继续推理): %s", type(e).__name__)
            # 已停就退出, 没停 sleep 5s 再查
            with _stop_lock:
                if _stop_flag:
                    return
            time.sleep(5)

    _stop_poller_thread = _th_inf.Thread(target=_stop_poller, name="inference-stop-poller", daemon=True)
    _stop_poller_thread.start()
    logger.info("[INFERENCE] stop_poller 后台线程启动 (每 5s 查 server, 主循环不阻塞)")

    def _should_stop():
        """主循环用. atomic 读 flag, 不阻塞."""
        with _stop_lock:
            return _stop_flag

    # ===== 共用工具函数 =====
    def _read_state():
        """读取舵机状态, 返回 (servo_ids, state_normalized) 或 (None, None)"""
        try:
            # timeout=2s: 推理循环 ~5Hz, 舵机 state 查询 server 慢就跳过本周期不卡死
            r = client.get(f"/api/device/{device_id}/servos", timeout=2.0)
            servos = r.json().get("servos", [])
        except Exception:
            return None, None
        if not servos:
            return None, None
        sorted_s = sorted(servos, key=lambda s: s["id"])
        return [s["id"] for s in sorted_s], [s["pos"] / pos_max for s in sorted_s]

    # 摄像头图像缓存: cam 短暂断连 (WiFi 抖动 / 重连) 时用最近一帧撑住, 推理不间断.
    # 默认连续失败 30 帧 (约 3-6s) 才放弃, 给 cam 重连留时间.
    _last_cam_image = None
    _cam_fail_streak = 0
    _CAM_FAIL_THRESHOLD = 30   # 连续失败 30 次才返 None (大概 3-6s)

    def _read_camera():
        """读取摄像头图像. cam 短暂掉线时返回最近一帧 (推理连续性优先)."""
        nonlocal _last_cam_image, _cam_fail_streak
        if not use_vision or not camera_id:
            return None
        try:
            # timeout=3s: 摄像头 frame 可能稍慢 (jpeg encode), 给 3s 上限
            img_r = client.get(f"/api/camera/{camera_id}/frame", timeout=3.0)
            if img_r.status_code == 200 and img_r.content:
                img = Image.open(io.BytesIO(img_r.content)).convert("RGB").resize((640, 480))
                _last_cam_image = img
                if _cam_fail_streak > 0:
                    logger.info("[CAM] 图像恢复 (前 %d 次失败已用缓存撑住)", _cam_fail_streak)
                _cam_fail_streak = 0
                return img
        except Exception as e:
            logger.debug("[CAM] frame 获取失败 (%d/%d): %s",
                          _cam_fail_streak + 1, _CAM_FAIL_THRESHOLD, type(e).__name__)
        # 取图失败 (网络 / 204 / cam 离线)
        _cam_fail_streak += 1
        if _cam_fail_streak == 1:
            logger.debug("[CAM] 首次失败, 用缓存图继续推理")
        elif _cam_fail_streak == 10:
            logger.warning("[CAM] 连续 10 次失败, 仍用缓存图; 若达 %d 次放弃",
                           _CAM_FAIL_THRESHOLD)
        if _cam_fail_streak >= _CAM_FAIL_THRESHOLD:
            if _last_cam_image is not None:
                logger.warning("[CAM] 连续 %d 次失败, 放弃缓存返 None (用户应检查 cam)",
                               _cam_fail_streak)
                _last_cam_image = None
            return None
        return _last_cam_image   # 用上次成功的图撑住

    def _build_obs(state_list, cam_image):
        """构建 LeRobot 观测 dict.

        ACT/Diffusion: manual MEAN_STD normalization + ImageNet normalization
        VLA (SmolVLA/Pi0): raw values — the VLM preprocessor handles normalization
        """
        state_t = torch.tensor([state_list], dtype=torch.float32)
        if torch.cuda.is_available():
            state_t = state_t.cuda()

        if is_vla and _vla_pre is not None:
            # VLA + 完整 lerobot pipeline 路径 (推荐)
            # raw obs 用 dataset 时的 key (wrist), 不带 batch dim — preprocessor 会自动:
            #   RenameObservations (wrist → cam_high/base_0_rgb 等)
            #   AddBatchDimension
            #   Normalizer (用训练时 dataset stats: q01/q99 for pi05)
            #   Pi05PrepareStateTokenizer (state 离散化)
            #   TokenizerProcessor (task → observation.language.tokens)
            #   DeviceProcessor (move to GPU)
            raw = {
                "observation.state": state_t.squeeze(0),  # (n_servos,) 不带 batch dim
                "task": task_description,
            }
            if use_vision:
                img = cam_image or Image.new("RGB", (640, 480))
                img_arr = np.array(img, dtype=np.float32) / 255.0
                img_t = torch.from_numpy(img_arr.transpose(2, 0, 1))  # (3, H, W) 不带 batch
                raw[_vision_key] = img_t
            # === 检查点 3: raw obs shape (preprocessor 输入) ===
            if not getattr(_build_obs, "_logged_check3", False):
                logger.info("[CHECK-3] raw obs (preprocessor 输入), 首次推理 dump:")
                for _k, _v in raw.items():
                    _info = (f"shape={tuple(_v.shape)} dtype={_v.dtype}"
                              if hasattr(_v, "shape") else f"value={_v!r}")
                    logger.info("    %s: %s", _k, _info)
                _build_obs._logged_check3 = True
            try:
                processed = _vla_pre(raw)
            except Exception as _e:
                # 预处理失败时 dump 详细信息便于诊断
                logger.error("[CHECK-3] preprocessor failed: %s", _e)
                logger.error("    raw keys: %s", list(raw.keys()))
                for _k, _v in raw.items():
                    if hasattr(_v, "shape"):
                        logger.error("    %s shape=%s dtype=%s", _k, tuple(_v.shape), _v.dtype)
                raise
            # === 检查点 4: 处理后 batch shape (model 输入) ===
            if not getattr(_build_obs, "_logged_check4", False):
                logger.info("[CHECK-4] processed batch (model 输入), 首次推理 dump:")
                if isinstance(processed, dict):
                    for _k, _v in processed.items():
                        _info = (f"shape={tuple(_v.shape)} dtype={_v.dtype}"
                                  if hasattr(_v, "shape") else f"type={type(_v).__name__}")
                        logger.info("    %s: %s", _k, _info)
                else:
                    logger.info("    type=%s, attrs=%s", type(processed).__name__,
                                 [a for a in dir(processed) if not a.startswith("_")][:8])
                _build_obs._logged_check4 = True
            return processed
        elif is_vla:
            # VLA + preprocessor 加载失败的 fallback (没 normalize/没 tokenize, pi05 仍会缺 language.tokens)
            obs = {"observation.state": state_t}
            if use_vision:
                img = cam_image or Image.new("RGB", (640, 480))
                img_arr = np.array(img, dtype=np.float32) / 255.0
                img_t = torch.from_numpy(img_arr.transpose(2, 0, 1)).unsqueeze(0)
                if torch.cuda.is_available():
                    img_t = img_t.cuda()
                obs[_vision_key] = img_t
            obs["task"] = task_description
        else:
            # ACT/Diffusion: manual MEAN_STD normalization. ACT from-scratch 训练时
            # input_features 自动从 dataset 推导, 推理时 _vision_key 跟 dataset 一致 (wrist).
            state_norm = (state_t - _state_mean) / (_state_std + 1e-8)
            obs = {"observation.state": state_norm}
            if use_vision:
                img = cam_image or Image.new("RGB", (640, 480))
                img_arr = np.array(img, dtype=np.float32) / 255.0
                img_t = torch.from_numpy(img_arr.transpose(2, 0, 1)).unsqueeze(0)
                if torch.cuda.is_available():
                    img_t = img_t.cuda()
                img_mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
                img_std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
                if torch.cuda.is_available():
                    img_mean, img_std = img_mean.cuda(), img_std.cuda()
                img_t = (img_t - img_mean) / img_std
                obs[_vision_key] = img_t
        return obs

    def _unnorm_action(action_tensor):
        """反归一化 action tensor → numpy [0,1].

        优先级:
        1. VLA + _vla_post 加载成功 → 走 lerobot postprocessor (UnnormalizerProcessor +
           AbsoluteActionsProcessor + DeviceProcessor)
        2. ACT/Diffusion 有 manual stats → manual un-normalization
        3. 兜底 → 直接 clamp (可能不准, 但不会崩)
        """
        at = action_tensor if isinstance(action_tensor, torch.Tensor) else action_tensor.get("action", list(action_tensor.values())[0])
        if is_vla and _vla_post is not None:
            # === 截断 padded action 到 dataset 实际维度 ===
            # VLA model 输出 action 总是 padded 到 max_action_dim (base config, pi0/pi05 = 32).
            # LoRA fine-tune 时 lerobot 不会更新 config.output_features.action.shape (仍是 32),
            # 但保存的 normalizer/unnormalizer stats 是 dataset 实际维度 (n_servos, 例如 6).
            # 推理时 _vla_post 的 UnnormalizerProcessor 用 6 维 stats 跟 32 维 action broadcast
            # → "tensor a (32) vs b (6)" 报错. 这里先 truncate 再喂 postprocessor.
            if hasattr(at, "shape") and at.dim() >= 1 and at.shape[-1] > n_servos:
                if not getattr(_unnorm_action, "_logged_check5", False):
                    logger.info("[CHECK-5] action truncate: model 输出 shape=%s → dataset 维度 %d",
                                 tuple(at.shape), n_servos)
                    _unnorm_action._logged_check5 = True
                at = at[..., :n_servos]
            # postprocessor: unnormalize → absolute (relative_actions 时) → cpu
            try:
                unnorm = _vla_post(at)
            except Exception as _e:
                logger.error("[CHECK-5] _vla_post failed: %s; action shape=%s",
                              _e, tuple(at.shape) if hasattr(at, "shape") else None)
                raise
            if isinstance(unnorm, torch.Tensor):
                return unnorm.clamp(0, 1).cpu().numpy()
            # postprocessor 返回 dict 时 (RobotAction style), 取 action key
            if isinstance(unnorm, dict):
                act_t = unnorm.get("action", list(unnorm.values())[0])
                return act_t.clamp(0, 1).cpu().numpy() if isinstance(act_t, torch.Tensor) else np.array(list(unnorm.values()))
            return at.clamp(0, 1).cpu().numpy()
        if not _has_manual_norm:
            # VLA fallback or 没有 manual stats — model 输出已在 [0,1] 附近
            # 仍需 truncate 到 n_servos 防止 ESP32 收到额外 padded 值
            if hasattr(at, "shape") and at.dim() >= 1 and at.shape[-1] > n_servos:
                at = at[..., :n_servos]
            return at.clamp(0, 1).cpu().numpy()
        # ACT/Diffusion manual unnormalize
        action_01 = at * _action_std + _action_mean
        return action_01.clamp(0, 1).cpu().numpy()

    def _ema_smooth(action_01):
        """跨 chunk 持续 EMA 低通滤波, 输入 1D 单帧或 2D (T, n_servos) 块."""
        nonlocal _ema_state
        if _ema_alpha >= 1.0:
            return action_01
        arr = np.asarray(action_01, dtype=np.float32)
        if arr.ndim == 1:
            if _ema_state is None:
                _ema_state = arr.copy()
            else:
                _ema_state = _ema_alpha * arr + (1.0 - _ema_alpha) * _ema_state
            return _ema_state.copy()
        out = np.empty_like(arr)
        if _ema_state is None:
            _ema_state = arr[0].copy()
        for i in range(arr.shape[0]):
            _ema_state = _ema_alpha * arr[i] + (1.0 - _ema_alpha) * _ema_state
            out[i] = _ema_state
        return out

    # ===== 执行循环 =====
    try:
        if execution_mode == "original" or not hasattr(model, 'predict_action_chunk'):
            # ===== 原始模式: select_action 逐步推理 (不改任何原有逻辑) =====
            while not _should_stop():
                t0 = time.perf_counter()
                servo_ids, state = _read_state()
                if servo_ids is None:
                    time.sleep(0.3)
                    continue

                cam_image = _read_camera()
                t_infer = time.perf_counter()

                if hasattr(model, 'select_action'):
                    obs = _build_obs(state, cam_image)
                    with torch.no_grad():
                        action_out = model.select_action(obs)
                    action = _unnorm_action(action_out).flatten().tolist()
                else:
                    action = model.predict(state)

                infer_ms = (time.perf_counter() - t_infer) * 1000
                action = _ema_smooth(np.asarray(action, dtype=np.float32))
                positions = [int(max(0, min(pos_max, a * pos_max))) for a in action]
                cmds = [{"id": servo_ids[i], "position": positions[i], "speed": 0}
                        for i in range(min(len(positions), len(servo_ids)))]
                try:
                    # timeout=2.0: 推理 5Hz 周期 200ms, 单次 HTTP 给 2s 容错;
                    # 超时即跳过本周期 (server/ESP32 假死时不阻塞推理线程)
                    client.post(f"/api/device/{device_id}/command",
                                json={"commands": cmds}, timeout=2.0)
                except Exception:
                    pass

                step_count += 1
                elapsed = time.perf_counter() - t0
                sleep_time = max(0, interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                actual_hz = 1.0 / max(elapsed, 0.001)
                print(f"\r  [original] step {step_count}  {actual_hz:.0f}Hz  infer: {infer_ms:.1f}ms  ", end="", flush=True)

        else:
            # ===== Chunk 模式: predict_action_chunk → ChunkOptimizer → batch 发送 =====
            from box2robot_gpu_worker.chunk_optimizer import ChunkOptimizer

            # Diffusion 的 predict_action_chunk 直接从空的 self._queues 读取 → 必须
            # 先 populate_queues, 否则触发 "torch.stack expects a non-empty TensorList".
            # 其他 queue 类策略 (pi0/smolvla/multi_task_dit/...) 在 predict_action_chunk
            # 内部已自行 populate, 不需要外部介入.
            _needs_manual_populate = (model_type == "diffusion")
            if _needs_manual_populate:
                sys.path.insert(0, str(Path(__file__).parent.parent / "lerobot" / "src"))
                from lerobot.policies.utils import populate_queues as _populate_queues
                from lerobot.utils.constants import OBS_IMAGES as _OBS_IMAGES

            # 读一次确定 servo 数量
            servo_ids, state = _read_state()
            while servo_ids is None and not _should_stop():
                time.sleep(0.5)
                servo_ids, state = _read_state()
            if _should_stop():
                raise KeyboardInterrupt

            cp = chunk_params or {}
            optimizer = ChunkOptimizer(
                chunk_size=chunk_size,
                strategy=execution_mode,
                n_servos=len(servo_ids),
                fixed_exec_steps=int(cp.get("fixed_exec_steps", 0)),
                certainty_threshold=float(cp.get("certainty_threshold", 0.15)),
                min_execute=int(cp.get("min_execute", 3)),
                max_skip=int(cp.get("max_skip", 15)),
                overlap_ratio=float(cp.get("overlap_ratio", 0.5)),
            )
            logger.info("ChunkOptimizer: strategy=%s, chunk=%d, servos=%d, params=%s",
                         execution_mode, chunk_size, len(servo_ids), cp)

            while not _should_stop():
                t0 = time.perf_counter()

                # 1. 读取当前状态 + 摄像头
                servo_ids, state = _read_state()
                if servo_ids is None:
                    time.sleep(0.3)
                    continue
                cam_image = _read_camera()

                # 2. 推理: 获取完整 chunk
                t_infer = time.perf_counter()
                obs = _build_obs(state, cam_image)
                with torch.no_grad():
                    if _needs_manual_populate:
                        # 与 Diffusion.select_action 前置逻辑一致: 先把多路 image
                        # 堆叠到 OBS_IMAGES key, 再 populate_queues, 最后调用
                        # predict_action_chunk (它内部从 queue 读取)
                        batch = dict(obs)
                        img_feats = getattr(model.config, 'image_features', None)
                        if img_feats:
                            batch[_OBS_IMAGES] = torch.stack(
                                [batch[k] for k in img_feats], dim=-4)
                        model._queues = _populate_queues(model._queues, batch)
                        raw_chunk = model.predict_action_chunk(batch)
                    else:
                        raw_chunk = model.predict_action_chunk(obs)  # (1, chunk_size, n_servos)
                # 反归一化: (1, chunk_size, n_servos) → (chunk_size, n_servos) [0,1]
                chunk_01 = (raw_chunk[0] * _action_std + _action_mean).clamp(0, 1).cpu().numpy()
                infer_ms = (time.perf_counter() - t_infer) * 1000

                # 3. ChunkOptimizer 决定执行步数
                n_exec, batch_actions = optimizer.feed_chunk(chunk_01)
                batch_actions = _ema_smooth(batch_actions)

                # 4. 转换为 play_batch 帧格式, 一次性发给 ESP32
                base_t = 0
                frame_interval_ms = int(1000 / fps)
                frames = []
                for i in range(n_exec):
                    positions = [int(max(0, min(pos_max, a * pos_max))) for a in batch_actions[i]]
                    frames.append({"t": base_t + i * frame_interval_ms, "p": positions})

                try:
                    # timeout=3.0: batch 帧多, 给 server 队列+ESP32 转发更长容错;
                    # 超时即跳过本批 (避免阻塞推理线程, watchdog 由外层 _should_stop 兜底)
                    client.post(f"/api/device/{device_id}/inference/batch",
                                json={"frames": frames, "ids": servo_ids}, timeout=3.0)
                except Exception:
                    pass

                step_count += n_exec
                elapsed_infer = time.perf_counter() - t0

                # 5. 等待 ESP32 执行完这批帧 (N步 * 帧间隔 - 已用时间)
                exec_time = n_exec * (1.0 / fps)
                wait_time = max(0, exec_time - elapsed_infer)
                if wait_time > 0:
                    # 分段 sleep, 中间检查 stop
                    check_interval = 1.0
                    waited = 0
                    while waited < wait_time and not _should_stop():
                        chunk_wait = min(check_interval, wait_time - waited)
                        time.sleep(chunk_wait)
                        waited += chunk_wait

                total_elapsed = time.perf_counter() - t0
                infer_hz = 1.0 / max(total_elapsed, 0.001)
                exec_hz = n_exec / max(total_elapsed, 0.001)
                print(f"\r  [{execution_mode}] step {step_count}  exec:{n_exec}@{fps}Hz  "
                      f"infer:{infer_ms:.0f}ms  cycle:{infer_hz:.1f}Hz  eff:{exec_hz:.0f}Hz  ", end="", flush=True)

    except KeyboardInterrupt:
        print("\nStopping...")

    # Cleanup
    try:
        client.post(f"/api/device/{device_id}/command", json={"torque": False})
    except Exception:
        pass
    if camera_id:
        try:
            client.post(f"/api/camera/{camera_id}/stream/mode", json={"mode": "idle"})
        except Exception:
            pass
    print("Stopped.")


def main():
    parser = argparse.ArgumentParser(description="Box2Robot Training Worker")
    sub = parser.add_subparsers(dest="command")

    # Train: process a job from server
    train = sub.add_parser("train", help="Download dataset and train")
    train.add_argument("--server", "-s", type=str, required=True, help="Server URL")
    train.add_argument("--job-id", "-j", type=str, required=True, help="Training job ID")
    train.add_argument("--key", "-k", type=str, default="", help="Pairing key")
    train.add_argument("--output", "-o", type=str, default="outputs", help="Output directory")

    # Inference: run trained model against remote arm
    infer = sub.add_parser("inference", help="Run inference on remote arm")
    infer.add_argument("--model", "-m", type=str, required=True, help="Model directory")
    infer.add_argument("--server", "-s", type=str, required=True, help="Server URL")
    infer.add_argument("--device", "-d", type=str, required=True, help="Device ID")
    infer.add_argument("--token", type=str, default="", help="Auth token")
    infer.add_argument("--fps", type=int, default=20, help="Inference FPS")

    args = parser.parse_args()

    if args.command == "train":
        worker = TrainingWorker(args.server, args.key, args.output)
        worker.process_job(args.job_id)

    elif args.command == "inference":
        run_inference_server(args.model, args.server, args.device, args.token, fps=args.fps)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
