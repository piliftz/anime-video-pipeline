#!/usr/bin/env python3
"""Produce an episode segment from its script: keyframes -> Wan motion -> assembled video.

Run the stages in order. Each one is incremental: finished work is skipped, so an interrupted
overnight run simply continues where it stopped.

  keyframes  one Animagine keyframe per cut + a contact sheet to review (regenerate a bad cut by
             giving it a "seed" in the script and re-running)
  motion     animate every cut with motion "wan" in ONE ComfyUI session (models load once);
             writes an MP4 preview per clip and logs the real time per clip
  assemble   build a pipeline storyboard for one language and render it (hard cuts, subtle grade,
             cinematic subtitles)
  status     what's done, what's missing, measured generation times

Example (Windows):
  .venv\\Scripts\\python scripts\\episode_producer.py series\\arc_forge\\ep01_pilot.script.json keyframes
  .venv\\Scripts\\python scripts\\episode_producer.py series\\arc_forge\\ep01_pilot.script.json motion
  .venv\\Scripts\\python scripts\\episode_producer.py series\\arc_forge\\ep01_pilot.script.json assemble --draft
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

from pipeline_utils import (
    OUTPUT_DIR,
    TEMP_DIR,
    PipelineError,
    ensure_dir,
    get_logger,
    load_json,
    resolve_path,
    save_json,
    setup_logging,
)

log = get_logger("producer")


# --------------------------------------------------------------------------- #
# Script access
# --------------------------------------------------------------------------- #
class Episode:
    def __init__(self, script_path: Path, segment: str):
        self.path = Path(script_path).resolve()
        self.base = self.path.parent
        self.script = load_json(self.path)
        self.prod = self.script.get("production")
        if not self.prod:
            raise PipelineError(f"{self.path} has no 'production' block.")
        segs = {s["id"]: s for s in self.script.get("segments", [])}
        if segment not in segs:
            raise PipelineError(f"Segment '{segment}' not found. Available: {', '.join(segs)}")
        self.segment = segs[segment]
        self.cuts = self.segment.get("cuts") or []
        if not self.cuts:
            raise PipelineError(f"Segment '{segment}' has no cuts yet (status: {self.segment.get('status')}).")
        self.name = f"ep{int(self.script['episode']):02d}_{segment}"
        self.work = resolve_path(self.base, self.prod["work_dir"]) / segment
        self.keyframe_dir = self.work / "keyframes"
        self.clip_dir = self.work / "clips"
        chars_file = resolve_path(self.base, self.prod.get("characters_file", "characters.json"))
        raw_chars = load_json(chars_file)
        self.characters = {k: {"tags": v["tags"]} for k, v in raw_chars.items() if isinstance(v, dict) and "tags" in v}
        self.fps = int(self.prod.get("fps", 24))

    def keyframe(self, cut: dict) -> Path:
        return self.keyframe_dir / f"{cut['id']}.png"

    def clip(self, cut: dict) -> Path:
        return self.clip_dir / cut["id"]

    def needs_keyframe(self, cut: dict) -> bool:
        return cut.get("motion", "still") in ("wan", "still") and bool(cut.get("keyframe_prompt"))


def wan_length(duration: float, fps: int) -> int:
    """Frames for a cut, rounded UP to Wan's required 4n+1."""
    n = max(5, round(duration * fps))
    return 4 * math.ceil((n - 1) / 4) + 1


# --------------------------------------------------------------------------- #
# Stage 1: keyframes
# --------------------------------------------------------------------------- #
def stage_keyframes(ep: Episode, cut_ids: list[str] | None, force: bool, comfy_url, comfy_dir) -> None:
    from contact_sheet import build_sheet, collect, save_sheet
    from image_generator import generate_images

    for cut in ep.cuts:
        unknown = [c for c in cut.get("characters", []) if c not in ep.characters]
        if unknown:
            raise PipelineError(f"[{cut['id']}] characters not in the characters file: {', '.join(unknown)}")
    scenes = []
    for cut in ep.cuts:
        if not ep.needs_keyframe(cut):
            continue
        gen = {"prompt": cut["keyframe_prompt"], "characters": cut.get("characters", [])}
        if cut.get("seed") is not None:
            gen["seed"] = cut["seed"]
        if cut.get("negative"):
            gen["negative"] = cut["negative"]
        scenes.append({"id": cut["id"], "clip": str(ep.keyframe(cut)), "generate": gen})
    storyboard = {
        "image_generation": {**ep.prod["image"], "output_dir": str(ep.keyframe_dir)},
        "characters": ep.characters,
        "scenes": scenes,
    }
    sb_path = save_json(TEMP_DIR / f"{ep.name}_keyframes.json", storyboard)
    generate_images(sb_path, cut_ids, force, comfy_url, comfy_dir)
    sheet = build_sheet(collect(ep.keyframe_dir, None), tile_h=300, cols=3)
    out = save_sheet(sheet, OUTPUT_DIR / f"{ep.name}_keyframes.png")
    print(f"Review the keyframes: {out}")


