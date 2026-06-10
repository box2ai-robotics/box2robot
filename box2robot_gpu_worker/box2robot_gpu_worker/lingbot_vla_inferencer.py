"""LingBot-VLA 4B 推理适配器 — Phase 2: 跟 lerobot serve.py 平级的推理入口.

模型架构跟 lerobot 完全不同 (Qwen2.5-VL + flow-matching action expert), 没法走
worker.run_inference_server 的 policy_cls.from_pretrained 路径. 改用 lingbot-vla
官方的 WS 推理服务 (deploy.lingbot_vla_policy):
  - 子进程跑 LingbotVLAServer (b2r-vla env, lerobot 0.4.2 + msgpack + websockets)
  - 主进程 (b2r env) 通过 WS msgpack 协议拉 action chunk
  - chunk → ChunkOptimizer → /api/device/.../inference/batch → ESP32

输入路径:
  model_dir/deploy_model/model.safetensors    (16GB 训练产物, dcp → safetensors 转过)
  model_dir/deploy_model/lingbotvla_cli.yaml  (训练配置, deploy 写死这个文件名)
  model_dir/deploy_model/norm_stats.json      (state/action 归一化 stats)
  model_dir/deploy_model/config.json          (PreTrainedConfig.from_pretrained 必需)
  model_dir/robot_config.yaml                 (origin_keys 映射, copy 到 lingbot-vla repo)

依赖:
  b2r env 需要 websockets + msgpack (不是 msgpack_numpy, 后者 import lingbot-vla 自带)
"""
from __future__ import annotations

import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("b2r.lingbot_vla_inferencer")

B2R_VLA_PYTHON = "/root/miniconda3/envs/b2r-vla/bin/python"
LINGBOT_VLA_REPO = "/root/autodl-tmp/workspace/box2robot/lingbot-vla"
LINGBOT_VLA_DEPLOY_MODULE = "deploy.lingbot_vla_policy"
ROBOT_CONFIG_DIR = Path(LINGBOT_VLA_REPO) / "configs" / "robot_configs"


