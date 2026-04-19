"""
数据集验证工具 — 检查录制数据 → LeRobot 转换 → 推理管线的完整性

功能:
  1. inspect  — 检查原始 JSON 轨迹: leader/follower 帧分布, ID 对齐
  2. replay   — 用 LeRobot 数据集的 action 序列直接控制机械臂 (绕过模型)
  3. compare  — 对比原始轨迹 vs LeRobot 数据集, 检查转换准确性

用法:
  python -m box2robot.dataset_verify inspect --traj cache/ds_xxx/dataset/traj_0000.json
  python -m box2robot.dataset_verify replay --dataset datasets/box2robot-xxx --server https://robot.box2ai.com --device B2R-XXX
  python -m box2robot.dataset_verify compare --traj cache/ds_xxx/dataset/ --dataset datasets/box2robot-xxx/
"""
import argparse
import json
import time
import sys
import logging
from pathlib import Path
from collections import Counter

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("b2r.verify")


def cmd_inspect(args):
    """检查原始 JSON 轨迹的 leader/follower 帧分布"""
    traj_path = Path(args.traj)
    files = sorted(traj_path.glob("*.json")) if traj_path.is_dir() else [traj_path]

    for f in files:
        with open(f) as fh:
            traj = json.load(fh)

        frames = traj.get("frames", [])
        leader_id = traj.get("leader_id", "?")
        follower_ids = traj.get("follower_ids", [])
        traj_id = traj.get("id", f.stem)

        print(f"\n{'='*60}")
        print(f"轨迹: {traj_id}  ({f.name})")
        print(f"  leader: {leader_id}")
        print(f"  followers: {follower_ids}")
        print(f"  总帧数: {len(frames)}")

        # 按 device_id/role 分类
        by_device = Counter()
        by_role = Counter()
        servo_ids_by_device = {}
        for frame in frames:
            did = frame.get("device_id", "unknown")
            role = frame.get("role", "unknown")
            by_device[did] += 1
            by_role[role] += 1
            if did not in servo_ids_by_device:
                ids = sorted([p["id"] for p in frame.get("positions", [])])
                servo_ids_by_device[did] = ids

        print(f"\n  按设备分布:")
        for did, count in by_device.most_common():
            ids = servo_ids_by_device.get(did, [])
            print(f"    {did}: {count} 帧, servo IDs = {ids}")

        print(f"\n  按角色分布:")
        for role, count in by_role.most_common():
            print(f"    {role}: {count} 帧")

        # 检查 leader 和 follower 帧的位置差异
        leader_frames = [f for f in frames if f.get("device_id") == leader_id]
        follower_frames = [f for f in frames
                          if f.get("device_id") != leader_id and f.get("device_id") != "unknown"]
        if leader_frames and follower_frames:
            # 取前 5 帧对比
            print(f"\n  Leader vs Follower 前5帧位置对比:")
            for i in range(min(5, len(leader_frames), len(follower_frames))):
                l_pos = {p["id"]: p["pos"] for p in leader_frames[i]["positions"]}
                f_pos = {p["id"]: p["pos"] for p in follower_frames[i]["positions"]}
                common_ids = sorted(set(l_pos.keys()) & set(f_pos.keys()))
                diffs = [abs(l_pos[sid] - f_pos[sid]) for sid in common_ids]
                avg_diff = np.mean(diffs) if diffs else 0
                print(f"    帧{i}: avg_diff={avg_diff:.1f}, "
                      f"leader={[l_pos[sid] for sid in common_ids[:3]]}... "
                      f"follower={[f_pos[sid] for sid in common_ids[:3]]}...")
        else:
            print(f"\n  ⚠️  单设备录制 (无 leader/follower 对比)")

        # 关键警告
        if len(by_device) > 1:
            print(f"\n  ⚠️  警告: 混合帧! convert.py 不区分 device_id/role,")
            print(f"     leader 和 follower 帧会被交替混入训练数据!")
            print(f"     这会导致: state=[leader_pos] → action=[follower_pos] 的错误映射")


