"""
Convert Box2Robot JSON trajectories -> LeRobot v3 dataset.

Supports:
  1. Single-arm: state=pos[t], action=pos[t+1]
  2. Dual-arm (leader-follower): state=follower_interp[t], action=leader_interp[t]
     Three streams (leader/follower/camera) aligned to uniform FPS via linear interpolation.
  3. Vision+State: adds camera images matched by timestamp ratio.

Usage:
    b2r-convert --input ./trajectories/ --output box2robot-pick-v1 --task "pick up object"
    b2r-convert --input ./trajectories/ --output box2robot-vision-v1 --task "pick" --images ./images/
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image


def _normalize_pos(pos: int, pos_max: int = 4095) -> float:
    """Servo encoder value -> [0, 1] float."""
    return pos / pos_max


def _detect_pos_max(frames: list[dict]) -> int:
    """Detect servo range from data. STS/HX=4095, SC=1023."""
    max_val = max(p["pos"] for f in frames for p in f["positions"])
    if max_val > 1023:
        return 4095   # STS3215 or HX (both 0-4095)
    return 1023       # SC09 (0-1023)


def load_trajectory(path: Path) -> dict:
    """Load a Box2Robot JSON trajectory file."""
    with open(path) as f:
        return json.load(f)


def _load_image(path: str, size: tuple = (480, 640)) -> Image.Image:
    """Load JPEG -> PIL Image (RGB, resized to HxW)."""
    img = Image.open(path).convert("RGB")
    if img.size != (size[1], size[0]):
        img = img.resize((size[1], size[0]), Image.BILINEAR)
    return img


def _match_images_to_frames(n_output_frames: int, image_dir: str) -> list:
    """Match camera JPEG files to output frames by position ratio.

    For N output frames and M camera images recorded in the same period:
    output frame i -> camera image floor(i * M / N)
    """
    if not os.path.isdir(image_dir):
        return [None] * n_output_frames

    img_files = sorted(
        [os.path.join(image_dir, f) for f in os.listdir(image_dir) if f.endswith('.jpg')]
    )
    if not img_files:
        return [None] * n_output_frames

    n_images = len(img_files)
    result = []
    for i in range(n_output_frames):
        img_idx = min(int(i * n_images / n_output_frames), n_images - 1)
        result.append(img_files[img_idx])
    return result


# ===== 时间对齐插值 =====

def _frames_to_arrays(frames: list[dict], servo_ids: list[int], pos_max: int
                      ) -> tuple[np.ndarray, np.ndarray]:
    """Extract timestamps and normalized position arrays from frames.

    Returns:
        timestamps: (N,) float array in milliseconds
        positions:  (N, n_servos) float array normalized [0,1]
    """
    timestamps = []
    positions = []
    for f in frames:
        timestamps.append(float(f.get("timestamp", 0)))
        pos_dict = {p["id"]: p["pos"] for p in f.get("positions", [])}
        row = [_normalize_pos(pos_dict.get(sid, 0), pos_max) for sid in servo_ids]
        positions.append(row)
    return np.array(timestamps), np.array(positions, dtype=np.float32)


def _interpolate_to_grid(timestamps: np.ndarray, positions: np.ndarray,
                         grid_times: np.ndarray) -> np.ndarray:
    """Linearly interpolate position data to a uniform time grid.

    Args:
        timestamps: (N,) source timestamps in ms
        positions:  (N, n_servos) source positions
        grid_times: (M,) target timestamps in ms

    Returns:
        (M, n_servos) interpolated positions
    """
    n_servos = positions.shape[1]
    result = np.zeros((len(grid_times), n_servos), dtype=np.float32)
    for j in range(n_servos):
        result[:, j] = np.interp(grid_times, timestamps, positions[:, j])
    return result


def _align_dual_streams(leader_frames: list[dict], follower_frames: list[dict],
                        servo_ids: list[int], pos_max: int, fps: int
                        ) -> tuple[np.ndarray, np.ndarray, int]:
    """Align leader and follower frame streams to a common uniform timeline.

    Returns:
        leader_aligned:   (T, n_servos) leader positions at uniform FPS
        follower_aligned: (T, n_servos) follower positions at uniform FPS
        n_frames: number of aligned frames
    """
    l_ts, l_pos = _frames_to_arrays(leader_frames, servo_ids, pos_max)
    f_ts, f_pos = _frames_to_arrays(follower_frames, servo_ids, pos_max)

    # Common time range: intersection of both streams
    t_start = max(l_ts[0], f_ts[0])
    t_end = min(l_ts[-1], f_ts[-1])

    if t_end <= t_start:
        # No overlap — fall back to follower-only
        return None, None, 0

    interval_ms = 1000.0 / fps
    n_frames = max(1, int((t_end - t_start) / interval_ms))
    grid = np.linspace(t_start, t_end, n_frames)

    leader_aligned = _interpolate_to_grid(l_ts, l_pos, grid)
    follower_aligned = _interpolate_to_grid(f_ts, f_pos, grid)

    return leader_aligned, follower_aligned, n_frames


def convert(
    input_path: Path,
    repo_id: str,
    task_description: str = "manipulation task",
    root: Path | None = None,
    fps: int = 20,
    images_dir: Path | None = None,
    image_size: tuple = (480, 640),
):
    """Convert Box2Robot trajectories to LeRobot dataset.

    Single-arm: state=pos[t], action=pos[t+1]
    Dual-arm:   state=follower[t], action=leader[t]  (aligned via interpolation)
    """
    sys.path.insert(0, str(Path(__file__).parent.parent / "lerobot" / "src"))
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    if input_path.is_file():
        traj_files = [input_path]
    else:
        traj_files = sorted(input_path.glob("*.json"))

    if not traj_files:
        print(f"No .json files found in {input_path}")
        return

    # Determine if vision mode
    use_vision = False
    if images_dir:
        for tf in traj_files:
            traj = load_trajectory(tf)
            traj_id = traj.get("id", tf.stem)
            img_subdir = images_dir / traj_id
            if img_subdir.is_dir() and any(img_subdir.glob("*.jpg")):
                use_vision = True
                break

    # Peek first file for servo config
    first_traj = load_trajectory(traj_files[0])
    first_frame = first_traj["frames"][0]
    servo_ids = sorted([p["id"] for p in first_frame["positions"]])
    n_servos = len(servo_ids)
    servo_names = [f"joint_{i}" for i in servo_ids]
    pos_max = _detect_pos_max(first_traj["frames"])

    print(f"Detected {n_servos} servos (IDs: {servo_ids}), range 0~{pos_max}")
    print(f"Vision mode: {'ON' if use_vision else 'OFF'}, target FPS: {fps}")
    print(f"Converting {len(traj_files)} trajectories -> {repo_id}")

    # Define features
    features = {
        "observation.state": {
            "dtype": "float32",
            "shape": (n_servos,),
            "names": servo_names,
        },
        "action": {
            "dtype": "float32",
            "shape": (n_servos,),
            "names": servo_names,
        },
    }
    if use_vision:
        features["observation.images.top"] = {
            "dtype": "image",
            "shape": (image_size[0], image_size[1], 3),
            "names": ["height", "width", "channels"],
        }

    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        fps=fps,
        features=features,
        root=root,
        robot_type="box2robot_arm",
        use_videos=False,
    )

    total_frames = 0
    skipped = 0

    for traj_file in traj_files:
        traj = load_trajectory(traj_file)
        all_frames = traj["frames"]
        if len(all_frames) < 2:
            print(f"  Skip {traj_file.name}: too few frames ({len(all_frames)})")
            skipped += 1
            continue

        traj_id = traj.get("id", traj_file.stem)
        leader_id = traj.get("leader_id", "")
        follower_ids = traj.get("follower_ids", [])
        file_pos_max = _detect_pos_max(all_frames)

        # ===== 最高优先: aligned_frames (Server 已按图像频率对齐) =====
        aligned = traj.get("aligned_frames", [])
        if aligned and len(aligned) >= 2 and aligned[0].get("positions"):
            n = len(aligned)
            # 检测是否有 leader_positions (主从) 还是纯单臂
            has_leader_pos = any(af.get("leader_positions") for af in aligned)
            is_single_arm = not has_leader_pos

            image_paths = [None] * n
            if use_vision:
                img_subdir = str(images_dir / traj_id) if images_dir else ""
                if os.path.isdir(img_subdir):
                    # aligned_frames 的 image_idx 直接对应排序后的图片文件
                    img_files = sorted(
                        [os.path.join(img_subdir, f) for f in os.listdir(img_subdir) if f.endswith('.jpg')]
                    )
                    for i, af in enumerate(aligned):
                        idx = af.get("image_idx", i)
                        if idx < len(img_files):
                            image_paths[i] = img_files[idx]
                else:
                    skipped += 1
                    print(f"  Skip {traj_file.name}: no images (vision mode)")
                    continue

            episode_frames = 0
            last_image = None
            for i, af in enumerate(aligned):
                f_positions = sorted(af.get("positions", []), key=lambda p: p["id"])
                state = np.array(
                    [_normalize_pos(p["pos"], file_pos_max) for p in f_positions],
                    dtype=np.float32,
                )

                if is_single_arm:
                    # 单臂+图像: state=pos[t], action=pos[t+1]
                    # 人拖到哪就是 action (下一帧位置 = 下一步该做什么)
                    if i < n - 1:
                        next_positions = sorted(
                            aligned[i + 1].get("positions", []),
                            key=lambda p: p["id"])
                        action = np.array(
                            [_normalize_pos(p["pos"], file_pos_max) for p in next_positions],
                            dtype=np.float32,
                        )
                    else:
                        action = state.copy()  # 最后一帧: 保持不动
                else:
                    # 主从: action = leader 位置 (已由 Server 对齐)
                    l_positions = af.get("leader_positions", f_positions)
                    l_positions = sorted(l_positions, key=lambda p: p["id"])
                    action = np.array(
                        [_normalize_pos(p["pos"], file_pos_max) for p in l_positions],
                        dtype=np.float32,
                    )

                frame_data = {
                    "observation.state": torch.from_numpy(state),
                    "action": torch.from_numpy(action),
                    "task": task_description,
                }
                if use_vision:
                    img_path = image_paths[i]
                    if img_path and os.path.isfile(img_path):
                        last_image = _load_image(img_path, image_size)
                    frame_data["observation.images.top"] = (
                        last_image if last_image is not None
                        else Image.new("RGB", (image_size[1], image_size[0]))
                    )

                dataset.add_frame(frame_data)
                episode_frames += 1

            if episode_frames > 0:
                dataset.save_episode()
                total_frames += episode_frames
                img_count = sum(1 for p in image_paths if p is not None)
                suffix = f" ({img_count} images)" if use_vision else ""
                mode_str = "ALIGNED-SINGLE" if is_single_arm else "ALIGNED-DUAL"
                print(f"  {traj_file.name}: {mode_str} {episode_frames} frames "
                      f"(1:1 image-bound){suffix}")
            continue  # 已处理, 跳过后续模式判断

        # ===== 判断录制模式 =====
        # 方式1 (新固件): Follower 帧自带 leader_positions → 完美同帧对齐
        follower_with_leader = [f for f in all_frames if f.get("leader_positions")]
        # 方式2 (旧固件): Leader/Follower 分开上报 → 需要插值对齐
        is_dual_inline = len(follower_with_leader) > 1
        is_dual_separate = False

        if not is_dual_inline:
            if leader_id and follower_ids:
                l_frames = [f for f in all_frames if f.get("device_id") == leader_id]
                f_frames = [f for f in all_frames if f.get("device_id") == follower_ids[0]]
                if l_frames and f_frames:
                    is_dual_separate = True
            elif any(f.get("role") == "follower" for f in all_frames):
                l_frames = [f for f in all_frames if f.get("role") == "leader"]
                f_frames = [f for f in all_frames if f.get("role") == "follower"]
                if l_frames and f_frames:
                    is_dual_separate = True

        if is_dual_inline:
            # ===== 新固件: Follower 帧自带 leader_positions (完美对齐, 零插值) =====
            frames = follower_with_leader
            n = len(frames)

            image_paths = [None] * n
            if use_vision:
                img_subdir = str(images_dir / traj_id) if images_dir else ""
                if traj.get("has_images") and os.path.isdir(img_subdir):
                    image_paths = _match_images_to_frames(n, img_subdir)
                else:
                    skipped += 1
                    print(f"  Skip {traj_file.name}: no images (vision mode)")
                    continue

            episode_frames = 0
            last_image = None
            for i, frame in enumerate(frames):
                # state = follower 实际位置
                f_positions = sorted(frame["positions"], key=lambda p: p["id"])
                state = np.array(
                    [_normalize_pos(p["pos"], file_pos_max) for p in f_positions],
                    dtype=np.float32,
                )
                # action = leader 命令位置 (同帧, 完美对齐)
                l_positions = sorted(frame["leader_positions"], key=lambda p: p["id"])
                action = np.array(
                    [_normalize_pos(p["pos"], file_pos_max) for p in l_positions],
                    dtype=np.float32,
                )

                frame_data = {
                    "observation.state": torch.from_numpy(state),
                    "action": torch.from_numpy(action),
                    "task": task_description,
                }
                if use_vision:
                    img_path = image_paths[i]
                    if img_path and os.path.isfile(img_path):
                        last_image = _load_image(img_path, image_size)
                    frame_data["observation.images.top"] = (
                        last_image if last_image is not None
                        else Image.new("RGB", (image_size[1], image_size[0]))
                    )

                dataset.add_frame(frame_data)
                episode_frames += 1

            if episode_frames > 0:
                dataset.save_episode()
                total_frames += episode_frames
                img_count = sum(1 for p in image_paths if p is not None)
                suffix = f" ({img_count} images)" if use_vision else ""
                print(f"  {traj_file.name}: DUAL-INLINE {episode_frames} frames (perfect align){suffix}")

        elif is_dual_separate:
            # ===== 旧固件: Leader/Follower 分开上报, 需要插值对齐 =====
            leader_aligned, follower_aligned, n = _align_dual_streams(
                l_frames, f_frames, servo_ids, file_pos_max, fps)

            if n < 2:
                print(f"  Skip {traj_file.name}: alignment failed (no time overlap)")
                skipped += 1
                continue

            image_paths = [None] * n
            if use_vision:
                img_subdir = str(images_dir / traj_id) if images_dir else ""
                if traj.get("has_images") and os.path.isdir(img_subdir):
                    image_paths = _match_images_to_frames(n, img_subdir)
                else:
                    skipped += 1
                    print(f"  Skip {traj_file.name}: no images (vision mode)")
                    continue

            episode_frames = 0
            last_image = None
            for i in range(n):
                state = follower_aligned[i]
                action = leader_aligned[i]
                frame_data = {
                    "observation.state": torch.from_numpy(state),
                    "action": torch.from_numpy(action),
                    "task": task_description,
                }
                if use_vision:
                    img_path = image_paths[i]
                    if img_path and os.path.isfile(img_path):
                        last_image = _load_image(img_path, image_size)
                    frame_data["observation.images.top"] = (
                        last_image if last_image is not None
                        else Image.new("RGB", (image_size[1], image_size[0]))
                    )
                dataset.add_frame(frame_data)
                episode_frames += 1

            if episode_frames > 0:
                dataset.save_episode()
                total_frames += episode_frames
                img_count = sum(1 for p in image_paths if p is not None)
                suffix = f" ({img_count} images)" if use_vision else ""
                print(f"  {traj_file.name}: DUAL-INTERP {episode_frames} frames "
                      f"(leader={len(l_frames)}, follower={len(f_frames)}){suffix}")

        else:
            # ===== 单臂模式: state=pos[t], action=pos[t+1] (原始逻辑) =====
            frames = all_frames
            image_paths = [None] * len(frames)
            if use_vision:
                img_subdir = str(images_dir / traj_id) if images_dir else ""
                if traj.get("has_images") and os.path.isdir(img_subdir):
                    image_paths = _match_images_to_frames(len(frames), img_subdir)
                else:
                    skipped += 1
                    print(f"  Skip {traj_file.name}: no images (vision mode)")
                    continue

            episode_frames = 0
            last_image = None
            for i, frame in enumerate(frames):
                positions = sorted(frame["positions"], key=lambda p: p["id"])
                state = np.array(
                    [_normalize_pos(p["pos"], file_pos_max) for p in positions],
                    dtype=np.float32,
                )
                if i < len(frames) - 1:
                    next_positions = sorted(frames[i + 1]["positions"], key=lambda p: p["id"])
                    action = np.array(
                        [_normalize_pos(p["pos"], file_pos_max) for p in next_positions],
                        dtype=np.float32,
                    )
                else:
                    action = state.copy()

                frame_data = {
                    "observation.state": torch.from_numpy(state),
                    "action": torch.from_numpy(action),
                    "task": task_description,
                }
                if use_vision:
                    img_path = image_paths[i]
                    if img_path and os.path.isfile(img_path):
                        last_image = _load_image(img_path, image_size)
                    frame_data["observation.images.top"] = (
                        last_image if last_image is not None
                        else Image.new("RGB", (image_size[1], image_size[0]))
                    )

                dataset.add_frame(frame_data)
                episode_frames += 1

            if episode_frames > 0:
                dataset.save_episode()
                total_frames += episode_frames
                img_count = sum(1 for p in image_paths if p is not None)
                suffix = f" ({img_count} images)" if use_vision else ""
                print(f"  {traj_file.name}: SINGLE {episode_frames} frames{suffix}")

    dataset.finalize()
    print(f"Done. {len(traj_files) - skipped} episodes, {total_frames} frames -> {dataset.root}")
    if skipped:
        print(f"  Skipped {skipped} trajectories")


def main():
    parser = argparse.ArgumentParser(description="Convert Box2Robot trajectories to LeRobot dataset")
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="JSON file or directory of JSON files")
    parser.add_argument("--output", "-o", type=str, required=True,
                        help="Dataset repo_id (e.g. box2robot-pick-v1)")
    parser.add_argument("--task", "-t", type=str, default="manipulation task",
                        help="Task description text")
    parser.add_argument("--root", type=Path, default=None,
                        help="Output directory (default: HF cache)")
    parser.add_argument("--fps", type=int, default=20,
                        help="Target FPS (default: 20)")
    parser.add_argument("--images", type=Path, default=None,
                        help="Image directory (e.g. ./images/)")
    parser.add_argument("--image-size", type=str, default="480x640",
                        help="Image size HxW (default: 480x640)")
    args = parser.parse_args()

    h, w = map(int, args.image_size.split("x"))
    convert(args.input, args.output, args.task, args.root, args.fps, args.images, (h, w))


if __name__ == "__main__":
    main()
