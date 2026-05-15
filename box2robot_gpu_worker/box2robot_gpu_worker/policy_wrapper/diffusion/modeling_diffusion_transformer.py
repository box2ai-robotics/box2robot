from __future__ import annotations

import einops
import torch
from torch import Tensor

from lerobot.policies.diffusion.modeling_diffusion import (
    DiffusionModel,
    DiffusionPolicy,
    DiffusionRgbEncoder,
    _make_noise_scheduler,
)
from lerobot.policies.pretrained import PreTrainedPolicy
from lerobot.utils.constants import OBS_ENV_STATE, OBS_IMAGES, OBS_STATE
from lerobot.utils.import_utils import require_package

from .configuration_diffusion_transformer import DiffusionTransformerConfig
from .transformer_diffusion import TransformerForDiffusion


class DiffusionTransformerPolicy(DiffusionPolicy):
    """LeRobot-compatible diffusion policy that swaps the CNN U-Net for a transformer denoiser."""

    config_class = DiffusionTransformerConfig
    name = "diffusion_transformer"

    def __init__(self, config: DiffusionTransformerConfig, **kwargs):
        del kwargs
        require_package("diffusers", extra="diffusion")
        PreTrainedPolicy.__init__(self, config)
        config.validate_features()
        self.config = config
        self._queues = None
        self.diffusion = DiffusionTransformerModel(config)
        self.reset()


class DiffusionTransformerModel(DiffusionModel):
    """Builtin diffusion model with a transformer sequence denoiser."""

    def __init__(self, config: DiffusionTransformerConfig):
        torch.nn.Module.__init__(self)
        self.config = config

        global_cond_dim = self.config.robot_state_feature.shape[0]
        if self.config.image_features:
            num_images = len(self.config.image_features)
            if self.config.use_separate_rgb_encoder_per_camera:
                encoders = [DiffusionRgbEncoder(config) for _ in range(num_images)]
                self.rgb_encoder = torch.nn.ModuleList(encoders)
                global_cond_dim += encoders[0].feature_dim * num_images
            else:
                self.rgb_encoder = DiffusionRgbEncoder(config)
                global_cond_dim += self.rgb_encoder.feature_dim * num_images
        if self.config.env_state_feature:
            global_cond_dim += self.config.env_state_feature.shape[0]

        self.unet = TransformerForDiffusion(
            input_dim=self.config.action_feature.shape[0],
            output_dim=self.config.action_feature.shape[0],
            horizon=self.config.horizon,
            n_obs_steps=self.config.n_obs_steps,
            cond_dim=global_cond_dim,
            n_layer=self.config.n_layers,
            n_head=self.config.n_heads,
            n_emb=self.config.n_emb,
            p_drop_emb=0.1,
            p_drop_attn=0.1,
            causal_attn=self.config.causal_attn,
            time_as_cond=True,
            obs_as_cond=True,
            n_cond_layers=0,
        )

        if config.compile_model:
            self.unet = torch.compile(self.unet, mode=config.compile_mode)

        self.noise_scheduler = _make_noise_scheduler(
            config.noise_scheduler_type,
            num_train_timesteps=config.num_train_timesteps,
            beta_start=config.beta_start,
            beta_end=config.beta_end,
            beta_schedule=config.beta_schedule,
            clip_sample=config.clip_sample,
            clip_sample_range=config.clip_sample_range,
            prediction_type=config.prediction_type,
        )

        if config.num_inference_steps is None:
            self.num_inference_steps = self.noise_scheduler.config.num_train_timesteps
        else:
            self.num_inference_steps = config.num_inference_steps

    def _prepare_global_conditioning(self, batch: dict[str, Tensor]) -> Tensor:
        """Return per-observation-step conditioning for the transformer denoiser."""

        batch_size, n_obs_steps = batch[OBS_STATE].shape[:2]
        global_cond_feats = [batch[OBS_STATE]]
        if self.config.image_features:
            if self.config.use_separate_rgb_encoder_per_camera:
                images_per_camera = einops.rearrange(batch[OBS_IMAGES], "b s n ... -> n (b s) ...")
                img_features_list = torch.cat(
                    [
                        encoder(images)
                        for encoder, images in zip(self.rgb_encoder, images_per_camera, strict=True)
                    ]
                )
                img_features = einops.rearrange(
                    img_features_list, "(n b s) ... -> b s (n ...)", b=batch_size, s=n_obs_steps
                )
            else:
                img_features = self.rgb_encoder(
                    einops.rearrange(batch[OBS_IMAGES], "b s n ... -> (b s n) ...")
                )
                img_features = einops.rearrange(
                    img_features, "(b s n) ... -> b s (n ...)", b=batch_size, s=n_obs_steps
                )
            global_cond_feats.append(img_features)

        if self.config.env_state_feature:
            global_cond_feats.append(batch[OBS_ENV_STATE])

        return torch.cat(global_cond_feats, dim=-1)

