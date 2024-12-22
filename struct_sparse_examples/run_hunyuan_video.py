import torch
from diffusers import HunyuanVideoPipeline, HunyuanVideoTransformer3DModel
from diffusers.utils import export_to_video

model_id = "tencent/HunyuanVideo"
transformer = HunyuanVideoTransformer3DModel.from_pretrained(
    model_id,
    subfolder="transformer",
    torch_dtype=torch.bfloat16,
    revision="refs/pr/18",
)
pipe = HunyuanVideoPipeline.from_pretrained(
    model_id,
    transformer=transformer,
    torch_dtype=torch.float16,
    revision="refs/pr/18",
).to("cuda")

pipe.vae.enable_tiling(
    # Make it runnable on GPUs with 48GB memory
    tile_sample_min_height=128,
    tile_sample_stride_height=96,
    tile_sample_min_width=128,
    tile_sample_stride_width=96,
    tile_sample_min_num_frames=32,
    tile_sample_stride_num_frames=24,
)

from para_attn.struct_sparse_attn.diffusers_adapters import sparsify_pipe

sparsify_pipe(pipe)

# torch._inductor.config.reorder_for_compute_comm_overlap = True
# pipe.transformer = torch.compile(pipe.transformer, mode="max-autotune-no-cudagraphs")

output = pipe(
    prompt="A cat walks on the grass, realistic",
    height=320,
    width=512,
    num_frames=61,
    num_inference_steps=30,
).frames[0]

print("Saving video to hunyuan_video.mp4")
export_to_video(output, "hunyuan_video.mp4", fps=15)
