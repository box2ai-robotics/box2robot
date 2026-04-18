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
  b2r record start/stop [name]           # Recording control
  b2r play [traj_id]                     # Play trajectory (no args = list)
  b2r snapshot                           # Camera snapshot
  b2r calibrate [servo_id]               # Auto-calibrate (0 = all)

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
        print("Usage: b2r record start/stop [name]")
        return
    server, token, device = _resolve()
    if not token:
        _need_login()
    if not device:
        _need_device()
    async with aiohttp.ClientSession() as s:
        if args[0] == "start":
            mode = args[1] if len(args) > 1 else "single"
            pp(await _post(s, server, token,
                           f"/api/device/{device}/record/start", {"mode": mode}))
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
                print(f"  {str(tid)[:8]}  {name} ({frames} frames)")
        else:
            pp(await _post(s, server, token,
                           f"/api/device/{device}/trajectory/{args[0]}/play"))


async def cmd_snapshot(args):
    server, token, device = _resolve()
    if not token:
        _need_login()
    # Use camera device if specified, else find one
    cam = args[0] if args else device
    if not cam:
        _need_device()
    async with aiohttp.ClientSession() as s:
        pp(await _post(s, server, token, f"/api/camera/{cam}/snapshot"))


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
    "calibrate": cmd_calibrate,
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
