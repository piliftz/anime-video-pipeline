#!/usr/bin/env python3
"""Sort a cluttered output/ folder into one folder per episode segment. Moves files, never deletes.

Layout after tidying (the same layout episode_producer.py writes to from now on):

  output/
    ep01_cold_open/        ep01_cold_open_en.mp4, _en.srt, _en_credits.txt, _ja..., _keyframes.png
      wan_previews/        c001.mp4, c002.mp4, ...  (Wan motion previews)
    ep01_act_a_seq1/ ...
    voicevox_terms/        each VOICEVOX character's terms (shared)
    review/                everything else: contact sheets, frame strips, test stills, small copies, logs

Segment names come from the episode scripts in series/*/*.script.json, so a file is only filed under a
segment that really exists. Runs as a dry run unless --apply is given. Existing files are never
overwritten: a name clash is reported and the file stays where it is.

  python scripts/tidy_output.py            # show what would move
  python scripts/tidy_output.py --apply    # move
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from pipeline_utils import OUTPUT_DIR, PROJECT_ROOT, PipelineError, get_logger, load_json, setup_logging

log = get_logger("tidy")

KEEP_AT_ROOT = {".gitkeep"}
SHARED_DIRS = {"voicevox_terms", "review"}
PREVIEW_RE = re.compile(r"^[a-z]+\d*_?\d+\.mp4$", re.IGNORECASE)  # c001.mp4, a1_06.mp4: per-cut Wan previews


def segment_names(series_dir: Path = PROJECT_ROOT / "series") -> list[str]:
    """ep<NN>_<segment> for every segment of every episode script (longest first, for prefix matching)."""
    names = set()
    for script in series_dir.glob("*/*.script.json"):
        data = load_json(script)
        ep = data.get("episode")
        if ep is None:
            continue
        names.update(f"ep{int(ep):02d}_{seg['id']}" for seg in data.get("segments", []) if seg.get("id"))
    return sorted(names, key=len, reverse=True)


def plan_moves(out_dir: Path, segments: list[str]) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    for item in sorted(out_dir.iterdir()):
        if item.name in KEEP_AT_ROOT or item.name in SHARED_DIRS:
            continue
        if item.is_dir():
            if item.name in segments:  # an existing segment folder: file loose Wan previews into wan_previews/
                for f in sorted(item.iterdir()):
                    if f.is_file() and PREVIEW_RE.match(f.name):
                        moves.append((f, item / "wan_previews" / f.name))
            else:
                moves.append((item, out_dir / "review" / item.name))
            continue
        seg = next((s for s in segments if item.name.startswith(s + "_") or item.stem == s), None)
        moves.append((item, out_dir / (seg or "review") / item.name))
    return moves


def apply_moves(moves: list[tuple[Path, Path]], apply: bool) -> tuple[int, int]:
    moved = skipped = 0
    for src, dst in moves:
        rel_src, rel_dst = src.relative_to(OUTPUT_DIR.parent), dst.relative_to(OUTPUT_DIR.parent)
        if dst.exists():
            log.warning("skip   %s (already exists: %s)", rel_src, rel_dst)
            skipped += 1
            continue
        if not apply:
            print(f"  {rel_src}  ->  {rel_dst}")
            moved += 1
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1
        except OSError as exc:  # e.g. Windows: the file is open in a player or Photos
            log.warning("skip   %s (%s; close any program using it and run again)", rel_src, exc)
            skipped += 1
    return moved, skipped


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sort output/ into one folder per episode segment (moves, never deletes).")
    p.add_argument("--apply", action="store_true", help="Actually move the files (default: dry run).")
    args = p.parse_args(argv)
    setup_logging()
    try:
        if not OUTPUT_DIR.is_dir():
            raise PipelineError(f"No output folder at {OUTPUT_DIR}")
        moves = plan_moves(OUTPUT_DIR, segment_names())
        if not moves:
            print("output/ is already tidy.")
            return 0
        moved, skipped = apply_moves(moves, args.apply)
        if args.apply:
            print(f"Moved {moved} item(s), skipped {skipped}.")
        else:
            print(f"\nDry run: {moved} item(s) would move, {skipped} skipped. Run again with --apply to move them.")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
