English | [中文](README.md)

# Box2Robot — Embodied AI Cloud Platform

**Plug-and-play robot arm with cloud training, shared skills, and AI agent control.**

<div align="center">
  <img src="assets/whole_robot_system.jpg" alt="Box2Robot System" width="500"/>
</div>

Box2Robot is an open-source embodied AI platform. It connects ESP32-powered robot arms and vision modules to a cloud platform for data collection, model training, and skill sharing. No complex setup — just flash, connect WiFi, bind your device, and start.

> **Current Firmware Package: ARM v0.7.2 / CAM v0.7.1** (Arm firmware v0.7.2 / Camera firmware v0.7.1; both built, packaged, and published for OTA; device-side upgrade and torque-protection validation pending)

## Getting Started

### 1. Get the Hardware

<div align="center">
  <a href="https://item.taobao.com/item.htm?abbucket=5&id=1030962099420">
    <img src="assets/hardware.jpg" alt="Box2AI Hardware" width="400"/>
  </a>
  <br>
  <a href="https://item.taobao.com/item.htm?abbucket=5&id=1030962099420">Purchase the Box2AI Robot Arm Kit (Taobao)</a>
</div>

Assemble the robot arm and connect servos to the driver board. The firmware comes pre-flashed. If you need to flash manually, see [Firmware Update](#firmware-update) below.

### Camera Module Installation

<div align="center">
  <img src="assets/Camera_base mounting_nut.jpg" style="width:30%;max-width:240px;"/>
  <img src="assets/Camera_base_mounting_hole.jpg" style="width:30%;max-width:240px;"/>
  <img src="assets/Camera_base_screw_placement_fixation.jpg" style="width:30%;max-width:240px;"/>
</div>

<div align="center">
  <img src="assets/Camera_placement_and_screw_fixation.jpg" style="width:45%;max-width:360px;"/>
  <img src="assets/camera_line_connect.jpg" style="width:45%;max-width:360px;"/>
</div>

### Battery Tray & Driver Board Installation

<div align="center">
  <img src="assets/battery_base_install1.jpg" style="width:45%;max-width:360px;"/>
  <img src="assets/battery_base_install2.jpg" style="width:45%;max-width:360px;"/>
</div>

<div align="center">
  <img src="assets/battery_base_and_driver_install.jpg" style="width:45%;max-width:360px;"/>
  <img src="assets/battery_install.jpg" style="width:45%;max-width:360px;"/>
</div>

> **The driver board and camera are separate devices: configure WiFi and bind each one to the same account.** After setting up the arm, connect to the camera hotspot and repeat provisioning and binding. Both use `192.168.4.1`, but the page belongs to the currently connected device.
> [Public user manual (no login)](https://robot.box2ai.com/#/pages/developer/docs?section=connect) · [Firmware, flashing and protection commands](bin/README_en.md)

### 2. Connect to Device Hotspot

Power on the device. It creates a WiFi hotspot:
- **Arm Driver Board:** `Box2Robot_XXXX` (XXXX = last 4 of MAC)
- **Vision-Audio Module:** `Box2Cam_XXXX`

Connect your phone/PC to this hotspot. A captive portal opens automatically (or go to `192.168.4.1`).

### 3. Configure WiFi

Enter your 2.4 GHz WiFi name and password in the portal. The device saves credentials, reboots, and connects to your network. Switch between Chinese and English in the portal. Rescanning displays `Scanning`, keeps old networks selectable, and refreshes the list on success.

### 4. Bind on Platform

Once online, the device gets a **6-digit binding code**:
- **Arm:** Shown on the OLED screen
- **Camera:** Announced via TTS voice

Then:
1. Open [**https://robot.box2ai.com**](https://robot.box2ai.com/#/)
2. Register an account
3. Go to **Device Management → Bind Device**
4. Enter the 6-digit code
5. Done!

You now have full access: remote control, calibration, data collection, cloud training, skill store, and voice interaction.

### 5. Button Operations

The gray button on the wireless driver box supports the following:

| Action | Function |
|--------|----------|
| **Single Press** | Release Torque — unlocks all servos so you can freely move the arm by hand |
| **Long Press (3+ seconds)** | Factory Reset — clears saved WiFi credentials; the device reboots into hotspot mode for re-provisioning |

---

## AI Agent Control (Skills CLI)

Start with the [step-by-step Skills guide](box2robot_skills/README.md) (Chinese) or the [English agent/API reference](box2robot_skills/SKILLS.md).

1. Sign in to the [console](https://robot.box2ai.com), bind the arm and check its online state and calibration.
2. Open account → [API keys](https://robot.box2ai.com/#/pages/profile/api-keys) → “＋ 生成新密钥”. Enter a name, choose scopes, generate, copy and save the full key. Plaintext is shown once. Basic control uses `device`; add other functional scopes as needed.
3. Get the [ClawHub package](https://clawhub.ai/boxjod/box2robot-skills), or clone this repository and enter `box2robot_skills`:

```bash
python -m pip install "aiohttp>=3.9,<4"
python b2r.py --help
# Configure B2R_API_KEY and B2R_DEVICE as shown in the guide, then:
python b2r.py devices
python b2r.py status
```

4. Read current position and calibrated limits before an authorized, supervised small movement. The guide includes PowerShell and Bash setup; API acceptance does not prove motion completed.
5. Follow camera streaming, recording, dataset/video export, ACT training/inference and skill-store workflows. Web-only features include Code as Policy, context skills, the personal skill repository and pose memory.

Agents should read [SKILL.md](box2robot_skills/SKILL.md) and use [SKILLS.md](box2robot_skills/SKILLS.md) for commands and endpoints. The standalone CLI does not implement `say`, `shell` or `exec`.

[Web API guide](https://robot.box2ai.com/#/pages/developer/docs?section=api) · [Code as Policy](https://robot.box2ai.com/#/pages/developer/docs?section=code-policy). Platform and model-provider keys are different. CLI logout only clears the local cache; revoke unused keys in the console.

---

## GPU Training & Inference Node (Advanced)

`box2robot_gpu_worker/` is a GPU compute node that connects to the Box2Robot cloud server and automatically picks up training and inference jobs. Requires an **RTX 3060+ GPU**, supports ACT (Action Chunking Transformer), Diffusion Policy, MLP and more.

Just 3 steps: install → start → enter binding code in the APP. Once bound, the Worker automatically polls for tasks, downloads datasets, trains models, and reports progress — all operations are managed from the APP.

When cloning, **you must include `--recurse-submodules`** to pull the LeRobot submodule:

```bash
git clone --recurse-submodules https://github.com/box2ai-robotics/box2robot.git
```

> Already cloned without submodules? Run `git submodule update --init --recursive` in the repo root to fetch LeRobot.

See [box2robot_gpu_worker/README.md](box2robot_gpu_worker/README.md) for details.

---

## Firmware Update

The device ships with the latest firmware pre-flashed. **Online OTA update is recommended**:

1. After binding, open [https://robot.box2ai.com](https://robot.box2ai.com/#/) → **Device Management**
2. In device details, click **Firmware Update**. The cloud will push the latest firmware automatically.

Current OTA versions are **ARM v0.7.2 / CAM v0.7.1**; update each device separately. Normal OTA writes only the application image and does not erase the entire flash. Stop recording, inference, and motion, support the arm, and keep power and networking stable. The version API, release notes, and SHA256 of both actual firmware downloads have been verified; publishing does not mean devices have already upgraded.

**Manual flashing** (for first-time flashing of blank boards, devices that won't boot, or custom-built firmware): see [`bin/README_en.md`](bin/README_en.md).

---

## Changelog

| Version | Date | Notes |
|---------|------|-------|
| v0.7.2 (arm) | 2026-10-10 | Compensate UART time in slider paths and skip repeated samples; per-joint raw-load thresholds, fixed 100-tick lead, no trip at equality; protection off restores output limit 1000; raw model and output-limit telemetry. |
| v0.7.1 (arm) | 2026-10-10 | Persistent torque-protection switch, on by default; disabling removes load limiting and target clamping while retaining thermal protection. WiFi rescans preserve previous results, show Scanning, and support Chinese/English. |
| v0.7.1 (cam) | 2026-10-12 | Fix WiFi provisioning after factory reset/scanning: stop scans, clear stale state and explicitly connect. Preserve bilingual setup and saved-network reconnection. |
| v0.7.0 (cam) | 2026-10-10 | WiFi provisioning retains selectable results while scanning, prevents duplicate scans, recovers from timeouts, and supports Chinese/English. Trusted HTTPS OTA validation is retained. |
| v0.6.9 (cam) | 2026-10-07 | OTA security and reliability: trusted HTTPS download URLs, no HTTP redirects, boot-partition switching only after inactive-image validation, a 30-minute overall timeout, a 60-second no-progress timeout, and failure-reason reporting. Firmware, bootloader, and partition images are provided; older packages are retained. Device-side OTA validation is pending. |
| v0.7.0 (arm) | 2026-10-07 | Add independent protection for all six joints: a 20 ms check interval (target 50 Hz), three-sample load filtering, factory limits 500/600/650/550/450/150 for IDs 1–6 in raw load units, backdrive yield with reduced torque and slow return after release, thermal/stall safeguards, and fast WebSocket telemetry plus immediate `servo_protection` events. |
| v0.6.6 (arm) | 2026-05-04 | Fix WS disconnect during recording (transport_poll_write): RAM recording buffer 1200→300 frames frees ~80KB BSS, heap recovers from 30KB to ~110KB; long recordings fall back to SPIFFS |
| v0.6.5 (arm) | 2026-05-02 | CLI adds ACT Skill Store commands (`store list/info/buy/run/mine`); device/trajectory/job listings now show short codes; GPU Worker training/inference flow polished |
| v0.6.3 | 2026-04-26 | Flash docs revamped (esptool + Flash Download Tool); legacy bin files archived under `History/` |
| v0.6.1 | 2026-04-19 | GPU Worker open-sourced, fix Hiwonder servo calibration offset write, add servo voltage range selection (5V/7.4V/12V), WiFi Leader-Follower teleoperation smoothness optimization |
| v0.5.1 | 2026-04-14 | Cloud platform integration, WebSocket relay, OTA, ESP-NOW 50Hz, camera MJPEG+ADPCM audio, voice AI, auto-calibration |
| v0.4.5 | 2026-03-23 | (LeRobot-ESP32) Hiwonder LX servo support, auto-detect servo type |

## Links

- **Cloud Platform:** [https://robot.box2ai.com](https://robot.box2ai.com/#/)
- **Hardware Purchase:** [Taobao Store](https://item.taobao.com/item.htm?abbucket=5&id=1030962099420)
- **Previous Project (ESP-NOW only):** [LeRobot-ESP32](https://github.com/box2ai-robotics/lerobot-esp32)
- **LeRobot Framework:** [Hugging Face LeRobot](https://github.com/huggingface/lerobot)

## License

Apache 2.0 License

---

If this project helps you, please give it a star!
