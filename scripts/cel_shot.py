"""2D cel shot: drawn keyframes + redrawn mouths/effects, rigid cut-out acting, lip sync -> finished MP4.

The owner-approved 2D anime route (2026-11-08). One spec (see cel_shot_template.json) describes a shot of hard cuts:

  1. voice      the line is synthesized exactly like character_shot (same storyboard, same cached WAV);
  2. drawings   per cut, one Animagine keyframe (image_generator's job, so cached keyframes are reused) plus masked
                redraws for mouth shapes, faces and effect "boil" frames. Each redraw is pasted back with
                ImageCompositeMasked, so pixels outside its mask are identical to its base;
  3. mattes     the character is cut out with rembg isnet-anime ($REMBG_PYTHON env, scripts/cel_matte_worker.py);
  4. frames     acting is RIGID only: the cut-out tilts / shifts / scales as one piece over a background plate
                (the owner rejected bending warps as "lens distortion"); extreme close-ups move the whole cel.
                Acting comes from the voice (inhale before speaking, nods on stressed syllables), mouths flap from
                the loudness envelope, drawings and acting on twos, the strike on ones with a smear, camera +
                embers + bloom on ones. Camera and acting are ONE bilinear resample per frame;
  5. video      the frames become a one-scene storyboard (voice, subtitles, SFX, impact frame, speed lines) that
                video_orchestrator encodes once.

Times in the spec: seconds, {"at_sentence": n, "delay": s} (1-based sentence of the line) or
{"after_speech": s} (seconds after the line ends).

    python scripts/cel_shot.py cel_shot_template.json --dry-run
    python scripts/cel_shot.py cel_shot_template.json --stills 0.5,3.2
    python scripts/cel_shot.py cel_shot_template.json [--draft] [--force]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

from pipeline_utils import (
    OUTPUT_DIR, TEMP_DIR, PipelineError, deep_merge, ensure_dir, get_logger, list_frames, load_json,
    resolve_path, save_json, setup_logging,
)

log = get_logger("cel_shot")
SCRIPTS_DIR = Path(__file__).resolve().parent
MATTE_WORKER = SCRIPTS_DIR / "cel_matte_worker.py"

SHOT_DEFAULTS: dict = {
    "id": None,
    "duration": 8.0,
    "fps": 24,
    "on": 2,                          # acting + mouth flaps: frames each drawing / pose is held
    "line": None,                     # same keys as character_shot's line (filled from its defaults)
    "character": {"name": "Hero", "tags": ""},
    "image_generation": {"checkpoint": "animagine", "output_dir": "assets/generated", "rating": "safe"},
    "redraw": {
        "steps": 28, "cfg": 5.0, "sampler": "euler_ancestral", "scheduler": "normal",
        "quality_tags": "masterpiece, high score, great score, absurdres",
        "negative": ("lowres, bad anatomy, text, error, worst quality, low quality, low score, bad score, "
                     "signature, watermark, blurry, deformed mouth, extra teeth"),
    },
    "matte": {"model": "isnet-anime", "bright_gamma": 0.45, "edge": [0.35, 0.65], "soften": 1.2, "grow": 25},
    "comp": {"embers": {"count": 70, "seed": 7}, "bloom": {"threshold": 0.93, "strength": 0.35}},
    "sfx": [],
    "fx": {},
    "cuts": [],
}
CUT_DEFAULTS: dict = {
    "id": None,
    "at": 0.0,
    "keyframe": None,                 # {"id", "prompt", "seed"} -> <output_dir>/<id>.png
    "drawings": [],                   # [{"id", "from": "key"|drawing id, "shapes", "prompt", "seed", "denoise",
                                      #   "fresh", "feather"}]
    "mouth": None,                    # {"closed": id|"key", "half": id (optional), "open": id}
    "boil": None,                     # {"drawings": ["key", id, ...], "on": 3, "after": {"at": time, "on": 2}}
    "layer": "matte",                 # matte | bright (dark close-up) | none (whole cel moves)
    "matte_ignore": [],               # [[x0, y0, x1, y1]] fractions to drop from the matte (e.g. a lantern)
    "acting": {"type": "stare"},
    "camera": {"zoom": [1.0, 1.06], "center": [0.5, 0.5], "slam": None},
    "embers": 0.9,                    # ember layer strength for this cut (0 = off)
}
ACTING_DEFAULTS: dict = {
    "talk": {"pivot": [0.58, 1.25], "head_down": [3.0, 26.0], "lift_before_cut": 0.33, "nod": [0.5, 6.0],
             "inhale_px": 7.0, "sway": [0.4, 3.0]},
    "strike": {"pivot": [0.41, 0.66], "at": 0.0, "travel": 0.12, "lean_deg": 2.0, "windup_px": [14.0, -10.0],
               "follow_px": [-10.0, 6.0], "lunge": 0.14, "settle": 0.08, "smear_taps": 6, "nod": [0.3, 3.0]},
    "shout": {"pivot": [0.5, 0.95], "scale": 1.03, "thrust": 0.03, "nod_px": 12.0, "tremble": [1.5, 0.7],
              "sway": [0.8, 3.0]},
    "stare": {"pivot": [0.5, 0.9], "scale": 1.02, "pant_px": 4.0, "period": 1.1, "sway": [0.3, 2.0]},
}
LAYERS = ("matte", "bright", "none")
SHAPES = ("ellipse", "rect")


# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #
def _is_time(v: object) -> bool:
    if isinstance(v, (int, float)):
        return True
    return isinstance(v, dict) and (("at_sentence" in v) != ("after_speech" in v))


def load_spec(path: Path) -> tuple[dict, Path]:
    from character_shot import SHOT_DEFAULTS as CS_DEFAULTS, fx_times

    raw = load_json(path)
    base = path.resolve().parent
    spec = deep_merge(SHOT_DEFAULTS, {k: v for k, v in raw.items() if k != "cuts"})
    spec["line"] = deep_merge(CS_DEFAULTS["line"], raw.get("line"))
    errors: list[str] = []
    if not spec["id"] or not str(spec["id"]).replace("_", "").replace("-", "").isalnum():
        errors.append("id: required, letters/digits/_/- only.")
    if not (spec["line"]["text"] or "").strip():
        errors.append("line.text: required.")
    if int(spec["on"]) not in (1, 2, 3):
        errors.append("on must be 1, 2 or 3.")
    if not str(spec["character"].get("tags") or "").strip():
        errors.append("character.tags: required (canonical appearance tags, used in every drawing).")
    for i, cue in enumerate(spec["sfx"]):
        p = resolve_path(base, cue.get("file"))
        if p is None or not p.is_file():
            errors.append(f"sfx[{i}]: file not found: {p}")
        if ("at" in cue) == ("at_sentence" in cue):
            errors.append(f"sfx[{i}]: give exactly one of 'at' (seconds) or 'at_sentence' (1-based).")
    for key, val in fx_times(spec["fx"]):
        if isinstance(val, dict) and ("at_sentence" not in val or "at" in val):
            errors.append(f"fx {key}: a timed dict needs 'at_sentence' (+ optional 'delay'), or use seconds.")

    cuts, seen = [], set()
    for i, rc in enumerate(raw.get("cuts") or []):
        c = deep_merge(CUT_DEFAULTS, rc)
        where = f"cuts[{i}]"
        if not c["id"]:
            errors.append(f"{where}: id required.")
        c["acting"] = deep_merge(ACTING_DEFAULTS.get(c["acting"].get("type"), {}), c["acting"])
        if c["acting"].get("type") not in ACTING_DEFAULTS:
            errors.append(f"{where}.acting.type must be one of {tuple(ACTING_DEFAULTS)}.")
        if c["layer"] not in LAYERS:
            errors.append(f"{where}.layer must be one of {LAYERS}.")
        if not _is_time(c["at"]):
            errors.append(f"{where}.at: seconds, {{at_sentence, delay}} or {{after_speech}}.")
        kf = c["keyframe"] or {}
        if not kf.get("id") or not str(kf.get("prompt") or "").strip():
            errors.append(f"{where}.keyframe: needs 'id' and 'prompt' (+ 'seed').")
        names = {"key"}
        for j, d in enumerate(c["drawings"]):
            dw = f"{where}.drawings[{j}]"
            if not d.get("id") or d["id"] in seen or d["id"] == kf.get("id"):
                errors.append(f"{dw}: unique 'id' required.")
            if d.get("from", "key") not in names:
                errors.append(f"{dw}.from '{d.get('from')}' must be 'key' or an EARLIER drawing of this cut.")
            for s in d.get("shapes") or []:
                if len(s) != 2 or s[0] not in SHAPES or len(s[1]) != 4:
                    errors.append(f"{dw}.shapes: each is [\"ellipse\"|\"rect\", [cx, cy, rx, ry]] (fractions).")
            if not d.get("shapes") or not str(d.get("prompt") or "").strip():
                errors.append(f"{dw}: needs 'shapes' and 'prompt'.")
            if not 0.0 < float(d.get("denoise", 0.75)) <= 1.0:
                errors.append(f"{dw}.denoise must be in (0, 1].")
            names.add(d.get("id"))
            seen.add(d.get("id"))
        seen.add(kf.get("id"))
        if c["mouth"]:
            for k in ("closed", "open"):
                if c["mouth"].get(k) not in names:
                    errors.append(f"{where}.mouth.{k}: 'key' or a drawing id of this cut.")
            if c["mouth"].get("half") is not None and c["mouth"]["half"] not in names:
                errors.append(f"{where}.mouth.half: 'key' or a drawing id of this cut.")
        if c["boil"]:
            if any(d not in names for d in c["boil"].get("drawings") or []) or len(c["boil"].get("drawings") or []) < 2:
                errors.append(f"{where}.boil.drawings: two or more of 'key' / drawing ids of this cut.")
            after = c["boil"].get("after")
            if after and not _is_time(after.get("at")):
                errors.append(f"{where}.boil.after.at: a time.")
        cam = c["camera"]
        if len(cam["zoom"]) != 2 or min(cam["zoom"]) < 1.0:
            errors.append(f"{where}.camera.zoom: [start, end], both >= 1.0 (below 1 shows past the drawing).")
        if cam.get("slam") and not _is_time(cam["slam"].get("at")):
            errors.append(f"{where}.camera.slam.at: a time.")
        if c["acting"].get("type") == "strike" and not _is_time(c["acting"]["at"]):
            errors.append(f"{where}.acting.at: the strike's impact time.")
        cuts.append(c)
    if not cuts:
        errors.append("cuts: at least one cut.")
    elif cuts[0]["at"] not in (0, 0.0):
        errors.append("cuts[0].at must be 0.")
    if errors:
        raise PipelineError(f"{path}:\n  " + "\n  ".join(errors))
    spec["cuts"] = cuts
    spec["_out_dir"] = resolve_path(base, spec["image_generation"]["output_dir"])
    return spec, base


def shot_view(spec: dict) -> dict:
    """The character_shot-shaped dict its storyboard/voice helpers expect."""
    return {"id": spec["id"], "duration": float(spec["duration"]), "line": spec["line"], "sfx": spec["sfx"],
            "fx": spec["fx"], "render": {"resolution": [1920, 1080], "fps": int(spec["fps"])}}


# --------------------------------------------------------------------------- #
# Drawings: keyframes (image_generator jobs) + masked redraws
# --------------------------------------------------------------------------- #
def _sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


def keyframe_job(spec: dict, cut: dict) -> tuple[dict, Path]:
    from image_generator import build_job, image_gen_config

    cfg = image_gen_config({"image_generation": {k: v for k, v in spec["image_generation"].items()
                                                 if k != "output_dir"}})
    kf = cut["keyframe"]
    name = spec["character"]["name"]
    scene = {"id": kf["id"], "generate": {"characters": [name], "prompt": kf["prompt"], "seed": kf.get("seed")}}
    job = build_job(scene, cfg, {name: {"tags": spec["character"]["tags"]}})
    return job, spec["_out_dir"] / f"{kf['id']}.png"


def mask_image(shapes: list, size: tuple[int, int], feather: float):
    from PIL import Image, ImageDraw, ImageFilter

    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    w, h = size
    for kind, (cx, cy, rx, ry) in shapes:
        box = [(cx - rx) * w, (cy - ry) * h, (cx + rx) * w, (cy + ry) * h]
        (d.ellipse if kind == "ellipse" else d.rectangle)(box, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(feather))
    return Image.merge("RGB", (m, m, m))


def redraw_job(spec: dict, cut: dict, d: dict) -> dict:
    """Everything that determines a redraw; the hash includes the base image's bytes."""
    out = spec["_out_dir"]
    base_id = cut["keyframe"]["id"] if d.get("from", "key") == "key" else d["from"]
    base = out / f"{base_id}.png"
    rd = spec["redraw"]
    positive = ", ".join(x for x in (rd["quality_tags"], spec["character"]["tags"], d["prompt"]) if x)
    job = {"positive": positive, "negative": rd["negative"], "seed": int(d.get("seed", 1)),
           "denoise": float(d.get("denoise", 0.75)), "fresh": bool(d.get("fresh", False)),
           "feather": float(d.get("feather", 18)), "shapes": d["shapes"], "steps": int(rd["steps"]),
           "cfg": float(rd["cfg"]), "sampler": rd["sampler"], "scheduler": rd["scheduler"],
           "base": base_id, "base_sha": _sha(base) if base.is_file() else None}
    job["hash"] = hashlib.sha1(json.dumps(job, sort_keys=True).encode()).hexdigest()[:16]
    return job


