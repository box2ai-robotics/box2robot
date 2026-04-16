#!/usr/bin/env python3
"""
Box2Robot CLI - 一行命令控制机械臂

用法:
  b2r login                              # 登录 (交互式输入密码)
  b2r devices                            # 列出设备
  b2r status                             # 舵机状态
  b2r move 1 2048                        # 1号舵机转到2048
  b2r home                               # 回零位
  b2r torque on/off                      # 力矩开关
  b2r record start/stop                  # 录制
  b2r play <traj_id>                     # 播放轨迹
  b2r say "回零位"                        # 自然语言
  b2r shell                              # 交互模式

环境变量:
  B2R_SERVER   服务器地址 (默认 https://robot.box2ai.com)
  B2R_TOKEN    JWT token (login 后自动保存到 ~/.b2r_token)
  B2R_DEVICE   默认机械臂设备ID (自动选第一个在线设备)
"""

import asyncio
import sys
import os
import json

# 定位 CLI 实现
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLI_DIR = os.path.join(os.path.dirname(SCRIPT_DIR),
                       "box2robot_server", "box2robot-cli")
sys.path.insert(0, CLI_DIR)

from client import Box2RobotClient

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


def get_client():
    token, server, device = load_token()
    server = os.environ.get("B2R_SERVER", server or "https://robot.box2ai.com")
    token = os.environ.get("B2R_TOKEN", token)
    device = os.environ.get("B2R_DEVICE", device)
    if not token:
        print("未登录，请先执行: b2r login")
        sys.exit(1)
    return Box2RobotClient(base_url=server, jwt_token=token), device


def pp(data):
    if isinstance(data, bytes):
        print(f"<binary {len(data)} bytes>")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


async def cmd_login(args):
    server = os.environ.get("B2R_SERVER", "https://robot.box2ai.com")
    if len(args) >= 2:
        user, pwd = args[0], args[1]
    elif len(args) == 1:
        user = args[0]
        import getpass
        pwd = getpass.getpass("密码: ")
    else:
        user = input("用户名: ")
        import getpass
        pwd = getpass.getpass("密码: ")

    client = Box2RobotClient(base_url=server)
    resp = await client.login(user, pwd)
    if client.jwt_token:
        # 自动获取默认设备
        from actions.device import list_devices
        devices = await list_devices(client)
        dev_list = devices if isinstance(devices, list) else devices.get("devices", [])
        arm_id = None
        for d in dev_list:
            online = d.get("online", False)
            dtype = d.get("device_type", "arm")
            name = d.get("nickname") or d.get("device_id", "")
            print(f"  [{dtype}] {name} - {'在线' if online else '离线'}")
            if dtype == "arm" and online and not arm_id:
                arm_id = d["device_id"]

        save_token(client.jwt_token, server, arm_id)
        print(f"\n登录成功! Token 已保存到 {TOKEN_FILE}")
        if arm_id:
            print(f"默认设备: {arm_id}")
    else:
        print(f"登录失败: {resp}")
    await client.close()


async def cmd_devices(args):
    client, _ = get_client()
    from actions.device import list_devices
    devices = await list_devices(client)
    dev_list = devices if isinstance(devices, list) else devices.get("devices", [])
    for d in dev_list:
        online = d.get("online", False)
        dtype = d.get("device_type", "arm")
        name = d.get("nickname") or d.get("device_id", "")
        did = d.get("device_id", "")
        mark = "*" if online else " "
        print(f"  {mark} [{dtype:6s}] {name:12s} {did}")
    await client.close()


async def cmd_status(args):
    client, device = get_client()
    device = args[0] if args else device
    if not device:
        print("未指定设备，请 b2r login 或设置 B2R_DEVICE")
        return
    from actions.servo import servo_status
    s = await servo_status(client, device)
    servos = s.get("servos", []) if isinstance(s, dict) else []
    torque = s.get("torque_enabled", None)
    print(f"力矩: {'ON' if torque else 'OFF' if torque is not None else '?'}")
    for sv in servos:
        print(f"  ID{sv['id']:2d}: pos={sv.get('pos',0):4d}  "
              f"load={sv.get('load',0):3d}  temp={sv.get('temp',0)}°C")
    await client.close()


