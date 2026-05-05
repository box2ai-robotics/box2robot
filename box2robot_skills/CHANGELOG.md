# Changelog

All notable changes to the `box2robot` ClawHub skill are documented here. This
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-05-05

### Added
- `b2r stream <cam_id>` — live MJPEG-over-WebSocket camera stream via
  `/ws/camera/{id}`. Supports `--out DIR` (per-frame archive),
  `--latest FILE` (rolling single-file overwrite, default `./frame.jpg`),
  and `--duration SEC` (auto-stop). Server auto-switches the camera
  into 10fps preview mode while a viewer is connected.
- `b2r store` subcommands for the ACT Skill Store marketplace:
  - `store list [keyword] [--type T] [--cat C]` — browse community skills
  - `store info <task>` — skill detail by code (`TASK-...`) or `task_id`
  - `store buy <task>` — purchase a paid skill (credit deduction)
  - `store run <task> [device]` — execute a skill on an arm device
  - `store mine` — list purchased skills
  - `store meta` — list categories / types / tags
- `b2r logout` — deletes the cached JWT at `~/.b2r_token` and prints
  guidance for revoking server-side access. Closes a Medium-severity
  ClawHub review note about credential persistence.
- Global `--yes` / `-y` flag — explicit, agent-safe bypass of the new
  confirmation gate. Stripped from argv before command dispatch.
- `_confirm()` safety gate (high-impact ops): `move`, `home`, `torque`,
  `play <id>`, `calibrate`, `deploy`, `store buy`, `store run` now
  prompt `Proceed? [y/N]` on a TTY and refuse to run on a non-TTY
  unless `--yes` was passed. Closes High and Medium ClawHub findings
  on hardware control and credit-spend without confirmation.
- `b2r stream` privacy banner printed at startup, plus a default
  `--duration 60` cap (was unlimited). `--duration 0` is now the only
  way to stream indefinitely. Closes a Medium privacy note about
  long-lived camera capture.
- SKILL.md documents the new commands, the `/ws/camera/{id}` endpoint,
  all `/api/act/*` HTTP endpoints, the confirmation/`--yes` model, and
  a "Review Findings Addressed" mapping table for reviewers.

### Changed
- Bumped skill version from `0.4.0` → `0.7.0` so it tracks the CLI's
  actual capability surface (previously documented capabilities lagged
  the implementation by several minor releases).
- `description` field updated to mention the new stream and store
  capabilities so vector search surfaces them.
- Pinned dependency: `aiohttp>=3.9,<4` in both `pyproject.toml` and
  the SKILL.md `install` spec (was `aiohttp>=3.8` / `aiohttp` with no
  upper bound). Closes a Low-severity supply-chain note.

### Security review summary
| Review finding | Resolution |
|----------------|-----------|
| Hardware motion without confirmation (High) | Confirmation gate + `--yes` opt-in |
| Store buy/run without approval (Medium) | Same confirmation gate |
| Unpinned aiohttp (Low) | Pinned `>=3.9,<4` |
| JWT persistence (Medium, Note) | New `b2r logout`, expanded docs |
| Camera stream privacy (Medium, Note) | 60s default cap + privacy banner |

### Notes for reviewers
- No new credentials are required. `B2R_TOKEN` (the existing
  `primaryEnv`) covers the new `/api/act/*` and `/ws/camera/{id}`
  endpoints — they reuse the same JWT.
- The new `stream` command opens a long-lived WebSocket. It only writes
  to user-specified local paths (`--out` / `--latest`) and never to
  arbitrary system locations.
- All network traffic still goes exclusively to `B2R_SERVER`
  (default `https://robot.box2ai.com`). No third-party endpoints are
  contacted.
- `stop-infer` is intentionally NOT gated — it's the safety-stop for
  a running inference loop and must be as fast to invoke as possible.

## [0.4.0] — initial ClawHub publication

- First public release on ClawHub.
- Commands: `login`, `devices`, `status`, `move`, `home`, `torque`,
  `record start/stop/status`, `play`, `snapshot`, `frame`, `download`,
  `dataset`, `video`, `calibrate`, `train`, `jobs`, `deploy`,
  `stop-infer`.
- Token storage at `~/.b2r_token` (mode 0600).
- Single dependency: `aiohttp`.
