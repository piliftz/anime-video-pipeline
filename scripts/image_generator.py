#!/usr/bin/env python3
"""Generate scene images with a local ComfyUI from the storyboard's `generate` blocks.

For every scene that has a `generate` block, this builds an SDXL text-to-image
graph (optionally with a high-resolution second pass), sends it to ComfyUI's
HTTP API, and saves the PNG to the scene's `clip` path (default
assets/generated/<scene_id>.png). A JSON sidecar records the exact prompt, seed,
model and settings.

Re-running is incremental: a scene is regenerated only when its prompt/settings
changed (sidecar hash differs), its image is missing, or you pass --force.
Images you supplied yourself (no sidecar) are never overwritten without --force.

Examples:
  python scripts/image_generator.py my_story.json
  python scripts/image_generator.py my_story.json --scenes s03 --force
  python scripts/image_generator.py my_story.json --comfy-dir C:\\Users\\you\\comfyui-anime
  python scripts/image_generator.py --list-checkpoints
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import zlib
from pathlib import Path

from pipeline_utils import (
    PipelineError,
    deep_merge,
    ensure_dir,
    get_logger,
    load_json,
    resolve_path,
    save_json,
    setup_logging,
)

log = get_logger("imagegen")

# Defaults follow the Animagine XL 4.0 model card (CFG 5, 28 steps, Euler a, tag prompts).
IMAGE_GEN_DEFAULTS: dict = {
    "checkpoint": None,          # None = auto: first checkpoint containing "animagine", else the first one
    "output_dir": "assets/generated",
    "width": 1344,               # SDXL-native 16:9-ish bucket
    "height": 768,
    "steps": 28,
    "cfg": 5.0,
    "sampler": "euler_ancestral",
    "scheduler": "normal",
    "seed": 1000,                # base seed; each scene gets base + crc32(scene id) unless it sets its own
    "rating": "safe",
    "quality_tags": "masterpiece, high score, great score, absurdres",
    "negative": ("lowres, bad anatomy, bad hands, text, error, missing finger, extra digits, fewer digits, "
                 "cropped, worst quality, low quality, low score, bad score, average score, signature, "
                 "watermark, username, blurry"),
    "hires": {
        "enabled": True,         # second pass at higher resolution for sharp 1080p + camera zoom headroom
        "scale": 1.5,            # 1344x768 -> 2016x1152
        "denoise": 0.4,
        "steps": 20,
        "upscale_method": "lanczos",
    },
}
GENERATE_DEFAULTS: dict = {"prompt": "", "characters": [], "negative": "", "seed": None,
                           "width": None, "height": None, "rating": None}


# --------------------------------------------------------------------------- #
# Storyboard helpers (also used by the orchestrator; keep import-light)
# --------------------------------------------------------------------------- #
def image_gen_config(raw_storyboard: dict) -> dict:
    return deep_merge(IMAGE_GEN_DEFAULTS, raw_storyboard.get("image_generation"))


def default_generated_clip(raw_storyboard: dict, scene_id: str) -> str:
    out = image_gen_config(raw_storyboard)["output_dir"].rstrip("/\\")
    return f"{out}/{scene_id}.png"


def validate_generate(scene_id: str, gen: dict, characters: dict) -> list[str]:
    errors = []
    if not str(gen.get("prompt") or "").strip():
        errors.append(f"[{scene_id}] generate.prompt is empty.")
    for name in gen.get("characters") or []:
        if name not in characters:
            errors.append(f"[{scene_id}] generate.characters: '{name}' is not defined in top-level 'characters'.")
    for key in ("width", "height"):
        v = gen.get(key)
        if v is not None and (int(v) % 8 or int(v) < 256):
            errors.append(f"[{scene_id}] generate.{key} must be a multiple of 8 and >= 256 (got {v}).")
    return errors


# --------------------------------------------------------------------------- #
# Prompt + graph construction
# --------------------------------------------------------------------------- #
def _join_tags(*parts: str) -> str:
    seen, out = set(), []
    for part in parts:
        for tag in str(part or "").split(","):
            t = tag.strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    return ", ".join(out)


_COUNT_TAG = re.compile(r"^(\d\+?(girl|boy|other)s?|solo|multiple (girls|boys)|no humans)$", re.IGNORECASE)


def build_job(scene: dict, cfg: dict, characters: dict) -> dict:
    gen = deep_merge(GENERATE_DEFAULTS, scene.get("generate"))
    char_tags = [characters[n].get("tags", "") for n in gen["characters"]]
    # Animagine-style order: subject counts first, then character appearance, then the scene,
    # then rating and quality tags.
    scene_tags = [t.strip() for t in str(gen["prompt"]).split(",") if t.strip()]
    counts = ", ".join(t for t in scene_tags if _COUNT_TAG.match(t))
    rest = ", ".join(t for t in scene_tags if not _COUNT_TAG.match(t))
    positive = _join_tags(counts, *char_tags, rest, gen["rating"] or cfg["rating"], cfg["quality_tags"])
    negative = _join_tags(cfg["negative"], gen["negative"])
    seed = gen["seed"] if gen["seed"] is not None else int(cfg["seed"]) + zlib.crc32(scene["id"].encode())
    job = {
        "positive": positive, "negative": negative, "seed": int(seed) % (2**63),
        "width": int(gen["width"] or cfg["width"]), "height": int(gen["height"] or cfg["height"]),
        "steps": int(cfg["steps"]), "cfg": float(cfg["cfg"]),
        "sampler": cfg["sampler"], "scheduler": cfg["scheduler"],
        "hires": dict(cfg["hires"]), "checkpoint": cfg["checkpoint"],
    }
    job["hash"] = hashlib.sha1(json.dumps(job, sort_keys=True).encode()).hexdigest()[:16]
    return job


def build_graph(job: dict, checkpoint: str, prefix: str) -> dict:
    """SDXL txt2img in ComfyUI API format, plus an optional pixel-space hires pass."""
    g: dict = {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"text": job["positive"], "clip": ["ckpt", 1]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"text": job["negative"], "clip": ["ckpt", 1]}},
        "latent": {"class_type": "EmptyLatentImage",
                   "inputs": {"width": job["width"], "height": job["height"], "batch_size": 1}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["ckpt", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["latent", 0],
            "seed": job["seed"], "steps": job["steps"], "cfg": job["cfg"], "sampler_name": job["sampler"],
            "scheduler": job["scheduler"], "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["ckpt", 2]}},
    }
    final = ["decode", 0]
    hires = job["hires"]
    if hires.get("enabled") and float(hires.get("scale", 1)) > 1.0:
        g.update({
            "upscale": {"class_type": "ImageScaleBy", "inputs": {
                "image": ["decode", 0], "upscale_method": hires["upscale_method"], "scale_by": float(hires["scale"])}},
            "encode": {"class_type": "VAEEncode", "inputs": {"pixels": ["upscale", 0], "vae": ["ckpt", 2]}},
            "refine": {"class_type": "KSampler", "inputs": {
                "model": ["ckpt", 0], "positive": ["pos", 0], "negative": ["neg", 0], "latent_image": ["encode", 0],
                "seed": job["seed"], "steps": int(hires["steps"]), "cfg": job["cfg"], "sampler_name": job["sampler"],
                "scheduler": job["scheduler"], "denoise": float(hires["denoise"])}},
            "decode2": {"class_type": "VAEDecode", "inputs": {"samples": ["refine", 0], "vae": ["ckpt", 2]}},
        })
        final = ["decode2", 0]
    g["save"] = {"class_type": "SaveImage", "inputs": {"images": final, "filename_prefix": prefix}}
    return g


def pick_checkpoint(available: list[str], wanted: str | None) -> str:
    if not available:
        raise PipelineError("ComfyUI lists no checkpoints. Run: python scripts/setup_models.py --comfy-dir <ComfyUI>")
    if wanted:
        if wanted in available:
            return wanted
        matches = [c for c in available if wanted.lower() in c.lower()]
        if len(matches) == 1:
            return matches[0]
        raise PipelineError(f"Checkpoint '{wanted}' not found (or ambiguous). Available:\n  " + "\n  ".join(available))
    for c in available:
        if "animagine" in c.lower():
            return c
    log.warning("No Animagine checkpoint found; using '%s'. Prompts/settings are tuned for Animagine.", available[0])
    return available[0]


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #
def generate_images(storyboard_path: Path, scene_ids: list[str] | None = None, force: bool = False,
                    comfy_url: str | None = None, comfy_dir: str | None = None,
                    keep_comfy: bool = False) -> list[Path]:
    from comfy_client import DEFAULT_URL, ComfyServer

    storyboard_path = Path(storyboard_path).resolve()
    raw = load_json(storyboard_path)
    base_dir = storyboard_path.parent
    cfg = image_gen_config(raw)
    characters = raw.get("characters", {})

    todo: list[tuple[dict, dict, Path]] = []
    errors: list[str] = []
    for i, scene in enumerate(raw.get("scenes", []), start=1):
        scene = {**scene, "id": str(scene.get("id") or f"scene_{i:02d}")}
        if not scene.get("generate"):
            continue
        if scene_ids and scene["id"] not in scene_ids:
            continue
        scene_errors = validate_generate(scene["id"], scene["generate"], characters)
        if scene_errors:
            errors += scene_errors
            continue
        target = resolve_path(base_dir, scene.get("clip") or default_generated_clip(raw, scene["id"]))
        if target.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            errors.append(f"[{scene['id']}] has a 'generate' block, so its clip must be an image path (got {target}).")
            continue
        job = build_job(scene, cfg, characters)
        sidecar = target.with_suffix(target.suffix + ".json")
        if target.exists() and not force:
            if not sidecar.exists():
                log.info("keep     %-12s %s (your own image; --force to replace)", scene["id"], target.name)
                continue
            if load_json(sidecar).get("hash") == job["hash"]:
                log.info("up to date %-10s %s", scene["id"], target.name)
                continue
        todo.append((scene, job, target))
    if errors:
        raise PipelineError("Image generation config errors:\n  - " + "\n  - ".join(errors))
    if not todo:
        log.info("All generated images are up to date.")
        return []

    written: list[Path] = []
    with ComfyServer(comfy_url or DEFAULT_URL, comfy_dir, keep_running=keep_comfy) as client:
        checkpoint = pick_checkpoint(client.checkpoints(), cfg["checkpoint"])
        samplers = client.combo_options("KSampler", "sampler_name")
        schedulers = client.combo_options("KSampler", "scheduler")
        if cfg["sampler"] not in samplers:
            raise PipelineError(f"Sampler '{cfg['sampler']}' unknown to ComfyUI. Options: {', '.join(samplers)}")
        if cfg["scheduler"] not in schedulers:
            raise PipelineError(f"Scheduler '{cfg['scheduler']}' unknown. Options: {', '.join(schedulers)}")
        log.info("Generating %d image(s) with %s", len(todo), checkpoint)
        for n, (scene, job, target) in enumerate(todo, start=1):
            t0 = time.time()
            graph = build_graph(job, checkpoint, f"anime_pipeline/{scene['id']}")
            outputs = client.wait(client.submit(graph))
            images = (outputs.get("save") or {}).get("images") or []
            if not images:
                raise PipelineError(f"[{scene['id']}] ComfyUI finished but returned no image.")
            ensure_dir(target.parent)
            data = client.download(images[0])
            if target.suffix.lower() == ".png":
                target.write_bytes(data)
            else:
                import io

                from PIL import Image

                Image.open(io.BytesIO(data)).convert("RGB").save(target, quality=95)
            save_json(target.with_suffix(target.suffix + ".json"),
                      {**job, "checkpoint": checkpoint, "scene": scene["id"],
                       "seconds": round(time.time() - t0, 1)})
            log.info("[%d/%d] %-12s %5.1fs -> %s", n, len(todo), scene["id"], time.time() - t0, target)
            written.append(target)
    return written


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate storyboard scene images with a local ComfyUI.")
    p.add_argument("storyboard", type=Path, nargs="?", help="Storyboard JSON with 'generate' blocks.")
    p.add_argument("--scenes", help="Comma-separated scene ids to (re)generate.")
    p.add_argument("--force", action="store_true", help="Regenerate even if up to date / user-supplied.")
    p.add_argument("--comfy-url", help="ComfyUI URL (default $COMFYUI_URL or http://127.0.0.1:8189).")
    p.add_argument("--comfy-dir", help="ComfyUI folder, to auto-start it (default $COMFYUI_DIR).")
    p.add_argument("--keep-comfy", action="store_true", help="Leave an auto-started ComfyUI running.")
    p.add_argument("--list-checkpoints", action="store_true", help="Print the checkpoints ComfyUI can see.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        if args.list_checkpoints:
            from comfy_client import DEFAULT_URL, ComfyServer

            with ComfyServer(args.comfy_url or DEFAULT_URL, args.comfy_dir) as client:
                for c in client.checkpoints():
                    print(c)
            return 0
        if not args.storyboard:
            raise PipelineError("Give a storyboard path (or --list-checkpoints).")
        ids = [s.strip() for s in args.scenes.split(",")] if args.scenes else None
        written = generate_images(args.storyboard, ids, args.force, args.comfy_url, args.comfy_dir, args.keep_comfy)
        print(f"{len(written)} image(s) generated.")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