def redraw_graph(job: dict, ckpt: str, image: str, mask: str, prefix: str) -> dict:
    """Masked redraw with core nodes; ImageCompositeMasked keeps every pixel outside the mask."""
    g = {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["ckpt", 1], "text": job["positive"]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["ckpt", 1], "text": job["negative"]}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image}},
        "msk": {"class_type": "LoadImageMask", "inputs": {"image": mask, "channel": "red"}},
        "ks": {"class_type": "KSampler", "inputs": {
            "model": ["ckpt", 0], "positive": ["pos", 0], "negative": ["neg", 0], "seed": job["seed"],
            "steps": job["steps"], "cfg": job["cfg"], "sampler_name": job["sampler"],
            "scheduler": job["scheduler"], "denoise": job["denoise"]}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "comp": {"class_type": "ImageCompositeMasked", "inputs": {
            "destination": ["img", 0], "source": ["dec", 0], "x": 0, "y": 0, "resize_source": False,
            "mask": ["msk", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["comp", 0], "filename_prefix": prefix}},
    }
    if job["fresh"]:   # true inpaint: the masked area is blanked, so a NEW shape can be drawn (open -> closed)
        g["enc"] = {"class_type": "VAEEncodeForInpaint", "inputs": {
            "pixels": ["img", 0], "vae": ["ckpt", 2], "mask": ["msk", 0], "grow_mask_by": 8}}
        g["ks"]["inputs"]["latent_image"] = ["enc", 0]
    else:              # partial repaint: keeps the drawing's structure under the noise mask
        g["enc"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["img", 0], "vae": ["ckpt", 2]}}
        g["noise"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["enc", 0], "mask": ["msk", 0]}}
        g["ks"]["inputs"]["latent_image"] = ["noise", 0]
    return g


