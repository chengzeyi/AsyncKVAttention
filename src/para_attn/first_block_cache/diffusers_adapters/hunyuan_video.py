import functools
import unittest
from typing import Any, Dict, Optional, Union

import torch
from diffusers import DiffusionPipeline, HunyuanVideoTransformer3DModel
from diffusers.models.modeling_outputs import Transformer2DModelOutput
from diffusers.utils import logging, scale_lora_layers, unscale_lora_layers, USE_PEFT_BACKEND

from para_attn.first_block_cache import utils

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


def apply_cache_on_transformer(
    transformer: HunyuanVideoTransformer3DModel,
    *,
    residual_diff_threshold=0.035,
):
    cached_transformer_blocks = torch.nn.ModuleList(
        [
            utils.CachedTransformerBlocks(
                transformer.transformer_blocks + transformer.single_transformer_blocks,
                transformer=transformer,
                residual_diff_threshold=residual_diff_threshold,
            )
        ]
    )
    dummy_single_transformer_blocks = torch.nn.ModuleList()

    original_forward = transformer.forward

    @functools.wraps(transformer.__class__.forward)
    def new_forward(
        self,
        hidden_states: torch.Tensor,
        timestep: torch.LongTensor,
        encoder_hidden_states: torch.Tensor,
        encoder_attention_mask: torch.Tensor,
        pooled_projections: torch.Tensor,
        guidance: torch.Tensor = None,
        attention_kwargs: Optional[Dict[str, Any]] = None,
        return_dict: bool = True,
        **kwargs,
    ) -> Union[torch.Tensor, Dict[str, torch.Tensor]]:
        with unittest.mock.patch.object(
            self,
            "transformer_blocks",
            cached_transformer_blocks,
        ), unittest.mock.patch.object(
            self,
            "single_transformer_blocks",
            dummy_single_transformer_blocks,
        ):
            if getattr(self, "_is_parallelized", False):
                return original_forward(
                    hidden_states,
                    timestep,
                    encoder_hidden_states,
                    encoder_attention_mask,
                    pooled_projections,
                    guidance=guidance,
                    attention_kwargs=attention_kwargs,
                    return_dict=return_dict,
                    **kwargs,
                )
            else:
                if attention_kwargs is not None:
                    attention_kwargs = attention_kwargs.copy()
                    lora_scale = attention_kwargs.pop("scale", 1.0)
                else:
                    lora_scale = 1.0

                if USE_PEFT_BACKEND:
                    # weight the lora layers by setting `lora_scale` for each PEFT layer
                    scale_lora_layers(self, lora_scale)
                else:
                    if attention_kwargs is not None and attention_kwargs.get("scale", None) is not None:
                        logger.warning(
                            "Passing `scale` via `attention_kwargs` when not using the PEFT backend is ineffective."
                        )

                batch_size, num_channels, num_frames, height, width = hidden_states.shape
                p, p_t = self.config.patch_size, self.config.patch_size_t
                post_patch_num_frames = num_frames // p_t
                post_patch_height = height // p
                post_patch_width = width // p

                # 1. RoPE
                image_rotary_emb = self.rope(hidden_states)

                # 2. Conditional embeddings
                temb = self.time_text_embed(timestep, guidance, pooled_projections)
                hidden_states = self.x_embedder(hidden_states)
                encoder_hidden_states = self.context_embedder(encoder_hidden_states, timestep, encoder_attention_mask)

                encoder_hidden_states = encoder_hidden_states[:, encoder_attention_mask[0].bool()]

                hidden_states, encoder_hidden_states = self.call_transformer_blocks(
                    hidden_states, encoder_hidden_states, temb, guidance, image_rotary_emb
                )

                # 5. Output projection
                hidden_states = self.norm_out(hidden_states, temb)
                hidden_states = self.proj_out(hidden_states)

                hidden_states = hidden_states.reshape(
                    batch_size, post_patch_num_frames, post_patch_height, post_patch_width, -1, p_t, p, p
                )
                hidden_states = hidden_states.permute(0, 4, 1, 5, 2, 6, 3, 7)
                hidden_states = hidden_states.flatten(6, 7).flatten(4, 5).flatten(2, 3)

                if USE_PEFT_BACKEND:
                    # remove `lora_scale` from each PEFT layer
                    unscale_lora_layers(self, lora_scale)

                if not return_dict:
                    return (hidden_states,)

                return Transformer2DModelOutput(sample=hidden_states)

    transformer.forward = new_forward.__get__(transformer)

    def call_transformer_blocks(self, hidden_states, encoder_hidden_states, temb, guidance, image_rotary_emb):
        # 4. Transformer blocks
        if torch.is_grad_enabled() and self.gradient_checkpointing:

            def create_custom_forward(module, return_dict=None):
                def custom_forward(*inputs):
                    if return_dict is not None:
                        return module(*inputs, return_dict=return_dict)
                    else:
                        return module(*inputs)

                return custom_forward

            ckpt_kwargs: Dict[str, Any] = {"use_reentrant": False}

            for block in self.transformer_blocks:
                hidden_states, encoder_hidden_states = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(block),
                    hidden_states,
                    encoder_hidden_states,
                    temb,
                    None,
                    image_rotary_emb,
                    **ckpt_kwargs,
                )

            for block in self.single_transformer_blocks:
                hidden_states, encoder_hidden_states = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(block),
                    hidden_states,
                    encoder_hidden_states,
                    temb,
                    None,
                    image_rotary_emb,
                    **ckpt_kwargs,
                )

        else:
            for block in self.transformer_blocks:
                hidden_states, encoder_hidden_states = block(
                    hidden_states, encoder_hidden_states, temb, None, image_rotary_emb
                )

            for block in self.single_transformer_blocks:
                hidden_states, encoder_hidden_states = block(
                    hidden_states, encoder_hidden_states, temb, None, image_rotary_emb
                )

        return hidden_states, encoder_hidden_states

    transformer.call_transformer_blocks = call_transformer_blocks.__get__(transformer)

    return transformer


def apply_cache_on_pipe(
    pipe: DiffusionPipeline,
    *,
    shallow_patch: bool = False,
    **kwargs,
):
    original_call = pipe.__class__.__call__

    if not getattr(original_call, "_is_cached", False):

        @functools.wraps(original_call)
        def new_call(self, *args, **kwargs):
            with utils.cache_context(utils.create_cache_context()):
                return original_call(self, *args, **kwargs)

        pipe.__class__.__call__ = new_call

        new_call._is_cached = True

    if not shallow_patch:
        apply_cache_on_transformer(pipe.transformer, **kwargs)

    pipe._is_cached = True

    return pipe