async def cmd_move(args):
    if len(args) < 2:
        print("用法: b2r move <servo_id> <position> [speed]")
        return
    client, device = get_client()
    sid = int(args[0])
    pos = int(args[1])
    spd = int(args[2]) if len(args) > 2 else 1000
    from actions.servo import move_servo
    r = await move_servo(client, device, sid, pos, spd)
    pp(r)
    await client.close()


async def cmd_home(args):
    client, device = get_client()
    from actions.servo import go_home
    r = await go_home(client, device)
    pp(r)
    await client.close()


async def cmd_torque(args):
    if not args:
        print("用法: b2r torque on/off")
        return
    client, device = get_client()
    enable = args[0].lower() in ("on", "true", "1", "yes")
    from actions.servo import set_torque
    r = await set_torque(client, device, enable)
    pp(r)
    await client.close()


async def cmd_record(args):
    if not args:
        print("用法: b2r record start/stop [name]")
        return
    client, device = get_client()
    if args[0] == "start":
        from actions.recording import record_start
        mode = args[1] if len(args) > 1 else "single"
        r = await record_start(client, device, mode=mode)
    elif args[0] == "stop":
        from actions.recording import record_stop
        name = args[1] if len(args) > 1 else None
        r = await record_stop(client, device, name=name)
    elif args[0] == "status":
        from actions.recording import record_status
        r = await record_status(client, device)
    else:
        print("用法: b2r record start/stop/status")
        await client.close()
        return
    pp(r)
    await client.close()


async def cmd_play(args):
    if not args:
        # 列出轨迹
        client, device = get_client()
        from actions.recording import list_trajectories
        r = await list_trajectories(client, device)
        trajs = r if isinstance(r, list) else r.get("trajectories", [])
        for t in trajs:
            tid = t.get("id", t.get("traj_id", "?"))
            name = t.get("name", "unnamed")
            frames = t.get("frame_count", "?")
            print(f"  {tid[:8]}  {name} ({frames} frames)")
        await client.close()
        return
    client, device = get_client()
    from actions.recording import play_trajectory
    r = await play_trajectory(client, device, args[0])
    pp(r)
    await client.close()


async def cmd_say(args):
    if not args:
        print("用法: b2r say \"回零位\"")
        return
    text = " ".join(args)
    client, device = get_client()
    _, _, _ = load_token()
    from intents import IntentRouter
    router = IntentRouter(client, default_arm_id=device)
    result = await router.execute(text)
    pp(result)
    await client.close()


async def cmd_exec(args):
    if not args:
        print("用法: b2r exec <action> [json_params]")
        return
    from actions import get_action
    action_name = args[0]
    action_info = get_action(action_name)
    if not action_info:
        print(f"Action 不存在: {action_name}")
        return
    client, device = get_client()
    params = {}
    # 自动填 device_id
    for p in action_info.params:
        if p.name in ("device_id", "leader_id") and device:
            params[p.name] = device
    if len(args) > 1:
        try:
            params.update(json.loads(" ".join(args[1:])))
        except json.JSONDecodeError:
            for kv in args[1:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k] = v
    try:
        r = await action_info.func(client, **params)
        pp(r)
    except Exception as e:
        print(f"Error: {e}")
    await client.close()


async def cmd_shell(args):
    client, device = get_client()
    from intents import IntentRouter
    router = IntentRouter(client, default_arm_id=device)
    print(f"Box2Robot Shell (设备: {device or '未设置'})")
    print("输入自然语言或 action 名，quit 退出\n")
    while True:
        try:
            text = input("盒宝> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("quit", "exit", "q"):
            break
        result = await router.execute(text)
        pp(result)
    await client.close()


COMMANDS = {
    "login": cmd_login,
    "devices": cmd_devices,
    "status": cmd_status,
    "move": cmd_move,
    "home": cmd_home,
    "torque": cmd_torque,
    "record": cmd_record,
    "play": cmd_play,
    "say": cmd_say,
    "exec": cmd_exec,
    "shell": cmd_shell,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        print("命令列表:")
        for name in COMMANDS:
            print(f"  {name}")
        return

    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"未知命令: {cmd}")
        print(f"可用命令: {', '.join(COMMANDS.keys())}")
        return

    asyncio.run(COMMANDS[cmd](sys.argv[2:]))


if __name__ == "__main__":
    main()