def _state(target: Path, job_hash: str, force: bool) -> str:
    """'ok' (up to date), 'keep' (user-supplied: no sidecar), or 'todo'."""
    if not target.is_file() or force:
        return "todo"
    side = target.with_suffix(".png.json")
    if not side.is_file():
        return "keep"
    return "ok" if load_json(side).get("hash") == job_hash else "todo"


def make_drawings(spec: dict, force: bool, keep_comfy: bool, dry: bool = False) -> list[str]:
    """Generate missing/changed keyframes and redraws in dependency order, in ONE ComfyUI session."""
    plan: list[tuple[str, dict, dict | None]] = []
    for cut in spec["cuts"]:
        plan.append(("key", cut, None))
        plan += [("redraw", cut, d) for d in cut["drawings"]]

    def status(kind: str, cut: dict, d: dict | None) -> tuple[str, dict, Path]:
        if kind == "key":
            job, target = keyframe_job(spec, cut)
        else:
            job, target = redraw_job(spec, cut, d), spec["_out_dir"] / f"{d['id']}.png"
        return _state(target, job["hash"], force), job, target

    pending = []
    for kind, cut, d in plan:
        st, _, target = status(kind, cut, d)
        if st == "keep":
            log.info("keep     %s (your own image, no sidecar; --force to redraw)", target.name)
        elif st == "todo":
            pending.append(target.stem)
    if dry or not pending:
        if not pending:
            log.info("All %d drawings up to date.", len(plan))
        return pending

    from comfy_client import DEFAULT_URL, ComfyServer
    from image_generator import build_graph, pick_checkpoint

    ensure_dir(spec["_out_dir"] / "_masks")
    with ComfyServer(DEFAULT_URL, None, keep_running=keep_comfy) as client:
        ckpt = pick_checkpoint(client.checkpoints(), spec["image_generation"].get("checkpoint"))
        for key, node, field in (("sampler", "KSampler", "sampler_name"), ("scheduler", "KSampler", "scheduler")):
            options = client.combo_options(node, field)
            if spec["redraw"][key] not in options:
                raise PipelineError(f"redraw.{key} '{spec['redraw'][key]}' unknown to ComfyUI: {', '.join(options)}")
        for kind, cut, d in plan:      # re-check in order: a redrawn base invalidates its redraws
            st, job, target = status(kind, cut, d)
            if st != "todo":
                continue
            t0 = time.time()
            if kind == "key":
                graph = build_graph(job, ckpt, f"cel_shot/{target.stem}")
            else:
                from PIL import Image

                base = spec["_out_dir"] / f"{job['base']}.png"
                if not base.is_file():
                    raise PipelineError(f"{target.name}: base drawing {base} is missing.")
                mask = spec["_out_dir"] / "_masks" / f"{target.stem}.png"
                mask_image(job["shapes"], Image.open(base).size, job["feather"]).save(mask)
                graph = redraw_graph(job, ckpt, client.upload_image(base, base.name),
                                     client.upload_image(mask, f"{target.stem}_mask.png"), f"cel_shot/{target.stem}")
            outputs = client.wait(client.submit(graph))
            images = (outputs.get("save") or {}).get("images") or []
            if not images:
                raise PipelineError(f"{target.name}: ComfyUI finished but returned no image.")
            target.write_bytes(client.download(images[0]))
            save_json(target.with_suffix(".png.json"),
                      {**job, "checkpoint": ckpt, "seconds": round(time.time() - t0, 1)})
            log.info("drew     %-24s %5.1fs", target.name, time.time() - t0)
    return pending


