"""Local LeRobot extensions bundled with the Box2Robot worker.

These imports register custom policy configs with LeRobot's ChoiceRegistry without
modifying the vendored LeRobot source tree.
"""

from __future__ import annotations

import importlib
from threading import Lock

_IMPORT_LOCK = Lock()
_IMPORTED = False


def ensure_lerobot_policy_extensions() -> None:
    """Import local policy extensions exactly once.

    LeRobot discovers 3rd-party policies through config-class registration side effects.
    We keep those imports behind a helper so training and inference can both enable the
    same local extensions before touching LeRobot's factories.
    """

    global _IMPORTED
    if _IMPORTED:
        return
    with _IMPORT_LOCK:
        if _IMPORTED:
            return
        importlib.import_module("box2robot_gpu_worker.policy_wrapper.diffusion")
        _IMPORTED = True
