import functools

import torch
from diffusers import DiffusionPipeline, MochiTransformer3DModel

from para_attn.para_attn_interface import CubicAttnMode


def cubify_transformer(transformer: MochiTransformer3DModel, *, num_temporal_chunks=None, num_spatial_chunks=None):
    original_forward = transformer.forward

    @functools.wraps(transformer.__class__.forward)
    def new_forward(
        self,
        hidden_states: torch.Tensor,
        encoder_hidden_states: torch.Tensor,
        *args,
        **kwargs,
    ):
        batch_size, num_channels, num_frames, height, width = hidden_states.shape
        p = self.config.patch_size

        post_patch_height = height // p
        post_patch_width = width // p

        with CubicAttnMode(
            grid=(
                num_frames if num_temporal_chunks is None else num_temporal_chunks,
                post_patch_height if num_spatial_chunks is None else num_spatial_chunks,
            ),
            structure_range=(0, num_frames * post_patch_height * post_patch_width),
        ):
            output = original_forward(
                hidden_states,
                encoder_hidden_states,
                *args,
                **kwargs,
            )

        return output

    new_forward = new_forward.__get__(transformer)
    transformer.forward = new_forward

    original_time_embed_forward = transformer.time_embed.forward

    @functools.wraps(transformer.time_embed.__class__.forward)
    def new_time_embed_forward(
        self,
        timestep: torch.LongTensor,
        encoder_hidden_states: torch.Tensor,
        encoder_attention_mask: torch.Tensor,
        *args,
        **kwargs,
    ):
        with CubicAttnMode.disable():
            output = original_time_embed_forward(
                timestep, encoder_hidden_states, encoder_attention_mask, *args, **kwargs
            )
        return output

    new_time_embed_forward = new_time_embed_forward.__get__(transformer.time_embed)
    transformer.time_embed.forward = new_time_embed_forward

    return transformer


def cubify_pipe(
    pipe: DiffusionPipeline, *, shallow_patch: bool = False, num_temporal_chunks=None, num_spatial_chunks=None
):
    if not shallow_patch:
        cubify_transformer(
            pipe.transformer, num_temporal_chunks=num_temporal_chunks, num_spatial_chunks=num_spatial_chunks
        )

    return pipe