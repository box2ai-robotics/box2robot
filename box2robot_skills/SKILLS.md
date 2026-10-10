# Box2Robot Skills — Agent and HTTP reference

For the complete Chinese walkthrough, API Key creation and shell setup, read [README.md](README.md). For operation authorization and target checks, read [SKILL.md](SKILL.md). This reference describes the bundled standalone `b2r.py`, not the separate internal Action CLI.

## First connection

1. Sign in to the web console, bind the arm and verify calibration.
2. Account → API 密钥 → ＋ 生成新密钥 → name → scopes → 生成 → 复制 → 我已保存. The full key is shown once. Choose `device` for basic control; add functional scopes for later steps.
3. Set `B2R_API_KEY` in the runtime environment. Run `python b2r.py devices` and set `B2R_DEVICE` to the intended complete device ID. See README for PowerShell and Bash secret-input examples.
4. Run `python b2r.py status`. Check detailed feedback and calibrated limits in the web console or `GET /api/device/{id}/servos` before motion.
5. Execute only the authorized action; verify device feedback afterward. A platform key does not replace the model provider key used by Code as Policy.

## Commands and API routes

Prefix each command below with `python b2r.py`. All paths are relative to `B2R_SERVER` (default `https://robot.box2ai.com`). Replace IDs with complete values from responses, not truncated list output.

| Command | Method / route | Notes |
|---|---|---|
| `login` | POST `/api/auth/login`, then GET `/api/devices` | Prompts for username/password |
| `login --key KEY` | GET `/api/devices` | Saves key as-is; no JWT exchange |
| `logout` | Local only | Deletes cache, does not revoke key |
| `devices` | GET `/api/devices` | Includes full device IDs |
| `status [ARM_ID]` | GET `/api/device/{id}/servos` | CLI output is a subset of response fields |
| `move SERVO_ID POSITION [SPEED]` | POST `/api/device/{id}/command` | Body: id, position, speed; default speed 1000 |
| `home` | POST `/api/device/{id}/go_home` | Physical motion |
| `torque on/off` | POST `/api/device/{id}/torque` | Body: enable boolean |
| `record start [--cam CAM_ID]` | POST `/api/device/{id}/record/start` | CLI uses mode single; optional camera_id |
| `record stop [NAME]` | POST `/api/device/{id}/record/stop` | Optional name; retain full returned trajectory ID |
| `record status` | GET `/api/device/{id}/record/status` | Query only |
| `play` | GET `/api/device/{id}/trajectories` | Lists, does not move |
| `play TRAJECTORY_ID` | POST `/api/device/{id}/trajectory/{tid}/play` | Motion; no CLI stop-play command |
| `snapshot CAM_ID` | POST `/api/camera/{id}/snapshot` | Requests capture, does not save a file |
| `frame CAM_ID [out.jpg]` | GET `/api/camera/{id}/frame` | Saves JPEG; 204 means no cached frame |
| `stream CAM_ID [--latest FILE] [--out DIR] [--duration SEC]` | WS `/ws/camera/{id}` | Binary JPEG; default duration 60 seconds |
| `download TRAJECTORY_ID [DIR]` | GET trajectory images list and `/api/traj-image/{tid}/{idx}` | Images only |
| `dataset TRAJECTORY_ID [DIR]` | GET trajectory data and images | JSON + images |
| `video TRAJECTORY_ID [out.mp4] [--fps N]` | GET trajectory images | Local ffmpeg or OpenCV encoding |
| `calibrate [SERVO_ID]` | POST `/api/device/{id}/calibrate` | 0 = all; drives hardware, not a setup prerequisite to run automatically |
| `train [--steps N] [--name NAME]` | POST `/api/training/jobs` | Interactive trajectory selection; ACT, batch 32, chunk 20 |
| `jobs` | GET `/api/training/jobs` | Displayed ID is truncated |
| `deploy JOB_ID` | GET job/devices, POST `/api/training/jobs/{id}/deploy` | Interactive GPU/arm/camera/mode selection |
| `stop-infer JOB_ID` | POST `/api/training/jobs/{id}/stop-inference` | Stops this inference job |
| `store list [keyword] [--type T] [--cat C]` | GET `/api/act/tasks` | Browse |
| `store info TASK_REF` | GET `/api/act/tasks/{ref}` | Read price, type and prerequisites |
| `store buy TASK_REF` | POST `/api/act/tasks/{ref}/purchase` | Spends credits |
| `store run TASK_REF [ARM_ID]` | POST `/api/act/tasks/{ref}/execute` | Body: device_id; behavior depends on skill type |
| `store mine` | GET `/api/act/my-purchases` | Purchased store skills |
| `store meta` | GET `/api/act/meta` | Types/categories/tags |

Trajectory data/images paths are `/api/device/{arm_id}/trajectory/{trajectory_id}/data` and `/api/device/{arm_id}/trajectory/{trajectory_id}/images`.

## HTTP examples

Use the configured environment key; no password login is required for a platform API Key. Bash:

```bash
curl --fail-with-body "$B2R_SERVER/api/devices" \
  -H "Authorization: Bearer $B2R_API_KEY"
curl --fail-with-body "$B2R_SERVER/api/device/$B2R_DEVICE/servos" \
  -H "Authorization: Bearer $B2R_API_KEY"
# Full IDs, unlike the abbreviated CLI displays:
curl --fail-with-body "$B2R_SERVER/api/device/$B2R_DEVICE/trajectories" \
  -H "Authorization: Bearer $B2R_API_KEY"
curl --fail-with-body "$B2R_SERVER/api/training/jobs" \
  -H "Authorization: Bearer $B2R_API_KEY"
```

For a previously authorized small move, POST `/api/device/{id}/command` with JSON `{"id":1,"position":2068,"speed":300}` **only if the current position, calibrated range and workspace make that target appropriate**. HTTP calls bypass CLI prompts; apply the same target and authorization checks in your application.

## Reliable workflows

- Camera-assisted demonstration: explicit arm/camera → support arm → authorized torque release → record start → user demonstration → record stop → retain full ID → inspect frames/images → authorized playback.
- Training: consistent demonstrations → train and confirm parameters → retain full job ID → jobs / web monitor → completed model → deploy and verify selected hardware → observe → stop-infer and verify stop.
- Store: search → info → verify compatibility and price → purchase if needed and authorized → run on explicit arm → observe and use the execution-specific stop controls.
- Code as Policy: open the [web guide](https://robot.box2ai.com/#/pages/developer/docs?section=code-policy), select direct instruction or demonstration skills, configure a model provider key, generate, preview, then execute only when authorized. Preview may read camera images. There is no standalone `cap` command.

`--yes` skips the motion/purchase confirmation only. It does not make interactive training/deployment unattended. Use explicit IDs and bounded capture; do not automate infinite retries or infer completion from HTTP acceptance.

## Credentials and failures

- Priority: B2R_API_KEY > B2R_TOKEN > ~/.b2r_token. B2R_DEVICE and B2R_SERVER override cached values.
- Empty scopes mean full access. Device paths need device scope; camera paths need camera. CLI deployment also queries training jobs and devices before the inference action. See README for scope combinations.
- 401: invalid/revoked credential or wrong server; 403: scope or resource access; 409: inspect current mode/task before retry. Keep error details without exposing credentials.
- Logout only removes the local cache. Clear environment credentials separately and revoke the key in the signed-in web account. API Keys cannot call key-management endpoints.
- No `say`, `shell` or `exec` subcommands exist in this package. Do not refer users to absent monorepo/audio directories or promise all internal Actions are shipped here.