# --------------------------------------------------------------------------- #
# Stage 2: motion
# --------------------------------------------------------------------------- #
def clip_job(ep: Episode, cut: dict) -> tuple[str, dict, int, str]:
    from video_generator import VIDEO_DEFAULTS

    v = ep.prod.get("video", {})
    cfg = {**VIDEO_DEFAULTS, **{k: v[k] for k in ("width", "height", "steps", "weight_dtype") if k in v},
           "length": wan_length(float(cut["duration"]), ep.fps), "fps": ep.fps}
    seed = int(cut.get("motion_seed", v.get("seed", 42)))
    prompt = ", ".join(p for p in (v.get("style_prefix", ""), cut.get("action", ""), cut.get("motion_prompt", "")) if p)
    kf = ep.keyframe(cut)
    digest = hashlib.sha1(kf.read_bytes()).hexdigest() if kf.is_file() else "missing"
    job_hash = hashlib.sha1(json.dumps([digest, prompt, cfg, seed], sort_keys=True).encode()).hexdigest()[:16]
    return prompt, cfg, seed, job_hash


def stage_motion(ep: Episode, cut_ids: list[str] | None, force: bool, comfy_url, comfy_dir) -> int:
    from comfy_client import DEFAULT_URL, ComfyServer
    from video_generator import animate_with_client, preflight, write_preview

    todo = []
    for cut in ep.cuts:
        if cut.get("motion") != "wan" or (cut_ids and cut["id"] not in cut_ids):
            continue
        if not ep.keyframe(cut).is_file():
            raise PipelineError(f"[{cut['id']}] keyframe missing; run the 'keyframes' stage first.")
        prompt, cfg, seed, job_hash = clip_job(ep, cut)
        meta_path = ep.clip(cut) / "clip.json"
        if not force and meta_path.is_file() and load_json(meta_path).get("job_hash") == job_hash:
            log.info("up to date %-6s (%s)", cut["id"], ep.clip(cut))
            continue
        todo.append((cut, prompt, cfg, seed, job_hash))
    if not todo:
        log.info("All motion clips are up to date.")
        return 0
    total_frames = sum(t[2]["length"] for t in todo)
    log.info("Animating %d cut(s), %d frames total, in one ComfyUI session.", len(todo), total_frames)
    failures = []
    t_all = time.time()
    with ComfyServer(comfy_url or DEFAULT_URL, comfy_dir) as client:
        preflight(client, todo[0][2])
        for n, (cut, prompt, cfg, seed, job_hash) in enumerate(todo, start=1):
            log.info("[%d/%d] %s: %s", n, len(todo), cut["id"], cut.get("shot", ""))
            try:
                meta = animate_with_client(client, ep.keyframe(cut), prompt, ep.clip(cut), cfg, seed)
            except PipelineError as exc:  # e.g. out of GPU memory: keep going with the other cuts
                log.error("[%s] failed: %s", cut["id"], exc)
                failures.append(cut["id"])
                continue
            meta["job_hash"] = job_hash
            save_json(ep.clip(cut) / "clip.json", meta)
            write_preview(ep.clip(cut), ensure_dir(OUTPUT_DIR / ep.name) / f"{cut['id']}.mp4", ep.fps)
            per_frame = meta["generation_seconds"] / max(1, meta["frames"])
            log.info("[%s] %.1f min for %d frames (%.1f s/frame)", cut["id"], meta["generation_seconds"] / 60,
                     meta["frames"], per_frame)
    log.info("Motion stage finished in %.1f min. Previews: %s", (time.time() - t_all) / 60, OUTPUT_DIR / ep.name)
    if failures:
        log.error("Failed cuts: %s (re-run 'motion' to retry only these)", ", ".join(failures))
        return 1
    return 0


