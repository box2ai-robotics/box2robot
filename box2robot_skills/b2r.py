#!/usr/bin/env python3
"""
Box2Robot CLI — control your robotic arm with a single command.

Usage:
  b2r login [user] [pass]                # Login (saves token to ~/.b2r_token)
  b2r devices                            # List devices
  b2r status                             # Servo status
  b2r move <id> <pos> [speed]            # Move servo
  b2r home                               # Go to home position
  b2r torque on/off                      # Toggle torque
  b2r record start [--cam CAM_ID] [name] # Record (with optional camera)
  b2r record stop [name]                 # Stop recording
  b2r record status                      # Recording status
  b2r play [traj_id]                     # Play trajectory (no args = list)
  b2r snapshot                           # Camera snapshot
  b2r frame [cam_id] [output.jpg]        # Download latest camera frame
  b2r download <traj_id> [output_dir]    # Download trajectory images
  b2r dataset <traj_id> [output_dir]     # Download full dataset (JSON + images)
  b2r video <traj_id> [output.mp4]       # Generate video from trajectory images
  b2r calibrate [servo_id]               # Auto-calibrate (0 = all)
  b2r train [--steps N] [--name NAME]    # Submit training job (interactive)
  b2r jobs                               # List training jobs
  b2r deploy <job_id>                    # Deploy inference (interactive)
  b2r stop-infer <job_id>               # Stop inference

Environment variables:
  B2R_SERVER   Server URL        (default: https://robot.box2ai.com)
  B2R_TOKEN    JWT bearer token  (overrides ~/.b2r_token)
  B2R_DEVICE   Default device ID (overrides auto-select)

Credential storage:
  ~/.b2r_token  JSON {token, server, device}. Owner-only (0600).
"""

import asyncio
import sys
import os
import json
import stat

try:
    import aiohttp
except ImportError:
    print("Missing dependency: pip install aiohttp")
    sys.exit(1)

# ── Token persistence ─────────────────────────────────────────────────

TOKEN_FILE = os.path.expanduser("~/.b2r_token")


def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            data = json.load(f)
            return data.get("token"), data.get("server"), data.get("device")
    return None, None, None


def save_token(token, server, device=None):
    data = {"token": token, "server": server}
    if device:
        data["device"] = device
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)
    try:
        os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ── HTTP helpers ──────────────────────────────────────────────────────

def _resolve():
    """Return (server, token, device) from env vars or token file."""
    token, server, device = load_token()
    server = os.environ.get("B2R_SERVER", server or "https://robot.box2ai.com")
    token = os.environ.get("B2R_TOKEN", token)
    device = os.environ.get("B2R_DEVICE", device)
    return server, token, device


def _headers(token):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _get(session, server, token, path, params=None):
    async with session.get(f"{server}{path}", headers=_headers(token),
                           params=params) as r:
        return await r.json() if "json" in r.content_type else {"status": r.status}


async def _post(session, server, token, path, data=None):
    async with session.post(f"{server}{path}", headers=_headers(token),
                            json=data) as r:
        return await r.json() if "json" in r.content_type else {"status": r.status}


def _need_login():
    print("Not logged in. Run: python b2r.py login")
    sys.exit(1)


def _need_device():
    print("No device. Run 'b2r login' or set B2R_DEVICE.")
    sys.exit(1)