class LingbotVlaWsClient:
    """Spawn b2r-vla subprocess running deploy.lingbot_vla_policy WS server + WS client wrapper.

    Lifecycle: __init__ spawns subprocess, waits for model load (~30-60s for 4B + cold cache),
    connects WS, calls reset(robo_name). close() terminates subprocess.
    """

    def __init__(
        self,
        deploy_dir: Path,
        robot_config_path: Path,
        n_servos: int = 6,
        port: Optional[int] = None,
        use_length: int = 25,
        task_description: str = "manipulation task",
        load_timeout: int = 600,
        norm_stats_path: Optional[Path] = None,
        progress_cb=None,
    ):
        self.deploy_dir = Path(deploy_dir)
        self.n_servos = n_servos
        self.port = port or self._pick_port()
        self.use_length = use_length
        self.task = task_description
        self._load_timeout = load_timeout
        # norm_stats 优先用显式传入的; 否则按 deploy_dir/norm_stats.json (兼容老的 deploy_model layout)
        self.norm_stats_path = Path(norm_stats_path) if norm_stats_path else (self.deploy_dir / "norm_stats.json")
        # 阶段上报 callback (worker_progress_cb), 让 _connect_ws 的长等待循环也能往 server 报状态
        self._progress_cb = progress_cb or (lambda stage, msg="": None)
        # deploy.lingbot_vla_policy.LingbotVLAServer.reset() 用 cwd-relative 路径加载
        # configs/robot_configs/<robo_name>.yaml, 必须把训练时写的 robot_config copy 过去.
        self.robo_name = self._install_robot_config(Path(robot_config_path))
        self.proc = self._spawn_server()
        self.client = self._connect_ws()
        # reset 会触发 server 端加载 FeatureTransform (norm_stats + tokenizer + image_processor).
        self.client.reset(robo_name=self.robo_name)
        logger.info("[LINGBOT-VLA-INFER] ready (robo=%s, port=%d, deploy=%s)",
                    self.robo_name, self.port, self.deploy_dir)

    @staticmethod
    def _pick_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _install_robot_config(self, src: Path) -> str:
        if not src.is_file():
            raise FileNotFoundError(f"robot_config not found: {src}")
        ROBOT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Unique 名字 (job_id + pid + ts), 避免并发推理冲突
        job_tag = src.parent.parent.name if src.parent.parent else "x"
        robo_name = f"b2r_infer_{job_tag}_{os.getpid()}_{int(time.time())}"
        dst = ROBOT_CONFIG_DIR / f"{robo_name}.yaml"
        dst.write_bytes(src.read_bytes())
        logger.info("[LINGBOT-VLA-INFER] robot_config installed → %s", dst)
        return robo_name

    def _spawn_server(self) -> subprocess.Popen:
        cmd = [
            B2R_VLA_PYTHON, "-m", LINGBOT_VLA_DEPLOY_MODULE,
            "--model_path", str(self.deploy_dir),
            "--port", str(self.port),
            "--use_length", str(self.use_length),
            "--norm_path", str(self.norm_stats_path),
        ]
        env = os.environ.copy()
        env["PATH"] = f"/root/miniconda3/envs/b2r-vla/bin:{env.get('PATH', '')}"
        env["TOKENIZERS_PARALLELISM"] = "false"
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        env.setdefault("HF_HOME", "/root/autodl-fs/data/box2robot-base-models")
        env.setdefault("CUDA_VISIBLE_DEVICES", "0")
        # PyAV bundled FFmpeg symlinks (trainer 也用同样的 fix)
        av_libs = "/root/miniconda3/envs/b2r-vla/lib/python3.12/site-packages/av.libs"
        env["LD_LIBRARY_PATH"] = f"{av_libs}:{env.get('LD_LIBRARY_PATH', '')}"

        logger.info("[LINGBOT-VLA-INFER] spawning server: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=LINGBOT_VLA_REPO, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True,
        )
        threading.Thread(target=self._relay_stdout, args=(proc,), daemon=True).start()
        return proc

    @staticmethod
    def _relay_stdout(proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    logger.info("[LINGBOT-VLA-SERVER] %s", line)
        except Exception:
            pass

    def _connect_ws(self):
        # lingbot-vla 自带的 WS client 用相对 import (from .msgpack_numpy import ...)
        # 必须先把 repo 加 sys.path 再用绝对 module 名 import.
        if LINGBOT_VLA_REPO not in sys.path:
            sys.path.insert(0, LINGBOT_VLA_REPO)
        try:
            from deploy.websocket_client_policy import WebsocketClientPolicy
        except ImportError as e:
            raise RuntimeError(
                f"无法 import lingbot-vla WS client: {e}. "
                f"b2r env 需要装 websockets + msgpack: "
                f"pip install websockets msgpack typing_extensions"
            )

        start = time.time()
        last_log = 0.0
        last_stage_post = 0.0
        while time.time() - start < self._load_timeout:
            # subprocess 死了立即 raise (避免 600s 死等)
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"[LINGBOT-VLA-INFER] server subprocess exited "
                    f"(returncode={self.proc.returncode}) before ready"
                )
            try:
                client = WebsocketClientPolicy(host="127.0.0.1", port=self.port)
                logger.info("[LINGBOT-VLA-INFER] WS connected after %.1fs", time.time() - start)
                return client
            except (ConnectionRefusedError, OSError):
                now = time.time()
                elapsed = int(now - start)
                if now - last_log > 15:
                    logger.info("[LINGBOT-VLA-INFER] waiting for server... (%ds)", elapsed)
                    last_log = now
                # 每 10s 给 server 报一次进度, 让前端知道正在等模型加载
                if now - last_stage_post > 10:
                    try:
                        self._progress_cb("loading_model",
                            f"加载 4B 模型权重 ({elapsed}s, 通常 60-90s)")
                    except Exception:
                        pass
                    last_stage_post = now
                time.sleep(2)
        raise TimeoutError(f"[LINGBOT-VLA-INFER] server not ready in {self._load_timeout}s")

    def predict_chunk(self, state_norm: np.ndarray, image_rgb: np.ndarray) -> np.ndarray:
        """Inference: state + image → action chunk.

        state_norm: (n_servos,) float32 in [0, 1] (raw servo position / pos_max)
        image_rgb : (H, W, 3) uint8
        returns   : (use_length, n_servos) float32 in [0, 1]
        """
        # 用 robot_config 里 origin_keys 对应的字段名 — convert_features 会切片到 target_features
        obs = {
            "observation.state": np.asarray(state_norm, dtype=np.float32),
            "observation.images.wrist": np.asarray(image_rgb, dtype=np.uint8),
            "task": self.task,
        }
        result = self.client.infer(obs)
        # server 端 unapply 后返回 dict, 含原始 action key
        action = result.get("action")
        if action is None:
            # 兜底: 找第一个非元数据 key 当 action
            for k, v in result.items():
                if k.startswith("server_") or k == "action_is_pad":
                    continue
                if hasattr(v, "shape") and len(v.shape) >= 2:
                    action = v
                    break
        if action is None:
            raise RuntimeError(
                f"[LINGBOT-VLA-INFER] server 未返回 action; keys={list(result.keys())}")
        return np.asarray(action, dtype=np.float32)

    def close(self) -> None:
        try:
            if hasattr(self, "client"):
                self.client._ws.close()
        except Exception:
            pass
        try:
            if hasattr(self, "proc"):
                self.proc.terminate()
                self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            (ROBOT_CONFIG_DIR / f"{self.robo_name}.yaml").unlink(missing_ok=True)
        except Exception:
            pass


