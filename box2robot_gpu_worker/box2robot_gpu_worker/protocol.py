"""Worker ↔ Server 通信协议常量（v1.0 冻结）— Worker 侧镜像

权威来源在 box2robot_server/box2robot-server/core/worker_protocol.py。
本文件必须与之保持一致，每次升版前对照检查。

详见 box2robot 项目根的 claude_md/worker_server_protocol.md。
"""

API_VERSION = "1.0"


WORKER_STATUS_TRANSITIONS: dict = {
    "pending":     {"downloading", "training", "completed", "failed", "cancelled"},
    "downloading": {"pending", "training", "interrupted", "completed", "failed", "cancelled"},
    "training":    {"paused", "interrupted", "completed", "failed", "cancelled"},
    "paused":      {"training", "completed", "cancelled"},
    "interrupted": {"training", "downloading", "completed", "failed", "cancelled"},
    "deploying":   {"completed"},
    "completed":   set(),
    "failed":      set(),
    "cancelled":   set(),
}

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


ERROR_CODES = frozenset({
    "OOM",
    "CUDA_ERROR",
    "DATASET_CORRUPT",
    "DATASET_DOWNLOAD_FAIL",
    "IMAGE_DOWNLOAD_FAIL",
    "CHECKPOINT_LOAD_FAIL",
    "DISK_FULL",
    "MODEL_NOT_FOUND",
    "INFERENCE_TIMEOUT",
    "ARM_DISCONNECTED",
    "USER_CANCELLED",
    "UNKNOWN",
})


STOP_REASONS = frozenset({
    "user_cancel",
    "arm_offline",
    "gpu_offline",
    "completed",
})


HEARTBEAT_INTERVAL_S = 10
POLL_JOB_INTERVAL_S = 5
CONFIG_REFRESH_INTERVAL_S = 30
UPGRADE_CHECK_INTERVAL_S = 60
CHECK_INFERENCE_INTERVAL_S = 5
