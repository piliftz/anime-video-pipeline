#!/usr/bin/env python3
"""Animate a keyframe image into a short clip with Wan 2.2 TI2V-5B in local ComfyUI.

The graph mirrors ComfyUI's official template `video_wan2_2_5B_ti2v.json`, with the
keyframe connected as `start_image` (image-to-video). Frames come back as lossless
PNGs into a folder, which the orchestrator can use directly as a scene `clip`
(directory of frames) and which keeps the project's single-lossy-encode rule.
A small MP4 preview is also written so you can look at the result immediately.

Phase-0 test (Windows):
  .venv\\Scripts\\python scripts\\video_generator.py assets\\generated\\s02_hikari.png ^
      --prompt "anime style, the girl's scarf and hair sway in the wind, rain falls, slow camera push-in"

Options worth knowing:
  --length 121      frames (24 fps; 121 = 5 s). Must be 4n+1. Fewer = faster.
  --size 1280x704   Wan 2.2 5B native 720p. 960x544 is ~2x faster.
  --weight-dtype    fp8_e4m3fn (default; halves model memory) or default (fp16, slower on 8 GB).
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

from pipeline_utils import (
    BT709_TAGS,
    BT709_VF,
    OUTPUT_DIR,
    PROJECT_ROOT,
    PipelineError,
    ensure_dir,
    get_logger,
    require_binary,
    save_json,
    setup_logging,
)

log = get_logger("videogen")

WAN_FILES = {
    "unet": "wan2.2_ti2v_5B_fp16.safetensors",
    "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "vae": "wan2.2_vae.safetensors",
}
# Negative prompt used verbatim by the official Wan 2.2 template (Wan was trained with Chinese prompts).
WAN_NEGATIVE = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，"
                "丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
                "杂乱的背景，三条腿，背景人很多，倒着走")
VIDEO_DEFAULTS = {
    "width": 1280, "height": 704, "length": 121, "fps": 24,
    "steps": 20, "cfg": 5.0, "sampler": "uni_pc", "scheduler": "simple", "shift": 8.0,
    "weight_dtype": "fp8_e4m3fn",
}


def build_wan_graph(image_name: str, prompt: str, negative: str, seed: int, cfg: dict, prefix: str) -> dict:
    return {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": WAN_FILES["unet"], "weight_dtype": cfg["weight_dtype"]}},
        "shift": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["unet", 0], "shift": float(cfg["shift"])}},
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": WAN_FILES["clip"], "type": "wan", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": WAN_FILES["vae"]}},
        "image": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["clip", 0]}},
        "latent": {"class_type": "Wan22ImageToVideoLatent", "inputs": {
            "vae": ["vae", 0], "start_image": ["image", 0], "width": int(cfg["width"]),
            "height": int(cfg["height"]), "length": int(cfg["length"]), "batch_size": 1}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["shift", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["latent", 0],
            "seed": seed, "steps": int(cfg["steps"]), "cfg": float(cfg["cfg"]), "sampler_name": cfg["sampler"],
            "scheduler": cfg["scheduler"], "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": prefix}},
    }


def check_models(client) -> None:
    missing = []
    for node, field, key in (("UNETLoader", "unet_name", "unet"), ("CLIPLoader", "clip_name", "clip"),
                             ("VAELoader", "vae_name", "vae")):
        if WAN_FILES[key] not in client.combo_options(node, field):
            missing.append(WAN_FILES[key])
    if missing:
        raise PipelineError("ComfyUI can't see these Wan 2.2 files: " + ", ".join(missing)
                            + "\nRun:  python scripts/setup_models.py --model wan2.2-ti2v-5b   (then retry)")
    client.node_inputs("Wan22ImageToVideoLatent")  # raises a clear error if this ComfyUI is too old


def write_preview(frames_dir: Path, out: Path, fps: int) -> Path:
    require_binary("ffmpeg")
    cmd = ["ffmpeg", "-y", "-v", "error", "-framerate", str(fps), "-i", str(frames_dir / "frame_%05d.png"),
           "-vf", BT709_VF, "-c:v", "libx264", "-crf", "18", "-preset", "medium", *BT709_TAGS,
           "-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(f"ffmpeg preview failed: {proc.stderr.strip()[-800:]}")
    return out


def animate(image: Path, prompt: str, out_dir: Path, cfg: dict, seed: int = 42, negative: str = WAN_NEGATIVE,
            comfy_url: str | None = None, comfy_dir: str | None = None, keep_comfy: bool = False) -> dict:
    from comfy_client import DEFAULT_URL, ComfyServer

    image = Path(image).resolve()
    if not image.is_file():
        raise PipelineError(f"Keyframe not found: {image}")
    if (int(cfg["length"]) - 1) % 4:
        raise PipelineError(f"--length must be 4n+1 (e.g. 49, 81, 121); got {cfg['length']}.")
    if int(cfg["width"]) % 32 or int(cfg["height"]) % 32:
        raise PipelineError("Width and height must be multiples of 32 (e.g. 1280x704, 960x544).")
    with ComfyServer(comfy_url or DEFAULT_URL, comfy_dir, keep_running=keep_comfy) as client:
        check_models(client)
        if cfg["weight_dtype"] not in client.combo_options("UNETLoader", "weight_dtype"):
            raise PipelineError(f"weight_dtype '{cfg['weight_dtype']}' not supported by this ComfyUI.")
        for field, key in (("sampler_name", "sampler"), ("scheduler", "scheduler")):
            options = client.combo_options("KSampler", field)
            if cfg[key] not in options:
                raise PipelineError(f"{field} '{cfg[key]}' unknown to this ComfyUI. Options: {', '.join(options)}")
        digest = hashlib.sha1(image.read_bytes()).hexdigest()[:10]
        uploaded = client.upload_image(image, f"anime_pipeline_{image.stem}_{digest}.png")
        graph = build_wan_graph(uploaded, prompt, negative, seed, cfg, f"anime_pipeline/clips/{image.stem}")
        log.info("Animating %s: %dx%d, %d frames (%.1f s), %d steps, weights %s",
                 image.name, cfg["width"], cfg["height"], cfg["length"], (cfg["length"] - 1) / cfg["fps"],
                 cfg["steps"], cfg["weight_dtype"])
        log.info("This can take many minutes on an 8 GB laptop GPU; ComfyUI's own progress is in "
                 "temp/comfyui_server.log (or its console if you started it yourself).")
        t0 = time.time()
        outputs = client.wait(client.submit(graph), timeout=6 * 3600)
        gen_seconds = time.time() - t0
        frames = (outputs.get("save") or {}).get("images") or []
        if not frames:
            raise PipelineError("ComfyUI finished but returned no frames.")
        ensure_dir(out_dir)
        for old in out_dir.glob("frame_*.png"):
            old.unlink()
        for i, ref in enumerate(frames, start=1):
            (out_dir / f"frame_{i:05d}.png").write_bytes(client.download(ref))
    meta = {"keyframe": str(image), "prompt": prompt, "negative": negative, "seed": seed, **cfg,
            "frames": len(frames), "generation_seconds": round(gen_seconds, 1)}
    save_json(out_dir / "clip.json", meta)
    return meta


def _size(value: str) -> tuple[int, int]:
    try:
        w, h = value.lower().split("x")
        return int(w), int(h)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Size must look like 1280x704") from exc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Animate a keyframe with Wan 2.2 TI2V-5B (local ComfyUI).")
    p.add_argument("image", type=Path, help="Keyframe image (e.g. assets/generated/s02_hikari.png).")
    p.add_argument("--prompt", required=True, help="Describe the scene AND the motion/camera in plain English.")
    p.add_argument("--negative", default=WAN_NEGATIVE)
    p.add_argument("--out", type=Path, help="Frame folder (default assets/generated/clips/<image stem>).")
    p.add_argument("--size", type=_size, default=(VIDEO_DEFAULTS["width"], VIDEO_DEFAULTS["height"]))
    p.add_argument("--length", type=int, default=VIDEO_DEFAULTS["length"], help="Frames, 4n+1 (121 = 5 s).")
    p.add_argument("--steps", type=int, default=VIDEO_DEFAULTS["steps"])
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--weight-dtype", default=VIDEO_DEFAULTS["weight_dtype"],
                   choices=["default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"])
    p.add_argument("--comfy-url")
    p.add_argument("--comfy-dir")
    p.add_argument("--keep-comfy", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    cfg = {**VIDEO_DEFAULTS, "width": args.size[0], "height": args.size[1], "length": args.length,
           "steps": args.steps, "weight_dtype": args.weight_dtype}
    out_dir = args.out or (PROJECT_ROOT / "assets" / "generated" / "clips" / args.image.stem)
    try:
        t0 = time.time()
        meta = animate(args.image, args.prompt, out_dir, cfg, args.seed, args.negative,
                       args.comfy_url, args.comfy_dir, args.keep_comfy)
        preview = write_preview(out_dir, ensure_dir(OUTPUT_DIR) / f"{args.image.stem}_wan_preview.mp4", cfg["fps"])
        print(f"\nFrames : {out_dir}  ({meta['frames']} frames)")
        print(f"Preview: {preview}")
        print(f"Generation time: {meta['generation_seconds'] / 60:.1f} min "
              f"(total incl. ComfyUI startup/model load: {(time.time() - t0) / 60:.1f} min)")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