def _resolve_deploy_paths(model_dir: str) -> tuple[Path, Path, dict]:
    """从 worker 传入的 model_dir 找推理 model 目录 + robot_config.yaml + b2r_config.json.

    优先级 (按 lingbot README 官方推荐: train.output_dir/checkpoints/*/hf_ckpt):
      1. <model_dir>/output/checkpoints/global_step_*/hf_ckpt/  (训练原生输出, sharded safetensors)
      2. <model_dir>/deploy_model/                              (老版手动 dcp → safetensors, 兼容)
      3. <model_dir>/                                           (顶层就是 deploy 内容)

    hf_ckpt 包含完整 sharded safetensors + 完整 lingbotvla_cli.yaml + config.json + tokenizer,
    是 lingbot 训练自带的输出, 不需要手动转换. 老的 deploy_model/ 路径只为向下兼容保留.
    """
    md = Path(model_dir).resolve()

    def _is_loadable(p: Path) -> bool:
        if not p.is_dir():
            return False
        return ((p / "model.safetensors").is_file()
                or (p / "model.safetensors.index.json").is_file())

    deploy_dir = None
    # 1. 找 hf_ckpt (取 step 最大的)
    hf_ckpt_root = md / "output" / "checkpoints"
    if hf_ckpt_root.is_dir():
        hf_ckpts = sorted(hf_ckpt_root.glob("global_step_*/hf_ckpt"),
                          key=lambda p: int(p.parent.name.split("_")[-1]))
        for hp in reversed(hf_ckpts):
            if _is_loadable(hp):
                deploy_dir = hp
                break
    # 2. fallback: deploy_model / 顶层
    if deploy_dir is None:
        for cand in (md / "deploy_model", md, md.parent / "deploy_model"):
            if _is_loadable(cand):
                deploy_dir = cand
                break
    if deploy_dir is None:
        raise FileNotFoundError(
            f"[LINGBOT-VLA-INFER] 找不到可用的 lingbot ckpt (hf_ckpt 或 deploy_model). "
            f"检索过: {md / 'output/checkpoints/global_step_*/hf_ckpt'}, "
            f"{md / 'deploy_model'}, {md}"
        )
    logger.info("[LINGBOT-VLA-INFER] using model path: %s", deploy_dir)

    # 配套文件检查 (lingbotvla_cli.yaml + config.json 是 deploy 加载必需; norm_stats 可在外面找)
    for f in ("lingbotvla_cli.yaml", "config.json"):
        if not (deploy_dir / f).is_file():
            raise FileNotFoundError(f"[LINGBOT-VLA-INFER] {deploy_dir} 缺 {f}")
    # norm_stats: 优先在 deploy_dir, 不在就用 model_dir/norm_stats.json (trainer 写在这里)
    if not (deploy_dir / "norm_stats.json").is_file():
        external_norm = md / "norm_stats.json"
        if external_norm.is_file():
            logger.info("[LINGBOT-VLA-INFER] 用外部 norm_stats.json: %s", external_norm)
        # _spawn_server 那里会显式传 --norm_path, 所以这里只要保证文件存在某处

    # robot_config.yaml 在 model_dir 下 (trainer 写到 model_dir/robot_config.yaml)
    robot_config = None
    for cand in (deploy_dir.parent / "robot_config.yaml",
                 deploy_dir / "robot_config.yaml", md / "robot_config.yaml"):
        if cand.is_file():
            robot_config = cand
            break
    if robot_config is None:
        raise FileNotFoundError(
            f"[LINGBOT-VLA-INFER] 找不到 robot_config.yaml (origin_keys 映射). "
            f"训练应该写到 model_dir/robot_config.yaml"
        )

    # b2r_config.json (n_servos / pos_max / chunk_size)
    b2r_cfg = {}
    for cand in (md / "b2r_config.json", deploy_dir.parent / "b2r_config.json",
                 deploy_dir / "b2r_config.json"):
        if cand.is_file():
            try:
                b2r_cfg = json.loads(cand.read_text())
                break
            except Exception:
                pass

    return deploy_dir, robot_config, b2r_cfg