def cmd_replay(args):
    """用 LeRobot 数据集的 action 直接控制机械臂 (绕过模型推理)"""
    import httpx

    sys.path.insert(0, str(Path(__file__).parent.parent / "lerobot" / "src"))
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    ds_path = Path(args.dataset)
    dataset = LeRobotDataset(repo_id=ds_path.name, root=ds_path)
    pos_max = args.pos_max
    fps = dataset.fps

    logger.info("Dataset: %s, episodes=%d, frames=%d, fps=%d",
                ds_path.name, dataset.num_episodes, dataset.num_frames, fps)

    client = httpx.Client(base_url=args.server, timeout=10)
    device_id = args.device

    # 检查设备在线
    try:
        r = client.get(f"/api/device/{device_id}/servos")
        servos = r.json().get("servos", [])
        servo_ids = sorted([s["id"] for s in servos])
        logger.info("机械臂在线: %s, servos=%s", device_id, servo_ids)
    except Exception as e:
        logger.error("机械臂不在线: %s", e)
        return

    episode = args.episode
    if episode >= dataset.num_episodes:
        logger.error("Episode %d 不存在 (共 %d 个)", episode, dataset.num_episodes)
        return

    # 获取该 episode 的帧范围
    from_idx = dataset.episode_data_index["from"][episode].item()
    to_idx = dataset.episode_data_index["to"][episode].item()
    n_frames = to_idx - from_idx

    logger.info("回放 Episode %d: %d 帧 @ %d Hz", episode, n_frames, fps)
    logger.info("数据集 state/action 范围 (归一化 0~1):")

    # 预览前几帧
    for i in range(min(3, n_frames)):
        sample = dataset[from_idx + i]
        state = sample["observation.state"].numpy()
        action = sample["action"].numpy()
        logger.info("  帧%d: state=%s  action=%s", i,
                    [f"{v:.3f}" for v in state[:3]], [f"{v:.3f}" for v in action[:3]])

    # 开启力矩
    client.post(f"/api/device/{device_id}/command", json={"torque": True})
    logger.info("力矩已开启, 开始回放...")

    interval = 1.0 / fps
    try:
        for i in range(n_frames):
            t0 = time.perf_counter()

            sample = dataset[from_idx + i]
            action = sample["action"].numpy()  # (n_servos,) [0, 1]
            positions = [int(max(0, min(pos_max, a * pos_max))) for a in action]

            cmds = [{"id": servo_ids[j], "position": positions[j], "speed": 0}
                    for j in range(min(len(positions), len(servo_ids)))]
            client.post(f"/api/device/{device_id}/command", json={"commands": cmds})

            elapsed = time.perf_counter() - t0
            sleep_time = max(0, interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

            actual_hz = 1.0 / max(time.perf_counter() - t0, 0.001)
            print(f"\r  帧 {i+1}/{n_frames}  {actual_hz:.0f}Hz  pos={positions[:3]}...  ", end="", flush=True)

    except KeyboardInterrupt:
        print("\n停止")

    # 释放力矩
    client.post(f"/api/device/{device_id}/command", json={"torque": False})
    print(f"\n回放完成, 力矩已释放")


def cmd_compare(args):
    """对比原始 JSON 轨迹 vs LeRobot 数据集, 检查转换准确性"""
    sys.path.insert(0, str(Path(__file__).parent.parent / "lerobot" / "src"))
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    traj_dir = Path(args.traj)
    ds_path = Path(args.dataset)
    dataset = LeRobotDataset(repo_id=ds_path.name, root=ds_path)

    traj_files = sorted(traj_dir.glob("*.json"))
    logger.info("原始轨迹: %d 个文件", len(traj_files))
    logger.info("LeRobot 数据集: %d episodes, %d frames", dataset.num_episodes, dataset.num_frames)

    for ep in range(min(len(traj_files), dataset.num_episodes)):
        with open(traj_files[ep]) as f:
            traj = json.load(f)

        frames = traj.get("frames", [])
        from_idx = dataset.episode_data_index["from"][ep].item()
        to_idx = dataset.episode_data_index["to"][ep].item()

        # 检查原始帧中的设备混合
        devices = set(f.get("device_id", "") for f in frames)
        roles = set(f.get("role", "") for f in frames)

        print(f"\n--- Episode {ep}: {traj_files[ep].name} ---")
        print(f"  原始: {len(frames)} 帧, devices={devices}, roles={roles}")
        print(f"  LeRobot: {to_idx - from_idx} 帧")

        if len(devices) > 1:
            print(f"  ⚠️  混合帧问题! leader+follower 帧交替出现")
            # 展示具体的混合模式
            pattern = "".join("L" if f.get("role") == "leader" else "F" for f in frames[:20])
            print(f"  前20帧 role 模式: {pattern}")

        # 对比前几帧的 state
        pos_max = 4095
        max_val = max(p["pos"] for f in frames for p in f.get("positions", [{"pos":0}]))
        if max_val <= 1023:
            pos_max = 1023
        elif max_val <= 1000:
            pos_max = 1000

        print(f"  pos_max={pos_max}")
        mismatches = 0
        for i in range(min(5, len(frames), to_idx - from_idx)):
            orig_positions = sorted(frames[i]["positions"], key=lambda p: p["id"])
            orig_state = [p["pos"] / pos_max for p in orig_positions]

            ds_sample = dataset[from_idx + i]
            ds_state = ds_sample["observation.state"].numpy().tolist()

            diff = sum(abs(a - b) for a, b in zip(orig_state, ds_state))
            match = "✓" if diff < 0.01 else "✗"
            if diff >= 0.01:
                mismatches += 1
            role = frames[i].get("role", "?")
            did = frames[i].get("device_id", "?")[-12:]
            print(f"    帧{i} [{match}] role={role} dev=...{did}  "
                  f"orig={[f'{v:.3f}' for v in orig_state[:3]]}  "
                  f"ds={[f'{v:.3f}' for v in ds_state[:3]]}  diff={diff:.4f}")

        if mismatches > 0:
            print(f"  ❌ {mismatches}/5 帧不匹配!")


def main():
    parser = argparse.ArgumentParser(description="Box2Robot 数据集验证工具")
    sub = parser.add_subparsers(dest="command")

    p_inspect = sub.add_parser("inspect", help="检查原始 JSON 轨迹")
    p_inspect.add_argument("--traj", "-t", type=str, required=True, help="轨迹 JSON 文件或目录")

    p_replay = sub.add_parser("replay", help="用数据集 action 直接控制机械臂")
    p_replay.add_argument("--dataset", "-d", type=str, required=True, help="LeRobot 数据集路径")
    p_replay.add_argument("--server", "-s", type=str, default="https://robot.box2ai.com")
    p_replay.add_argument("--device", type=str, required=True, help="机械臂 device_id")
    p_replay.add_argument("--episode", "-e", type=int, default=0, help="Episode 编号")
    p_replay.add_argument("--pos-max", type=int, default=4095)

    p_compare = sub.add_parser("compare", help="对比原始轨迹 vs LeRobot 数据集")
    p_compare.add_argument("--traj", "-t", type=str, required=True, help="原始轨迹 JSON 目录")
    p_compare.add_argument("--dataset", "-d", type=str, required=True, help="LeRobot 数据集路径")

    args = parser.parse_args()
    if args.command == "inspect":
        cmd_inspect(args)
    elif args.command == "replay":
        cmd_replay(args)
    elif args.command == "compare":
        cmd_compare(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