def pp(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _pick(prompt, options, key_fn, label_fn):
    """Interactive picker: show numbered list, return selected item."""
    if not options:
        return None
    if len(options) == 1:
        print(f"  Auto-selected: {label_fn(options[0])}")
        return options[0]
    print(prompt)
    for i, opt in enumerate(options):
        print(f"  [{i+1}] {label_fn(opt)}")
    while True:
        try:
            choice = input(f"Choose [1-{len(options)}]: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except (ValueError, EOFError, KeyboardInterrupt):
            return None


# ── Commands ──────────────────────────────────────────────────────────

async def cmd_login(args):
    server = os.environ.get("B2R_SERVER", "https://robot.box2ai.com")
    if len(args) >= 2:
        user, pwd = args[0], args[1]
    elif len(args) == 1:
        user = args[0]
        import getpass
        pwd = getpass.getpass("Password: ")
    else:
        user = input("Username: ")
        import getpass
        pwd = getpass.getpass("Password: ")

    async with aiohttp.ClientSession() as s:
        resp = await _post(s, server, None, "/api/auth/login",
                           {"username": user, "password": pwd})
        token = resp.get("token")
        if not token:
            print(f"Login failed: {resp}")
            return

        # Auto-select first online arm
        devices = await _get(s, server, token, "/api/devices")
        dev_list = devices if isinstance(devices, list) else devices.get("devices", [])
        arm_id = None
        for d in dev_list:
            online = d.get("online", False)
            dtype = d.get("device_type", "arm")
            name = d.get("nickname") or d.get("device_id", "")
            print(f"  [{dtype}] {name} - {'online' if online else 'offline'}")
            if dtype == "arm" and online and not arm_id:
                arm_id = d["device_id"]

        save_token(token, server, arm_id)
        print(f"\nLogin OK! Token saved to {TOKEN_FILE}")
        if arm_id:
            print(f"Default device: {arm_id}")


async def cmd_devices(args):
    server, token, _ = _resolve()
    if not token:
        _need_login()
    async with aiohttp.ClientSession() as s:
        devices = await _get(s, server, token, "/api/devices")
        dev_list = devices if isinstance(devices, list) else devices.get("devices", [])
        for d in dev_list:
            online = d.get("online", False)
            dtype = d.get("device_type", "arm")
            name = d.get("nickname") or d.get("device_id", "")
            did = d.get("device_id", "")
            mark = "*" if online else " "
            print(f"  {mark} [{dtype:6s}] {name:12s} {did}")


async def cmd_status(args):
    server, token, device = _resolve()
    if not token:
        _need_login()
    device = args[0] if args else device
    if not device:
        _need_device()
    async with aiohttp.ClientSession() as s:
        data = await _get(s, server, token, f"/api/device/{device}/servos")
        servos = data.get("servos", []) if isinstance(data, dict) else []
        torque = data.get("torque_enabled")
        print(f"Torque: {'ON' if torque else 'OFF' if torque is not None else '?'}")
        for sv in servos:
            print(f"  ID{sv['id']:2d}: pos={sv.get('pos',0):4d}  "
                  f"load={sv.get('load',0):3d}  temp={sv.get('temp',0)}°C")


async def cmd_move(args):
    if len(args) < 2:
        print("Usage: b2r move <servo_id> <position> [speed]")
        return
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    data = {"id": int(args[0]), "position": int(args[1]),
            "speed": int(args[2]) if len(args) > 2 else 1000}
    async with aiohttp.ClientSession() as s:
        pp(await _post(s, server, token, f"/api/device/{device}/command", data))


async def cmd_home(args):
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    async with aiohttp.ClientSession() as s:
        pp(await _post(s, server, token, f"/api/device/{device}/go_home"))


async def cmd_torque(args):
    if not args:
        print("Usage: b2r torque on/off")
        return
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    enable = args[0].lower() in ("on", "true", "1", "yes")
    async with aiohttp.ClientSession() as s:
        pp(await _post(s, server, token, f"/api/device/{device}/torque",
                       {"enable": enable}))


async def cmd_record(args):
    if not args:
        print("Usage: b2r record start [--cam CAM_ID] [name]")
        print("       b2r record stop [name]")
        print("       b2r record status")
        return
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    async with aiohttp.ClientSession() as s:
        if args[0] == "start":
            # Parse --cam CAM_ID from args
            camera_id = ""
            rest = args[1:]
            if "--cam" in rest:
                idx = rest.index("--cam")
                if idx + 1 < len(rest):
                    camera_id = rest[idx + 1]
                    rest = rest[:idx] + rest[idx+2:]
            mode = "single"
            # If no --cam but cameras are online, offer to pick one
            if not camera_id:
                devs = await _get(s, server, token, "/api/devices")
                dev_list = devs if isinstance(devs, list) else devs.get("devices", [])
                cams = [d for d in dev_list
                        if d.get("device_type") == "camera" and d.get("online")]
                if cams:
                    print("Online cameras detected. Record with camera?")
                    print("  [0] No camera (servo data only)")
                    for i, c in enumerate(cams):
                        name = c.get("nickname") or c.get("device_id", "")
                        print(f"  [{i+1}] {name} ({c['device_id']})")
                    try:
                        choice = input(f"Choose [0-{len(cams)}] (default 0): ").strip()
                        if choice and int(choice) > 0:
                            camera_id = cams[int(choice) - 1]["device_id"]
                    except (ValueError, EOFError, KeyboardInterrupt):
                        pass
            body = {"mode": mode}
            if camera_id:
                body["camera_id"] = camera_id
            resp = await _post(s, server, token,
                               f"/api/device/{device}/record/start", body)
            pp(resp)
            if resp.get("status") == "recording" and camera_id:
                print(f"  Recording with camera: {camera_id}")
            max_dur = resp.get("max_duration_s", 600)
            print(f"  Max duration: {max_dur}s. Run 'b2r record stop' when done.")

        elif args[0] == "stop":
            body = {"name": args[1]} if len(args) > 1 else {}
            pp(await _post(s, server, token,
                           f"/api/device/{device}/record/stop", body))
        elif args[0] == "status":
            pp(await _get(s, server, token,
                          f"/api/device/{device}/record/status"))
        else:
            print("Usage: b2r record start/stop/status")


async def cmd_play(args):
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    async with aiohttp.ClientSession() as s:
        if not args:
            data = await _get(s, server, token,
                              f"/api/device/{device}/trajectories")
            trajs = data if isinstance(data, list) else data.get("trajectories", [])
            for t in trajs:
                tid = t.get("id", t.get("traj_id", "?"))
                name = t.get("name", "unnamed")
                frames = t.get("frame_count", "?")
                imgs = " +img" if t.get("has_images") else ""
                print(f"  {str(tid)[:8]}  {name} ({frames} frames{imgs})")
        else:
            pp(await _post(s, server, token,
                           f"/api/device/{device}/trajectory/{args[0]}/play"))


async def cmd_snapshot(args):
    server, token, device = _resolve()
    if not token:
        _need_login()
    cam = args[0] if args else device
    if not cam:
        _need_device()
    async with aiohttp.ClientSession() as s:
        pp(await _post(s, server, token, f"/api/camera/{cam}/snapshot"))


async def cmd_frame(args):
    """Download latest camera JPEG frame to a local file."""
    server, token, device = _resolve()
    if not token:
        _need_login()
    cam = args[0] if args else device
    if not cam:
        _need_device()
    output = args[1] if len(args) > 1 else "frame.jpg"
    async with aiohttp.ClientSession() as s:
        url = f"{server}/api/camera/{cam}/frame"
        async with s.get(url, headers=_headers(token)) as r:
            if r.status == 204:
                print("No frame available (camera may be idle)")
                return
            if r.status != 200:
                print(f"Error: HTTP {r.status}")
                return
            data = await r.read()
            with open(output, "wb") as f:
                f.write(data)
            print(f"Saved {len(data)} bytes -> {output}")


async def cmd_download(args):
    """Download all images from a trajectory."""
    if not args:
        print("Usage: b2r download <traj_id> [output_dir]")
        return
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    traj_id = args[0]
    out_dir = args[1] if len(args) > 1 else f"images_{traj_id[:8]}"
    os.makedirs(out_dir, exist_ok=True)

    async with aiohttp.ClientSession() as s:
        data = await _get(s, server, token,
                          f"/api/device/{device}/trajectory/{traj_id}/images")
        images = data.get("images", [])
        if not images:
            print(f"No images for trajectory {traj_id}")
            return
        print(f"Downloading {len(images)} images...")
        count = 0
        for img in images:
            url_path = img.get("url", f"/api/traj-image/{traj_id}/{img['index']}")
            async with s.get(f"{server}{url_path}",
                             headers=_headers(token)) as r:
                if r.status == 200:
                    jpeg = await r.read()
                    fname = img.get("filename", f"{img['index']:06d}.jpg")
                    with open(os.path.join(out_dir, fname), "wb") as f:
                        f.write(jpeg)
                    count += 1
                    if count % 20 == 0:
                        print(f"  {count}/{len(images)}...")
        print(f"Done: {count} images saved to {out_dir}/")


async def cmd_dataset(args):
    """Download full dataset: trajectory JSON + images."""
    if not args:
        print("Usage: b2r dataset <traj_id> [output_dir]")
        print("       Downloads trajectory JSON data + all images.")
        return
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    traj_id = args[0]
    out_dir = args[1] if len(args) > 1 else f"dataset_{traj_id[:12]}"
    os.makedirs(out_dir, exist_ok=True)

    async with aiohttp.ClientSession() as s:
        # 1. Download trajectory JSON
        print("Downloading trajectory data...")
        traj = await _get(s, server, token,
                          f"/api/device/{device}/trajectory/{traj_id}/data")
        if "error" in traj:
            print(f"Error: {traj['error']}")
            return
        json_path = os.path.join(out_dir, f"{traj_id}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(traj, f, ensure_ascii=False)
        frames = traj.get("frame_count", 0)
        dur = traj.get("duration_ms", 0) / 1000
        print(f"  Saved: {json_path} ({frames} frames, {dur:.1f}s)")

        # 2. Download images if available
        img_data = await _get(s, server, token,
                              f"/api/device/{device}/trajectory/{traj_id}/images")
        images = img_data.get("images", [])
        if images:
            img_dir = os.path.join(out_dir, "images")
            os.makedirs(img_dir, exist_ok=True)
            print(f"Downloading {len(images)} images...")
            count = 0
            for img in images:
                url_path = img.get("url", f"/api/traj-image/{traj_id}/{img['index']}")
                async with s.get(f"{server}{url_path}",
                                 headers=_headers(token)) as r:
                    if r.status == 200:
                        jpeg = await r.read()
                        fname = img.get("filename", f"{img['index']:06d}.jpg")
                        with open(os.path.join(img_dir, fname), "wb") as f:
                            f.write(jpeg)
                        count += 1
                        if count % 20 == 0:
                            print(f"  {count}/{len(images)}...")
            print(f"  Saved: {count} images to {img_dir}/")
        else:
            print("  No images (servo-only dataset)")
        print(f"\nDataset saved to {out_dir}/")


async def cmd_video(args):
    """Generate MP4 video from trajectory images using ffmpeg or opencv."""
    if not args:
        print("Usage: b2r video <traj_id> [output.mp4] [--fps 10]")
        return
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    traj_id = args[0]

    # Parse output and fps
    output = "video.mp4"
    fps = 10
    rest = args[1:]
    for i, a in enumerate(rest):
        if a == "--fps" and i + 1 < len(rest):
            fps = int(rest[i + 1])
        elif not a.startswith("--") and i == 0:
            output = a

    async with aiohttp.ClientSession() as s:
        # Get image list
        img_data = await _get(s, server, token,
                              f"/api/device/{device}/trajectory/{traj_id}/images")
        images = img_data.get("images", [])
        if not images:
            print(f"No images for trajectory {traj_id}")
            return

        # Download to temp dir
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            print(f"Downloading {len(images)} frames...")
            for img in images:
                url_path = img.get("url", f"/api/traj-image/{traj_id}/{img['index']}")
                async with s.get(f"{server}{url_path}",
                                 headers=_headers(token)) as r:
                    if r.status == 200:
                        jpeg = await r.read()
                        fname = f"{img['index']:06d}.jpg"
                        with open(os.path.join(tmp, fname), "wb") as f:
                            f.write(jpeg)

            # Try ffmpeg first, fall back to opencv
            import subprocess
            pattern = os.path.join(tmp, "%06d.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-framerate", str(fps),
                     "-i", pattern, "-c:v", "libx264",
                     "-pix_fmt", "yuv420p", output],
                    check=True, capture_output=True)
                print(f"Video saved: {output} ({len(images)} frames, {fps}fps)")
                return
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass

            # Fallback: opencv
            try:
                import cv2
                frame0 = cv2.imread(os.path.join(tmp, "000000.jpg"))
                if frame0 is None:
                    print("Error: cannot read frames")
                    return
                h, w = frame0.shape[:2]
                writer = cv2.VideoWriter(output, cv2.VideoWriter_fourcc(*'mp4v'),
                                         fps, (w, h))
                for img in images:
                    fpath = os.path.join(tmp, f"{img['index']:06d}.jpg")
                    frame = cv2.imread(fpath)
                    if frame is not None:
                        writer.write(frame)
                writer.release()
                print(f"Video saved: {output} ({len(images)} frames, {fps}fps)")
                return
            except ImportError:
                pass

            print("Error: ffmpeg or opencv (cv2) required.")
            print("  Install: https://ffmpeg.org  or  pip install opencv-python")


async def cmd_train(args):
    """Submit a training job. Interactive selection of datasets."""
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()

    # Parse optional flags
    steps = 100000
    job_name = ""
    i = 0
    while i < len(args):
        if args[i] == "--steps" and i + 1 < len(args):
            steps = int(args[i + 1]); i += 2
        elif args[i] == "--name" and i + 1 < len(args):
            job_name = args[i + 1]; i += 2
        else:
            i += 1

    async with aiohttp.ClientSession() as s:
        # 1. Fetch trajectories with images (training needs image data)
        data = await _get(s, server, token,
                          f"/api/device/{device}/trajectories")
        trajs = data if isinstance(data, list) else data.get("trajectories", [])
        if not trajs:
            print("No trajectories found. Record some data first.")
            return

        # Show trajectories for selection
        print(f"\nAvailable trajectories ({len(trajs)}):")
        for i, t in enumerate(trajs):
            imgs = f" +{t.get('image_count', 0)}img" if t.get("has_images") else ""
            dur = t.get("duration_ms", 0) / 1000
            print(f"  [{i+1}] {t['name'] or 'unnamed':20s} "
                  f"{t['frame_count']:4d} frames  {dur:.1f}s{imgs}  {t['id'][:12]}")

        # Let user pick datasets (comma-separated)
        print(f"\nSelect datasets (comma-separated, e.g. 1,2,3 or 'all'):")
        try:
            choice = input("Datasets: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not choice:
            return

        if choice.lower() == "all":
            selected = trajs
        else:
            indices = []
            for part in choice.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    indices.extend(range(int(a), int(b) + 1))
                else:
                    indices.append(int(part))
            selected = [trajs[i - 1] for i in indices if 0 < i <= len(trajs)]

        if not selected:
            print("No datasets selected.")
            return

        dataset_ids = [t["id"] for t in selected]
        total_frames = sum(t["frame_count"] for t in selected)
        print(f"\nSelected {len(dataset_ids)} datasets, {total_frames} total frames")

        # Training parameters
        if not job_name:
            job_name = selected[0].get("name", "training") if len(selected) == 1 else "batch_training"
            try:
                name_input = input(f"Job name [{job_name}]: ").strip()
                if name_input:
                    job_name = name_input
            except (EOFError, KeyboardInterrupt):
                pass

        print(f"\nTraining config:")
        print(f"  Model:      ACT (Action Chunking Transformer)")
        print(f"  Steps:      {steps}")
        print(f"  Batch size: 32")
        print(f"  Chunk size: 20")
        print(f"  Datasets:   {len(dataset_ids)}")

        try:
            confirm = input("\nSubmit? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if confirm and confirm != "y":
            print("Cancelled.")
            return

        resp = await _post(s, server, token, "/api/training/jobs", {
            "model_type": "act",
            "dataset_ids": dataset_ids,
            "train_steps": steps,
            "batch_size": 32,
            "chunk_size": 20,
            "name": job_name,
        })
        if "error" in resp:
            print(f"Error: {resp['error']}")
        else:
            print(f"\nJob submitted: {resp.get('id', '?')}")
            print(f"  Status: {resp.get('status', '?')}")
            print(f"  Track:  b2r jobs")


async def cmd_jobs(args):
    """List training jobs."""
    server, token, _ = _resolve()
    if not token:
        _need_login()
    async with aiohttp.ClientSession() as s:
        data = await _get(s, server, token, "/api/training/jobs")
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        if not jobs:
            print("No training jobs.")
            return
        for j in jobs:
            status = j.get("status", "?")
            name = j.get("name", "unnamed")
            jid = j.get("id", "?")
            progress = j.get("progress", 0)
            steps = j.get("train_steps", 0)
            # Status indicator
            if status == "training":
                pct = f" {progress}/{steps}" if steps else ""
                flag = f"[TRAINING{pct}]"
            elif status == "deploying":
                flag = "[INFERENCE]"
            elif status == "completed":
                flag = "[DONE]"
            elif status == "failed":
                flag = "[FAILED]"
            elif status == "cancelled":
                flag = "[CANCELLED]"
            else:
                flag = f"[{status.upper()}]"
            print(f"  {flag:16s} {name:20s} {jid[:16]}")


async def cmd_deploy(args):
    """Deploy inference from a completed training job."""
    if not args:
        print("Usage: b2r deploy <job_id>")
        print("       Run 'b2r jobs' to see job IDs.")
        return
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    job_id = args[0]

    async with aiohttp.ClientSession() as s:
        # Verify job exists and is deployable
        job = await _get(s, server, token, f"/api/training/jobs/{job_id}")
        if "error" in job:
            print(f"Error: {job['error']}")
            return
        if job.get("status") not in ("completed", "cancelled"):
            print(f"Job status is '{job.get('status')}', must be 'completed' to deploy.")
            return

        # Get devices for selection
        devs = await _get(s, server, token, "/api/devices")
        dev_list = devs if isinstance(devs, list) else devs.get("devices", [])
        arms = [d for d in dev_list if d.get("device_type") == "arm"]
        gpus = [d for d in dev_list if d.get("device_type") == "gpu"]
        cams = [d for d in dev_list if d.get("device_type") == "camera"]

        online_arms = [d for d in arms if d.get("online")]
        online_gpus = [d for d in gpus if d.get("online")]
        online_cams = [d for d in cams if d.get("online")]

        if not online_gpus:
            print("No GPU device online. Cannot deploy inference.")
            return
        if not online_arms:
            print("No arm device online. Cannot deploy inference.")
            return

        # Pick GPU
        gpu = _pick("Select GPU device:",
                     online_gpus, lambda d: d["device_id"],
                     lambda d: f"{d.get('nickname') or d['device_id']} ({d['device_id']})")
        if not gpu:
            return

        # Pick ARM
        arm = _pick("Select arm device:",
                     online_arms, lambda d: d["device_id"],
                     lambda d: f"{d.get('nickname') or d['device_id']} ({d['device_id']})")
        if not arm:
            return

        # Pick camera (optional)
        cam_id = ""
        if online_cams:
            print("Select camera (optional, for visual inference):")
            print("  [0] No camera")
            for i, c in enumerate(online_cams):
                name = c.get("nickname") or c.get("device_id", "")
                print(f"  [{i+1}] {name} ({c['device_id']})")
            try:
                ch = input(f"Choose [0-{len(online_cams)}] (default 0): ").strip()
                if ch and int(ch) > 0:
                    cam_id = online_cams[int(ch) - 1]["device_id"]
            except (ValueError, EOFError, KeyboardInterrupt):
                pass

        # Execution mode
        print("\nExecution mode:")
        print("  [1] original  — LeRobot default (~5Hz)")
        print("  [2] fixed     — Full chunk batch (20Hz)")
        print("  [3] adaptive  — FAST-ACT skip frames (1-5Hz→20Hz)")
        print("  [4] overlap   — Sliding window ensemble (20Hz)")
        try:
            mode_ch = input("Choose [1-4] (default 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            mode_ch = ""
        modes = {"1": "original", "2": "fixed", "3": "adaptive", "4": "overlap"}
        exec_mode = modes.get(mode_ch, "original")

        body = {
            "gpu_device_id": gpu["device_id"],
            "arm_device_id": arm["device_id"],
            "execution_mode": exec_mode,
        }
        if cam_id:
            body["camera_device_id"] = cam_id

        print(f"\nDeploying inference:")
        print(f"  Job: {job_id[:16]}")
        print(f"  GPU: {gpu.get('nickname') or gpu['device_id']}")
        print(f"  ARM: {arm.get('nickname') or arm['device_id']}")
        if cam_id:
            print(f"  CAM: {cam_id}")
        print(f"  Mode: {exec_mode}")

        resp = await _post(s, server, token,
                           f"/api/training/jobs/{job_id}/deploy", body)
        if "error" in resp:
            print(f"Error: {resp['error']}")
        else:
            print(f"\nInference deployed! Stop with: b2r stop-infer {job_id}")


async def cmd_stop_infer(args):
    """Stop inference for a training job."""
    if not args:
        print("Usage: b2r stop-infer <job_id>")
        return
    server, token, _ = _resolve()
    if not token:
        _need_login()
    async with aiohttp.ClientSession() as s:
        resp = await _post(s, server, token,
                           f"/api/training/jobs/{args[0]}/stop-inference")
        pp(resp)


async def cmd_calibrate(args):
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    servo_id = int(args[0]) if args else 0
    async with aiohttp.ClientSession() as s:
        pp(await _post(s, server, token,
                       f"/api/device/{device}/calibrate",
                       {"servo_id": servo_id}))


# ── Dispatch ──────────────────────────────────────────────────────────

COMMANDS = {
    "login": cmd_login,
    "devices": cmd_devices,
    "status": cmd_status,
    "move": cmd_move,
    "home": cmd_home,
    "torque": cmd_torque,
    "record": cmd_record,
    "play": cmd_play,
    "snapshot": cmd_snapshot,
    "frame": cmd_frame,
    "download": cmd_download,
    "dataset": cmd_dataset,
    "video": cmd_video,
    "calibrate": cmd_calibrate,
    "train": cmd_train,
    "jobs": cmd_jobs,
    "deploy": cmd_deploy,
    "stop-infer": cmd_stop_infer,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        print("Commands:")
        for name in COMMANDS:
            print(f"  {name}")
        return
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
        return
    asyncio.run(COMMANDS[cmd](sys.argv[2:]))


if __name__ == "__main__":
    main()