# --------------------------------------------------------------------------- #
# Mattes (rembg in its own env)
# --------------------------------------------------------------------------- #
def matte_path(spec: dict, cut: dict) -> Path:
    tag = "_bright" if cut["layer"] == "bright" else ""
    return spec["_out_dir"] / "_masks" / f"{cut['keyframe']['id']}{tag}_alpha.png"


def make_mattes(spec: dict, work: Path, force: bool) -> None:
    items = []
    for cut in spec["cuts"]:
        if cut["layer"] == "none":
            continue
        out = matte_path(spec, cut)
        if out.is_file() and not force:
            continue
        items.append({"image": str(spec["_out_dir"] / f"{cut['keyframe']['id']}.png"), "out": str(out),
                      "gamma": spec["matte"]["bright_gamma"] if cut["layer"] == "bright" else None})
    if not items:
        return
    py = os.environ.get("REMBG_PYTHON")
    if not py or not Path(py).is_file():
        raise PipelineError(
            "Character mattes need rembg in its own env. Once (PowerShell):\n"
            "  python -m venv $HOME\\tools\\rembg-env; & $HOME\\tools\\rembg-env\\Scripts\\pip install \"rembg[cpu]\"\n"
            "  setx REMBG_PYTHON \"$HOME\\tools\\rembg-env\\Scripts\\python.exe\"\n"
            "or set a cut's \"layer\": \"none\" to move the whole drawing instead.")
    job = save_json(work / "matte_job.json", {"model": spec["matte"]["model"], "items": items})
    log.info("Matting %d keyframe(s) with %s", len(items), spec["matte"]["model"])
    proc = subprocess.run([py, "-I", str(MATTE_WORKER), str(job)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(f"Matte worker failed ({proc.returncode}): {proc.stderr.strip()[-800:]}")


# --------------------------------------------------------------------------- #
# Voice-driven acting signals
# --------------------------------------------------------------------------- #
class Voice:
    """Loudness grid (10 ms), stressed-syllable onsets and the nod signal, all in shot time."""

    def __init__(self, voice: dict, offset: float, sentences: list[dict]):
        import numpy as np

        self.offset = offset
        self.speech_start = offset + float(sentences[0]["start"])
        self.speech_end = offset + float(sentences[-1]["end"])
        with wave.open(str(voice["audio"])) as wf:
            sr, nch = wf.getframerate(), wf.getnchannels()
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float32) / 32768
        if nch > 1:
            pcm = pcm.reshape(-1, nch).mean(1)
        hop = sr // 100
        n = len(pcm) // hop
        db = 20 * np.log10(np.sqrt((pcm[:n * hop].reshape(n, hop) ** 2).mean(1)) + 1e-6) if n else np.zeros(0)
        self.onsets: list[tuple[float, float]] = []
        if voice.get("fallback"):
            return
        for i in range(4, n):
            if db[i] > -27 and db[i] - db[i - 4:i].min() > 10:
                t0 = offset + i / 100
                if not self.onsets or t0 - self.onsets[-1][0] > 0.16:
                    self.onsets.append((t0, float(np.clip((db[i:i + 8].max() + 30) / 14, 0.35, 1.0))))

    def nod(self, t: float, tau: float = 0.10) -> float:
        """Damped impulse after every stressed syllable (the head leads the sound by 30 ms)."""
        v = 0.0
        for t0, a in self.onsets:
            d = t - t0 + 0.03
            if 0 < d < 8 * tau:
                v += a * (d / tau) * math.exp(1 - d / tau)
        return v


def noise(t: float, seed: int) -> float:
    """Smooth, non-repeating idle drift in -1..1 (sum of incommensurate sines)."""
    import numpy as np

    r = np.random.default_rng(seed)
    f, p = r.uniform(0.25, 0.9, 3), r.uniform(0, 6.28, 3)
    return float(sum(math.sin(2 * math.pi * fi * t + pi) for fi, pi in zip(f, p)) / 3)


def jitter(t: float, seed: int, fps: int, step: int = 2) -> tuple[float, float]:
    """Stepped tremble: a new random offset every `step` frames (drawn shake, not video shake)."""
    import numpy as np

    r = np.random.default_rng(seed * 1000 + int(t * fps) // step)
    return float(r.uniform(-1, 1)), float(r.uniform(-1, 1))


def _clamp01(u: float) -> float:
    return min(max(u, 0.0), 1.0)


def ease_io(u: float) -> float:
    u = _clamp01(u)
    return u * u * (3 - 2 * u)


def ease_in(u: float) -> float:
    return _clamp01(u) ** 3


def ease_out(u: float) -> float:
    return 1 - (1 - _clamp01(u)) ** 3


def back_out(u: float, s: float = 2.2) -> float:
    u = _clamp01(u) - 1
    return 1 + (s + 1) * u ** 3 + s * u ** 2


def strike_scale(a: dict, t: float) -> float:
    s0 = a["_at"] - float(a["travel"])
    return 1.0 if t < s0 else 1.0 + float(a["lunge"]) * ease_in((t - s0) / float(a["travel"]))


def layer_pose(cut: dict, t: float, v: Voice, fps: int) -> tuple:
    """Rigid pose (pivot_x, pivot_y, degrees, scale, dx, dy) of the character layer (or whole cel) at time t."""
    a, kind = cut["acting"], cut["acting"]["type"]
    px, py = a["pivot"]
    n = v.nod(t)
    if kind == "talk":
        inhale = ease_io((t - (v.speech_start - 0.30)) / 0.30) * (1 - ease_io((t - (v.speech_start + 0.05)) / 0.6))
        deg, px_down = a["head_down"]
        lift = dip = 0.0
        if a.get("lift_before_cut") is not None and cut["_end"] is not None:
            at = cut["_end"] - float(a["lift_before_cut"])
            lift = ease_io((t - at) / 0.30)                              # beat: she looks up before the cut
            dip = math.exp(-((t - (at - 0.02)) / 0.06) ** 2)              # tiny antic before the lift
        else:
            deg = px_down = 0.0
        sd, sp = a["sway"]
        return (px, py, deg * (1 - lift) + a["nod"][0] * n + sd * noise(t, 1) + 0.6 * dip, 1.0,
                sp * noise(t, 2), px_down * (1 - lift) + a["nod"][1] * n + 5 * dip - a["inhale_px"] * inhale)
    if kind == "strike":
        s1 = a["_at"]
        s0 = s1 - float(a["travel"])
        lean, (wx, wy), (fx, fy) = float(a["lean_deg"]), a["windup_px"], a["follow_px"]
        w = ease_io((t - cut["_at"]) / 0.18)
        if t < s0:                                                        # wind back: lean away
            deg, k, dx, dy = -lean * w, 1.0, wx * w, wy * w
        elif t < s1:                                                      # lunge into the camera
            u = ease_in((t - s0) / (s1 - s0))
            deg, k, dx, dy = -lean + 2 * lean * u, strike_scale(a, t), wx + (fx - wx) * u, wy + (fy - wy) * u
        else:                                                             # overshoot, settle
            e = ease_out((t - s1) / 0.40)
            deg = lean - 0.75 * lean * e
            k = 1 + float(a["lunge"]) - float(a["settle"]) * back_out((t - s1) / 0.40)
            dx, dy = fx * (1 - 0.6 * e), fy * (1 - 0.5 * e)
        return (px, py, deg + a["nod"][0] * n, k, dx, dy + a["nod"][1] * n)
    if kind == "shout":                                                   # whole cel: thrust, nods, tremble
        u = ease_out((t - cut["_at"]) / 0.18)
        jx, jy = jitter(t, 3, fps)
        rage = float(a["tremble"][0 if t < v.speech_end else 1])            # trembling, calmer after the line
        sd, sp = a["sway"]
        return (px, py, sd * noise(t, 6) - 0.6 * n, float(a["scale"]) + float(a["thrust"]) * back_out(u) + 0.01 * n,
                rage * jx + sp * noise(t, 5), float(a["nod_px"]) * n + rage * jy)
    pant = math.sin(2 * math.pi * (t - cut["_at"]) / float(a["period"]))   # stare: catching her breath
    sd, sp = a["sway"]
    return (px, py, sd * noise(t, 7), float(a["scale"]), sp * noise(t, 8), float(a["pant_px"]) * pant)


# --------------------------------------------------------------------------- #
# Frame compositor
# --------------------------------------------------------------------------- #
class Compositor:
    """Holds drawings, mattes and plates; renders one frame per call (camera + rigid pose = one resample)."""

    W, H = 1920, 1080

    def __init__(self, spec: dict, voice: Voice, flaps: list[dict]):
        import numpy as np
        from PIL import Image, ImageFilter

        self.np, self.Image, self.ImageFilter = np, Image, ImageFilter
        self.spec, self.voice, self.flaps = spec, voice, flaps
        self.fps, self.on = int(spec["fps"]), int(spec["on"])
        self.img: dict[str, object] = {}
        self.alpha: dict[str, object] = {}
        self.plates: dict[tuple, object] = {}
        size = None
        for cut in spec["cuts"]:
            names = {cut["keyframe"]["id"], *(d["id"] for d in cut["drawings"])}
            for name in names:
                im = Image.open(spec["_out_dir"] / f"{name}.png").convert("RGB")
                if size and im.size != size:
                    raise PipelineError(f"{name}.png is {im.size}, other drawings are {size}: all must match.")
                size = im.size
                self.img[name] = np.asarray(im, np.float32) / 255
            if cut["layer"] != "none":
                self.alpha[cut["id"]] = self._load_alpha(cut)
        self.SW, self.SH = size
        uu = np.arange(self.W, dtype=np.float32) + 0.5
        vv = np.arange(self.H, dtype=np.float32) + 0.5
        self.UU, self.VV = np.meshgrid(uu, vv)
        emb = spec["comp"]["embers"]
        rng = np.random.default_rng(int(emb.get("seed", 7)))
        n = int(emb.get("count", 70))
        self.emb = dict(x=rng.random(n), y=rng.random(n), v=0.04 + 0.08 * rng.random(n), r=1.5 + 3.5 * rng.random(n),
                        ph=rng.random(n) * 6.28, a=0.5 + 0.5 * rng.random(n))

    # ---- assets
    def _load_alpha(self, cut: dict):
        np, Image, ImageFilter = self.np, self.Image, self.ImageFilter
        a = np.asarray(Image.open(matte_path(self.spec, cut)).convert("L"), np.float32) / 255
        h, w = a.shape
        for x0, y0, x1, y1 in cut["matte_ignore"]:
            a[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = 0.0
        lo, hi = self.spec["matte"]["edge"]
        a = np.clip((a - lo) / (hi - lo), 0, 1)                          # crisp cel edge, no half-transparent body
        im = Image.fromarray((a * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(self.spec["matte"]["soften"]))
        return np.asarray(im, np.float32) / 255

    def _push_pull(self, img, known):
        """Fill unknown pixels from their surroundings (smooth pyramid average): the plate behind the character."""
        np, Image = self.np, self.Image
        if min(img.shape[:2]) < 4:
            k = known.sum()
            return np.broadcast_to((img * known[..., None]).sum((0, 1)) / max(k, 1e-6), img.shape).copy()
        h2, w2 = img.shape[0] // 2 * 2, img.shape[1] // 2 * 2
        pi = (img * known[..., None])[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2, 3).sum((1, 3))
        pk = known[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2).sum((1, 3))
        coarse = self._push_pull(pi / np.maximum(pk, 1e-6)[..., None], (pk > 0).astype(np.float32))
        up = np.stack([np.asarray(Image.fromarray(coarse[..., c]).resize((img.shape[1], img.shape[0]), Image.BILINEAR))
                       for c in range(3)], axis=-1)
        return img * known[..., None] + up * (1 - known[..., None])

    def plate(self, cut: dict, key: int, img):
        if (cut["id"], key) not in self.plates:
            np, Image = self.np, self.Image
            grow = int(self.spec["matte"]["grow"]) | 1
            a = Image.fromarray((self.alpha[cut["id"]] * 255).astype(np.uint8)).filter(self.ImageFilter.MaxFilter(grow))
            known = (np.asarray(a, np.float32) / 255 < 0.03).astype(np.float32)
            self.plates[(cut["id"], key)] = self._push_pull(img, known)
        return self.plates[(cut["id"], key)]

    # ---- timing
    def shape_at(self, t: float) -> str:
        lt = t - self.voice.offset
        for c in self.flaps:
            if c["start"] <= lt < c["end"]:
                return c["value"]
        return "X"

    def cut_at(self, t: float) -> dict:
        return [c for c in self.spec["cuts"] if c["_at"] <= t + 1e-6][-1]

    def acting_time(self, cut: dict, t: float) -> float:
        a = cut["acting"]
        if a["type"] == "strike" and a["_at"] - float(a["travel"]) - 1e-6 <= t < a["_at"] + 0.10:
            return t                                                      # the strike runs on ones
        return math.floor(t * self.fps / self.on + 1e-6) * self.on / self.fps

    def boil_index(self, cut: dict, ta: float) -> int:
        b = cut["boil"]
        if not b:
            return 0
        step = int(b.get("on", 3))
        if b.get("after") and ta >= b["after"]["_at"]:
            step = int(b["after"].get("on", 2))
        return int((ta * self.fps) // step) % len(b["drawings"])

    def drawing(self, cut: dict, ta: float):
        key = cut["keyframe"]["id"]

        def name(n: str) -> str:
            return key if n == "key" else n

        img = self.img[key]
        m = cut["mouth"]
        if m:
            sh = self.shape_at(ta)
            if sh in "XA":
                pick = m["closed"]
            elif sh in "BGF":
                pick = m.get("half") or m["open"]
            else:
                pick = m["open"]
            img = self.img[name(pick)]
        bi = self.boil_index(cut, ta)
        if cut["boil"]:
            boil = name(cut["boil"]["drawings"][bi])
            if boil != key:
                img = img + (self.img[boil] - self.img[key])              # redraws only differ inside their masks
        return img, bi

    # ---- sampling
    def cam_coords(self, z: float, cx: float, cy: float):
        cover = max(self.W / self.SW, self.H / self.SH)
        bw, bh = self.W / (cover * z), self.H / (cover * z)
        x0 = min(max(cx * self.SW - bw / 2, 0), self.SW - bw)
        y0 = min(max(cy * self.SH - bh / 2, 0), self.SH - bh)
        return x0 + self.UU * (bw / self.W) - 0.5, y0 + self.VV * (bh / self.H) - 0.5

    def sample(self, img, x, y):
        np = self.np
        x = np.clip(x, 0, self.SW - 1.001)
        y = np.clip(y, 0, self.SH - 1.001)
        x0, y0 = x.astype(np.int32), y.astype(np.int32)
        fx, fy = (x - x0)[..., None], (y - y0)[..., None]
        a, b = img[y0, x0], img[y0, x0 + 1]
        c, d = img[y0 + 1, x0], img[y0 + 1, x0 + 1]
        return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy

    def pose_map(self, pose: tuple, x, y, k_override: float | None = None):
        """Inverse rigid map (rotation + uniform scale + shift): output coords -> drawing coords. No bending."""
        px, py, deg, k, dx, dy = pose
        if k_override is not None:
            k = k_override
        px, py = px * self.SW, py * self.SH
        a = -math.radians(deg)
        c, s = math.cos(a), math.sin(a)
        ux, uy = x - px - dx, y - py - dy
        return px + (c * ux - s * uy) / k, py + (s * ux + c * uy) / k

    def embers(self, t: float):
        np, W, H, e = self.np, self.W, self.H, self.emb
        layer = np.zeros((H, W, 3), np.float32)
        ys = (e["y"] - e["v"] * t) % 1.0
        xs = (e["x"] + 0.015 * np.sin(e["ph"] + t * 1.7)) % 1.0
        for x, y, r, a, ph in zip(xs * W, ys * H, e["r"], e["a"], e["ph"]):
            flick = a * (0.7 + 0.3 * math.sin(ph + t * 9))
            x0, x1 = int(max(0, x - 3 * r)), int(min(W, x + 3 * r))
            y0, y1 = int(max(0, y - 3 * r)), int(min(H, y + 3 * r))
            if x1 <= x0 or y1 <= y0:
                continue
            gy, gx = np.mgrid[y0:y1, x0:x1]
            g = np.exp(-((gx - x) ** 2 + (gy - y) ** 2) / (2 * (r * 0.6) ** 2)) * flick
            layer[y0:y1, x0:x1] += g[..., None] * np.array([1.0, 0.62, 0.25], np.float32)
        return layer

    def bloom(self, frame):
        np, Image, W, H = self.np, self.Image, self.W, self.H
        b = self.spec["comp"]["bloom"]
        th = float(b["threshold"])
        lum = frame.max(axis=2)
        bright = np.clip((lum - th) / max(1e-3, 1 - th), 0, 1)[..., None] * frame
        small = Image.fromarray((bright * 255).astype(np.uint8)).resize((W // 4, H // 4), Image.BILINEAR)
        glow = np.asarray(small.filter(self.ImageFilter.GaussianBlur(10)).resize((W, H), Image.BILINEAR),
                          np.float32) / 255
        return frame + glow * float(b["strength"])

    def render(self, t: float):
        np = self.np
        cut = self.cut_at(t)
        end = cut["_end"] if cut["_end"] is not None else float(self.spec["duration"])
        cam = cut["camera"]
        z0, z1 = cam["zoom"]
        u = (t - cut["_at"]) / max(1e-6, end - cut["_at"])
        z = z0 + (z1 - z0) * (1 - (1 - u) ** 2)                            # ease-out push, on ones
        slam = cam.get("slam")
        if slam and t >= slam["_at"]:
            k = min(1.0, (t - slam["_at"]) / float(slam.get("settle", 0.18)))
            z *= 1.0 + float(slam.get("amount", 0.10)) * (1 - k) ** 2
        ta = self.acting_time(cut, t)
        img, bi = self.drawing(cut, ta)
        x, y = self.cam_coords(z, *cam["center"])
        pose = layer_pose(cut, ta, self.voice, self.fps)
        a = cut["acting"]
        if cut["id"] in self.alpha:                                       # character layer over its clean plate
            alpha = self.alpha[cut["id"]][..., None]
            bg = self.sample(self.plate(cut, bi, img), x, y)
            smear = a["type"] == "strike" and a["_at"] - float(a["travel"]) <= t < a["_at"]
            taps = [ta - (1 / self.fps) * j / 5 for j in range(int(a["smear_taps"]))] if smear else [None]
            fg = np.zeros((self.H, self.W, 3), np.float32)
            al = np.zeros((self.H, self.W, 1), np.float32)
            for tt in taps:                                               # smear = rigid layer averaged along travel
                sx, sy = self.pose_map(pose, x, y, None if tt is None else strike_scale(a, tt))
                m = self.sample(alpha, sx, sy)
                fg += self.sample(img, sx, sy) * m
                al += m
            fr = fg / len(taps) + bg * (1 - al / len(taps))
        else:
            fr = self.sample(img, *self.pose_map(pose, x, y))
        return np.clip(self.bloom(fr) + self.embers(t) * float(cut["embers"]), 0, 1)

    def save(self, frame, path: Path) -> None:
        self.Image.fromarray((frame * 255 + 0.5).astype(self.np.uint8)).save(path, compress_level=1)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def resolve_at(v: object, sentences: list[dict], offset: float, speech_end: float) -> float:
    from character_shot import resolve_time

    if isinstance(v, (int, float)):
        return float(v)
    if "after_speech" in v:
        return speech_end + float(v["after_speech"])
    return resolve_time(v, sentences, offset)


def resolve_times(spec: dict, sentences: list[dict], offset: float, speech_end: float) -> None:
    cuts = spec["cuts"]
    for c in cuts:
        c["_at"] = resolve_at(c["at"], sentences, offset, speech_end)
        if c["acting"]["type"] == "strike":
            c["acting"]["_at"] = resolve_at(c["acting"]["at"], sentences, offset, speech_end)
        if c["camera"].get("slam"):
            c["camera"]["slam"]["_at"] = resolve_at(c["camera"]["slam"]["at"], sentences, offset, speech_end)
        if c["boil"] and c["boil"].get("after"):
            c["boil"]["after"]["_at"] = resolve_at(c["boil"]["after"]["at"], sentences, offset, speech_end)
    for i, c in enumerate(cuts):
        c["_end"] = cuts[i + 1]["_at"] if i + 1 < len(cuts) else None
        if c["_end"] is not None and c["_end"] <= c["_at"]:
            raise PipelineError(f"cut '{cuts[i + 1]['id']}' starts at {c['_end']:.2f}s, not after "
                                f"'{c['id']}' ({c['_at']:.2f}s): cuts must be in time order.")
    if cuts[-1]["_at"] >= float(spec["duration"]):
        raise PipelineError(f"last cut starts at {cuts[-1]['_at']:.2f}s, after the shot ends ({spec['duration']}s).")


def frames_hash(spec: dict, voice: dict) -> str:
    h = hashlib.sha1(json.dumps({k: v for k, v in spec.items() if k != "_out_dir"}, sort_keys=True,
                                default=str).encode())
    h.update(Path(voice["audio"]).read_bytes())
    for cut in spec["cuts"]:
        for name in [cut["keyframe"]["id"], *(d["id"] for d in cut["drawings"])]:
            h.update(_sha(spec["_out_dir"] / f"{name}.png").encode())
        if cut["layer"] != "none":
            h.update(_sha(matte_path(spec, cut)).encode())
    return h.hexdigest()[:16]


def run(args: argparse.Namespace) -> Path | None:
    import character_shot as cs

    spec, base = load_spec(args.spec)
    work = ensure_dir(TEMP_DIR / "cel_shots" / spec["id"])
    frames_dir = work / "frames"
    output = args.output or (OUTPUT_DIR / spec["id"] / f"{spec['id']}{'_draft' if args.draft else ''}.mp4")
    shot = shot_view(spec)

    if args.dry_run:
        pending = make_drawings(spec, args.force, False, dry=True)
        print(f"Spec OK: {len(spec['cuts'])} cuts, {len(pending)} drawing(s) to generate"
              + (f" ({', '.join(pending)})" if pending else "") + ".")
        return None

    probe = cs.build_storyboard(shot, base, frames_dir, output, [0.0] * len(spec["sfx"]))
    voice = cs.synthesize(probe, args.tts_fallback)
    sentences = voice.get("boundaries") or [{"start": 0.0, "end": voice["duration"]}]
    offset = float(spec["line"]["offset"])
    v = Voice(voice, offset, sentences)
    resolve_times(spec, sentences, offset, v.speech_end)
    log.info("Voice %.2fs, speech %.2f-%.2fs; cuts at %s", voice["duration"], v.speech_start, v.speech_end,
             ", ".join(f"{c['id']} {c['_at']:.2f}s" for c in spec["cuts"]))

    make_drawings(spec, args.force_drawings, args.keep_comfy)
    make_mattes(spec, work, args.force_drawings)

    flaps: list[dict] = []
    cues = cs.rhubarb_cues(voice, spec["line"]["recognizer"], work)
    if cues:
        flaps = cs.drawn_mouth_cues(cues, Path(voice["audio"]), offset, float(spec["duration"]), int(spec["fps"]),
                                    int(spec["on"]), spec["line"]["mouth_source"], float(spec["line"]["mouth_lead"]))
    comp = Compositor(spec, v, flaps)

    if args.stills:
        for t in args.stills:
            p = work / f"still_{t:05.2f}.png"
            comp.save(comp.render(t), p)
            print(p)
        return None

    stamp = work / "frames.hash"
    key = frames_hash(spec, voice)
    total = int(round(float(spec["duration"]) * int(spec["fps"])))
    if args.force or not stamp.is_file() or stamp.read_text() != key or len(list_frames(frames_dir)) != total:
        ensure_dir(frames_dir)
        for old in frames_dir.glob("frame_*.png"):
            old.unlink()
        t0 = time.time()
        for f in range(1, total + 1):
            comp.save(comp.render((f - 1) / int(spec["fps"])), frames_dir / f"frame_{f:05d}.png")
            if f % 24 == 0:
                log.info("frame %d/%d (%.2f s/frame)", f, total, (time.time() - t0) / f)
        stamp.write_text(key)
    else:
        log.info("Frames up to date (%d).", total)
    if args.frames_only:
        return None

    sfx_times = [cs.resolve_time(c, sentences, offset) for c in spec["sfx"]]
    shot["fx"] = cs.resolve_fx(spec["fx"], sentences, offset)
    storyboard = cs.build_storyboard(shot, base, frames_dir, output, sfx_times)
    sb_path = save_json(work / f"{spec['id']}.storyboard.json", storyboard)
    from video_orchestrator import main as orchestrate

    argv = [str(sb_path), "-o", str(output), "--tts-fallback", args.tts_fallback]
    if args.draft:
        argv.append("--draft")
    if orchestrate(argv) != 0:
        raise PipelineError(f"Video render failed for storyboard {sb_path}")
    return output


def _times(s: str) -> list[float]:
    try:
        return [float(x) for x in s.split(",") if x.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("comma-separated seconds, e.g. 0.5,3.2") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a 2D cel anime shot (drawn cuts, rigid acting, lip sync).")
    p.add_argument("spec", type=Path, help="Shot spec JSON (see cel_shot_template.json).")
    p.add_argument("-o", "--output", type=Path, help="Output MP4 (default output/<id>/<id>.mp4).")
    p.add_argument("--dry-run", action="store_true", help="Validate the spec and list drawings to generate.")
    p.add_argument("--stills", type=_times, help="Only render these times (seconds) to PNGs for review.")
    p.add_argument("--draft", action="store_true", help="Half-resolution video encode (frames stay full size).")
    p.add_argument("--frames-only", action="store_true", help="Stop after the frames.")
    p.add_argument("--force", action="store_true", help="Re-render frames even if nothing changed.")
    p.add_argument("--force-drawings", action="store_true",
                   help="Redraw every keyframe/redraw and re-matte (overwrites images you supplied).")
    p.add_argument("--keep-comfy", action="store_true", help="Leave an auto-started ComfyUI running.")
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
