"""
ChunkOptimizer — 自适应分块执行优化器 (WiFi 远程推理)

基于 FAST-ACT 论文的跳帧策略，适配 Box2Robot WiFi 远程控制系统。
在不修改 LeRobot 代码的前提下，通过分析 chunk 预测一致性，
减少推理次数并提高执行频率。

执行模式:
  original — LeRobot 默认: 每步推理 select_action, 逐条发指令 (~5Hz)
  fixed    — 固定全 chunk 执行: 推理一次执行 chunk_size 步 (~1Hz 推理, 20Hz 执行)
  adaptive — 自适应跳帧: 根据预测一致性动态决定执行步数 (1-5Hz 推理, 20Hz 执行)
  overlap  — 滑动窗口重叠: 每次执行半个 chunk, 重叠部分做 temporal ensemble (~2Hz 推理, 20Hz 执行)
"""
import numpy as np
import logging
from typing import Optional

logger = logging.getLogger("box2robot.chunk_optimizer")


class ChunkOptimizer:
    """Adaptive chunk execution for WiFi-remote ACT inference.

    Usage:
        optimizer = ChunkOptimizer(chunk_size=20, strategy='adaptive')
        ...
        raw_chunk = model.predict_action_chunk(obs)  # (1, chunk_size, n_servos)
        chunk_np = unnormalize(raw_chunk)             # (chunk_size, n_servos)
        n_exec, actions = optimizer.feed_chunk(chunk_np)
        send_batch(actions)  # actions shape: (n_exec, n_servos)
    """

    def __init__(
        self,
        chunk_size: int = 20,
        strategy: str = "fixed",
        n_servos: int = 6,
        # adaptive 参数 (对应论文 FAST-ACT)
        certainty_threshold: float = 0.15,  # 论文 COT — 越小越容易跳帧
        safety_warmup: int = 3,             # 论文 mcet — 前 N 步不跳
        min_execute: int = 3,               # 最少执行步数
        max_skip: int = 15,                 # 论文 MCOD — 最大跳跃步数
        # fixed 参数
        fixed_exec_steps: int = 0,          # 0 = 全 chunk, >0 = 只执行前 N 步
        # overlap 参数
        overlap_ratio: float = 0.5,         # 重叠比例 (0.5 = 半步重叠)
        ensemble_decay: float = 0.01,       # temporal ensemble 衰减系数 k
    ):
        self.chunk_size = chunk_size
        self.strategy = strategy
        self.n_servos = n_servos
        self.certainty_threshold = certainty_threshold
        self.safety_warmup = safety_warmup
        self.min_execute = min_execute
        self.max_skip = min(max_skip, chunk_size - 1)
        self.fixed_exec_steps = fixed_exec_steps if fixed_exec_steps > 0 else chunk_size
        self.overlap_ratio = overlap_ratio
        self.ensemble_decay = ensemble_decay

        # 状态
        self.global_step = 0
        self.chunk_history: list[tuple[int, np.ndarray]] = []  # (step, chunk)
        self._prev_tail: Optional[np.ndarray] = None  # overlap 模式缓存上一个 chunk 尾部

    def reset(self):
        """Reset optimizer state for new episode."""
        self.global_step = 0
        self.chunk_history.clear()
        self._prev_tail = None

    def feed_chunk(self, actions: np.ndarray) -> tuple[int, np.ndarray]:
        """Process a new chunk from the model and decide how many steps to execute.

        Args:
            actions: shape (chunk_size, n_servos), unnormalized positions [0, pos_max]

        Returns:
            (n_execute, batch_actions) where batch_actions shape is (n_execute, n_servos)
        """
        assert actions.ndim == 2 and actions.shape[0] >= 1

        if self.strategy == "fixed":
            result = self._strategy_fixed(actions)
        elif self.strategy == "adaptive":
            result = self._strategy_adaptive(actions)
        elif self.strategy == "overlap":
            result = self._strategy_overlap(actions)
        else:
            raise ValueError(f"Unknown strategy: {self.strategy}")

        n_exec, batch = result
        self.chunk_history.append((self.global_step, actions.copy()))
        # 保留最近 5 个 chunk 的历史
        if len(self.chunk_history) > 5:
            self.chunk_history.pop(0)
        self.global_step += n_exec

        return n_exec, batch

    # ===== 策略实现 =====

    def _strategy_fixed(self, actions: np.ndarray) -> tuple[int, np.ndarray]:
        """固定执行 N 步 (默认全 chunk) — 最简单, 最高吞吐."""
        n = min(len(actions), self.fixed_exec_steps)
        return n, actions[:n]

    def _strategy_adaptive(self, actions: np.ndarray) -> tuple[int, np.ndarray]:
        """自适应跳帧 — 基于 FAST-ACT detect_skip_timestep 思想.

        分析当前 chunk 与历史 chunk 在重叠区域的预测一致性:
        - 一致 (低 uncertainty) → 多执行, 省推理
        - 发散 (高 uncertainty) → 少执行, 快修正
        """
        skip_count = self._compute_skip_count(actions)
        n_exec = max(self.min_execute, skip_count + 1)
        n_exec = min(n_exec, len(actions))

        logger.debug("adaptive: skip=%d, n_exec=%d, step=%d",
                     skip_count, n_exec, self.global_step)
        return n_exec, actions[:n_exec]

    def _strategy_overlap(self, actions: np.ndarray) -> tuple[int, np.ndarray]:
        """滑动窗口半步重叠 — 平滑过渡, 无突变.

        每次执行 chunk * overlap_ratio 步, 重叠部分与上一个 chunk 的
        尾部做 temporal ensemble (指数加权平均).
        """
        n_exec = max(1, int(len(actions) * self.overlap_ratio))

        if self._prev_tail is not None and len(self._prev_tail) > 0:
            # 重叠区域: prev_tail 和 actions 的前 overlap_len 步
            overlap_len = min(len(self._prev_tail), n_exec)
            blended = actions[:n_exec].copy()
            for i in range(overlap_len):
                # 权重: 越靠后越信任新 chunk
                w_new = (i + 1) / (overlap_len + 1)
                w_old = 1.0 - w_new
                blended[i] = w_old * self._prev_tail[i] + w_new * actions[i]
            result = blended
        else:
            result = actions[:n_exec].copy()

        # 缓存当前 chunk 的尾部 (未执行部分) 供下一轮 ensemble
        self._prev_tail = actions[n_exec:].copy() if n_exec < len(actions) else None

        return n_exec, result

    # ===== 跳帧分析 (核心算法, 仿 FAST-ACT detect_skip_timestep) =====

    def _compute_skip_count(self, new_chunk: np.ndarray) -> int:
        """Analyze prediction consistency between new chunk and history.

        Principle (from FAST-ACT paper):
        - Multiple overlapping chunks predict the same future timestep
        - If these predictions agree (low std), that step is "certain"
        - Certain steps can be executed without re-querying the model

        Returns: number of steps that can be safely skipped (0 = no skip)
        """
        # Warmup: don't skip
        if self.global_step < self.safety_warmup:
            return 0

        if not self.chunk_history:
            return 0

        # Compare with the most recent previous chunk
        prev_step, prev_chunk = self.chunk_history[-1]
        # How many steps have passed since prev chunk was predicted
        steps_elapsed = self.global_step - prev_step

        if steps_elapsed >= len(prev_chunk):
            return 0  # No overlap

        # Overlap: prev_chunk[steps_elapsed:] should predict the same as new_chunk[0:]
        overlap_len = min(len(prev_chunk) - steps_elapsed, len(new_chunk), self.max_skip + 1)

        skip_count = 0
        for i in range(overlap_len):
            prev_action = prev_chunk[steps_elapsed + i]
            new_action = new_chunk[i]

            # Compute per-servo difference, then sum → uncertainty
            diff = np.abs(prev_action - new_action)
            uncertainty = np.sum(diff)

            if uncertainty <= self.certainty_threshold:
                skip_count += 1
            else:
                break

        return min(skip_count, self.max_skip)

    # ===== 工具方法 =====

    def get_stats(self) -> dict:
        """Return optimizer statistics for logging."""
        return {
            "strategy": self.strategy,
            "global_step": self.global_step,
            "history_len": len(self.chunk_history),
            "chunk_size": self.chunk_size,
        }
