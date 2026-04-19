"""
Simple MLP policy for testing the training pipeline.
No LeRobot dependency — pure PyTorch.

Input: servo state [n_servos] (normalized 0~1)
Output: action [n_servos] (normalized 0~1)

Training: supervised learning on trajectory data (state → next_state).
"""
import json
import time
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

logger = logging.getLogger("box2robot.mlp_policy")


class MLPPolicy(nn.Module):
    """Simple feedforward MLP: state → action"""

    def __init__(self, n_servos: int = 6, hidden_dim: int = 128, n_layers: int = 3):
        super().__init__()
        layers = []
        in_dim = n_servos
        for _ in range(n_layers):
            layers.extend([nn.Linear(in_dim, hidden_dim), nn.ReLU(), nn.Dropout(0.1)])
            in_dim = hidden_dim
        layers.append(nn.Linear(hidden_dim, n_servos))
        layers.append(nn.Sigmoid())  # Output in [0, 1]
        self.net = nn.Sequential(*layers)
        self.n_servos = n_servos

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)

    def predict(self, state: list[float]) -> list[float]:
        """Single inference: state list → action list"""
        with torch.no_grad():
            x = torch.tensor([state], dtype=torch.float32)
            if next(self.parameters()).is_cuda:
                x = x.cuda()
            y = self(x)
            return y.squeeze(0).cpu().tolist()


class TrajectoryDataset(Dataset):
    """Dataset from Box2Robot JSON trajectories."""

    def __init__(self, trajectories: list[dict], pos_max: int = 4095):
        self.states = []
        self.actions = []
        for traj in trajectories:
            frames = traj.get("frames", [])
            if len(frames) < 2:
                continue
            for i in range(len(frames) - 1):
                state = self._frame_to_state(frames[i], pos_max)
                action = self._frame_to_state(frames[i + 1], pos_max)
                if state is not None and action is not None:
                    self.states.append(state)
                    self.actions.append(action)

    def _frame_to_state(self, frame: dict, pos_max: int) -> Optional[list[float]]:
        positions = frame.get("positions", [])
        if not positions:
            return None
        sorted_pos = sorted(positions, key=lambda p: p.get("id", 0))
        return [p.get("pos", 0) / pos_max for p in sorted_pos]

    def __len__(self):
        return len(self.states)

    def __getitem__(self, idx):
        return (
            torch.tensor(self.states[idx], dtype=torch.float32),
            torch.tensor(self.actions[idx], dtype=torch.float32),
        )


def detect_pos_max(trajectories: list[dict]) -> int:
    """Detect servo range from trajectory data."""
    max_val = 0
    for traj in trajectories:
        for frame in traj.get("frames", []):
            for p in frame.get("positions", []):
                max_val = max(max_val, p.get("pos", 0))
    if max_val > 1023:
        return 4095   # STS3215 or HX (both 0-4095)
    return 1023       # SC09 (0-1023)


def detect_n_servos(trajectories: list[dict]) -> int:
    """Detect number of servos from first frame."""
    for traj in trajectories:
        frames = traj.get("frames", [])
        if frames:
            return len(frames[0].get("positions", []))
    return 6


def train_mlp(
    trajectories: list[dict],
    output_dir: str = "outputs/mlp_test",
    train_steps: int = 10000,
    batch_size: int = 64,
    lr: float = 1e-3,
    hidden_dim: int = 128,
    n_layers: int = 3,
    progress_callback=None,
    custom_params: dict = None,
):
    """Train MLP policy on trajectory data.

    Args:
        trajectories: List of Box2Robot trajectory dicts
        output_dir: Where to save the model
        train_steps: Total training steps
        batch_size: Batch size
        lr: Learning rate
        progress_callback: callable(step, total_steps, metrics_dict) for progress reporting
        custom_params: Additional parameters (lr, hidden_dim, etc.)

    Returns:
        dict with training results
    """
    # Apply custom params
    if custom_params:
        lr = float(custom_params.get("lr", lr))
        hidden_dim = int(custom_params.get("hidden_dim", hidden_dim))
        n_layers = int(custom_params.get("n_layers", n_layers))

    pos_max = detect_pos_max(trajectories)
    n_servos = detect_n_servos(trajectories)
    logger.info("Training MLP: %d servos, pos_max=%d, %d trajectories",
                n_servos, pos_max, len(trajectories))

    # Build dataset
    dataset = TrajectoryDataset(trajectories, pos_max)
    if len(dataset) == 0:
        raise ValueError("No training data found in trajectories")

    logger.info("Dataset: %d samples", len(dataset))
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, drop_last=True)

    # Build model
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = MLPPolicy(n_servos=n_servos, hidden_dim=hidden_dim, n_layers=n_layers)
    model = model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.MSELoss()

    # Training loop
    model.train()
    step = 0
    best_loss = float("inf")
    losses = []
    t0 = time.time()

    while step < train_steps:
        for states, actions in dataloader:
            if step >= train_steps:
                break
            states = states.to(device)
            actions = actions.to(device)

            pred = model(states)
            loss = criterion(pred, actions)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            loss_val = loss.item()
            losses.append(loss_val)
            step += 1

            if loss_val < best_loss:
                best_loss = loss_val

            # Report progress every 100 steps
            if step % 100 == 0 or step == train_steps:
                elapsed = time.time() - t0
                steps_per_sec = step / elapsed if elapsed > 0 else 0
                avg_loss = sum(losses[-100:]) / min(len(losses), 100)
                metrics = {
                    "loss": round(avg_loss, 6),
                    "best_loss": round(best_loss, 6),
                    "steps_per_sec": round(steps_per_sec, 1),
                    "elapsed_sec": round(elapsed, 1),
                }
                if progress_callback:
                    progress_callback(step, train_steps, metrics)
                if step % 1000 == 0:
                    logger.info("Step %d/%d loss=%.6f best=%.6f speed=%.1f steps/s",
                                step, train_steps, avg_loss, best_loss, steps_per_sec)

    # Save model
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Save PyTorch model
    torch.save(model.state_dict(), out_path / "model.pth")

    # Save config
    config = {
        "model_type": "mlp",
        "n_servos": n_servos,
        "hidden_dim": hidden_dim,
        "n_layers": n_layers,
        "pos_max": pos_max,
        "train_steps": train_steps,
        "batch_size": batch_size,
        "lr": lr,
        "best_loss": best_loss,
        "total_samples": len(dataset),
        "training_time_sec": round(time.time() - t0, 1),
    }
    with open(out_path / "config.json", "w") as f:
        json.dump(config, f, indent=2)

    logger.info("Model saved to %s (best_loss=%.6f)", out_path, best_loss)
    return config


def load_mlp_model(checkpoint_dir: str, device: str = "cpu") -> MLPPolicy:
    """Load a trained MLP model from checkpoint directory."""
    ckpt = Path(checkpoint_dir)
    with open(ckpt / "config.json") as f:
        config = json.load(f)

    model = MLPPolicy(
        n_servos=config["n_servos"],
        hidden_dim=config["hidden_dim"],
        n_layers=config["n_layers"],
    )
    model.load_state_dict(torch.load(ckpt / "model.pth", map_location=device, weights_only=True))
    model.eval()
    if device == "cuda" and torch.cuda.is_available():
        model = model.cuda()
    return model
