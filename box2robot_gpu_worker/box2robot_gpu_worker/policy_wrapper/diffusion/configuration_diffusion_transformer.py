from __future__ import annotations

from dataclasses import dataclass

from lerobot.configs import PreTrainedConfig
from lerobot.policies.diffusion.configuration_diffusion import DiffusionConfig


@PreTrainedConfig.register_subclass("diffusion_transformer")
@dataclass
class DiffusionTransformerConfig(DiffusionConfig):
    """LeRobot-compatible diffusion config with a transformer denoiser backend."""

    use_transformer: bool = True
    n_layers: int = 4
    n_heads: int = 8
    n_emb: int = 256
    causal_attn: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.use_transformer = True

