import torch
import torch.distributed as dist
from diffusers import MochiPipeline
from diffusers.utils import export_to_video

dist.init_process_group()

pipe = MochiPipeline.from_pretrained("genmo/mochi-1-preview", torch_dtype=torch.float16).to(f"cuda:{dist.get_rank()}")

# Enable memory savings
# pipe.enable_model_cpu_offload()
pipe.enable_vae_tiling()

from para_attn.context_parallel import init_context_parallel_mesh
from para_attn.context_parallel.diffusers_adapters import parallelize_pipe

parallelize_pipe(
    pipe,
    mesh=init_context_parallel_mesh(
        pipe.device.type,
        max_batch_dim_size=2,
        max_ring_dim_size=2,
    ),
)

torch._inductor.config.reorder_for_compute_comm_overlap = True
pipe.transformer = torch.compile(pipe.transformer, mode="max-autotune-no-cudagraphs")

prompt = "Close-up of a chameleon's eye, with its scaly skin changing color. Ultra high resolution 4k."
frames = pipe(prompt, num_frames=84).frames[0]

if dist.get_rank() == 0:
    export_to_video(frames, "mochi.mp4", fps=30)

dist.destroy_process_group()