# --------------------------------------------------------------------------- #
# Stage 3: assemble
# --------------------------------------------------------------------------- #
def build_storyboard(ep: Episode, lang: str) -> tuple[dict, list[str]]:
    from audio_manifest import load_manifest, path_for_name

    asm = ep.prod.get("assembly", {})
    voices = asm.get(f"voices_{lang}", {})
    manifest = load_manifest()
    warnings: list[str] = []
    scenes = []
    for cut in ep.cuts:
        motion = cut.get("motion", "still")
        scene: dict = {"id": cut["id"], "duration": float(cut["duration"]), "fx": dict(cut.get("fx", {}))}
        if motion == "wan" and (ep.clip(cut) / "clip.json").is_file():
            scene.update({"clip": str(ep.clip(cut)), "sequence_fps": ep.fps, "extend": "freeze"})
        elif motion in ("wan", "still"):
            if not ep.keyframe(cut).is_file():
                warnings.append(f"{cut['id']}: no keyframe yet, skipped")
                continue
            if motion == "wan":
                warnings.append(f"{cut['id']}: no motion clip yet, using the still keyframe")
            scene["clip"] = str(ep.keyframe(cut))
        elif motion == "clip":
            src = resolve_path(ep.base, cut.get("clip"))
            if src is None or not src.exists():
                warnings.append(f"{cut['id']}: pre-rendered clip not found ({src}), skipped")
                continue
            scene.update({"clip": str(src), "sequence_fps": ep.fps, "extend": "freeze"})
        lines = cut.get("dialogue") or []
        if len(lines) > 1:
            raise PipelineError(f"[{cut['id']}] has {len(lines)} dialogue lines; split it into one line per cut.")
        if lines:
            line = lines[0]
            if not line.get(lang):
                raise PipelineError(f"[{cut['id']}] dialogue has no '{lang}' text.")
            scene["narration"] = {"speaker": line["speaker"], "text": line[lang], "offset": 0.25, "tail": 0.45}
            sub_lang = asm.get("subtitle_language", {}).get(lang, lang)
            if sub_lang != lang:
                if not line.get(sub_lang):
                    raise PipelineError(f"[{cut['id']}] dialogue has no '{sub_lang}' text for the subtitles.")
                scene["narration"]["subtitle_text"] = line[sub_lang]
            style = line.get(f"{lang}_style")  # per-line emotion style, e.g. VOICEVOX "悲しみ"
            if style and voices.get(line["speaker"], {}).get("engine") == "voicevox":
                scene["narration"]["voicevox"] = {**voices[line["speaker"]].get("voicevox", {}), "style": style}
        sfx = []
        for cue in cut.get("sfx", []):
            path = path_for_name(cue["name"], manifest)
            if path is None or not path.is_file():
                warnings.append(f"{cut['id']}: sfx '{cue['name']}' not registered yet, skipped "
                                f"(python scripts/fetch_sfx.py script {ep.path.name})")
                continue
            sfx.append({"file": str(path), "at": float(cue.get("at", 0.0)), "volume": float(cue.get("volume", 1.0))})
        if sfx:
            scene["sfx"] = sfx
        scenes.append(scene)
    present = {sc["id"] for sc in scenes}
    music = []
    for cue in (ep.segment.get("audio") or {}).get("music", []):
        path = path_for_name(cue["name"], manifest)
        if path is None or not path.is_file():
            warnings.append(f"music '{cue['name']}' not registered yet, skipped "
                            "(download it, then python scripts/audio_manifest.py add ...)")
            continue
        if cue.get("start_cut") not in present or cue.get("end_cut") not in present:
            warnings.append(f"music '{cue['name']}': cut range {cue.get('start_cut')}..{cue.get('end_cut')} "
                            "not in this render, skipped")
            continue
        out = {k: v for k, v in cue.items() if k in ("volume", "loop", "fade_in", "fade_out", "duck_to", "duck_fade")}
        out.update({"file": str(path), "start_scene": cue["start_cut"], "end_scene": cue["end_cut"],
                    "drops": [{"start_scene": d["start_cut"], "end_scene": d["end_cut"]} for d in cue.get("drops", [])
                              if d["start_cut"] in present and d["end_cut"] in present]})
        music.append(out)
    grade = asm.get("grade", {})
    title = ep.script["title"].get(lang) or ep.script["title"]["en"]
    storyboard = {
        "project": {"title": f"{ep.script['series'].get(lang, '')} - {title}",
                    "output": str(OUTPUT_DIR / f"{ep.name}_{lang}.mp4"), "fps": ep.fps, "fit": "cover"},
        "speakers": voices,
        "audio": {"music": music, "require_license": True},
        "transitions": {"default": {"type": "cut", "duration": 0.0}},
        "subtitles": asm.get("subtitles", {}),
        "fx_defaults": {"color_grade": {"enabled": bool(grade), **grade},
                        "anime_timing": asm.get("anime_timing", {"enabled": False})},
        "scenes": scenes,
    }
    return storyboard, warnings


def stage_assemble(ep: Episode, lang: str, passthrough: list[str]) -> int:
    from video_orchestrator import main as orchestrate

    if not ep.prod.get("assembly", {}).get(f"voices_{lang}"):
        raise PipelineError(f"No voices_{lang} cast in the script's production.assembly block.")
    storyboard, warnings = build_storyboard(ep, lang)
    for w in warnings:
        log.warning("%s", w)
    sb_path = save_json(TEMP_DIR / f"{ep.name}_{lang}.storyboard.json", storyboard)
    log.info("Storyboard: %s (%d cuts)", sb_path, len(storyboard["scenes"]))
    code = orchestrate([str(sb_path), *passthrough])
    write_voicevox_credits(ep, lang, storyboard)
    return code


