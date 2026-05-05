---
name: box2robot
description: Control Box2Robot robotic arms via cloud API — move servos, record trajectories with camera, stream live frames, browse the ACT skill store, download datasets, generate videos, and orchestrate AI training/inference.
version: 0.7.0
homepage: https://robot.box2ai.com
emoji: "\U0001F916"
metadata:
  openclaw:
    requires:
      anyBins: [python3, python]
      config: [~/.b2r_token]
    primaryEnv: B2R_TOKEN
    install:
      - kind: uv
        package: "aiohttp>=3.9,<4"
        bins: []
---

# Box2Robot — Robotic Arm Control Skill

Control ESP32-based robotic arms through a cloud server API. Move servos, record trajectories with camera, download datasets, generate replay videos, and orchestrate AI training/inference — all from a single CLI (`b2r.py`).

> **Official skill** published by the Box2Robot team (https://robot.box2ai.com).

## Safety & Supervision

> **This skill controls physical robotic hardware and camera/microphone peripherals.**
>
> - **Human supervision required**: Do NOT run autonomously without operator oversight. Servo torque and motion commands cause physical movement that could injure people or damage objects.
> - **Built-in confirmation gate**: Every command that moves hardware or spends account credits is gated by a confirmation prompt — see *Confirmation & Safety Gating* below. AI agents cannot trigger these actions silently.
> - **Privacy-sensitive operations** (`snapshot`, `frame`, `stream`, `record start --cam`) access camera hardware — only invoke with user consent. `stream` defaults to a 60-second cap; pass `--duration 0` only when explicitly required.
> - **No OS shell access**: All operations are HTTP/WebSocket requests to `B2R_SERVER`. No arbitrary OS commands are executed. The only local subprocess is `ffmpeg` (optional, for `b2r video` generation from downloaded JPEG frames).
> - **Token sensitivity**: `~/.b2r_token` stores a JWT that grants device control. Created with mode 0600 (owner-only). Treat like an SSH key. Run `b2r logout` (or delete the file) when no longer needed; change your account password to revoke server-side.

## Confirmation & Safety Gating

The CLI gates these high-impact commands behind an interactive confirmation prompt:

| Command | Reason it is gated |
|---------|-------------------|
| `move`, `home`, `play <id>` | Causes physical motion of the arm |
| `torque on/off` | `off` may cause the arm to drop; `on` locks it suddenly |
| `calibrate` | Drives servos to physical end-stops |
| `deploy` | Starts autonomous ML inference loop |
| `store buy` | Spends account credits |
| `store run` | Causes physical motion via a community-uploaded skill |

**Behavior:**
- **Interactive TTY** → prompts `Proceed? [y/N]` (default: no).
- **Non-interactive** (agent, CI, pipe) → command **refuses to run** unless `--yes` / `-y` is on the command line.
- **`--yes` / `-y`** is a global flag that explicitly skips the prompt. AI agents wrapping this CLI MUST surface the action to the user and only pass `--yes` after explicit user approval.

```bash
# Interactive use — prompted before motion:
b2r move 1 2048
#   CONFIRM: Move servo on B2R-XXXXXXXXXXXX (physical motion)  (id=1, position=2048, speed=1000)
#   Proceed? [y/N]: y

# Agent use — must pass --yes after surfacing intent to user:
b2r --yes move 1 2048    # or: b2r move 1 2048 --yes
```

Read-only commands (`devices`, `status`, `record status`, `play` without args, `jobs`, `store list/info/mine/meta`, `frame`, `snapshot`, `download`, `dataset`, `video`) and the safety-stop command (`stop-infer`) are **not** gated.

## Credential Flow

```
login → POST /api/auth/login → JWT token
  → saved to ~/.b2r_token (mode 0600, owner-only)
  → all subsequent commands use this token automatically
  → override with B2R_TOKEN env var
  → delete ~/.b2r_token to revoke
```

All network calls go exclusively to `B2R_SERVER` (default: `https://robot.box2ai.com`). No other endpoints are contacted.

## Environment Variables

| Variable | Required | Description | Default |
|----------|----------|-------------|---------|
| `B2R_SERVER` | No | Server URL | `https://robot.box2ai.com` |
| `B2R_TOKEN` | No | JWT token (overrides ~/.b2r_token) | — |
| `B2R_DEVICE` | No | Default device ID (overrides auto-select) | — |

None are strictly required at install time. The `login` command handles authentication interactively and persists the token to `~/.b2r_token`. `B2R_TOKEN` is the primary credential variable and can be set to skip interactive login.

## Setup

```bash
# Install pinned dependency (3.9 ≤ aiohttp < 4)
pip install "aiohttp>=3.9,<4"

# Login (one-time, token cached to ~/.b2r_token, mode 0600)
python b2r.py login <username> <password>

# Revoke local token when done
python b2r.py logout
```

## Commands

### Device & Status
```bash
b2r.py devices                     # List devices (* = online)
b2r.py status                      # Servo positions, load, temperature
```

### Servo Control
```bash
b2r.py torque on                   # Lock servos
b2r.py torque off                  # Release (allows manual dragging)
b2r.py home                        # Return to home position
b2r.py move <servo_id> <pos> [spd] # Move a single servo
# Position: 0-4095 (home varies per joint), Speed: 0-4000 (default 1000)
```

### Recording & Playback
```bash
b2r.py record start                # Start recording (servo data only)
b2r.py record start --cam CAM-xxx  # Record with camera (servo + images)
b2r.py record stop [name]          # Stop and save
b2r.py record status               # Current recording status
b2r.py play                        # List all trajectories
b2r.py play <traj_id>              # Play a trajectory
```

When starting a recording, if online cameras are detected, the CLI offers an interactive prompt to select one. Camera recording captures synchronized JPEG frames alongside servo position data.

### Camera
```bash
b2r.py snapshot                    # Request camera snapshot
b2r.py frame [cam_id] [out.jpg]   # Download latest JPEG frame to local file
b2r.py stream <cam_id> [--out DIR] [--latest FILE] [--duration SEC]
                                   # Live MJPEG-over-WebSocket stream @ ~10Hz
# Default: writes ./frame.jpg, auto-stops after 60s (privacy cap)
# --out DIR     : save every frame as DIR/000001.jpg, 000002.jpg, ...
# --latest FILE : overwrite a single rolling file (default: ./frame.jpg)
# --duration SEC: auto-stop after N seconds. Default 60. Pass 0 for unlimited.
```

`stream` connects to `/ws/camera/{cam_id}`. The server auto-switches the camera into 10fps preview mode on first viewer and back to idle when all viewers disconnect — no manual mode toggle needed.

> **Privacy note**: These commands access camera hardware. Only invoke with user consent.

### ACT Skill Store
```bash
b2r.py store list [keyword] [--type T] [--cat C]   # Browse community-uploaded skills
b2r.py store info <task>                            # Skill detail (TASK-... code or task_id)
b2r.py store buy <task>                             # Purchase a paid skill (deducts credits)
b2r.py store run <task> [device]                    # Execute a purchased skill on a device
b2r.py store mine                                   # List skills you've purchased
b2r.py store meta                                   # List available categories / types / tags
```

The ACT Store is a marketplace of reusable, pre-trained robot skills (e.g. "wave", "pour water"). Free skills can be run directly; paid skills require `store buy` first. Execution sends the skill to the selected arm device and runs server-side inference — physical movement still requires human supervision.

### Data Download
```bash
b2r.py download <traj_id> [dir]    # Download trajectory images only
b2r.py dataset <traj_id> [dir]     # Download full dataset (JSON + images)
b2r.py video <traj_id> [out.mp4]   # Generate MP4 video from trajectory images
b2r.py video <traj_id> out.mp4 --fps 5  # Custom frame rate
```

`dataset` downloads the trajectory JSON (all frames with positions, timestamps, calibration snapshots) plus all camera images into a local directory.

`video` downloads images to a temp directory and encodes them using `ffmpeg` (preferred) or `opencv-python` (fallback). Neither is required at install time — the command reports a clear error if both are missing.

### Calibration
```bash
b2r.py calibrate [servo_id]        # Auto-calibrate (0 = all servos)
```

> **Hardware note**: Calibration physically moves servos to their limits. Ensure the arm is clear of obstacles.

### Training & Inference
```bash
b2r.py train                       # Submit training job (interactive)
b2r.py train --steps 50000 --name my_model
b2r.py jobs                        # List training jobs and status
b2r.py deploy <job_id>             # Deploy inference (interactive device selection)
b2r.py stop-infer <job_id>         # Stop inference
```

`train` interactively lists available trajectories, lets you select datasets (e.g., `1,3,5` or `1-5` or `all`), confirms parameters, then submits to the server.

`deploy` interactively selects GPU device, arm device, camera (optional), and execution mode (original/fixed/adaptive/overlap), then deploys.

## API Endpoints Used

All commands are thin wrappers over HTTP API calls to `B2R_SERVER`:

| Command | Method | Endpoint |
|---------|--------|----------|
| login | POST | `/api/auth/login` |
| logout | (local) | Deletes `~/.b2r_token` — no network call |
| devices | GET | `/api/devices` |
| status | GET | `/api/device/{id}/servos` |
| move | POST | `/api/device/{id}/command` |
| home | POST | `/api/device/{id}/go_home` |
| torque | POST | `/api/device/{id}/torque` |
| record start | POST | `/api/device/{id}/record/start` |
| record stop | POST | `/api/device/{id}/record/stop` |
| record status | GET | `/api/device/{id}/record/status` |
| play | GET/POST | `/api/device/{id}/trajectories`, `.../trajectory/{id}/play` |
| snapshot | POST | `/api/camera/{id}/snapshot` |
| frame | GET | `/api/camera/{id}/frame` |
| stream | WS | `/ws/camera/{id}` (binary JPEG frames @ ~10Hz) |
| store list | GET | `/api/act/tasks` |
| store info | GET | `/api/act/tasks/{ref}` |
| store buy | POST | `/api/act/tasks/{ref}/purchase` |
| store run | POST | `/api/act/tasks/{ref}/execute` |
| store mine | GET | `/api/act/my-purchases` |
| store meta | GET | `/api/act/meta` |
| download | GET | `.../trajectory/{id}/images`, `/api/traj-image/{id}/{idx}` |
| dataset | GET | `.../trajectory/{id}/data`, `.../trajectory/{id}/images` |
| video | GET | `.../trajectory/{id}/images`, `/api/traj-image/{id}/{idx}` |
| calibrate | POST | `/api/device/{id}/calibrate` |
| train | POST | `/api/training/jobs` |
| jobs | GET | `/api/training/jobs` |
| deploy | POST | `/api/training/jobs/{id}/deploy` |
| stop-infer | POST | `/api/training/jobs/{id}/stop-inference` |

## Preflight Checks (for AI Agents)

Agents should verify before executing servo/recording commands:

| Step | Check | On Failure |
|------|-------|------------|
| 1 | Device online | "Device offline — check power" |
| 2 | Device type = arm | "Not a robotic arm" |
| 3 | Calibration exists | Run `calibrate` first |

## Orchestration Examples

### Record training data with camera
```
1. b2r torque off
2. b2r record start --cam CAM-xxx
3. [user demonstrates task by hand]
4. b2r record stop my_dataset
5. b2r dataset <traj_id>           # download locally
6. b2r video <traj_id> demo.mp4    # generate preview video
```

### Train and deploy
```
1. b2r train                       # select datasets, submit job
2. b2r jobs                        # monitor progress
3. b2r deploy <job_id>             # deploy to GPU + arm  (confirmation required)
4. [robot executes learned skill]
5. b2r stop-infer <job_id>         # stop when done       (no confirmation — safety stop)
```

## Review Findings Addressed (ClawHub Security Review)

| Finding | Mitigation in this version |
|---------|---------------------------|
| **Tool Misuse — physical motion without confirmation** (High) | All motion commands (`move`, `home`, `torque`, `play <id>`, `calibrate`, `deploy`, `store run`) are gated by `_confirm()`. Non-interactive callers must pass `--yes`/`-y` after explicit user approval. |
| **Tool Misuse — store buy/run without approval** (Medium) | `store buy` and `store run` go through the same confirmation gate; `--yes` flag is the only bypass. |
| **Supply Chain — unpinned aiohttp** (Low) | `aiohttp` pinned to `>=3.9,<4` in both `pyproject.toml` and the SKILL.md install spec. |
| **Identity — JWT persistence** (Medium, Note) | New `b2r logout` command for one-shot revocation. Token storage path, mode (0600), and revocation steps documented in *Safety & Supervision*. |
| **Privacy — camera stream** (Medium, Note) | `b2r stream` now defaults to `--duration 60` (auto-stop after 60s). A privacy banner is printed at stream start. Unlimited streaming requires explicit `--duration 0`. |
