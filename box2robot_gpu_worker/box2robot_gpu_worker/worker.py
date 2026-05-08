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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("box2robot.worker")


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

        model_type = job_info.get("model_type", "act")
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
            # 训练入口: 全部走 LeRobot pipeline (ACT/Diffusion/VLA).
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
    DATASET_VISION_KEY = "observation.images.top"

    # 已知 VLA base 期望的相机 key (用于 _get_base_visual_keys 离线/网络失败兜底).
    # 数据来源: 各 base 的 config.json input_features. 第一个 key 是主视角,
    # rename_map 把我们的 'observation.images.top' 映射到这里; 其余 cam 会被
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
            "vision_backbone", "resize_shape", "crop_ratio", "crop_shape", "crop_is_random",
            "pretrained_backbone_weights", "use_group_norm",
            "spatial_softmax_num_keypoints", "use_separate_rgb_encoder_per_camera",
            "down_dims", "kernel_size", "n_groups",
            "diffusion_step_embed_dim", "use_film_scale_modulation",
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

    @classmethod
    def _add_policy_param(cls, cmd: list, model_type: str, key: str, value) -> bool:
        """加 --policy.{key}={value} 到 cmd, 但只在该 model 实际支持时.

        前端 schema 可能给所有模型加了通用字段 (grad_clip_norm/dtype/...), 但有些 model
        config 没暴露这些字段 — 直接传会撞 draccus 'unrecognized arguments' 让训练 exit 2.
        本函数:
        1. 把前端 key 通过 PARAM_ALIASES 映射到真实 config 字段名 (如 lr→optimizer_lr)
        2. 用 POLICY_FIELDS[model_type] 校验, 不在白名单的静默跳过 + warning
        3. 通过则 append --policy.{真实字段}={value}

        Returns True if added, False if skipped.
        """
        real_key = cls.PARAM_ALIASES.get(key, key)
        fields = cls.POLICY_FIELDS.get(model_type)
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

        # 3. 拼最终信息: 关键字提示 + 最后 5 行原始日志
        # (信号类终止已在第 1 步直接 return, 这里 sig_msg 必空, 不再拼接)
        parts = [f"训练失败 (exit code {returncode})"]
        if kw_hint:
            parts.append(kw_hint)
        if tail_lines:
            parts.append("最后日志:\n  " + "\n  ".join(tail_lines[-5:]))
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
            "    'observation.images.top': torch.zeros(3, 480, 640, dtype=torch.uint8),",
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

        VLA base 的相机 key 通常和我们 Box2Robot dataset 的 'observation.images.top'
        不一样, 直接训会抛 "All image features are missing from the batch". 拿到 base
        期望的 key 列表后, 外层用 --rename_map 把 dataset 的 top 映射到 base 第一个 cam,
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
        import subprocess
        from box2robot_gpu_worker.convert import convert

        is_vla = model_type in self.VLA_MODELS

        # 优先用 process_job 透传过来的指纹 (与 dataset_ids 同源, 防止 server 删轨迹后口径错位).
        # 仅在 fingerprint 缺失 (旧调用路径) 时退回用 trajectories.id 现算.
        if not ds_fingerprint:
            ds_fingerprint = self._ds_fingerprint([t.get("id", "") for t in trajectories])
        # === 下载 cache 共享 (按 fp), LeRobot dataset 每 job 独立 ===
        # 共享: cache/ds_<fp>/  -- 同 fp 多 job 共享原始下载 (节流量, multi-slot lock 串行下载)
        # 独立: datasets/<repo>/ -- 每 job 一份转换好的 dataset, 同数据集跑不同 model 互不影响
        #       stats.json (pi05 quantile augment 不污染另一 job)
        ds_cache_dir = Path(__file__).parent.parent / "cache" / f"ds_{ds_fingerprint}"
        ds_dir = ds_cache_dir / "dataset"
        img_dir = ds_cache_dir / "images"
        # job_id 从 model_dir 解析 (model_dir = outputs/<job_id>/model)
        job_id_for_ds = Path(model_dir).parent.name
        # repo_id 含 job_id 前缀 + fp 后缀, 既可读又确保 multi-slot 同 fp 不冲突
        repo_id = f"box2robot-{job_id_for_ds}-{ds_fingerprint[:8]}"

        # Step 1: Convert to LeRobot format (per-job 目录, 同 dataset 多 job 各保一份)
        has_images = img_dir.is_dir() and any(img_dir.iterdir())
        datasets_root = Path(__file__).parent.parent / "datasets" / repo_id
        dataset_marker = datasets_root / "meta" / "info.json"

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
            else:
                logger.info("Converting to LeRobot format (vision=%s)...", has_images)
                if progress_cb:
                    progress_cb(0, train_steps, {"phase": "converting", "message": "转换为 LeRobot 数据集格式..."})
                convert(
                    input_path=ds_dir,
                    repo_id=repo_id,
                    task_description=custom_params.get("task", "manipulation task"),
                    fps=20,
                    images_dir=img_dir if has_images else None,
                    root=datasets_root,
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

        cmd += [
            f"--dataset.repo_id={repo_id}",
            f"--dataset.root={datasets_root}",
            f"--steps={train_steps}",
            f"--batch_size={batch_size}",
            f"--num_workers=0",
            f"--output_dir={model_dir}",
            "--policy.push_to_hub=false",
            "--wandb.enable=false",
            f"--save_freq={max(100, min(5000, train_steps // 5))}",
            "--log_freq=1",
        ]

        if is_vla:
            # VLA: fine-tune from pretrained base
            pretrained_path = custom_params.get(
                "pretrained_path",
                self.VLA_PRETRAINED.get(model_type, f"lerobot/{model_type}_base"),
            )
            cmd.append(f"--policy.path={pretrained_path}")

            # === 图像 key 适配 (rename_map) ===
            # base 训练时用了不同数据集 (pi0_base=aloha, pi05_base=droid, smolvla=...),
            # input_features 里的 cam 命名跟我们 Box2Robot dataset 的 'observation.images.top'
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
                    # 把我们的 top 映射到 base 第一个 cam (一般是主视角, 如 droid 的 exterior_1_left)
                    rename_map_dict = {self.DATASET_VISION_KEY: base_visual_keys[0]}
                    rename_str = json.dumps(rename_map_dict)
                    cmd.append(f"--rename_map={rename_str}")
                    n_padded = max(0, len(base_visual_keys) - 1)
                    logger.info("[RENAME-MAP] 来源: 自动生成 (base 第一个 cam)")
                    logger.info("[RENAME-MAP] dict: %s", rename_map_dict)
                    logger.info("[RENAME-MAP] CLI arg: --rename_map=%s", rename_str)
                    logger.info("[RENAME-MAP] %s -> %s (base 共 %d 个 cam; %d 个会被 -1 填充)",
                                self.DATASET_VISION_KEY, base_visual_keys[0],
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
            # batch 里仍然是 'observation.images.top' → 训练第一步报 "All image features are missing".
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
            # SmolVLA: freeze vision + train expert only 默认开 (省显存); 其它 VLA 默认关
            if model_type == "smolvla":
                self._add_policy_param(cmd, model_type, "freeze_vision_encoder",
                                        custom_params.get("freeze_vision_encoder", "true"))
                self._add_policy_param(cmd, model_type, "train_expert_only",
                                        custom_params.get("train_expert_only", "true"))
                self._add_policy_param(cmd, model_type, "train_state_proj",
                                        custom_params.get("train_state_proj", "true"))
            # Pi0/Pi05: 可选 compile + expert-only
            elif model_type in ("pi0", "pi0_fast", "pi05"):
                if custom_params.get("compile_model"):
                    self._add_policy_param(cmd, model_type, "compile_model",
                                            custom_params["compile_model"])
                if custom_params.get("train_expert_only"):
                    self._add_policy_param(cmd, model_type, "train_expert_only",
                                            custom_params["train_expert_only"])
            # VLA chunk_size/n_action_steps: use model defaults (50) unless explicitly overridden
            if chunk_size > 1 and custom_params.get("override_chunk_size"):
                self._add_policy_param(cmd, model_type, "chunk_size", chunk_size)
                self._add_policy_param(cmd, model_type, "n_action_steps", chunk_size)
        else:
            # ACT/Diffusion/GR00T 等: train from scratch
            cmd.append(f"--policy.type={model_type}")
            cmd.append(f"--policy.repo_id=box2robot/{repo_id}")
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
        if resume_from_step:
            ckpt_root = Path(model_dir) / "checkpoints"
            candidates = [
                ckpt_root / f"{int(resume_from_step):06d}",
                ckpt_root / str(resume_from_step),
            ]
            ckpt_path = next((c for c in candidates if c.exists()), None)
            if ckpt_path is not None:
                cmd.append("--resume=true")
                cmd.append(f"--checkpoint_path={ckpt_path}")
                logger.info("Resuming from checkpoint: %s (step %d)", ckpt_path, resume_from_step)
            else:
                logger.warning("Checkpoint step %d not found in %s, training from scratch",
                               resume_from_step, ckpt_root)
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
                         # chunk_size / n_action_steps / horizon 在主分支已处理 (传 server
                         # 选定的统一值), 不再从 custom_params 透传以免重复
                         "chunk_size", "n_action_steps", "horizon",
                         # peft_* 已在上面以顶层 --peft.* 形式处理, 不要再走 --policy.*
                         "peft_enable", "peft_method_type", "peft_r",
                         "peft_target_modules", "peft_full_training_modules"}
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
        metrics_re = re.compile(r'\bstep:(\d+)\b.*\bloss:([\d.e+-]+)\b')
        # Match tqdm progress: "Training:  15%|...| 150/10000 [01:23<..."
        tqdm_re = re.compile(r'Training:\s+\d+%\|.*\|\s*(\d+)/(\d+)\s+\[')

        for line in proc.stdout:
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

            # 解析 INFO metrics 行 (有 loss 数据)
            m = metrics_re.search(line)
            if m:
                try:
                    step = int(m.group(1))
                    loss = float(m.group(2))
                    if step > last_report_step:
                        metrics = {"loss": loss}
                        for kv in re.findall(r'(\w+):([\d.e+-]+)', line):
                            if kv[0] not in ("step", "smpl", "ep"):
                                try:
                                    metrics[kv[0]] = float(kv[1])
                                except ValueError:
                                    pass
                        metrics["log"] = line
                        progress_cb(step, train_steps, metrics)
                        last_report_step = step
                except Exception:
                    pass
            # 解析 tqdm 进度条
            elif is_tqdm:
                tm = tqdm_re.search(line)
                if tm:
                    try:
                        step = int(tm.group(1))
                        if step > last_report_step:
                            progress_cb(step, train_steps, {"log": line})
                            last_report_step = step
                    except Exception:
                        pass
            # 其他重要行 (WARNING/ERROR/INFO 但非 metrics)
            elif any(k in line for k in ("WARNING", "ERROR", "Creating", "End of", "Checkpoint", "Start")):
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
            "task_description": custom_params.get("task", "manipulation task"),
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
            data["error_msg"] = error_msg
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
    import io
    import numpy as np
    import torch
    from PIL import Image

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
    #   1. RenameObservations  — top → 模型期望的 cam (base_0_rgb 等)
    #   2. AddBatchDimension   — 加 batch 维
    #   3. RelativeActions     — 相对动作转换 (use_relative_actions=true 时)
    #   4. NormalizerProcessor — 用训练时 dataset stats (q01/q99 for pi05) 归一化 state/action
    #   5. Pi05PrepareStateTokenizer — pi05 把 state 离散化进 token
    #   6. TokenizerProcessor  — PaliGemma tokenizer 把 'task' 字符串 → observation.language.tokens
    #   7. DeviceProcessor     — 移到 GPU
    # 之前 worker 手动构造 batch 跳过这些, 导致缺 language.tokens / normalize 不一致 / state 没离散化等.
    # 正解: 用 lerobot make_pre_post_processors 加载训练时保存的完整 pipeline (含 stats).
    _vla_pre = _vla_post = None
    _vision_key = "observation.images.top"  # ACT/Diffusion 默认 (跟 dataset 一致, 它们 from-scratch)
    if is_vla:
        try:
            from lerobot.policies import make_pre_post_processors
            _vla_pre, _vla_post = make_pre_post_processors(
                policy_cfg=model.config,
                pretrained_path=ckpt_path,
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

    _stop_flag = False

    def _should_stop():
        nonlocal last_stop_check
        if _stop_flag:
            return True
        now = time.time()
        if now - last_stop_check < 5:
            return False
        last_stop_check = now
        if not job_id:
            return False
        try:
            # 检查 Server 是否停止了推理
            r = client.get(f"/api/training/jobs/{job_id}/check-inference")
            if r.status_code == 200:
                data = r.json()
                # v1.0+ 单源信号 should_stop + stop_reason; 老 server 没这字段时回退到 running/arm_online
                if "should_stop" in data:
                    if data.get("should_stop"):
                        logger.info("推理停止 (reason=%s)", data.get("stop_reason") or "unknown")
                        return True
                else:
                    # 旧 server 兼容路径
                    if not data.get("running", True):
                        logger.info("推理已被 Server 停止")
                        return True
                    if not data.get("arm_online", True):
                        logger.warning("机械臂离线，自动停止推理")
                        return True
        except Exception:
            pass
        return False

    # ===== 共用工具函数 =====
    def _read_state():
        """读取舵机状态, 返回 (servo_ids, state_normalized) 或 (None, None)"""
        try:
            r = client.get(f"/api/device/{device_id}/servos")
            servos = r.json().get("servos", [])
        except Exception:
            return None, None
        if not servos:
            return None, None
        sorted_s = sorted(servos, key=lambda s: s["id"])
        return [s["id"] for s in sorted_s], [s["pos"] / pos_max for s in sorted_s]

    def _read_camera():
        """读取摄像头图像"""
        if not use_vision or not camera_id:
            return None
        try:
            img_r = client.get(f"/api/camera/{camera_id}/frame")
            if img_r.status_code == 200 and img_r.content:
                return Image.open(io.BytesIO(img_r.content)).convert("RGB").resize((640, 480))
        except Exception:
            pass
        return None

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
            # raw obs 用 dataset 时的 key (top), 不带 batch dim — preprocessor 会自动:
            #   RenameObservations (top → cam_high/base_0_rgb 等)
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
                raw["observation.images.top"] = img_t
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
            # input_features 自动从 dataset 推导, 推理时 _vision_key 跟 dataset 一致 (top).
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
                    client.post(f"/api/device/{device_id}/command", json={"commands": cmds})
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
                    client.post(f"/api/device/{device_id}/inference/batch",
                                json={"frames": frames, "ids": servo_ids})
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