def write_voicevox_credits(ep: Episode, lang: str, storyboard: dict) -> None:
    """VOICEVOX characters each have their own terms; most require a credit like 'VOICEVOX:Name'.
    Write the credit lines for the video description, plus each character's terms to read."""
    # Credit exactly the voices that speak in this render (not the whole cast list).
    spoken = {sc["narration"]["speaker"] for sc in storyboard["scenes"] if sc.get("narration")}
    names = sorted({v["voicevox"]["speaker"] for k, v in storyboard["speakers"].items()
                    if k in spoken and v.get("engine") == "voicevox" and v.get("voicevox", {}).get("speaker")})
    if not names:
        return
    out = ensure_dir(OUTPUT_DIR) / f"{ep.name}_{lang}_credits.txt"
    lines = ["Voices (paste into the video description / end credits):"] + [f"VOICEVOX:{n}" for n in names]
    terms_dir = OUTPUT_DIR / "voicevox_terms"
    try:
        from voicevox_client import VoicevoxServer

        with VoicevoxServer() as vv:
            for n in names:
                _, uuid, _ = vv.resolve(n, None)
                ensure_dir(terms_dir)
                (terms_dir / f"{n}.md").write_text(vv.policy(uuid), encoding="utf-8")
        lines += ["", f"Each character's terms of use were saved to {terms_dir}. READ THEM before publishing,",
                  "especially for monetized videos: the terms differ per character."]
    except PipelineError as exc:
        lines += ["", f"(Could not fetch the terms automatically: {exc}) Check each character's terms on the "
                  "VOICEVOX website before publishing."]
    existing = out.read_text(encoding="utf-8") + "\n" if out.is_file() else ""  # audio credits from the render
    out.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")
    log.info("VOICEVOX credits: %s", out)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def stage_status(ep: Episode) -> None:
    print(f"\n{ep.name}  ({ep.path.name})\n")
    print(f"{'cut':<6} {'motion':<6} {'dur':>5}  {'keyframe':<9} {'clip':<22} shot")
    total_s = 0.0
    for cut in ep.cuts:
        motion = cut.get("motion", "still")
        kf = "yes" if ep.keyframe(cut).is_file() else ("-" if not ep.needs_keyframe(cut) else "MISSING")
        clip = "-"
        if motion == "wan":
            meta_path = ep.clip(cut) / "clip.json"
            if meta_path.is_file():
                meta = load_json(meta_path)
                _, _, _, job_hash = clip_job(ep, cut)
                stale = "" if meta.get("job_hash") == job_hash else " (STALE)"
                clip = f"{meta['generation_seconds'] / 60:.1f} min{stale}"
                total_s += meta["generation_seconds"]
            else:
                clip = f"todo ({wan_length(float(cut['duration']), ep.fps)} frames)"
        print(f"{cut['id']:<6} {motion:<6} {cut['duration']:>4.1f}s  {kf:<9} {clip:<22} {cut.get('shot', '')}")
    if total_s:
        print(f"\nMeasured Wan time so far: {total_s / 60:.1f} min")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Produce an episode segment: keyframes -> motion -> assemble.")
    p.add_argument("script", type=Path, help="Episode script JSON (e.g. series/arc_forge/ep01_pilot.script.json)")
    p.add_argument("stage", choices=["keyframes", "motion", "assemble", "status"])
    p.add_argument("--segment", default="cold_open")
    p.add_argument("--cuts", help="Comma-separated cut ids to (re)process, e.g. c003,c006")
    p.add_argument("--force", action="store_true", help="Redo even if up to date.")
    p.add_argument("--lang", default="en", help="Assembly language: en or ja (needs voices_<lang> in the script).")
    p.add_argument("--comfy-url")
    p.add_argument("--comfy-dir")
    p.add_argument("-v", "--verbose", action="store_true")
    args, passthrough = p.parse_known_args(argv)
    setup_logging(args.verbose)
    try:
        ep = Episode(args.script, args.segment)
        cut_ids = [c.strip() for c in args.cuts.split(",")] if args.cuts else None
        if args.stage == "keyframes":
            stage_keyframes(ep, cut_ids, args.force, args.comfy_url, args.comfy_dir)
        elif args.stage == "motion":
            return stage_motion(ep, cut_ids, args.force, args.comfy_url, args.comfy_dir)
        elif args.stage == "assemble":
            return stage_assemble(ep, args.lang, passthrough)
        else:
            stage_status(ep)
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted. Re-run the same stage to continue where it stopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
