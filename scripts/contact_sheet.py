#!/usr/bin/env python3
"""Lay out generated images in one labeled grid so you can compare takes and pick the best.

Labels show the file name and, when a generation sidecar (<image>.png.json) exists, its seed.
Images are grouped in rows by name prefix (e.g. ref_kaito_portrait_1..4 share a row).

Examples:
  python scripts/contact_sheet.py assets/generated/arc_forge/refs -o output/refs_sheet.png
  python scripts/contact_sheet.py assets/generated --filter s0 --cols 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

from pipeline_utils import IMAGE_EXTENSIONS, PipelineError, get_logger, load_pil_font, setup_logging

log = get_logger("sheet")


def collect(folder: Path, name_filter: str | None) -> list[Path]:
    if not folder.is_dir():
        raise PipelineError(f"Folder not found: {folder}")
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS and not p.name.startswith("."))
    if name_filter:
        files = [p for p in files if name_filter.lower() in p.name.lower()]
    if not files:
        raise PipelineError(f"No images in {folder}" + (f" matching '{name_filter}'" if name_filter else ""))
    return files


def group_key(path: Path) -> str:
    """'ref_kaito_portrait_3' -> 'ref_kaito_portrait'; files without a trailing number are their own group."""
    return re.sub(r"_\d+$", "", path.stem)


def build_sheet(files: list[Path], tile_h: int = 420, cols: int | None = None) -> Image.Image:
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(group_key(f), []).append(f)
    if cols is None:
        rows = [(name, members) for name, members in groups.items()]
    else:
        rows = [("", files[i:i + cols]) for i in range(0, len(files), cols)]
    font = load_pil_font(None, max(14, tile_h // 22))
    head_font = load_pil_font(None, max(16, tile_h // 16))
    label_h, head_h, pad = max(24, tile_h // 15), max(30, tile_h // 11), 10
    built: list[tuple[str, list[Image.Image]]] = []
    for name, row in rows:
        row_tiles = []
        for f in row:
            img = Image.open(f).convert("RGB")
            w = round(img.width * tile_h / img.height)
            tile = Image.new("RGB", (w, tile_h + label_h), (24, 24, 28))
            tile.paste(img.resize((w, tile_h), Image.Resampling.LANCZOS), (0, label_h))
            # Short per-tile label: take number (or full name when ungrouped) + seed.
            take = re.search(r"_(\d+)$", f.stem)
            label = f"#{take.group(1)}" if (name and take) else f.stem
            sidecar = f.with_suffix(f.suffix + ".json")
            if sidecar.exists():
                try:
                    label += f" · seed {json.loads(sidecar.read_text(encoding='utf-8'))['seed']}"
                except (ValueError, KeyError):
                    pass
            ImageDraw.Draw(tile).text((6, 3), label, fill=(255, 220, 90), font=font)
            row_tiles.append(tile)
        built.append((name, row_tiles))
    width = max(sum(t.width for t in r) + pad * (len(r) + 1) for _, r in built)
    height = sum(max(t.height for t in r) + (head_h if n else 0) for n, r in built) + pad * (len(built) + 1)
    sheet = Image.new("RGB", (width, height), (12, 12, 16))
    draw = ImageDraw.Draw(sheet)
    y = pad
    for name, r in built:
        if name:
            draw.text((pad, y + 2), name, fill=(120, 230, 255), font=head_font)
            y += head_h
        x = pad
        for t in r:
            sheet.paste(t, (x, y))
            x += t.width + pad
        y += max(t.height for t in r) + pad
    return sheet


def save_sheet(sheet: Image.Image, path: Path) -> Path:
    """Save, tolerating a locked target. On Windows an image viewer (e.g. Photos) that has the
    previous sheet open locks the file and the save fails with 'Invalid argument'/'Permission
    denied'. Then save under a timestamped name instead of crashing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        sheet.save(path)
        return path
    except OSError as exc:
        alt = path.with_name(f"{path.stem}_{time.strftime('%H%M%S')}{path.suffix}")
        sheet.save(alt)
        log.warning("Could not overwrite %s (%s); it is probably open in an image viewer. Saved %s instead.",
                    path.name, exc.strerror or exc, alt.name)
        return alt


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Make a labeled contact sheet from a folder of images.")
    p.add_argument("folder", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("output/contact_sheet.png"))
    p.add_argument("--filter", help="Only include files whose name contains this text.")
    p.add_argument("--cols", type=int, help="Fixed columns instead of one row per take group.")
    p.add_argument("--tile-height", type=int, default=420)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        sheet = build_sheet(collect(args.folder, args.filter), args.tile_height, args.cols)
        out = save_sheet(sheet, args.output)
        print(f"{out}  ({sheet.width}x{sheet.height})")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
