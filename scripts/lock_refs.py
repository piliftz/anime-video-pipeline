#!/usr/bin/env python3
"""Lock picked character reference takes into the series folder.

Reads a picks file (e.g. series/arc_forge/locked_refs.json), copies each picked generated image
into series/<show>/refs/ as a compact JPEG (small enough to commit), and records the take's seed,
size and exact prompt from its generation sidecar. Locked refs are the inputs for IP-Adapter
character consistency later on; they are NOT regenerated or overwritten by image_generator.py.

  python scripts/lock_refs.py series/arc_forge/locked_refs.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

from pipeline_utils import PipelineError, get_logger, load_json, resolve_path, save_json, setup_logging

log = get_logger("lockrefs")


def lock(picks_path: Path, max_height: int = 1536, quality: int = 92) -> list[Path]:
    picks_path = picks_path.resolve()
    data = load_json(picks_path)
    base = picks_path.parent
    src_dir = resolve_path(base, data.get("source_dir"))
    out_dir = resolve_path(base, data.get("refs_dir", "refs"))
    if src_dir is None or not src_dir.is_dir():
        raise PipelineError(f"source_dir not found: {src_dir}. Generate the reference takes first.")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for character, kinds in data.get("picks", {}).items():
        for kind, pick in kinds.items():
            src = src_dir / f"{pick['take']}.png"
            if not src.is_file():
                raise PipelineError(f"[{character}/{kind}] picked take not found: {src}")
            dest = out_dir / f"{character.lower()}_{kind}.jpg"
            img = Image.open(src).convert("RGB")
            if img.height > max_height:
                img = img.resize((round(img.width * max_height / img.height), max_height), Image.Resampling.LANCZOS)
            img.save(dest, quality=quality, optimize=True)
            sidecar = src.with_suffix(".png.json")
            meta = load_json(sidecar) if sidecar.is_file() else {}
            pick.update({
                "file": dest.relative_to(base).as_posix(),
                "seed": meta.get("seed"),
                "checkpoint": meta.get("checkpoint"),
                "size": [meta.get("width"), meta.get("height")],
                "prompt": meta.get("positive"),
            })
            log.info("locked %-6s %-9s <- %s (seed %s) -> %s", character, kind, src.name, meta.get("seed"), dest)
            written.append(dest)
    save_json(picks_path, data)
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Copy picked reference takes into the series refs folder.")
    p.add_argument("picks", type=Path, help="Picks JSON, e.g. series/arc_forge/locked_refs.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        written = lock(args.picks)
        print(f"{len(written)} reference image(s) locked. Commit them:  git add series && git commit -m \"Lock refs\"")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
