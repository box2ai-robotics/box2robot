---
name: box2robot
description: Control Box2Robot robotic arms via cloud API — move servos, record trajectories, calibrate, camera, voice, and orchestrate AI training/inference.
version: 0.2.0
homepage: https://robot.box2ai.com
emoji: "\U0001F916"
metadata:
  openclaw:
    requires:
      env: [B2R_TOKEN]
      bins: [python3]
      anyBins: [python3, python]
    primaryEnv: B2R_TOKEN
    install:
      - kind: uv
        package: aiohttp
        bins: []
---

# Box2Robot — Robotic Arm Control Skill

Control ESP32-based robotic arms through a cloud server API. Move servos, record trajectories, calibrate, capture images, voice interaction, and orchestrate AI training/inference — all from a single CLI.

## Setup

```bash
# Install dependency
pip install aiohttp

# Login (one-time, token cached to ~/.b2r_token)
python b2r.py login <username> <password>

# Or set environment variables
export B2R_SERVER="https://robot.box2ai.com"
export B2R_TOKEN="<jwt_token>"
export B2R_DEVICE="B2R-XXXXXXXXXXXX"
```

## CLI Quick Reference

### Device & Status
```bash
b2r.py devices                     # List devices (* = online)
b2r.py status                      # Servo positions, load, temperature
```

### Servo Control
```bash
b2r.py torque on                   # Lock servos
b2r.py torque off                  # Release (allows manual dragging)
b2r.py home                        # Safe return to home position
b2r.py move <servo_id> <pos> [spd] # Move a single servo
# Position: 0-4095 (center 2048), Speed: 0-4000 (default 1000)
```

### Recording & Playback
```bash
b2r.py record start                # Start recording
b2r.py record stop [name]          # Stop and save
b2r.py play                        # List all trajectories
b2r.py play <traj_id>              # Play a trajectory
```

### Camera & Calibration
```bash
b2r.py snapshot                    # Camera snapshot
b2r.py calibrate [servo_id]        # Auto-calibrate (0 = all)
```

## Action System (79 Actions)

Invoke via `b2r.py exec <action_name>`. Parameters in JSON or key=value format.

### device (Device Management)
| Action | Description |
|--------|-------------|
| `device.list` | List devices |
| `device.status` | Device status (control mode + servos) |
| `device.bind` | Bind device (requires bind_code) |
| `device.unbind` | Unbind device |
| `device.rename` | Rename device |
| `device.factory_reset` | Factory reset |

### servo (Servo Control)
| Action | Parameters | Description |
|--------|------------|-------------|
| `servo.status` | device_id | Read position/load/temperature |
| `servo.move` | device_id, servo_id, position, [speed] | Move single servo |
| `servo.move_batch` | device_id, commands:[{id,position,speed}] | Batch move |
| `servo.torque` | device_id, enable:bool | Toggle torque |
| `servo.go_home` | device_id | Return to home position |
| `servo.set_id` | device_id, old_id, new_id | Burn servo ID |

### recording (Recording & Playback)
| Action | Parameters | Description |
|--------|------------|-------------|
| `record.start` | device_id, [mode], [camera_id] | Start recording (single/dual/phone) |
| `record.stop` | device_id, [name] | Stop recording |
| `trajectory.list` | device_id | List trajectories |
| `trajectory.play` | device_id, trajectory_id | Play trajectory |
| `trajectory.stop` | device_id, trajectory_id | Stop playback |
| `trajectory.delete` | device_id, trajectory_id | Delete trajectory |

### pairing (Leader-Follower)
| Action | Parameters | Description |
|--------|------------|-------------|
| `pairing.create` | leader_id, follower_ids, [use_espnow] | Create pairing |
| `pairing.disconnect` | leader_id | Disconnect |
| `pairing.connect` | leader_id | Reconnect |
| `pairing.delete` | leader_id | Delete pairing |
| `espnow.discover` | — | Scan nearby devices |

### calibration
| Action | Parameters | Description |
|--------|------------|-------------|
| `calibrate.auto` | device_id, [servo_id=0] | Auto-calibrate (0 = all) |
| `calibrate.manual` | device_id, servos:[{id,min,max,mid}] | Manual calibration |
| `calibrate.center_offset` | device_id | Write EEPROM center offset |

### camera (Camera & Voice)
| Action | Parameters | Description |
|--------|------------|-------------|
| `camera.snapshot` | device_id | Take photo |
| `camera.stream_mode` | device_id, mode | Stream mode (idle/preview/inference) |
| `camera.voice_start` | device_id | Enable microphone |
| `camera.voice_stop` | device_id | Disable microphone |
| `camera.tts` | device_id, prompt | Text-to-speech |

### training (Training & Inference)
| Action | Parameters | Description |
|--------|------------|-------------|
| `training.submit` | name, dataset_ids, [model_type] | Submit training job |
| `training.list` | [page] | Job list |
| `training.deploy` | job_id, arm_device_id, gpu_device_id | Deploy inference |
| `training.stop_inference` | job_id | Stop inference |

### store (Skill Store)
| Action | Parameters | Description |
|--------|------------|-------------|
| `store.list` | [search], [category] | Browse store |
| `store.execute` | task_id, device_id | Execute skill |

### config (Configuration)
| Action | Parameters | Description |
|--------|------------|-------------|
| `config.get` | device_id | View config |
| `config.set` | device_id, params:{key:val} | Update config |
| `config.speed` | device_id, speed(100-4000) | Set speed |
| `config.volume` | device_id, volume(0-255) | Volume |

## Natural Language

Via `b2r.py say "text"`:

| Input | Maps to |
|-------|---------|
| "go home" / "回零位" | servo.go_home |
| "release torque" / "释放力矩" | servo.torque(False) |
| "start recording" / "开始录制" | record.start |
| "take a photo" / "拍照" | camera.snapshot |
| "auto calibrate" / "自动校准" | calibrate.auto |
| "move servo 1 to 2048" | servo.move(1, 2048) |

## Preflight Checks

Agents must verify before executing:

| Step | Check | On Failure |
|------|-------|------------|
| 1 | Device online | "Device offline — check power" |
| 2 | Device type = arm | "Not a robotic arm" |
| 3 | Control mode = idle | release_control first |
| 4 | Calibration exists | calibrate.auto first |
| 5 | Servo count matches | Trajectory vs device axis count |

## Orchestration Examples

### "Teach you to pour water, record 5 demos"
```
1. torque off
2. camera.stream_mode(cam_id, "preview")
3. Loop 5x:
   record.start(mode="single", camera_id)
   [user demonstrates]
   record.stop(name="pour_water_{n}")
4. training.submit(name="pour_water", dataset_ids)
```

### Visual Inference Loop
```
1. training.deploy(job_id, arm_id, gpu_id, cam_id)
2. GPU auto-loop: image → inference → command → arm
3. training.stop_inference(job_id)
```

## HTTP API Direct Access

```bash
# Login
curl -X POST https://robot.box2ai.com/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"xxx","password":"xxx"}'

# Move servo
curl -X POST https://robot.box2ai.com/api/device/B2R-xxx/command \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"id":1,"position":2048,"speed":1000}'
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `B2R_SERVER` | Server URL | `https://robot.box2ai.com` |
| `B2R_TOKEN` | JWT token (overrides ~/.b2r_token) | — |
| `B2R_DEVICE` | Default device ID | — |
