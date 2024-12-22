import functools
from typing import Any, Dict, Union

import torch
from diffusers import DiffusionPipeline, HunyuanVideoTransformer3DModel
from diffusers.models.modeling_outputs import Transformer2DModelOutput

from para_attn.para_attn_interface import SparseKVAttnMode, StructSparseAttnMode


def sparsify_transformer(transformer: HunyuanVideoTransformer3DModel):
    @functools.wraps(transformer.__class__.forward)
    def new_forward(
        self,
        hidden_states: torch.Tensor,
        timestep: torch.LongTensor,
        encoder_hidden_states: torch.Tensor,
        encoder_attention_mask: torch.Tensor,
        pooled_projections: torch.Tensor,
        guidance: torch.Tensor = None,
        return_dict: bool = True,
    ) -> Union[torch.Tensor, Dict[str, torch.Tensor]]:
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

        # 3. Attention mask preparation
        latent_sequence_length = hidden_states.shape[1]
        latent_attention_mask = torch.ones(
            batch_size, 1, latent_sequence_length, device=hidden_states.device, dtype=torch.bool
        )  # [B, 1, N]
        attention_mask = torch.cat(
            [latent_attention_mask, encoder_attention_mask.unsqueeze(1).to(torch.bool)], dim=-1
        )  # [B, 1, N + M]

        attention_mask = attention_mask[:1, ..., :1, :]

        sparse_mask = torch.zeros(post_patch_num_frames, post_patch_num_frames, dtype=torch.bool)
        for i, mask_row in enumerate(sparse_mask):
            mask_row[
                max(0, i - (post_patch_num_frames + 1) // 2) : min(
                    post_patch_num_frames, i + (post_patch_num_frames + 1) // 2
                )
            ] = True
        with StructSparseAttnMode(
            sparse_mask=sparse_mask,
            sparse_range_query=(
                0,
                post_patch_num_frames * post_patch_height * post_patch_width,
            ),
        ), SparseKVAttnMode():
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
                        attention_mask,
                        image_rotary_emb,
                        **ckpt_kwargs,
                    )

                for block in self.single_transformer_blocks:
                    hidden_states, encoder_hidden_states = torch.utils.checkpoint.checkpoint(
                        create_custom_forward(block),
                        hidden_states,
                        encoder_hidden_states,
                        temb,
                        attention_mask,
                        image_rotary_emb,
                        **ckpt_kwargs,
                    )

            else:
                for block in self.transformer_blocks:
                    hidden_states, encoder_hidden_states = block(
                        hidden_states, encoder_hidden_states, temb, attention_mask, image_rotary_emb
                    )

                for block in self.single_transformer_blocks:
                    hidden_states, encoder_hidden_states = block(
                        hidden_states, encoder_hidden_states, temb, attention_mask, image_rotary_emb
                    )

        # 5. Output projection
        hidden_states = self.norm_out(hidden_states, temb)
        hidden_states = self.proj_out(hidden_states)

        hidden_states = hidden_states.reshape(
            batch_size, post_patch_num_frames, post_patch_height, post_patch_width, -1, p_t, p, p
        )
        hidden_states = hidden_states.permute(0, 4, 1, 5, 2, 6, 3, 7)
        hidden_states = hidden_states.flatten(6, 7).flatten(4, 5).flatten(2, 3)

        if not return_dict:
            return (hidden_states,)

        return Transformer2DModelOutput(sample=hidden_states)

    new_forward = new_forward.__get__(transformer)
    transformer.forward = new_forward

    return transformer


def sparsify_pipe(
    pipe: DiffusionPipeline,
    *,
    shallow_patch: bool = False,
    **kwargs,
):
    if not shallow_patch:
        sparsify_transformer(pipe.transformer, **kwargs)

    return pipe
