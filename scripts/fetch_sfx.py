#!/usr/bin/env python3
"""Find CC0 sound effects on Freesound, audition candidates, and register the keeper.

Needs a free Freesound API key: create one at https://freesound.org/apiv2/apply and set it once:
  setx FREESOUND_API_KEY "your-key"        (Windows; open a new terminal afterwards)

Only sounds licensed CC0 (public domain, any use, no attribution) are searched AND re-checked on
the result, so nothing with attribution or non-commercial terms slips in. Files are Freesound's
high-quality preview MP3s (about 128 kbps), fine for SFX; original files need a full OAuth login.

  python scripts/fetch_sfx.py search "chain clink" --name chain_clink      # downloads 3 candidates
  python scripts/fetch_sfx.py keep chain_clink 2                           # register candidate #2
  python scripts/fetch_sfx.py script series/arc_forge/ep01_pilot.script.json   # search every SFX the script needs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline_utils import (
    ASSETS_DIR,
    TEMP_DIR,
    PipelineError,
    ensure_dir,
    get_logger,
    load_json,
    save_json,
    setup_logging,
)

log = get_logger("sfx")

API = os.environ.get("FREESOUND_API_BASE", "https://freesound.org/apiv2")
CANDIDATES = TEMP_DIR / "sfx_candidates"
SFX_DIR = ASSETS_DIR / "audio" / "sfx"
CC0_URL_MARK = "publicdomain/zero"


def _key() -> str:
    key = os.environ.get("FREESOUND_API_KEY", "").strip()
    if not key:
        raise PipelineError("Set FREESOUND_API_KEY first (free key: https://freesound.org/apiv2/apply), e.g.\n"
                            '  setx FREESOUND_API_KEY "your-key"   then open a new terminal.')
    return key


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "anime-video-pipeline"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        hint = " (check FREESOUND_API_KEY)" if exc.code in (401, 403) else ""
        raise PipelineError(f"Freesound returned HTTP {exc.code}{hint}.") from exc
    except urllib.error.URLError as exc:
        raise PipelineError(f"Cannot reach Freesound ({exc.reason}).") from exc


def search(query: str, max_duration: float = 8.0, count: int = 3) -> list[dict]:
    params = {
        "query": query,
        "filter": f'license:"Creative Commons 0" duration:[0.1 TO {max_duration}]',
        "fields": "id,name,username,license,duration,previews,url",
        "sort": "score",
        "page_size": str(max(count * 3, 10)),
        "token": _key(),
    }
    data = json.loads(_get(f"{API}/search/text/?{urllib.parse.urlencode(params)}"))
    results = [r for r in data.get("results", []) if CC0_URL_MARK in (r.get("license") or "")]  # re-check
    return results[:count]


def stage_candidates(query: str, name: str, max_duration: float, count: int) -> list[dict]:
    results = search(query, max_duration, count)
    if not results:
        raise PipelineError(f"No CC0 results for '{query}' under {max_duration}s; try other words or --max-duration.")
    out_dir = ensure_dir(CANDIDATES)
    for old in out_dir.glob(f"{name}_*.mp3"):
        old.unlink()
    staged = []
    for i, r in enumerate(results, start=1):
        target = out_dir / f"{name}_{i}.mp3"
        target.write_bytes(_get(r["previews"]["preview-hq-mp3"], timeout=60))
        staged.append({"n": i, "file": str(target), "id": r["id"], "title": r["name"], "author": r["username"],
                       "duration": r["duration"], "url": r["url"], "license_url": r["license"]})
        print(f"  #{i}  {r['duration']:5.1f}s  {r['name'][:50]:<50}  by {r['username']}   -> {target}")
    save_json(out_dir / f"{name}.json", {"query": query, "candidates": staged})
    return staged


def keep(name: str, n: int, kind: str = "sfx") -> Path:
    from audio_manifest import add

    meta_path = CANDIDATES / f"{name}.json"
    if not meta_path.is_file():
        raise PipelineError(f"No candidates for '{name}'. Run: "
                            f"python scripts/fetch_sfx.py search \"...\" --name {name}")
    cand = next((c for c in load_json(meta_path)["candidates"] if c["n"] == n), None)
    if cand is None or not Path(cand["file"]).is_file():
        raise PipelineError(f"Candidate #{n} for '{name}' not found.")
    if CC0_URL_MARK not in cand["license_url"]:
        raise PipelineError(f"Candidate #{n} is not CC0 ({cand['license_url']}).")
    dest = ensure_dir(SFX_DIR) / f"{name}.mp3"
    shutil.copyfile(cand["file"], dest)
    add(dest, name=name, kind=kind, source="freesound", license_="CC0", author=cand["author"], url=cand["url"],
        attribution=None, force=True)
    return dest


def script_queries(script_path: Path) -> dict[str, str]:
    """name -> search query for every SFX cue in an episode script (production.audio.sfx_library)."""
    script = load_json(script_path)
    lib = (script.get("production", {}).get("audio", {}) or {}).get("sfx_library", {})
    if not lib:
        raise PipelineError("The script has no production.audio.sfx_library (name -> search words).")
    return {name: spec["search"] for name, spec in lib.items() if spec.get("search")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CC0 sound effects from Freesound.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="Download candidates to temp/sfx_candidates for listening.")
    s.add_argument("query")
    s.add_argument("--name", required=True, help="Id used in scripts, e.g. chain_clink")
    s.add_argument("--max-duration", type=float, default=8.0)
    s.add_argument("--count", type=int, default=3)
    k = sub.add_parser("keep", help="Register candidate N as the sound for NAME.")
    k.add_argument("name")
    k.add_argument("n", type=int)
    k.add_argument("--kind", default="sfx", choices=["sfx", "ambience"])
    sc = sub.add_parser("script", help="Search candidates for every SFX an episode script needs.")
    sc.add_argument("script", type=Path)
    sc.add_argument("--auto-keep", action="store_true", help="Register candidate #1 for every name (no audition).")
    args = p.parse_args(argv)
    setup_logging()
    try:
        if args.cmd == "search":
            stage_candidates(args.query, args.name, args.max_duration, args.count)
            print(f"Listen in {CANDIDATES}, then: python scripts/fetch_sfx.py keep {args.name} <n>")
        elif args.cmd == "keep":
            print(f"kept {keep(args.name, args.n, args.kind)}")
        else:
            from audio_manifest import path_for_name

            for name, query in script_queries(args.script).items():
                if path_for_name(name):
                    print(f"have    {name}")
                    continue
                print(f"search  {name}: '{query}'")
                try:
                    stage_candidates(query, name, 8.0, 3)
                    if args.auto_keep:
                        print(f"  kept #1 -> {keep(name, 1)}")
                except PipelineError as exc:
                    log.warning("%s: %s", name, exc)
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