def run_inference_lingbot_vla(
    model_dir: str,
    server_url: str,
    device_id: str,
    token: str = "",
    pos_max: int = 4095,
    fps: int = 20,
    camera_id: str = "",
    chunk_size: int = 25,
    job_id: str = "",
    execution_mode: str = "fixed",
    chunk_params: Optional[dict] = None,
):
    """LingBot-VLA 推理主循环. 跟 worker.run_inference_server 平级.

    Worker 在 model_type == "lingbot_vla" 时调本函数, 不走 lerobot policy_cls.from_pretrained.
    """
    import httpx
    from PIL import Image

    # 阶段上报: 让前端知道 worker 不只是"已就绪", 而是在哪一步具体卡了.
    # server: POST /api/training/jobs/{id}/inference-stage {stage, message, key}
    # stage ∈ loading_model / model_ready / inferring (server 端 VALID_STAGES 校验)
    def _post_stage(stage: str, message: str = "") -> None:
        if not job_id:
            return
        try:
            r = httpx.post(
                f"{server_url}/api/training/jobs/{job_id}/inference-stage",
                json={"stage": stage, "message": message, "key": ""},
                headers={"Authorization": f"Bearer {token}"} if token else {},
                timeout=3.0,
            )
            if r.status_code != 200:
                logger.debug("[STAGE] %s -> %d", stage, r.status_code)
        except Exception as e:
            logger.debug("[STAGE] %s post failed: %s", stage, type(e).__name__)

    _post_stage("loading_model", "解析模型路径")
    deploy_dir, robot_config, b2r_cfg = _resolve_deploy_paths(model_dir)
    n_servos = b2r_cfg.get("n_servos", 6)
    pos_max = b2r_cfg.get("pos_max", pos_max)
    use_length = b2r_cfg.get("chunk_size", chunk_size)
    task_desc = b2r_cfg.get("task_description", "manipulation task")

    # norm_stats 可能在 deploy_dir 内 (老布局) 或在 model_dir 顶层 (trainer 写在那里)
    md = Path(model_dir).resolve()
    norm_stats_path = None
    for cand in (deploy_dir / "norm_stats.json", md / "norm_stats.json"):
        if cand.is_file():
            norm_stats_path = cand
            break
    if norm_stats_path is None:
        raise FileNotFoundError(
            f"[LINGBOT-VLA-INFER] 找不到 norm_stats.json (deploy_dir={deploy_dir}, md={md})"
        )

    logger.info(
        "[LINGBOT-VLA-INFER] resolved: deploy=%s, robot_config=%s, norm_stats=%s, "
        "n_servos=%d, pos_max=%d, use_length=%d, task=%r",
        deploy_dir, robot_config, norm_stats_path, n_servos, pos_max, use_length, task_desc)

    _post_stage("loading_model", "启动推理子进程 (b2r-vla env)")
    policy = LingbotVlaWsClient(
        deploy_dir=deploy_dir, robot_config_path=robot_config,
        n_servos=n_servos, use_length=use_length, task_description=task_desc,
        norm_stats_path=norm_stats_path,
        progress_cb=_post_stage,
    )
    _post_stage("model_ready", "模型已加载, 等待开始推理")

    client = httpx.Client(
        base_url=server_url, timeout=10,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )

    if camera_id:
        try:
            client.post(f"/api/camera/{camera_id}/stream/mode", json={"mode": "inference"})
        except Exception:
            pass
    try:
        client.post(f"/api/device/{device_id}/command", json={"torque": True})
    except Exception:
        pass

    # === stop poller (背景线程, 同 worker.run_inference_server 设计) ===
    stop_flag = False
    stop_lock = threading.Lock()
    stop_reason = ""

    def stop_poller():
        nonlocal stop_flag, stop_reason
        while True:
            with stop_lock:
                if stop_flag:
                    return
            try:
                if job_id:
                    r = client.get(f"/api/training/jobs/{job_id}/check-inference", timeout=3.0)
                    if r.status_code == 200:
                        data = r.json()
                        if bool(data.get("should_stop", False)):
                            with stop_lock:
                                stop_flag = True
                                stop_reason = data.get("stop_reason") or "server requested"
                            logger.info("[LINGBOT-VLA-INFER] stop signal: %s", stop_reason)
                            return
            except Exception:
                pass
            time.sleep(5)

    threading.Thread(target=stop_poller, name="lingbot-vla-stop-poller", daemon=True).start()

    def should_stop() -> bool:
        with stop_lock:
            return stop_flag

    def read_state():
        try:
            r = client.get(f"/api/device/{device_id}/servos", timeout=2.0)
            servos = r.json().get("servos", [])
        except Exception:
            return None, None
        if not servos:
            return None, None
        sorted_s = sorted(servos, key=lambda s: s["id"])
        return ([s["id"] for s in sorted_s],
                np.array([s["pos"] / pos_max for s in sorted_s], dtype=np.float32))

    last_image: Optional[np.ndarray] = None
    fail_streak = 0
    FAIL_THRESHOLD = 30

    def read_camera() -> Optional[np.ndarray]:
        nonlocal last_image, fail_streak
        if not camera_id:
            return None
        try:
            r = client.get(f"/api/camera/{camera_id}/frame", timeout=3.0)
            if r.status_code == 200 and r.content:
                img = Image.open(io.BytesIO(r.content)).convert("RGB").resize((640, 480))
                last_image = np.asarray(img, dtype=np.uint8)
                fail_streak = 0
                return last_image
        except Exception:
            pass
        fail_streak += 1
        if fail_streak >= FAIL_THRESHOLD:
            last_image = None
            return None
        return last_image

    from box2robot_gpu_worker.chunk_optimizer import ChunkOptimizer
    cp = chunk_params or {}
    strategy = execution_mode if execution_mode != "original" else "fixed"
    optimizer = ChunkOptimizer(
        chunk_size=use_length, strategy=strategy, n_servos=n_servos,
        fixed_exec_steps=int(cp.get("fixed_exec_steps", 0)),
        certainty_threshold=float(cp.get("certainty_threshold", 0.15)),
        min_execute=int(cp.get("min_execute", 3)),
        max_skip=int(cp.get("max_skip", 15)),
        overlap_ratio=float(cp.get("overlap_ratio", 0.5)),
    )
    ema_alpha = max(0.0, min(1.0, float(cp.get("ema_alpha", 0.3))))
    ema_state: Optional[np.ndarray] = None

    def ema_smooth(arr: np.ndarray) -> np.ndarray:
        nonlocal ema_state
        if ema_alpha >= 1.0:
            return arr
        arr = np.asarray(arr, dtype=np.float32)
        if arr.ndim == 1:
            ema_state = arr.copy() if ema_state is None else ema_alpha * arr + (1.0 - ema_alpha) * ema_state
            return ema_state.copy()
        out = np.empty_like(arr)
        if ema_state is None:
            ema_state = arr[0].copy()
        for i in range(arr.shape[0]):
            ema_state = ema_alpha * arr[i] + (1.0 - ema_alpha) * ema_state
            out[i] = ema_state
        return out

    interval = 1.0 / fps
    frame_interval_ms = int(1000 / fps)
    step_count = 0

    logger.info("[LINGBOT-VLA-INFER] loop start: %s @ %dHz chunk=%d strategy=%s",
                device_id, fps, use_length, strategy)
    try:
        while not should_stop():
            t0 = time.perf_counter()
            servo_ids, state = read_state()
            if servo_ids is None:
                time.sleep(0.3)
                continue
            img = read_camera()
            if img is None:
                img = np.zeros((480, 640, 3), dtype=np.uint8)

            t_infer = time.perf_counter()
            try:
                chunk_01 = policy.predict_chunk(state, img)
            except Exception as e:
                logger.error("[LINGBOT-VLA-INFER] predict failed (%s); retry in 0.5s", e)
                time.sleep(0.5)
                continue
            if chunk_01.shape[-1] > n_servos:
                chunk_01 = chunk_01[..., :n_servos]
            chunk_01 = np.clip(chunk_01, 0.0, 1.0)
            infer_ms = (time.perf_counter() - t_infer) * 1000

            n_exec, batch_actions = optimizer.feed_chunk(chunk_01)
            batch_actions = ema_smooth(batch_actions)
            frames = [
                {"t": i * frame_interval_ms,
                 "p": [int(max(0, min(pos_max, a * pos_max))) for a in batch_actions[i]]}
                for i in range(n_exec)
            ]
            try:
                client.post(f"/api/device/{device_id}/inference/batch",
                            json={"frames": frames, "ids": servo_ids}, timeout=3.0)
            except Exception:
                pass

            # 第一批 frames 发出 → 切到 inferring (server 端 stage = inferring)
            if step_count == 0:
                _post_stage("inferring", f"推理中, 首帧已下发 ({n_exec} 帧 @ {fps}Hz)")
            step_count += n_exec
            elapsed = time.perf_counter() - t0
            wait = max(0.0, n_exec * interval - elapsed)
            if wait > 0:
                waited = 0.0
                while waited < wait and not should_stop():
                    chunk_wait = min(1.0, wait - waited)
                    time.sleep(chunk_wait)
                    waited += chunk_wait
            total = time.perf_counter() - t0
            print(f"\r  [lingbot_vla] step {step_count}  exec:{n_exec}@{fps}Hz  "
                  f"infer:{infer_ms:.0f}ms  cycle:{1.0/max(total,0.001):.1f}Hz  ",
                  end="", flush=True)
    except KeyboardInterrupt:
        print("\n[LINGBOT-VLA-INFER] interrupted")
    finally:
        try:
            client.post(f"/api/device/{device_id}/command", json={"torque": False})
        except Exception:
            pass
        if camera_id:
            try:
                client.post(f"/api/camera/{camera_id}/stream/mode", json={"mode": "idle"})
            except Exception:
                pass
        policy.close()
    logger.info("[LINGBOT-VLA-INFER] stopped after %d steps", step_count)
