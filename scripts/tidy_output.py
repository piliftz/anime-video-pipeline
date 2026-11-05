#!/usr/bin/env python3
"""Sort a cluttered output/ folder into one folder per episode segment. Moves files, never deletes.

Layout after tidying (the same layout episode_producer.py writes to from now on):

  output/
    ep01_cold_open/        ep01_cold_open_en.mp4, _en.srt, _en_credits.txt, _ja..., _keyframes.png
      wan_previews/        c001.mp4, c002.mp4, ...  (Wan motion previews)
    ep01_act_a_seq1/ ...
    voicevox_terms/        each VOICEVOX character's terms (shared)
    review/                known throwaway files: frame strips, contact/review sheets, frame grabs, small copies,
                           GPU logs (deleted by --clean)
    other/                 anything unrecognized (never deleted: you decide)

With --clean it also DELETES files that are safe to lose (permanently: they don't go to the Recycle Bin):
  - everything in output/review/ (known throwaway patterns only: *_strip.png, *_sheet.png, *_review.png,
    *_frames.png, en_N.png / ja_N.png, gpu_log*.csv, *_small.mp4)
  - on a name clash (an old render at the top of output/ vs. one in its segment folder), the OLDER copy
  - *_small.mp4 copies anywhere
  - a draft render (_draft.mp4 + its .srt and _credits.txt) once the full render of the same segment and
    language exists and is newer
It never deletes full renders, their subtitles or credits, keyframe sheets, Wan previews or voicevox_terms.

Segment names come from the episode scripts in series/*/*.script.json, so a file is only filed under a
segment that really exists. Runs as a dry run unless --apply is given. Existing files are never
overwritten: a name clash is reported and the file stays where it is.

  python scripts/tidy_output.py            # show what would move
  python scripts/tidy_output.py --apply    # move
  python scripts/tidy_output.py --clean            # show what would move AND what would be deleted
  python scripts/tidy_output.py --clean --apply    # move, then delete
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
SHARED_DIRS = {"voicevox_terms", "review", "other"}
# Known throwaway review artifacts (frame strips, contact/review sheets, frame grabs, small copies, GPU logs).
# Only these go to review/ and get deleted by --clean; anything unrecognized goes to other/ and is KEPT.
THROWAWAY_RE = re.compile(r"(_small\.mp4|_strip\.png|_sheet\.png|_review\.png|_frames\.png)$"
                          r"|^(en|ja)_\d+\.png$|^gpu_log.*\.csv$", re.IGNORECASE)
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
        if item.name == "review" and item.is_dir():  # rescue anything not on the throwaway list
            moves += [(f, out_dir / "other" / f.name) for f in sorted(item.iterdir())
                      if not THROWAWAY_RE.search(f.name)]
            continue
        if item.name in KEEP_AT_ROOT or item.name in SHARED_DIRS:
            continue
        if item.is_dir():
            if item.name in segments:  # an existing segment folder: file loose Wan previews into wan_previews/
                for f in sorted(item.iterdir()):
                    if f.is_file() and PREVIEW_RE.match(f.name):
                        moves.append((f, item / "wan_previews" / f.name))
            else:
                moves.append((item, out_dir / "other" / item.name))
            continue
        seg = next((s for s in segments if item.name.startswith(s + "_") or item.stem == s), None)
        folder = seg or ("review" if THROWAWAY_RE.search(item.name) else "other")
        moves.append((item, out_dir / folder / item.name))
    return moves


def _final_files(out_dir: Path, moves: list[tuple[Path, Path]]) -> dict[Path, Path]:
    """Where every file in output/ ends up after the moves -> where it is now (lets a dry run plan deletions)."""
    moved = dict(moves)
    final: dict[Path, Path] = {}
    for f in out_dir.rglob("*"):
        if not f.is_file():
            continue
        dst = moved.get(f)
        if dst is None:  # inside a moved folder?
            for parent in f.parents:
                if parent in moved:
                    dst = moved[parent] / f.relative_to(parent)
                    break
        final[dst or f] = f
    return final


def plan_deletions(out_dir: Path, moves: list[tuple[Path, Path]], segments: list[str]) -> list[Path]:
    """Current paths of files that are safe to delete (see the module docstring)."""
    final = _final_files(out_dir, moves)
    review = out_dir / "review"
    doomed: list[Path] = []
    for dst, src in final.items():
        if (review in dst.parents and THROWAWAY_RE.search(dst.name)) or dst.name.endswith("_small.mp4"):
            doomed.append(src)
            continue
        m = re.match(r"^(.*)_draft(\.mp4|\.srt|_credits\.txt)$", dst.name)
        if m and dst.parent.name in segments:
            full = dst.with_name(f"{m.group(1)}.mp4")
            full_src = final.get(full)
            draft_video = final.get(dst.with_name(f"{m.group(1)}_draft.mp4"), src)
            if full_src is not None and full_src.stat().st_mtime >= draft_video.stat().st_mtime:
                doomed.append(src)
    return sorted(doomed)


def delete_files(paths: list[Path], apply: bool) -> tuple[int, int, int]:
    deleted = failed = size = 0
    for f in paths:
        rel = f.relative_to(OUTPUT_DIR.parent)
        n = f.stat().st_size
        if not apply:
            print(f"  delete  {rel}  ({n / 1e6:.1f} MB)")
            deleted += 1
            size += n
            continue
        try:
            f.unlink()
            log.info("deleted %s", rel)
            deleted += 1
            size += n
        except OSError as exc:
            log.warning("could not delete %s (%s; close any program using it and run again)", rel, exc)
            failed += 1
    return deleted, failed, size


def _remove_empty_dirs(out_dir: Path) -> None:
    for d in sorted((d for d in out_dir.rglob("*") if d.is_dir()), key=lambda d: len(d.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()


def apply_moves(moves: list[tuple[Path, Path]], apply: bool, newest_wins: bool = False) -> tuple[int, int]:
    """Move src -> dst. On a name clash: skip, or with newest_wins (clean mode) keep the newer file of the two
    and delete the older one (an old render left at the top of output/ vs. a fresh one in its segment folder)."""
    moved = skipped = 0
    for src, dst in moves:
        rel_src, rel_dst = src.relative_to(OUTPUT_DIR.parent), dst.relative_to(OUTPUT_DIR.parent)
        if dst.exists():
            if not (newest_wins and src.is_file() and dst.is_file()):
                log.warning("skip   %s (already exists: %s)", rel_src, rel_dst)
                skipped += 1
                continue
            older, newer = (src, dst) if src.stat().st_mtime <= dst.stat().st_mtime else (dst, src)
            if not apply:
                print(f"  delete  {older.relative_to(OUTPUT_DIR.parent)}  (older duplicate of "
                      f"{newer.relative_to(OUTPUT_DIR.parent)})")
                moved += 1
                continue
            try:
                if older is src:
                    src.unlink()
                    log.info("deleted %s (older duplicate of %s)", rel_src, rel_dst)
                else:
                    src.replace(dst)
                    log.info("replaced %s with newer %s", rel_dst, rel_src)
                moved += 1
            except OSError as exc:
                log.warning("skip   %s (%s; close any program using it and run again)", rel_src, exc)
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


def tidy_and_clean() -> str:
    """Move + clean for real (what episode_producer runs after every successful stage). Returns a summary line."""
    if not OUTPUT_DIR.is_dir():
        return "output/ does not exist yet"
    segments = segment_names()
    moved, skipped = apply_moves(plan_moves(OUTPUT_DIR, segments), apply=True, newest_wins=True)
    deleted, failed, size = delete_files(plan_deletions(OUTPUT_DIR, [], segments), apply=True)
    _remove_empty_dirs(OUTPUT_DIR)
    if not (moved or skipped or deleted or failed):
        return "output/ already tidy"
    return (f"moved {moved}, deleted {deleted} ({size / 1e6:.1f} MB)"
            + (f", {skipped + failed} skipped (in use or name clash)" if skipped or failed else ""))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sort output/ into one folder per episode segment; --clean deletes "
                                            "review files, small copies and superseded drafts.")
    p.add_argument("--apply", action="store_true", help="Actually move (and with --clean, delete). Default: dry run.")
    p.add_argument("--clean", action="store_true", help="Also delete files that are safe to lose (permanent).")
    args = p.parse_args(argv)
    setup_logging()
    try:
        if not OUTPUT_DIR.is_dir():
            raise PipelineError(f"No output folder at {OUTPUT_DIR}")
        segments = segment_names()
        moves = plan_moves(OUTPUT_DIR, segments)
        doomed = plan_deletions(OUTPUT_DIR, moves, segments) if args.clean else []
        if not moves and not doomed:
            print("output/ is already tidy.")
            return 0
        if args.apply:
            moved, skipped = apply_moves(moves, apply=True, newest_wins=args.clean)
            print(f"Moved {moved} item(s), skipped {skipped}.")
            if args.clean:  # re-plan on the real, moved layout
                deleted, failed, size = delete_files(plan_deletions(OUTPUT_DIR, [], segments), apply=True)
                _remove_empty_dirs(OUTPUT_DIR)
                tail = f", {failed} failed." if failed else "."
                print(f"Deleted {deleted} file(s), {size / 1e6:.1f} MB freed{tail}")
            return 0
        moved, skipped = apply_moves(moves, apply=False, newest_wins=args.clean)
        deleted, _, size = delete_files(doomed, apply=False)
        print(f"\nDry run: {moved} item(s) would move, {skipped} skipped"
              + (f", {deleted} file(s) would be DELETED ({size / 1e6:.1f} MB)" if args.clean else "")
              + ". Add --apply to do it.")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
