from __future__ import annotations

from typing import Any

import torch

from lerobot.processor import PolicyAction, PolicyProcessorPipeline
from lerobot.policies.diffusion.processor_diffusion import make_diffusion_pre_post_processors

from .configuration_diffusion_transformer import DiffusionTransformerConfig


def make_diffusion_transformer_pre_post_processors(
    config: DiffusionTransformerConfig,
    dataset_stats: dict[str, dict[str, torch.Tensor]] | None = None,
) -> tuple[
    PolicyProcessorPipeline[dict[str, Any], dict[str, Any]],
    PolicyProcessorPipeline[PolicyAction, PolicyAction],
]:
    """Reuse LeRobot's stock diffusion processors for the transformer variant."""

    return make_diffusion_pre_post_processors(config, dataset_stats=dataset_stats)

