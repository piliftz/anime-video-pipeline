"""Character shot: VRM model + one voice line -> lip-synced, expressive Blender render -> finished MP4.

Pipeline (each step reuses the rest of this repo):
  1. voice      the line is synthesized through voiceover_generator with the SAME single-scene storyboard the
                orchestrator renders later, so both use the identical cached WAV (lip-sync stays in sync);
  2. mouth      Rhubarb Lip Sync turns the WAV into timed mouth shapes ($RHUBARB_EXE or 'rhubarb' on PATH);
  3. frames     headless Blender ($BLENDER_EXE) runs blender_vrm_shot.py: VRM + MToon cel shading + outlines,
                background plane, lip-sync, expressions, blinks, gestures, camera, Eevee PNG frames;
  4. video      the frame folder becomes the scene clip of a storyboard (narration, subtitles, SFX), rendered by
                video_orchestrator (one lossy encode, loudness-normalized mix).

Times in the shot spec are seconds into the shot ("at") or relative to a sentence of the line
("at_sentence": 1-based, plus "delay" seconds). See character_shot_template.json for every key.

    python scripts/character_shot.py character_shot_template.json
    python scripts/character_shot.py character_shot_template.json --draft --tts-fallback silence
    python scripts/character_shot.py character_shot_template.json --benchmark 48   # Eevee speed, no video
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline_utils import (
    OUTPUT_DIR, TEMP_DIR, PipelineError, deep_merge, get_logger, list_frames, load_json, resolve_path,
    save_json, setup_logging,
)

log = get_logger("character_shot")
SCRIPTS_DIR = Path(__file__).resolve().parent
BLENDER_SCRIPT = SCRIPTS_DIR / "blender_vrm_shot.py"

SHOT_DEFAULTS = {
    "id": None,
    "vrm": None,
    "background": None,
    "duration": 10.0,
    "render": {"resolution": [1920, 1080], "fps": 24, "on": 2, "samples": 16},
    "line": {"text": "", "voice": None, "rate": "+0%", "pitch": "+0Hz", "volume": "+0%", "engine": "edge",
             "voicevox": None, "offset": 1.0, "tail": 0.5, "recognizer": "pocketSphinx",
             "mouth_style": "anime", "mouth_gain": 1.0, "subtitle_text": None},
    # [{"name": VRM expression | "shape_keys": {key: weight}, "weight", "at"|"at_sentence"+"delay", "ramp"}]
    "expressions": [],
    "gestures": [],      # [{"name": fist_raise|fist_to_chest|head_down|nod, "side", "at"|"at_sentence", "hold"}]
    "blink": {"enabled": True, "interval": 3.4, "first": 1.2, "closeup_hold_max": 2.5},
    # House rule (owner, 2026-11-07): strike gestures always wear the fighting face (VRoid shape-key names).
    "strike_face": {"enabled": True, "ramp": 0.06,
                    "shape_keys": {"Fcl_BRW_Angry": 1.9, "Fcl_EYE_Angry": 1.0},
                    "gritted": {"Fcl_MTH_I": 1.3, "Fcl_MTH_E": 0.4}},
    "cuts": [],          # CUT_DEFAULTS-shaped camera setups; the camera hard-cuts at each one's start
    "camera": {"background_distance": 6.0, "punches": []},
    "lighting": {"key_color": [1.0, 0.9, 0.8], "key_strength": 1.5, "key_direction": [1.0, 0.9, 0.9],
                 "back_strength": 3.0, "ambient_color": [0.5, 0.46, 0.44], "ambient_strength": 0.25,
                 "exposure": 0.95, "lit_tint": [1.0, 0.96, 0.9], "match_background": 0.0,
                 "shadow_color": [0.25, 0.32, 0.65], "shadow_tint": 0.55, "shade_darken": 0.8,
                 "toony": 0.98, "shading_shift": -0.15, "face_shading_shift": 0.6,
                 "rim_color": [0.4, 0.65, 1.0], "rim_strength": 0.8, "rim_power": 6.0, "rim_lift": 0.0,
                 "catchlight": {"materials": ["Highlight"], "strength": 2.5},
                 "background_brightness": 1.0},
    "spring_bones": True,
    "sfx": [],           # [{"file", "at"|"at_sentence", "volume"}]
    "fx": {},            # passed to the storyboard scene (fade_in, fade_out, color_grade, ...)
}
CUT_DEFAULTS = {
    "framing": "medium",       # extreme_closeup | closeup | medium_closeup | medium
    "target": "chest",         # eyes | face | chest | right_hand | left_hand
    "lock": 0.0,               # seconds after the cut starts at which the target position is measured
    "focal_mm": 50.0, "yaw_deg": 0.0, "low_angle_deg": 0.0, "dutch_deg": 0.0,
    "offset_x": 0.0, "offset_y": 0.0, "push_in": 0.0, "fstop": 4.0,
    "face_shadow": None,       # {"edge": metres above the eyes}: hard flag shadow across the upper face
}
RECOGNIZERS = ("pocketSphinx", "phonetic")
MOUTH_STYLES = ("anime", "visemes")


# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #
def load_shot(path: Path) -> tuple[dict, Path]:
    raw = load_json(path)
    base = path.resolve().parent
    shot = deep_merge(SHOT_DEFAULTS, raw)
    errors = []
    if not shot["id"] or not str(shot["id"]).replace("_", "").replace("-", "").isalnum():
        errors.append("id: required, letters/digits/_/- only.")
    vrm = resolve_path(base, shot["vrm"])
    if vrm is None or not vrm.is_file():
        errors.append(f"vrm: file not found: {vrm} (export your character from VRoid Studio as VRM 1.0).")
    bg = resolve_path(base, shot["background"])
    if bg is not None and not bg.is_file():
        errors.append(f"background: file not found: {bg}")
    if not (shot["line"]["text"] or "").strip():
        errors.append("line.text: required.")
    if shot["line"]["recognizer"] not in RECOGNIZERS:
        errors.append(f"line.recognizer must be one of {RECOGNIZERS} (phonetic for non-English).")
    if shot["line"]["mouth_style"] not in MOUTH_STYLES:
        errors.append(f"line.mouth_style must be one of {MOUTH_STYLES}.")
    for i, cue in enumerate(shot["expressions"]):
        if ("name" in cue) == ("shape_keys" in cue):
            errors.append(f"expressions[{i}]: give exactly one of 'name' (VRM expression) or 'shape_keys'.")
    shot["cuts"] = [deep_merge(CUT_DEFAULTS, c) for c in shot["cuts"]] or [deep_merge(CUT_DEFAULTS, {"at": 0.0})]
    if int(shot["render"]["on"]) not in (1, 2, 3):
        errors.append("render.on must be 1, 2 or 3 (frames each drawing is held).")
    for i, cue in enumerate(shot["sfx"]):
        p = resolve_path(base, cue.get("file"))
        if p is None or not p.is_file():
            errors.append(f"sfx[{i}]: file not found: {p}")
    for group in ("expressions", "gestures", "sfx", "cuts"):
        for i, cue in enumerate(shot[group]):
            if ("at" in cue) == ("at_sentence" in cue):
                errors.append(f"{group}[{i}]: give exactly one of 'at' (seconds) or 'at_sentence' (1-based).")
    for i, p in enumerate(shot["camera"]["punches"]):
        if ("at" in p) == ("at_sentence" in p):
            errors.append(f"camera.punches[{i}]: give exactly one of 'at' or 'at_sentence'.")
    if errors:
        raise PipelineError(f"{path}:\n  " + "\n  ".join(errors))
    shot["_vrm"], shot["_background"] = vrm, bg
    return shot, base


def resolve_time(cue: dict, sentences: list[dict], offset: float) -> float:
    if "at" in cue:
        return float(cue["at"])
    n = int(cue["at_sentence"])
    if not 1 <= n <= len(sentences):
        raise PipelineError(f"at_sentence {n}: the line has {len(sentences)} sentence(s).")
    return offset + float(sentences[n - 1]["start"]) + float(cue.get("delay", 0.0))


# --------------------------------------------------------------------------- #
# Storyboard (voice + final video)
# --------------------------------------------------------------------------- #
def build_storyboard(shot: dict, base: Path, frames_dir: Path, output: Path, sfx_times: list[float]) -> dict:
    line, render = shot["line"], shot["render"]
    voice_defaults = {k: line[k] for k in ("voice", "rate", "pitch", "volume", "engine", "voicevox") if line[k]}
    narration = {"text": line["text"], "offset": float(line["offset"]), "tail": float(line["tail"])}
    if line["subtitle_text"]:
        narration["subtitle_text"] = line["subtitle_text"]
    sfx = [{"file": str(resolve_path(base, c["file"])), "at": round(t, 3), "volume": float(c.get("volume", 1.0))}
           for c, t in zip(shot["sfx"], sfx_times)]
    return {
        "project": {"title": shot["id"], "output": str(output), "resolution": render["resolution"],
                    "fps": render["fps"]},
        "voice_defaults": voice_defaults,
        "scenes": [{
            "id": shot["id"], "clip": str(frames_dir), "sequence_fps": render["fps"],
            "duration": float(shot["duration"]), "extend": "freeze", "narration": narration,
            "sfx": sfx, "fx": shot["fx"],
        }],
    }


def synthesize(storyboard: dict, fallback: str) -> dict:
    from voiceover_generator import generate_for_storyboard

    voices = generate_for_storyboard(storyboard, TEMP_DIR / "voiceovers", fallback=fallback)
    return voices[storyboard["scenes"][0]["id"]]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def find_tool(env: str, name: str, hint: str) -> str:
    exe = os.environ.get(env) or shutil.which(name)
    if not exe or not Path(exe).is_file():
        raise PipelineError(f"{name} not found. Set ${env} to its full path or put it on PATH. {hint}")
    return exe


def rhubarb_cues(voice: dict, recognizer: str, work: Path) -> list[dict]:
    wav = Path(voice["audio"])
    if voice.get("fallback"):
        log.warning("Voice is a silent placeholder: no mouth movement.")
        return []
    key = hashlib.sha1(wav.read_bytes() + recognizer.encode() + voice["text"].encode("utf-8")).hexdigest()[:12]
    out = work / f"mouth_{key}.json"
    if not out.is_file():
        exe = find_tool("RHUBARB_EXE", "rhubarb", "Download: github.com/DanielSWolf/rhubarb-lip-sync/releases")
        cmd = [exe, "-q", "-r", recognizer, "-f", "json", "--extendedShapes", "GHX", "-o", str(out)]
        if recognizer == "pocketSphinx":
            dialog = work / "dialog.txt"
            dialog.write_text(voice["text"], encoding="utf-8")
            cmd += ["-d", str(dialog)]
        log.info("Rhubarb (%s) on %s", recognizer, wav.name)
        proc = subprocess.run([*cmd, str(wav)], capture_output=True, text=True)
        if proc.returncode != 0 or not out.is_file():
            raise PipelineError(f"Rhubarb failed ({proc.returncode}): {proc.stderr.strip()[-500:]}")
    return load_json(out)["mouthCues"]


def run_blender(job_path: Path, log_path: Path) -> None:
    exe = find_tool("BLENDER_EXE", "blender", "Install Blender 4.2+ (winget install BlenderFoundation.Blender).")
    cmd = [exe, "--background", "--python-exit-code", "1", "--python", str(BLENDER_SCRIPT), "--", str(job_path)]
    log.info("Blender: rendering frames (full log: %s)", log_path)
    with open(log_path, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            if "| blender_vrm_shot |" in line or "Traceback" in line or "Error" in line:
                print(line.rstrip(), flush=True)
        rc = proc.wait()
    if rc != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        raise PipelineError(f"Blender exited with {rc}. Last log lines:\n" + "\n".join(tail))


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #
def background_mean(path: Path) -> list[float]:
    """Mean sRGB colour (0-1) of the background, used to tint the character toward the scene."""
    from PIL import Image

    with Image.open(path) as im:
        pixels = list(im.convert("RGB").resize((64, 36)).getdata())
    return [round(sum(c) / (len(pixels) * 255), 4) for c in zip(*pixels)]


def make_job(shot: dict, voice: dict, cues: list[dict], frames_dir: Path, work: Path) -> dict:
    offset = float(shot["line"]["offset"])
    sentences = voice.get("boundaries") or [{"start": 0.0, "end": voice["duration"]}]

    def timed(items: list[dict]) -> list[dict]:
        out = []
        for c in items:
            d = {k: v for k, v in c.items() if k not in ("at_sentence", "delay")}
            d["at"] = round(resolve_time(c, sentences, offset), 4)
            out.append(d)
        return sorted(out, key=lambda d: d["at"])

    end = offset + float(voice["duration"])
    if end > float(shot["duration"]):
        raise PipelineError(f"The line ends at {end:.2f}s but the shot is {shot['duration']}s; "
                            "raise 'duration' or lower line.offset.")
    camera = dict(shot["camera"])
    camera["punches"] = timed(camera["punches"])
    cuts = timed(shot["cuts"])
    if cuts[0]["at"] > 0:
        raise PipelineError(f"The first cut must start at 0 s (it starts at {cuts[0]['at']:.2f}s).")
    render = shot["render"]
    return {
        "vrm": str(shot["_vrm"]), "background": str(shot["_background"]) if shot["_background"] else None,
        "frames_dir": str(frames_dir), "log": str(work / "render_log.json"),
        "save_blend": str(work / f"{shot['id']}.blend"),
        "duration": float(shot["duration"]), "fps": int(render["fps"]), "on": int(render["on"]),
        "resolution": list(render["resolution"]), "samples": int(render["samples"]),
        "mouth_cues": [{"start": round(offset + c["start"], 4), "end": round(offset + c["end"], 4),
                        "value": c["value"]} for c in cues],
        "mouth_gain": float(shot["line"]["mouth_gain"]), "mouth_style": shot["line"]["mouth_style"],
        "speech": [[offset, end]],
        "expressions": timed(shot["expressions"]), "gestures": timed(shot["gestures"]),
        "strike_face": shot["strike_face"],
        "blink": shot["blink"], "camera": camera, "cuts": cuts, "lighting": shot["lighting"],
        "background_mean": background_mean(shot["_background"]) if shot["_background"] else None,
        "spring_bones": bool(shot["spring_bones"]),
    }


def render_frames(job: dict, work: Path, force: bool) -> None:
    frames_dir = Path(job["frames_dir"])
    # The renderer's own code is part of the key: a change to blender_vrm_shot.py re-renders.
    job_hash = hashlib.sha1(json.dumps(job, sort_keys=True).encode() + BLENDER_SCRIPT.read_bytes()).hexdigest()[:12]
    stamp = frames_dir / ".job_hash"
    expected = int(round(job["duration"] * job["fps"]))
    if job.get("frame_limit"):
        expected = min(expected, int(job["frame_limit"]))
    if (not force and stamp.is_file() and stamp.read_text().strip() == job_hash
            and len(list(frames_dir.glob("frame_*.png"))) == expected):
        log.info("Frames up to date (%d) in %s; skipping Blender (--force to re-render).", expected, frames_dir)
        return
    job_path = save_json(work / ("benchmark_job.json" if job.get("frame_limit") else "job.json"), job)
    stamp.unlink(missing_ok=True)
    run_blender(job_path, work / "blender.log")
    stamp.write_text(job_hash)


def run(args: argparse.Namespace) -> Path | None:
    if args.benchmark is not None and args.benchmark < 2:
        raise PipelineError("--benchmark needs at least 2 frames (the first frame includes shader compilation).")
    shot, base = load_shot(args.spec)
    work = TEMP_DIR / "character_shots" / shot["id"]
    work.mkdir(parents=True, exist_ok=True)
    frames_dir = work / ("benchmark_frames" if args.benchmark else "frames")
    output = args.output or (OUTPUT_DIR / shot["id"] / f"{shot['id']}{'_draft' if args.draft else ''}.mp4")

    # Voice first (sentence timings drive every 'at_sentence'); sfx times need them too.
    probe_sb = build_storyboard(shot, base, frames_dir, output, [0.0] * len(shot["sfx"]))
    voice = synthesize(probe_sb, args.tts_fallback)
    sentences = voice.get("boundaries") or [{"start": 0.0, "end": voice["duration"]}]
    log.info("Voice %.2fs, %d sentence(s): %s", voice["duration"], len(sentences),
             " | ".join(f"{s['start']:.2f}s" for s in sentences))
    cues = rhubarb_cues(voice, shot["line"]["recognizer"], work)
    job = make_job(shot, voice, cues, frames_dir, work)

    if args.benchmark:
        job.update(on=1, frame_limit=args.benchmark, save_blend=None, log=str(work / "benchmark_log.json"))
        render_frames(job, work, force=True)
        stats = load_json(Path(job["log"]))
        print(f"Eevee {stats['resolution'][0]}x{stats['resolution'][1]}, {stats['samples']} samples, "
              f"{stats['drawings']} frames: first {stats['first_frame_s']:.2f}s, "
              f"average after the first {stats['avg_s_per_drawing_after_first']:.2f}s/frame "
              f"(min {stats['min_s']:.2f}, max {stats['max_s']:.2f}). Log: {job['log']}")
        return None

    render_frames(job, work, args.force)
    n = len(list_frames(frames_dir))
    log.info("%d frames ready in %s", n, frames_dir)
    if args.frames_only:
        return None

    sfx_times = [resolve_time(c, sentences, float(shot["line"]["offset"])) for c in shot["sfx"]]
    storyboard = build_storyboard(shot, base, frames_dir, output, sfx_times)
    sb_path = save_json(work / f"{shot['id']}.storyboard.json", storyboard)
    from video_orchestrator import main as orchestrate

    argv = [str(sb_path), "-o", str(output), "--tts-fallback", args.tts_fallback]
    if args.draft:
        argv.append("--draft")
    if orchestrate(argv) != 0:
        raise PipelineError(f"Video render failed for storyboard {sb_path}")
    return output


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a lip-synced VRM character shot with Blender, then encode it.")
    p.add_argument("spec", type=Path, help="Shot spec JSON (see character_shot_template.json).")
    p.add_argument("-o", "--output", type=Path, help="Output MP4 (default output/<id>/<id>.mp4).")
    p.add_argument("--draft", action="store_true", help="Half-resolution video encode (frames stay full size).")
    p.add_argument("--frames-only", action="store_true", help="Stop after the Blender frames.")
    p.add_argument("--force", action="store_true", help="Re-render frames even if the job is unchanged.")
    p.add_argument("--benchmark", type=int, metavar="N",
                   help="Render the first N frames on ones (every frame) and report Eevee seconds per frame.")
    p.add_argument("--tts-fallback", choices=["none", "silence"], default="none",
                   help="If the TTS engine is unreachable: fail (none) or use a silent placeholder.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        out = run(args)
        if out:
            print(out)
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
