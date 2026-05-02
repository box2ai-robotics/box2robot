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

    def process_job(self, job_id: str, resume_from_step: int = None):
        """Download dataset, train, report progress, complete.

        Args:
            resume_from_step: If set, resume training from this checkpoint step
                              (uses LeRobot --resume --checkpoint_path)
        """
        import hashlib
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

        model_type = job_info.get("model_type", "mlp")
        train_steps = job_info.get("train_steps", 10000)
        batch_size = job_info.get("batch_size", 64)
        chunk_size = job_info.get("chunk_size", 1)
        custom_params = job_info.get("custom_params", {})
        if isinstance(custom_params, str):
            custom_params = json.loads(custom_params) if custom_params else {}
        dataset_ids = job_info.get("dataset_ids", [])
        if isinstance(dataset_ids, str):
            dataset_ids = json.loads(dataset_ids) if dataset_ids else []

        # 2. 用 dataset_ids 算特征码, 检查本地缓存
        ds_fingerprint = hashlib.md5("_".join(sorted(dataset_ids)).encode()).hexdigest()[:12]
        ds_cache_dir = Path(__file__).parent.parent / "cache" / f"ds_{ds_fingerprint}"
        ds_dir = ds_cache_dir / "dataset"
        img_base = ds_cache_dir / "images"

        if (ds_dir / "traj_0000.json").exists():
            # 缓存命中 — 跳过下载
            logger.info("Dataset CACHED (fingerprint=%s), skip download!", ds_fingerprint)
            has_any_images = img_base.is_dir() and any(img_base.iterdir())
            # 从缓存加载轨迹 (训练需要)
            trajectories = []
            for f in sorted(ds_dir.glob("traj_*.json")):
                with open(f) as fh:
                    trajectories.append(json.load(fh))
        else:
            # 缓存未命中 — 下载完整数据集
            logger.info("Downloading dataset (fingerprint=%s)...", ds_fingerprint)
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

        logger.info("Dataset: %d trajectories, model=%s, steps=%d (fingerprint=%s)",
                     len(trajectories), model_type, train_steps, ds_fingerprint)

        # 2. Preprocessing + Train
        self._report_progress(job_id, 0, train_steps, {
            "phase": "preprocessing",
            "message": f"数据集下载完成: {len(trajectories)} 条轨迹" + (f", {sum(1 for d in img_base.iterdir() if d.is_dir())} 组图像" if has_any_images else ""),
        })
        self._report_status(job_id, "training")
        model_dir = str(self.output_dir / job_id / "model")

        def progress_cb(step, total, metrics):
            if self._should_stop or self._should_pause:
                return
            resp = self._report_progress(job_id, step, total, metrics)
            if resp and resp.get("should_stop"):
                logger.warning("Server requested stop")
                self._should_stop = True
            elif resp and resp.get("should_pause"):
                logger.warning("Server requested pause — will save checkpoint and stop")
                self._should_pause = True

        try:
            if model_type == "mlp":
                from box2robot_gpu_worker.mlp_policy import train_mlp
                result = train_mlp(
                    trajectories=trajectories,
                    output_dir=model_dir,
                    train_steps=train_steps,
                    batch_size=batch_size,
                    progress_callback=progress_cb,
                    custom_params=custom_params,
                )
            else:
                # For ACT and other models, use the LeRobot training pipeline
                # First convert trajectories to LeRobot format, then train
                result = self._train_lerobot(
                    trajectories, model_type, model_dir,
                    train_steps, batch_size, chunk_size, custom_params, progress_cb,
                    resume_from_step=resume_from_step,
                )

            if self._should_stop:
                self._report_status(job_id, "cancelled")
                return

            if self._should_pause:
                # 暂停: 上报 checkpoint 列表，保持 paused 状态 (server 已设为 paused)
                ckpts = self._scan_checkpoints(model_dir)
                if ckpts:
                    self._report_progress(job_id, ckpts[-1], train_steps,
                                          {"checkpoints": ckpts, "log": f"训练已暂停 (step {ckpts[-1]})，已保存 {len(ckpts)} 个 checkpoint"})
                logger.info("Training paused at checkpoint. Checkpoints: %s", ckpts)
                return

            # 3. Complete
            self._report_status(job_id, "completed", model_path=model_dir)
            logger.info("Training complete: %s", model_dir)
            logger.info("Results: %s", json.dumps(result, indent=2))

        except Exception as e:
            logger.error("Training failed: %s", e, exc_info=True)
            self._report_status(job_id, "failed", error_msg=str(e))

    # VLA models that fine-tune from pretrained base (vision-language-action)
    VLA_MODELS = {"smolvla", "pi0", "pi0_fast", "pi05"}
    # Default pretrained base for each VLA model (HuggingFace Hub)
    VLA_PRETRAINED = {
        "smolvla": "lerobot/smolvla_base",
        "pi0": "lerobot/pi0_base",
        "pi0_fast": "lerobot/pi0_fast_base",
        "pi05": "lerobot/pi05_base",
    }

    def _train_lerobot(self, trajectories, model_type, model_dir,
                       train_steps, batch_size, chunk_size, custom_params, progress_cb,
                       resume_from_step: int = None):
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

        # 复用 process_job 中已算好的特征码和缓存路径
        import hashlib
        traj_ids = sorted(t.get("id", "") for t in trajectories)
        ds_fingerprint = hashlib.md5("_".join(traj_ids).encode()).hexdigest()[:12]
        ds_cache_dir = Path(__file__).parent.parent / "cache" / f"ds_{ds_fingerprint}"
        ds_dir = ds_cache_dir / "dataset"
        img_dir = ds_cache_dir / "images"
        repo_id = f"box2robot-{ds_fingerprint}"

        # Step 1: Convert to LeRobot format (缓存到 datasets/ 目录, 避免重复转换)
        has_images = img_dir.is_dir() and any(img_dir.iterdir())
        datasets_root = Path(__file__).parent.parent / "datasets" / repo_id
        dataset_marker = datasets_root / "meta" / "info.json"

        # VLA models require camera images
        if is_vla and not has_images:
            raise ValueError(
                f"{model_type.upper()} 是视觉语言动作模型，需要摄像头图像数据。"
                f"请使用带图像的数据集，或改用 ACT/Diffusion 等纯状态模型。"
            )

        if dataset_marker.exists():
            logger.info("LeRobot dataset already exists: %s (skipping conversion)", datasets_root)
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
        if progress_cb:
            progress_cb(0, train_steps, {"loss": 0})

        # Step 2: Train via lerobot CLI
        # 优先用 -m 模式 (pip install -e . 安装后，AutoDL 等云端环境)
        # Fallback 到直接路径 (本地开发，lerobot 作为子目录)
        lerobot_local = Path(__file__).parent.parent / "lerobot" / "src" / "lerobot" / "scripts" / "lerobot_train.py"
        try:
            import importlib.util
            use_module_mode = importlib.util.find_spec("lerobot") is not None
        except Exception:
            use_module_mode = False

        if use_module_mode:
            cmd = [sys.executable, "-m", "lerobot.scripts.lerobot_train"]
        elif lerobot_local.exists():
            cmd = [sys.executable, str(lerobot_local)]
        else:
            raise FileNotFoundError(
                "lerobot 未安装且本地子目录不存在。"
                "请 pip install -e lerobot/ 或确保 lerobot/ 子目录完整。"
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
            # Memory optimization (VLA models are large)
            dtype = custom_params.get("dtype", "bfloat16")
            cmd.append(f"--policy.dtype={dtype}")
            cmd.append(f"--policy.gradient_checkpointing={custom_params.get('gradient_checkpointing', 'true')}")
            # SmolVLA: freeze vision, train expert only (default for fine-tune)
            if model_type == "smolvla":
                cmd.append(f"--policy.freeze_vision_encoder={custom_params.get('freeze_vision_encoder', 'true')}")
                cmd.append(f"--policy.train_expert_only={custom_params.get('train_expert_only', 'true')}")
                cmd.append(f"--policy.train_state_proj={custom_params.get('train_state_proj', 'true')}")
            # Pi0: optional compile for speed
            elif model_type in ("pi0", "pi0_fast", "pi05"):
                if custom_params.get("compile_model"):
                    cmd.append(f"--policy.compile_model={custom_params['compile_model']}")
                # Pi0 expert-only fine-tune (less VRAM)
                if custom_params.get("train_expert_only"):
                    cmd.append(f"--policy.train_expert_only={custom_params['train_expert_only']}")
            # VLA chunk_size/n_action_steps: use model defaults (50) unless explicitly overridden
            if chunk_size > 1 and custom_params.get("override_chunk_size"):
                cmd.append(f"--policy.chunk_size={chunk_size}")
                cmd.append(f"--policy.n_action_steps={chunk_size}")
        else:
            # ACT/Diffusion/etc: train from scratch
            cmd.append(f"--policy.type={model_type}")
            cmd.append(f"--policy.repo_id=box2robot/{repo_id}")
            if chunk_size > 1:
                cmd.append(f"--policy.chunk_size={chunk_size}")
                cmd.append("--policy.n_action_steps=1")
                cmd.append("--policy.temporal_ensemble_coeff=0.01")

        # Resume from checkpoint (暂停后恢复训练)
        if resume_from_step:
            ckpt_path = Path(model_dir) / "checkpoints" / str(resume_from_step)
            if ckpt_path.exists():
                cmd.append("--resume=true")
                cmd.append(f"--checkpoint_path={ckpt_path}")
                logger.info("Resuming from checkpoint: %s (step %d)", ckpt_path, resume_from_step)
            else:
                logger.warning("Checkpoint %s not found, training from scratch", ckpt_path)
        # Custom params as CLI args (skip keys already handled above)
        _handled_keys = {"task", "pretrained_path", "dtype", "gradient_checkpointing",
                         "freeze_vision_encoder", "train_expert_only", "train_state_proj",
                         "compile_model", "override_chunk_size"}
        for k, v in custom_params.items():
            if k not in _handled_keys:
                cmd.append(f"--{k}={v}")

        logger.info("LeRobot train cmd: %s %s", model_type.upper(), " ".join(cmd[-6:]))

        # Run with real-time stdout forwarding for progress
        import os as _os
        train_env = {**_os.environ, "PYTHONUNBUFFERED": "1"}
        if not use_module_mode:
            # 本地子目录模式：手动设 PYTHONPATH 让子进程能 import lerobot
            train_env["PYTHONPATH"] = str(Path(__file__).parent.parent / "lerobot" / "src")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(Path(__file__).parent.parent),
            env=train_env,
        )

        last_report_step = 0
        last_report_time = 0
        last_ckpt_set: set = set()  # 已上报过的 checkpoint 集合
        import re
        # Match INFO log: "step:10 smpl:80 loss:41.394 grdn:627.730"
        metrics_re = re.compile(r'\bstep:(\d+)\b.*\bloss:([\d.e+-]+)\b')
        # Match tqdm progress: "Training:  15%|...| 150/10000 [01:23<..."
        tqdm_re = re.compile(r'Training:\s+\d+%\|.*\|\s*(\d+)/(\d+)\s+\[')

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
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
            raise RuntimeError(f"LeRobot training failed (exit code {proc.returncode})")

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
                return r.json()
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning("Progress report failed (retry %d/%d): %s", attempt + 1, max_retries, e)
                    time.sleep(3)
                else:
                    logger.warning("Progress report failed: %s", e)
        return {}

    def _report_status(self, job_id: str, status: str,
                       error_msg: str = None, model_path: str = None):
        url = f"{self.server_url}/api/training/jobs/{job_id}/status"
        data = {"status": status, "key": self.pairing_key}
        if error_msg:
            data["error_msg"] = error_msg
        if model_path:
            data["model_path"] = model_path
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

    MLP: 链式预测生成 chunk (state→a1→a2→...→aN)
    ACT: 原生 chunk 输出 (chunk_size actions per inference)
    """
    import io
    import numpy as np
    import torch
    from PIL import Image

    logger.info("Loading model from %s", model_dir)
    config_path = Path(model_dir) / "b2r_config.json"

    use_vision = False
    model_type = "mlp"
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        pos_max = config.get("pos_max", pos_max)
        use_vision = config.get("use_vision", False)
        model_type = config.get("model_type", "mlp")
        chunk_size = config.get("chunk_size", chunk_size)
    else:
        config = {"n_servos": 6, "pos_max": pos_max}

    # VLA models use LeRobot's built-in preprocessing (VLM handles images internally)
    is_vla = config.get("is_vla", model_type in ("smolvla", "pi0", "pi0_fast", "pi05"))
    task_description = config.get("task_description", "manipulation task")

    if model_type != "mlp":
        # LeRobot policy — 必须指向 checkpoints/.../pretrained_model/ 目录
        ckpt_path = config.get("lerobot_checkpoint", "")
        if not ckpt_path or not Path(ckpt_path).exists():
            ckpt_dirs = sorted(Path(model_dir).glob("checkpoints/*/pretrained_model"))
            ckpt_path = str(ckpt_dirs[-1]) if ckpt_dirs else ""
        if not ckpt_path or not (Path(ckpt_path) / "config.json").exists():
            raise FileNotFoundError(f"No pretrained_model found in {model_dir}")

        logger.info("Loading LeRobot %s from %s", model_type.upper(), ckpt_path)
        sys.path.insert(0, str(Path(__file__).parent.parent / "lerobot" / "src"))
        from lerobot.policies.factory import get_policy_class
        from safetensors.torch import load_file as _load_sf

        policy_cls = get_policy_class(model_type)
        model = policy_cls.from_pretrained(ckpt_path)
        model.eval()
        if torch.cuda.is_available():
            model = model.cuda()
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
    else:
        is_vla = False
        _has_manual_norm = False
        _state_mean = _state_std = _action_mean = _action_std = None
        from box2robot_gpu_worker.mlp_policy import load_mlp_model
        model = load_mlp_model(model_dir)
        logger.info("MLP model loaded: %d servos, pos_max=%d", config["n_servos"], pos_max)

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
                if not data.get("running", True):
                    logger.info("推理已被 Server 停止")
                    return True
                # 检查机械臂是否离线
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

        if is_vla:
            # VLA models: pass raw state, the policy's internal preprocessor handles it
            obs = {"observation.state": state_t}
            if use_vision:
                img = cam_image or Image.new("RGB", (640, 480))
                # VLA models expect PIL Image or raw uint8 tensor — not ImageNet-normalized
                img_arr = np.array(img, dtype=np.float32) / 255.0
                img_t = torch.from_numpy(img_arr.transpose(2, 0, 1)).unsqueeze(0)
                if torch.cuda.is_available():
                    img_t = img_t.cuda()
                obs["observation.images.top"] = img_t
            # VLA models need task/language prompt
            obs["task"] = task_description
        else:
            # ACT/Diffusion: manual MEAN_STD normalization
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
                obs["observation.images.top"] = img_t
        return obs

    def _unnorm_action(action_tensor):
        """反归一化 action tensor → numpy [0,1].

        ACT/Diffusion: manual un-normalization with saved mean/std
        VLA: output is already in normalized [0,1] space (or close to it)
        """
        at = action_tensor if isinstance(action_tensor, torch.Tensor) else action_tensor.get("action", list(action_tensor.values())[0])
        if is_vla or not _has_manual_norm:
            # VLA models output actions in the dataset's original space
            # The policy's internal postprocessor already un-normalizes
            return at.clamp(0, 1).cpu().numpy()
        else:
            action_01 = at * _action_std + _action_mean
            return action_01.clamp(0, 1).cpu().numpy()

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
