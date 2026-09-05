#!/usr/bin/env python3
"""
On-demand image generation via Diffusers (CLI).

The app launches this as a subprocess ONLY when you click "Generate". It loads
the model, creates one image, saves it, then EXITS — so VRAM is freed
automatically. No persistent server.

Supports any Diffusers text-to-image model (SD, SDXL, Flux, Z-Image, ...).
Z-Image models use the dedicated ZImagePipeline; everything else uses the
auto-detected pipeline.

Usage:
    python py/generate_image.py --prompt "a cat on a motorcycle" --output out.png
    python py/generate_image.py --model "stabilityai/stable-diffusion-xl-base-1.0" --prompt "..." --output out.png
"""

import argparse
import os
import sys


def build_pipeline(model):
    import torch

    try:
        from diffusers import ZImagePipeline
        has_zimage = True
    except ImportError:
        has_zimage = False

    # A local GGUF file → load via FluxPipeline.from_single_file + GGUF quantization
    # (low-VRAM; this is how you use the FHDR_ComfyUI-*.gguf you downloaded).
    if os.path.isfile(model) and model.lower().endswith(".gguf"):
        try:
            from diffusers import FluxPipeline, GGUFQuantizationConfig
        except ImportError as e:
            sys.stderr.write("This GGUF needs a newer diffusers. Run: pip install -U diffusers\n")
            raise e
        print("Loading Flux GGUF ...", file=sys.stderr, flush=True)
        pipe = FluxPipeline.from_single_file(
            model,
            quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
            torch_dtype=torch.bfloat16,
        )
    elif has_zimage and "z-image" in model.lower():
        print("Using ZImagePipeline ...", file=sys.stderr, flush=True)
        pipe = ZImagePipeline.from_pretrained(model, torch_dtype=torch.bfloat16)
    else:
        from diffusers import AutoPipelineForText2Image
        print("Using AutoPipelineForText2Image ...", file=sys.stderr, flush=True)
        pipe = AutoPipelineForText2Image.from_pretrained(model, torch_dtype=torch.bfloat16)

    # Fit 8 GB VRAM: offload stages to CPU RAM when needed.
    try:
        pipe.enable_model_cpu_offload()
    except Exception:
        pipe.to("cuda")
    return pipe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--model", default="Tongyi-MAI/Z-Image-Turbo")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--guidance", type=float, default=2.5)
    ap.add_argument("--negative_prompt", default="")
    args = ap.parse_args()

    try:
        pipe = build_pipeline(args.model)
    except ImportError as e:
        sys.stderr.write(
            "Missing dependency: %s\n"
            "Install Diffusers (Windows + NVIDIA):\n"
            "  pip install torch --index-url https://download.pytorch.org/whl/cu121\n"
            "  pip install diffusers accelerate transformers sentencepiece safetensors\n" % e
        )
        sys.exit(2)

    print("Generating ...", file=sys.stderr, flush=True)
    kwargs = {
        "prompt": args.prompt,
        "num_inference_steps": args.steps,
        "guidance_scale": args.guidance,
        "width": args.width,
        "height": args.height,
    }
    if args.negative_prompt:
        kwargs["negative_prompt"] = args.negative_prompt

    image = pipe(**kwargs).images[0]

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    image.save(args.output)
    print(args.output, flush=True)


if __name__ == "__main__":
    main()
