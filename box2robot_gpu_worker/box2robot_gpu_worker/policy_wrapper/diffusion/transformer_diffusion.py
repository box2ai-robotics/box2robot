from __future__ import annotations

import math
from typing import Optional, Union

import torch
import torch.nn as nn


class SinusoidalPosEmb(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        device = x.device
        half_dim = self.dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, device=device) * -emb)
        emb = x[:, None] * emb[None, :]
        return torch.cat((emb.sin(), emb.cos()), dim=-1)


class ModuleAttrMixin(nn.Module):
    def __init__(self):
        super().__init__()
        self._dummy_variable = nn.Parameter(torch.zeros(()))

    @property
    def device(self):
        return next(iter(self.parameters())).device

    @property
    def dtype(self):
        return next(iter(self.parameters())).dtype


class TransformerForDiffusion(ModuleAttrMixin):
    """Sequence denoiser adapted from the user's transformer diffusion variant."""

    def __init__(
        self,
        input_dim: int,
        output_dim: int,
        horizon: int,
        n_obs_steps: int | None = None,
        cond_dim: int = 0,
        n_layer: int = 12,
        n_head: int = 12,
        n_emb: int = 768,
        p_drop_emb: float = 0.1,
        p_drop_attn: float = 0.1,
        causal_attn: bool = False,
        time_as_cond: bool = True,
        obs_as_cond: bool = False,
        n_cond_layers: int = 0,
    ) -> None:
        super().__init__()

        if n_obs_steps is None:
            n_obs_steps = horizon

        t_main = horizon
        t_cond = 1
        if not time_as_cond:
            t_main += 1
            t_cond -= 1
        obs_as_cond = cond_dim > 0
        if obs_as_cond:
            assert time_as_cond
            t_cond += n_obs_steps

        self.input_emb = nn.Linear(input_dim, n_emb)
        self.pos_emb = nn.Parameter(torch.zeros(1, t_main, n_emb))
        self.drop = nn.Dropout(p_drop_emb)

        self.time_emb = SinusoidalPosEmb(n_emb)
        self.cond_obs_emb = nn.Linear(cond_dim, n_emb) if obs_as_cond else None

        self.cond_pos_emb = None
        self.encoder = None
        self.decoder = None
        encoder_only = False
        if t_cond > 0:
            self.cond_pos_emb = nn.Parameter(torch.zeros(1, t_cond, n_emb))
            if n_cond_layers > 0:
                encoder_layer = nn.TransformerEncoderLayer(
                    d_model=n_emb,
                    nhead=n_head,
                    dim_feedforward=4 * n_emb,
                    dropout=p_drop_attn,
                    activation="gelu",
                    batch_first=True,
                    norm_first=True,
                )
                self.encoder = nn.TransformerEncoder(encoder_layer=encoder_layer, num_layers=n_cond_layers)
            else:
                self.encoder = nn.Sequential(
                    nn.Linear(n_emb, 4 * n_emb),
                    nn.Mish(),
                    nn.Linear(4 * n_emb, n_emb),
                )
            decoder_layer = nn.TransformerDecoderLayer(
                d_model=n_emb,
                nhead=n_head,
                dim_feedforward=4 * n_emb,
                dropout=p_drop_attn,
                activation="gelu",
                batch_first=True,
                norm_first=True,
            )
            self.decoder = nn.TransformerDecoder(decoder_layer=decoder_layer, num_layers=n_layer)
        else:
            encoder_only = True
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=n_emb,
                nhead=n_head,
                dim_feedforward=4 * n_emb,
                dropout=p_drop_attn,
                activation="gelu",
                batch_first=True,
                norm_first=True,
            )
            self.encoder = nn.TransformerEncoder(encoder_layer=encoder_layer, num_layers=n_layer)

        if causal_attn:
            size = t_main
            mask = (torch.triu(torch.ones(size, size)) == 1).transpose(0, 1)
            mask = mask.float().masked_fill(mask == 0, float("-inf")).masked_fill(mask == 1, 0.0)
            self.register_buffer("mask", mask)
            if time_as_cond and obs_as_cond:
                t, s = torch.meshgrid(torch.arange(t_main), torch.arange(t_cond), indexing="ij")
                memory_mask = t >= (s - 1)
                memory_mask = (
                    memory_mask.float()
                    .masked_fill(memory_mask == 0, float("-inf"))
                    .masked_fill(memory_mask == 1, 0.0)
                )
                self.register_buffer("memory_mask", memory_mask)
            else:
                self.memory_mask = None
        else:
            self.mask = None
            self.memory_mask = None

        self.ln_f = nn.LayerNorm(n_emb)
        self.head = nn.Linear(n_emb, output_dim)
        self.encoder_only = encoder_only

        self.apply(self._init_weights)

    def _init_weights(self, module: nn.Module) -> None:
        ignored = (
            nn.Dropout,
            SinusoidalPosEmb,
            nn.TransformerEncoderLayer,
            nn.TransformerDecoderLayer,
            nn.TransformerEncoder,
            nn.TransformerDecoder,
            nn.ModuleList,
            nn.Mish,
            nn.Sequential,
        )
        if isinstance(module, (nn.Linear, nn.Embedding)):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if isinstance(module, nn.Linear) and module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.MultiheadAttention):
            for name in ("in_proj_weight", "q_proj_weight", "k_proj_weight", "v_proj_weight"):
                weight = getattr(module, name, None)
                if weight is not None:
                    nn.init.normal_(weight, mean=0.0, std=0.02)
            for name in ("in_proj_bias", "bias_k", "bias_v"):
                bias = getattr(module, name, None)
                if bias is not None:
                    nn.init.zeros_(bias)
        elif isinstance(module, nn.LayerNorm):
            nn.init.zeros_(module.bias)
            nn.init.ones_(module.weight)
        elif isinstance(module, TransformerForDiffusion):
            nn.init.normal_(module.pos_emb, mean=0.0, std=0.02)
            if module.cond_obs_emb is not None and module.cond_pos_emb is not None:
                nn.init.normal_(module.cond_pos_emb, mean=0.0, std=0.02)
        elif isinstance(module, ignored):
            return
        else:
            raise RuntimeError(f"Unaccounted module {module}")

    def forward(
        self,
        sample: torch.Tensor,
        timestep: Union[torch.Tensor, float, int],
        global_cond: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> torch.Tensor:
        del kwargs
        timesteps = timestep
        if not torch.is_tensor(timesteps):
            timesteps = torch.tensor([timesteps], dtype=torch.long, device=sample.device)
        elif len(timesteps.shape) == 0:
            timesteps = timesteps[None].to(sample.device)
        timesteps = timesteps.expand(sample.shape[0])
        time_emb = self.time_emb(timesteps).unsqueeze(1)

        input_emb = self.input_emb(sample)
        if self.encoder_only:
            token_embeddings = torch.cat([time_emb, input_emb], dim=1)
            length = token_embeddings.shape[1]
            pos = self.pos_emb[:, :length, :]
            x = self.drop(token_embeddings + pos)
            x = self.encoder(src=x, mask=self.mask)
            x = x[:, 1:, :]
        else:
            cond_embeddings = time_emb
            if self.cond_obs_emb is not None:
                if global_cond is None:
                    raise ValueError("global_cond is required when obs_as_cond=True")
                cond_obs_emb = self.cond_obs_emb(global_cond)
                cond_embeddings = torch.cat([cond_embeddings, cond_obs_emb], dim=1)
            cond_length = cond_embeddings.shape[1]
            cond_pos = self.cond_pos_emb[:, :cond_length, :]
            x = self.drop(cond_embeddings + cond_pos)
            memory = self.encoder(x)

            token_embeddings = input_emb
            length = token_embeddings.shape[1]
            pos = self.pos_emb[:, :length, :]
            x = self.drop(token_embeddings + pos)
            x = self.decoder(
                tgt=x,
                memory=memory,
                tgt_mask=self.mask,
                memory_mask=self.memory_mask,
            )

        x = self.ln_f(x)
        return self.head(x)

