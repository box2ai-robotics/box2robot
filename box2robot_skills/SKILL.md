---
name: box2robot
description: Control Box2Robot robotic arms via cloud API — move servos, record trajectories with camera, stream live frames, browse the ACT skill store, download datasets, generate videos, and orchestrate AI training/inference. Authenticate with an account login or a user API key (`b2r login --key` / `B2R_API_KEY`).
version: 0.8.0
homepage: https://robot.box2ai.com
emoji: "\U0001F916"
metadata:
  openclaw:
    requires:
      anyBins: [python3, python]
    primaryEnv: B2R_API_KEY
    install:
      - kind: uv
        package: "aiohttp>=3.9,<4"
        bins: []
---

# Box2Robot — robot control through the standalone CLI

Use the bundled `b2r.py` for device status, servo control, camera frames, recording, datasets, ACT training/inference and the skill store. Run commands from this skill directory with `python b2r.py ...` (or the installed `b2r` entrypoint).

## Read the relevant guide

- First setup, API Key creation, Windows/Bash examples and troubleshooting: [README.md](README.md).
- Command/API mapping, complete IDs and agent workflows: [SKILLS.md](SKILLS.md).
- Web-only workflows (Code as Policy, context skills, personal skill repository, pose memory): README section 6 and the linked web guides. Do not invent `say`, `shell`, `exec` or `cap` subcommands: this standalone CLI does not implement them.

## Authentication and target selection

1. Have the user sign in at https://robot.box2ai.com, bind devices and open account → API 密钥 → 生成新密钥. Name the key, select functional scopes, generate, copy and save the one-time plaintext. A `b2r_...` platform key is different from a model provider key.
2. Use `B2R_API_KEY` from the user's environment, or `python b2r.py login --key <API_KEY>` to persist it. Do not request plaintext in chat or expose it in logs. The latter command queries devices and caches the first online arm; always verify the target.
3. Run `python b2r.py devices`, set `B2R_DEVICE` to the intended complete arm device ID, then run `python b2r.py status`. Use explicit camera IDs for `snapshot`, `frame` and `stream`.

Credential order: `B2R_API_KEY` > `B2R_TOKEN` > `~/.b2r_token`. `B2R_SERVER` overrides the cached server (default https://robot.box2ai.com); `B2R_DEVICE` overrides the cached arm. Both JWT and API Key use `Authorization: Bearer`.

Scopes: device / camera / data / training / inference / store; empty means full access, not no access. Device routes including recording/trajectories require device. CLI deployment reads training jobs and devices before invoking inference, so use training + device + inference. Keys cannot manage `/api/keys`; use the signed-in web account.

`logout` only removes the local cache. It does not clear environment variables or revoke server access. Revoke API Keys in the web account. The cache holds plaintext and attempts POSIX 0600 permissions; Windows access also depends on filesystem ACLs.

## Before physical actions

- Confirm the user's intended device and operation, online state, calibration, valid joint range and unobstructed workspace. Human supervision is required for motion. Do not automatically calibrate or release someone else's control to make a command succeed.
- Positions are encoder counts, not degrees. Never assume 2048 is safe or home for every joint. Start with a small movement relative to fresh feedback, within the actual calibrated range.
- `move`, `home`, `torque`, `play <id>`, `calibrate`, `deploy`, `store buy` and `store run` use the CLI confirmation gate. Interactive callers answer `Proceed? [y/N]`; non-interactive callers must supply `--yes` / `-y`. Use it only for a concrete action already explicitly authorized by the user. Existing authorization for that action does not require another permission round.
- Camera access and recording require user consent. `stream` defaults to 60 seconds; use bounded duration unless unlimited streaming is explicitly requested.
- `torque off` removes support and may drop the arm. `home` causes motion. Neither is a general emergency stop. Use the operation-specific stop, observe the outcome, and report an unconfirmed stop.
- HTTP submission success (`sent`, `queued`) is not proof of completed movement. Check returned errors, fresh state and actual task results; never blindly retry motion, purchases or job submission.

## Workflow boundaries

- `play` with no arguments lists trajectories; it does not play them. The CLI truncates trajectory and job IDs in lists. Obtain complete IDs from create/stop responses or authenticated HTTP lists before subsequent commands.
- `snapshot CAM_ID` requests capture; `frame CAM_ID out.jpg` saves the cached JPEG; `stream CAM_ID --duration 15` starts a bounded live preview. A 204 frame response means no cached frame.
- `record start --cam CAM_ID` → user demonstration → `record stop NAME` → inspect data → authorized playback. `dataset ID DIR` downloads JSON/images; `download` downloads images only. `video` needs ffmpeg or opencv-python.
- `train` is interactive and currently submits ACT with batch size 32 and chunk size 20. Training may consume credits. `deploy JOB_ID` interactively selects an online GPU, arm, camera and mode; `--yes` does not answer selection prompts. Use the web training page for other models and official compute configuration.
- `stop-infer JOB_ID` stops that inference job without the motion confirmation gate. The CLI has no playback-stop subcommand; use the corresponding web controls or a verified HTTP stop endpoint.
- Read `store info` before buying/running. Execution depends on skill type and requirements; `store mine` lists purchases, not the personal VLA/CaP/context skill repository.

Only call the configured server for robot operations. Video generation may invoke local ffmpeg on downloaded images. Do not claim web-only features are available through this CLI.
