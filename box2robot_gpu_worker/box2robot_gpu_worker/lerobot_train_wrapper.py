"""Worker-owned wrapper around ``lerobot.scripts.lerobot_train``.

The wrapper exists so we can register local 3rd-party LeRobot policies before the
CLI parses ``--policy.type=...``. This keeps LeRobot vendored as a third-party lib.
"""

from __future__ import annotations

from box2robot_gpu_worker.lerobot_extensions import ensure_lerobot_policy_extensions


def main() -> None:
    ensure_lerobot_policy_extensions()
    from lerobot.scripts.lerobot_train import main as lerobot_main

    lerobot_main()


if __name__ == "__main__":
    main()

