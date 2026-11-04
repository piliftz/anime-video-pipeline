#!/usr/bin/env python3
"""Storyboard -> finished 1080p/24fps MP4.

Pipeline
  1. Load + validate the storyboard JSON (defaults merged, scenes sorted by 'order').
  2. Generate narration with edge-tts (cached in temp/voiceovers/).
  3. Plan the timeline: scene durations from narration length / explicit duration /
     clip length, transitions (cut, fade_black, crossfade).
  4. Build each scene: load clip/image/frame-sequence -> trim/speed/extend ->
     fit + Ken Burns -> color grade -> speed lines -> text overlays -> fades.
  5. Mix audio: narration + optional clip audio + BGM with automatic ducking.
  6. Render ONE lossless RGB master (libx264rgb -qp 0, PCM audio) in temp/.
  7. Final single lossy encode: burn ASS subtitles -> BT.709 yuv420p H.264
     (CRF 16, tune=animation) + AAC -> output MP4; export SRT alongside.

Examples
  python scripts/video_orchestrator.py storyboard_template.json
  python scripts/video_orchestrator.py storyboard_template.json --draft
  python scripts/video_orchestrator.py storyboard_template.json --dry-run
  python scripts/video_orchestrator.py storyboard_template.json --tts-fallback silence --allow-missing
  python scripts/video_orchestrator.py storyboard_template.json --scenes scene_01,scene_03 -o output/test.mp4
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from image_generator import default_generated_clip, image_gen_config, validate_generate
from pipeline_utils import (
    TEMP_DIR,
    PipelineError,
    classify_media,
    deep_merge,
    ensure_dir,
    format_seconds,
    get_logger,
    load_json,
    load_pil_font,
    media_duration,
    parse_color,
    require_binary,
    resolve_path,
    save_json,
    setup_logging,
)

log = get_logger("orchestrator")

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
PROJECT_DEFAULTS = {
    "title": "Untitled Anime Short",
    "output": "output/final.mp4",
    "resolution": [1920, 1080],
    "fps": 24,
    "fit": "cover",
    "background_color": "#000000",
    "crf": 16,
    "preset": "slow",
    "tune": "animation",
    "audio_bitrate": "192k",
    "loudness_lufs": -14.0,    # final mix loudness target (EBU R128); null = leave the mix untouched
}
AUDIO_DEFAULTS = {
    "narration_volume": 1.0,
    "clip_audio_volume": 0.6,
    "bgm": {
        "path": None, "volume": 0.25, "loop": True,
        "duck_to": 0.35,       # BGM gain multiplier while narration plays
        "duck_fade": 0.4,      # seconds to ramp down/up around narration
        "fade_in": 1.0, "fade_out": 2.0,
    },
    "music": [],                # list of MUSIC_CUE_DEFAULTS-shaped cues, placed by scene id or seconds
    "require_license": True,    # every audio file must be registered in assets/audio/manifest.json
}
MUSIC_CUE_DEFAULTS = {
    "file": None,
    "start_scene": None, "end_scene": None,   # place by scene ids (end_scene inclusive)...
    "start": 0.0, "end": None,                # ...or by absolute seconds
    "volume": 0.3, "loop": True,
    "fade_in": 1.0, "fade_out": 1.0,
    "duck_to": 0.4, "duck_fade": 0.3,         # gain while someone speaks
    "drops": [],                              # [{"start_scene"/"end_scene" or "start"/"end"}]: music cut to silence
}
SFX_CUE_DEFAULTS = {"file": None, "at": 0.0, "volume": 1.0}
TRANSITION_DEFAULTS = {"type": "cut", "duration": 0.5}
FX_DEFAULTS = {
    "ken_burns": {"enabled": False},
    "color_grade": {"enabled": False},
    "speed_lines": {"enabled": False},
    "anime_timing": {"enabled": False, "on": 2},   # hold each drawing 2 (twos) or 3 (threes) frames
    "shake": {"enabled": False, "amount": 10.0, "fps": 12},  # continuous energy rumble
    "impact": {"enabled": False, "at": [0.0]},     # inverted frame + flash + shake at these seconds
    "fade_in": 0.0,
    "fade_out": 0.0,
}
SCENE_DEFAULTS = {
    "id": None,
    "order": None,
    "clip": None,
    "duration": None,
    "min_duration": 2.0,
    "clip_start": 0.0,
    "clip_end": None,
    "playback_speed": 1.0,
    "extend": "loop",          # loop | boomerang | freeze  (when the clip is shorter than the scene)
    "sequence_fps": 8,         # for frame directories (AnimateDiff default output is 8 fps)
    "fit": None,               # override project fit
    "keep_clip_audio": False,
    "narration": None,
    "fx": {},
    "text_overlays": [],
    "transition_in": None,     # {"type": "crossfade"|"fade_black"|"cut", "duration": s}
    "generate": None,          # {"prompt", "characters", "negative", "seed", "width", "height", "rating"}
    "sfx": [],                 # [{"file", "at": seconds into the scene, "volume"}]
}
NARRATION_DEFAULTS = {"text": "", "speaker": None, "offset": 0.3, "tail": 0.6, "subtitle": True,
                      "subtitle_text": None}  # show different text than is spoken (e.g. JA dub, EN subs)
OVERLAY_DEFAULTS = {
    "text": "",
    "start": 0.0,
    "end": None,
    "position": "center",      # center|top|bottom|top_left|top_right|bottom_left|bottom_right or [x, y]
    "font": None,
    "font_size": 96,
    "color": "#FFFFFF",
    "stroke_color": "#1A1033",
    "stroke_width": 6,
    "shadow_color": "#000000",
    "shadow_offset": [4, 6],
    "shadow_opacity": 0.55,
    "box_color": None,
    "box_opacity": 0.5,
    "box_padding": 24,
    "align": "center",
    "max_width": 0.8,          # fraction of canvas width before wrapping
    "line_spacing": 10,
    "fade_in": 0.3,
    "fade_out": 0.3,
    "animation": "fade",       # fade | slide_up | none
    "margin": 80,
}
VALID_TRANSITIONS = {"cut", "fade_black", "crossfade"}
VALID_EXTEND = {"loop", "boomerang", "freeze"}


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class ScenePlan:
    index: int
    cfg: dict
    source: Path | None
    kind: str
    start: float = 0.0
    duration: float = 0.0
    natural_duration: float | None = None
    voice: dict | None = None
    transition: dict = field(default_factory=lambda: dict(TRANSITION_DEFAULTS))

    @property
    def id(self) -> str:
        return self.cfg["id"]

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def narration_start(self) -> float:
        return self.start + float(self.cfg["narration"]["offset"])


# --------------------------------------------------------------------------- #
# Storyboard loading & validation
# --------------------------------------------------------------------------- #
def load_storyboard(path: Path) -> tuple[dict, Path]:
    from subtitle_generator import SUBTITLE_DEFAULTS

    raw = load_json(path)
    if not isinstance(raw, dict):
        raise PipelineError("Storyboard root must be a JSON object.")
    sb = {
        "project": deep_merge(PROJECT_DEFAULTS, raw.get("project")),
        "voice_defaults": raw.get("voice_defaults", {}),
        "speakers": raw.get("speakers", {}),
        "audio": deep_merge(AUDIO_DEFAULTS, raw.get("audio")),  # music cues merged with defaults below
        "transitions": deep_merge({"default": TRANSITION_DEFAULTS}, raw.get("transitions")),
        "subtitles": deep_merge(SUBTITLE_DEFAULTS, raw.get("subtitles")),
        "fx_defaults": deep_merge(FX_DEFAULTS, raw.get("fx_defaults")),
        "characters": raw.get("characters", {}),
        "image_generation": image_gen_config(raw),
        "scenes": [],
    }
    sb["audio"]["music"] = [deep_merge(MUSIC_CUE_DEFAULTS, c) for c in (sb["audio"].get("music") or [])]
    scenes = raw.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise PipelineError("Storyboard needs a non-empty 'scenes' list.")
    for i, s in enumerate(scenes, start=1):
        if not isinstance(s, dict):
            raise PipelineError(f"Scene #{i} must be an object.")
        scene = deep_merge(SCENE_DEFAULTS, s)
        scene["id"] = str(scene["id"] or f"scene_{i:02d}")
        scene["_file_index"] = i
        if scene["generate"] and not scene["clip"]:
            scene["clip"] = default_generated_clip(raw, scene["id"])
        scene["fx"] = deep_merge(sb["fx_defaults"], s.get("fx"))
        nar = s.get("narration")
        if isinstance(nar, str):
            nar = {"text": nar}
        scene["narration"] = deep_merge(NARRATION_DEFAULTS, nar) if nar and (nar.get("text") or "").strip() else None
        scene["text_overlays"] = [deep_merge(OVERLAY_DEFAULTS, o) for o in (s.get("text_overlays") or [])]
        scene["sfx"] = [deep_merge(SFX_CUE_DEFAULTS, c) for c in (s.get("sfx") or [])]
        sb["scenes"].append(scene)
    sb["scenes"].sort(key=lambda sc: (sc["order"] if sc["order"] is not None else sc["_file_index"], sc["_file_index"]))
    return sb, path.resolve().parent


def validate_storyboard(sb: dict, base_dir: Path, allow_missing: bool) -> list[str]:
    """Return a list of human-readable problems. Empty list == valid."""
    from cinematic_fx import resolve_grade, resolve_ken_burns, validate_timing_and_impact

    errors: list[str] = []
    proj = sb["project"]
    w, h = proj["resolution"]
    if w % 2 or h % 2:
        errors.append(f"project.resolution must be even numbers for yuv420p (got {w}x{h}).")
    if float(proj["fps"]) <= 0:
        errors.append("project.fps must be > 0.")
    if proj["fit"] not in ("cover", "contain", "stretch"):
        errors.append("project.fit must be cover, contain or stretch.")
    lufs = proj.get("loudness_lufs")
    if lufs is not None and not -30 <= float(lufs) <= -5:
        errors.append(f"project.loudness_lufs must be between -30 and -5 (YouTube ~ -14), or null; got {lufs}.")
    seen: set[str] = set()
    for sc in sb["scenes"]:
        sid = sc["id"]
        if sid in seen:
            errors.append(f"Duplicate scene id '{sid}'.")
        seen.add(sid)
        src = resolve_path(base_dir, sc["clip"])
        if src is None:
            if not allow_missing:
                errors.append(f"[{sid}] has no 'clip' (use --allow-missing for a placeholder card).")
        elif not src.exists():
            if not allow_missing:
                hint = " (it has a 'generate' block: add --generate-images)" if sc["generate"] else ""
                errors.append(f"[{sid}] clip not found: {src}{hint}")
        if sc["generate"]:
            errors += validate_generate(sid, sc["generate"], sb["characters"])
        elif classify_media(src) == "unknown":
            errors.append(f"[{sid}] unsupported clip type: {src.suffix}")
        if sc["extend"] not in VALID_EXTEND:
            errors.append(f"[{sid}] extend must be one of {sorted(VALID_EXTEND)}.")
        if float(sc["playback_speed"]) <= 0:
            errors.append(f"[{sid}] playback_speed must be > 0.")
        if sc["duration"] is not None and float(sc["duration"]) <= 0:
            errors.append(f"[{sid}] duration must be > 0.")
        tr = sc["transition_in"]
        if tr and tr.get("type", "cut") not in VALID_TRANSITIONS:
            errors.append(f"[{sid}] transition_in.type must be one of {sorted(VALID_TRANSITIONS)}.")
        fx = sc["fx"]
        try:
            if fx["ken_burns"].get("enabled"):
                resolve_ken_burns(fx["ken_burns"])
            if fx["color_grade"].get("enabled"):
                resolve_grade(fx["color_grade"])
            if fx["speed_lines"].get("enabled"):
                style = fx["speed_lines"].get("style", "radial")
                if style not in ("radial", "horizontal", "vertical"):
                    raise PipelineError(f"speed_lines.style '{style}' invalid")
            validate_timing_and_impact(fx)
        except PipelineError as exc:
            errors.append(f"[{sid}] {exc}")
        nar = sc["narration"] or {}
        if nar.get("subtitle_text") is not None and not str(nar["subtitle_text"]).strip():
            errors.append(f"[{sid}] narration.subtitle_text is empty (remove it to subtitle the spoken text).")
        for j, ov in enumerate(sc["text_overlays"]):
            if not str(ov["text"]).strip():
                errors.append(f"[{sid}] text_overlays[{j}] has empty text.")
    errors += validate_audio(sb, base_dir)
    return errors


# --------------------------------------------------------------------------- #
# Timeline planning
# --------------------------------------------------------------------------- #
def probe_natural_duration(plan: ScenePlan) -> float | None:
    cfg = plan.cfg
    if plan.kind == "video":
        d = media_duration(plan.source)
    elif plan.kind == "sequence":
        from pipeline_utils import list_frames

        d = len(list_frames(plan.source)) / float(cfg["sequence_fps"])
    else:
        return None
    start = float(cfg["clip_start"] or 0)
    end = float(cfg["clip_end"]) if cfg["clip_end"] is not None else d
    return max(0.0, min(end, d) - start) / float(cfg["playback_speed"])


def plan_timeline(sb: dict, base_dir: Path, voices: dict[str, dict]) -> list[ScenePlan]:
    plans: list[ScenePlan] = []
    default_tr = sb["transitions"]["default"]
    for i, sc in enumerate(sb["scenes"]):
        src = resolve_path(base_dir, sc["clip"])
        kind = classify_media(src) if src is not None and src.exists() else "placeholder"
        plan = ScenePlan(index=i, cfg=sc, source=src if kind != "placeholder" else None, kind=kind)
        plan.natural_duration = probe_natural_duration(plan) if kind in ("video", "sequence") else None
        plan.voice = voices.get(sc["id"]) if sc["narration"] else None
        plan.transition = deep_merge(default_tr, sc["transition_in"]) if i > 0 else {"type": "cut", "duration": 0.0}

        narration_end = 0.0
        if plan.voice:
            nar = sc["narration"]
            narration_end = float(nar["offset"]) + float(plan.voice["duration"]) + float(nar["tail"])
        if sc["duration"] is not None:
            duration = float(sc["duration"])
            if narration_end > duration:
                log.warning("[%s] narration needs %.2fs but duration is %.2fs -> extending scene.",
                            sc["id"], narration_end, duration)
                duration = narration_end
        elif plan.voice:
            duration = max(float(sc["min_duration"]), narration_end)
        else:
            duration = max(float(sc["min_duration"]), plan.natural_duration or 0.0)
        plan.duration = round(duration, 4)
        plans.append(plan)

    t = 0.0
    for i, plan in enumerate(plans):
        tr = plan.transition
        if i > 0 and tr["type"] == "crossfade":
            d = min(float(tr["duration"]), plans[i - 1].duration / 2, plan.duration / 2)
            tr["duration"] = d
            t -= d
        plan.start = round(t, 4)
        t = plan.end
    return plans


def timeline_dict(sb: dict, plans: list[ScenePlan], canvas: tuple[int, int], storyboard_path: Path) -> dict:
    scenes = []
    for p in plans:
        entry = {"id": p.id, "start": p.start, "duration": p.duration, "end": p.end,
                 "source": str(p.source) if p.source else None, "kind": p.kind,
                 "transition_in": p.transition, "narration": None}
        if p.voice:
            entry["narration"] = {
                "start": round(p.narration_start, 4), "duration": p.voice["duration"],
                "text": p.voice["text"], "speaker": p.cfg["narration"].get("speaker"),
                "boundaries": p.voice.get("boundaries"), "audio": p.voice["audio"],
                "subtitle": p.cfg["narration"].get("subtitle", True),
                "placeholder_audio": bool(p.voice.get("fallback")),
            }
            sub_text = p.cfg["narration"].get("subtitle_text")
            if sub_text:
                from voiceover_generator import proportional_boundaries

                entry["narration"]["spoken_text"] = entry["narration"]["text"]
                entry["narration"]["text"] = sub_text
                entry["narration"]["boundaries"] = proportional_boundaries(sub_text, float(p.voice["duration"]))
        scenes.append(entry)
    return {
        "title": sb["project"]["title"], "storyboard": str(storyboard_path.resolve()),
        "canvas": list(canvas), "fps": sb["project"]["fps"],
        "duration": round(plans[-1].end, 4) if plans else 0.0, "scenes": scenes,
    }


def print_timeline(plans: list[ScenePlan]) -> None:
    print(f"\n{'#':>2}  {'scene':<16} {'start':>9} {'dur':>7}  {'kind':<11} {'trans':<11} narration")
    for p in plans:
        nar = ""
        if p.voice:
            flag = " (placeholder)" if p.voice.get("fallback") else ""
            nar = f"{p.voice['duration']:.2f}s{flag}: {p.voice['text'][:40]}"
        tr = p.transition["type"] if p.index else "-"
        print(f"{p.index + 1:>2}  {p.id:<16} {format_seconds(p.start):>9} {p.duration:>6.2f}s  {p.kind:<11} {tr:<11} {nar}")
    if plans:
        print(f"\nTotal duration: {format_seconds(plans[-1].end)}\n")


# --------------------------------------------------------------------------- #
# Scene building
# --------------------------------------------------------------------------- #
def placeholder_image(scene_id: str, canvas: tuple[int, int], index: int) -> np.ndarray:
    W, H = canvas
    hues = [(46, 26, 92), (16, 64, 96), (96, 24, 64), (20, 80, 60), (90, 60, 20)]
    top = np.array(hues[index % len(hues)], np.float32)
    bottom = top * 0.25
    grad = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    arr = (top * (1 - grad) + bottom * grad).repeat(W, axis=1).astype(np.uint8)
    img = Image.fromarray(arr)
    draw = ImageDraw.Draw(img)
    font = load_pil_font(None, int(H * 0.07))
    label = f"{scene_id}\n(missing clip)"
    bbox = draw.multiline_textbbox((0, 0), label, font=font, align="center")
    pos = ((W - (bbox[2] - bbox[0])) / 2, (H - (bbox[3] - bbox[1])) / 2)
    draw.multiline_text(pos, label, font=font, fill=(255, 255, 255), align="center",
                        stroke_width=max(2, H // 300), stroke_fill=(0, 0, 0))
    return np.asarray(img)


def load_scene_source(plan: ScenePlan, canvas: tuple[int, int]):
    from moviepy import ImageClip

    from cinematic_fx import load_source

    cfg = plan.cfg
    if plan.kind == "placeholder":
        return ImageClip(placeholder_image(plan.id, canvas, plan.index), duration=plan.duration)
    clip = load_source(plan.source, plan.duration, float(cfg["sequence_fps"]), with_audio=bool(cfg["keep_clip_audio"]))
    if plan.kind in ("video", "sequence"):
        start = float(cfg["clip_start"] or 0)
        end = float(cfg["clip_end"]) if cfg["clip_end"] is not None else None
        if start or end is not None:
            clip = clip.subclipped(start, min(end, clip.duration) if end is not None else None)
        speed = float(cfg["playback_speed"])
        if speed != 1.0:
            clip = clip.with_speed_scaled(speed)
    return clip


def fit_duration(clip, duration: float, mode: str):
    """Trim, loop, ping-pong or freeze *clip* to exactly *duration* seconds.

    Time is remapped explicitly (and clamped to the last frame) instead of using
    moviepy's Loop/TimeSymmetrize, whose float math can request t < 0 or
    t == duration and crash frame-indexed sources such as ImageSequenceClip.
    """
    from moviepy import ImageClip

    if isinstance(clip, ImageClip) or clip.duration is None:  # stills: any length is fine
        return clip.with_duration(duration)
    D = float(clip.duration)
    if D >= duration - 1e-3:
        return clip.subclipped(0, duration)
    fps = float(getattr(clip, "fps", None) or 24)
    last = max(0.0, D - 0.5 / fps)

    if mode == "loop":
        def remap(t):
            return np.clip(np.mod(t, D), 0.0, last)
        apply_to = ["mask", "audio"]
    elif mode == "boomerang":
        def remap(t):
            u = np.mod(t, 2 * D)
            return np.clip(np.where(u <= D, u, 2 * D - u), 0.0, last)
        apply_to = ["mask"]
        clip = clip.without_audio()  # reversed audio is never wanted
    elif mode == "freeze":
        def remap(t):
            return np.clip(t, 0.0, last)
        apply_to = ["mask"]
    else:
        raise PipelineError(f"Unknown extend mode '{mode}'")

    def time_func(t):
        r = remap(t)
        return float(r) if np.ndim(r) == 0 else r

    audio = clip.audio if mode == "freeze" else None  # frozen picture: let the sound simply end
    if audio is not None:
        clip = clip.without_audio()
    out = clip.time_transform(time_func, apply_to=apply_to, keep_duration=False).with_duration(duration)
    return out.with_audio(audio) if audio is not None else out


def _wrap_to_width(text: str, font, max_px: float) -> str:
    out_lines = []
    for para in str(text).split("\n"):
        words, line = para.split(" "), ""
        if len(words) == 1 and font.getlength(para) > max_px:  # CJK / no spaces: wrap by char
            for ch in para:
                if font.getlength(line + ch) > max_px and line:
                    out_lines.append(line)
                    line = ""
                line += ch
            out_lines.append(line)
            continue
        for word in words:
            trial = f"{line} {word}".strip()
            if font.getlength(trial) > max_px and line:
                out_lines.append(line)
                line = word
            else:
                line = trial
        out_lines.append(line)
    return "\n".join(out_lines)


def render_text_rgba(ov: dict, canvas: tuple[int, int]) -> np.ndarray:
    W, H = canvas
    k = H / 1080.0
    font = load_pil_font(ov["font"], max(8, int(ov["font_size"] * k)))
    stroke = int(round(ov["stroke_width"] * k))
    spacing = int(ov["line_spacing"] * k)
    text = _wrap_to_width(ov["text"], font, ov["max_width"] * W)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    l, t, r, b = probe.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke,
                                          spacing=spacing, align=ov["align"])
    sx, sy = (int(round(v * k)) for v in ov["shadow_offset"])
    pad = int(ov["box_padding"] * k) if ov["box_color"] else 0
    margin = stroke + max(abs(sx), abs(sy)) + 4
    w = int(r - l) + 2 * (pad + margin)
    h = int(b - t) + 2 * (pad + margin)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    origin = (pad + margin - l, pad + margin - t)
    if ov["box_color"]:
        box = parse_color(ov["box_color"]) + (int(255 * ov["box_opacity"]),)
        draw.rounded_rectangle([margin, margin, w - margin, h - margin], radius=int(16 * k), fill=box)
    if ov["shadow_opacity"] > 0 and (sx or sy):
        shadow = parse_color(ov["shadow_color"]) + (int(255 * ov["shadow_opacity"]),)
        draw.multiline_text((origin[0] + sx, origin[1] + sy), text, font=font, fill=shadow,
                            stroke_width=stroke, stroke_fill=shadow, spacing=spacing, align=ov["align"])
    draw.multiline_text(origin, text, font=font, fill=parse_color(ov["color"]) + (255,),
                        stroke_width=stroke, stroke_fill=parse_color(ov["stroke_color"]) + (255,),
                        spacing=spacing, align=ov["align"])
    return np.asarray(img)


def overlay_position(ov: dict, size: tuple[int, int], canvas: tuple[int, int]) -> tuple[float, float]:
    W, H = canvas
    w, h = size
    m = ov["margin"] * H / 1080.0
    pos = ov["position"]
    if isinstance(pos, (list, tuple)):
        x, y = pos
        x = x * W if 0 <= x <= 1 and isinstance(x, float) else x
        y = y * H if 0 <= y <= 1 and isinstance(y, float) else y
        return x - w / 2, y - h / 2  # coordinates are the overlay's center
    named = {
        "center": ((W - w) / 2, (H - h) / 2),
        "top": ((W - w) / 2, m),
        "bottom": ((W - w) / 2, H - h - m),
        "top_left": (m, m),
        "top_right": (W - w - m, m),
        "bottom_left": (m, H - h - m),
        "bottom_right": (W - w - m, H - h - m),
    }
    if pos not in named:
        raise PipelineError(f"Unknown overlay position '{pos}'. Use {sorted(named)} or [x, y].")
    return named[pos]


def build_overlay(ov: dict, canvas: tuple[int, int], scene_duration: float):
    from moviepy import ImageClip, vfx

    rgba = render_text_rgba(ov, canvas)
    start = max(0.0, float(ov["start"]))
    end = min(scene_duration, float(ov["end"])) if ov["end"] is not None else scene_duration
    if end <= start:
        raise PipelineError(f"Overlay '{ov['text'][:20]}' ends before it starts.")
    clip = ImageClip(rgba, duration=end - start).with_start(start)
    x, y = overlay_position(ov, (rgba.shape[1], rgba.shape[0]), canvas)
    if ov["animation"] == "slide_up":
        rise = 40 * canvas[1] / 1080.0
        dur_in = max(0.01, float(ov["fade_in"]) or 0.3)

        def pos(t, x=x, y=y):
            p = min(1.0, t / dur_in)
            p = 1 - (1 - p) ** 3
            return x, y + rise * (1 - p)

        clip = clip.with_position(pos)
    else:
        clip = clip.with_position((x, y))
    effects = []
    if ov["animation"] in ("fade", "slide_up"):
        if ov["fade_in"]:
            effects.append(vfx.CrossFadeIn(min(float(ov["fade_in"]), clip.duration / 2)))
        if ov["fade_out"]:
            effects.append(vfx.CrossFadeOut(min(float(ov["fade_out"]), clip.duration / 2)))
    return clip.with_effects(effects) if effects else clip


def build_scene(plan: ScenePlan, sb: dict, canvas: tuple[int, int], next_plan: ScenePlan | None):
    from moviepy import CompositeVideoClip, vfx

    from cinematic_fx import apply_scene_fx

    cfg, proj = plan.cfg, sb["project"]
    bg = parse_color(proj["background_color"])
    clip = load_scene_source(plan, canvas)
    clip = fit_duration(clip, plan.duration, cfg["extend"])
    clip = apply_scene_fx(clip, cfg["fx"], canvas, cfg["fit"] or proj["fit"], bg, fps=float(proj["fps"]))

    overlays = [build_overlay(ov, canvas, plan.duration) for ov in cfg["text_overlays"]]
    if overlays:
        clip = CompositeVideoClip([clip, *overlays], size=canvas).with_duration(plan.duration)

    fade_in = float(cfg["fx"].get("fade_in") or 0)
    fade_out = float(cfg["fx"].get("fade_out") or 0)
    if plan.transition["type"] == "fade_black":
        fade_in = max(fade_in, float(plan.transition["duration"]) / 2)
    if next_plan is not None and next_plan.transition["type"] == "fade_black":
        fade_out = max(fade_out, float(next_plan.transition["duration"]) / 2)
    effects = []
    if fade_in > 0:
        effects.append(vfx.FadeIn(min(fade_in, plan.duration / 2), initial_color=list(bg)))
    if fade_out > 0:
        effects.append(vfx.FadeOut(min(fade_out, plan.duration / 2), final_color=list(bg)))
    if plan.transition["type"] == "crossfade" and plan.index > 0:
        effects.append(vfx.CrossFadeIn(float(plan.transition["duration"])))
    if effects:
        clip = clip.with_effects(effects)
    return clip.with_duration(plan.duration)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def duck_envelope(intervals: list[tuple[float, float]], fade: float):
    fade = max(1e-3, fade)

    def env(t):
        t = np.asarray(t, dtype=np.float64)
        e = np.zeros_like(t)
        for s, end in intervals:
            ramp_in = np.clip((t - (s - fade)) / fade, 0, 1)
            ramp_out = np.clip(((end + fade) - t) / fade, 0, 1)
            e = np.maximum(e, np.minimum(ramp_in, ramp_out))
        return e

    return env


def audio_files(sb: dict, base_dir: Path) -> list[tuple[str, Path | None]]:
    """(label, resolved path) for every audio asset the storyboard uses (not voices)."""
    out = []
    bgm = sb["audio"]["bgm"].get("path")
    if bgm:
        out.append(("audio.bgm", resolve_path(base_dir, bgm)))
    for i, cue in enumerate(sb["audio"]["music"]):
        out.append((f"audio.music[{i}]", resolve_path(base_dir, cue["file"])))
    for sc in sb["scenes"]:
        for j, cue in enumerate(sc["sfx"]):
            out.append((f"[{sc['id']}] sfx[{j}]", resolve_path(base_dir, cue["file"])))
    return out


def validate_audio(sb: dict, base_dir: Path) -> list[str]:
    errors: list[str] = []
    ids = {sc["id"] for sc in sb["scenes"]}
    for i, cue in enumerate(sb["audio"]["music"]):
        for key in ("start_scene", "end_scene"):
            if cue[key] is not None and cue[key] not in ids:
                errors.append(f"audio.music[{i}].{key} '{cue[key]}' is not a scene id "
                              "(or that scene was filtered out).")
        for d in cue["drops"]:
            for key in ("start_scene", "end_scene"):
                if d.get(key) is not None and d[key] not in ids:
                    errors.append(f"audio.music[{i}].drops {key} '{d[key]}' is not a scene id.")
    manifest = None
    for label, path in audio_files(sb, base_dir):
        if path is None:
            errors.append(f"{label}: no file given.")
            continue
        if not path.is_file():
            errors.append(f"{label}: file not found: {path}")
            continue
        if sb["audio"]["require_license"]:
            from audio_manifest import entry_for, load_manifest

            manifest = manifest or load_manifest()
            try:
                entry_for(path, manifest)
            except PipelineError as exc:
                errors.append(f"{label}: {exc}")
    return errors


def _scene_window(plans: list[ScenePlan], start_scene, end_scene, start, end, total: float) -> tuple[float, float]:
    by_id = {p.id: p for p in plans}
    a = by_id[start_scene].start if start_scene else float(start or 0.0)
    b = by_id[end_scene].end if end_scene else (float(end) if end is not None else total)
    return max(0.0, a), min(total, b)


def build_music_cue(cue: dict, plans: list[ScenePlan], base_dir: Path, total: float,
                    narration_intervals: list[tuple[float, float]]):
    from moviepy import AudioFileClip, afx

    t0, t1 = _scene_window(plans, cue["start_scene"], cue["end_scene"], cue["start"], cue["end"], total)
    if t1 - t0 < 0.05:
        return None
    clip = AudioFileClip(str(resolve_path(base_dir, cue["file"])))
    length = t1 - t0
    if clip.duration < length and cue["loop"]:
        clip = clip.with_effects([afx.AudioLoop(duration=length)])
    else:
        clip = clip.subclipped(0, min(length, clip.duration))
    duck = float(cue["duck_to"])
    env = duck_envelope(narration_intervals, float(cue["duck_fade"])) if narration_intervals and duck < 1 else None
    drops = [_scene_window(plans, d.get("start_scene"), d.get("end_scene"), d.get("start"), d.get("end"), total)
             for d in cue["drops"]]
    drop_env = duck_envelope(drops, 0.06) if drops else None  # near-hard cut in and out of silence
    vol = float(cue["volume"])

    def gain_fn(get_frame, t):
        frame = get_frame(t)
        ta = np.asarray(t, dtype=np.float64) + t0  # absolute timeline time
        g = np.full_like(ta, vol, dtype=np.float64)
        if env is not None:
            g = g * (1.0 - (1.0 - duck) * env(ta))
        if drop_env is not None:
            g = g * (1.0 - drop_env(ta))
        return frame * (g[:, None] if np.ndim(frame) == 2 else g)

    clip = clip.transform(gain_fn)
    fx = []
    if cue["fade_in"]:
        fx.append(afx.AudioFadeIn(min(float(cue["fade_in"]), clip.duration / 2)))
    if cue["fade_out"]:
        fx.append(afx.AudioFadeOut(min(float(cue["fade_out"]), clip.duration / 2)))
    return (clip.with_effects(fx) if fx else clip).with_start(t0)


def write_audio_credits(sb: dict, base_dir: Path, output: Path) -> Path | None:
    """<output stem>_credits.txt with the attribution lines the used audio requires (fresh each render)."""
    credits = output.with_name(f"{output.stem}_credits.txt")
    credits.unlink(missing_ok=True)
    paths = [p for _, p in audio_files(sb, base_dir) if p is not None and p.is_file()]
    if not paths or not sb["audio"]["require_license"]:
        return None
    from audio_manifest import credit_lines

    lines = credit_lines(paths)
    if not lines:
        return None
    credits.write_text("Audio (paste into the video description):\n" + "\n".join(lines) + "\n", encoding="utf-8")
    log.info("Audio credits: %s", credits)
    return credits


def build_audio(sb: dict, plans: list[ScenePlan], scene_clips: list, base_dir: Path, total: float):
    from moviepy import AudioFileClip, CompositeAudioClip, afx

    audio_cfg = sb["audio"]
    layers = []
    narration_intervals: list[tuple[float, float]] = []

    for plan, clip in zip(plans, scene_clips):
        if plan.voice:
            vo = AudioFileClip(plan.voice["audio"]).with_start(plan.narration_start)
            vol = float(audio_cfg["narration_volume"])
            layers.append(vo.with_volume_scaled(vol) if vol != 1.0 else vo)
            narration_intervals.append((plan.narration_start, plan.narration_start + float(plan.voice["duration"])))
        if plan.cfg["keep_clip_audio"] and clip.audio is not None:
            layers.append(clip.audio.with_volume_scaled(float(audio_cfg["clip_audio_volume"])).with_start(plan.start))

    bgm_cfg = audio_cfg["bgm"]
    bgm_path = resolve_path(base_dir, bgm_cfg.get("path"))
    if bgm_path is not None:
        bgm = AudioFileClip(str(bgm_path))
        if bgm_cfg["loop"] and bgm.duration < total:
            bgm = bgm.with_effects([afx.AudioLoop(duration=total)])
        else:
            bgm = bgm.subclipped(0, min(total, bgm.duration))
        bgm = bgm.with_volume_scaled(float(bgm_cfg["volume"]))
        duck = float(bgm_cfg["duck_to"])
        if narration_intervals and duck < 1.0:
            env = duck_envelope(narration_intervals, float(bgm_cfg["duck_fade"]))

            def ducked(get_frame, t):
                frame = get_frame(t)
                gain = 1.0 - (1.0 - duck) * env(t)
                return frame * (gain[:, None] if np.ndim(frame) == 2 else gain)

            bgm = bgm.transform(ducked)
        fx = []
        if bgm_cfg["fade_in"]:
            fx.append(afx.AudioFadeIn(float(bgm_cfg["fade_in"])))
        if bgm_cfg["fade_out"]:
            fx.append(afx.AudioFadeOut(float(bgm_cfg["fade_out"])))
        layers.append(bgm.with_effects(fx) if fx else bgm)

    for cue in audio_cfg["music"]:
        layer = build_music_cue(cue, plans, base_dir, total, narration_intervals)
        if layer is not None:
            layers.append(layer)
    for plan in plans:
        for cue in plan.cfg["sfx"]:
            at = plan.start + float(cue["at"])
            if at >= total:
                continue
            sfx = AudioFileClip(str(resolve_path(base_dir, cue["file"])))
            sfx = sfx.subclipped(0, min(sfx.duration, total - at))
            vol = float(cue["volume"])
            layers.append((sfx.with_volume_scaled(vol) if vol != 1.0 else sfx).with_start(at))

    if not layers:
        return None
    return CompositeAudioClip(layers).with_duration(total)


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def assemble(scene_clips: list, plans: list[ScenePlan], canvas: tuple[int, int], bg):
    from moviepy import CompositeVideoClip, concatenate_videoclips

    total = plans[-1].end
    if any(p.transition["type"] == "crossfade" for p in plans[1:]):
        placed = [c.with_start(p.start) for c, p in zip(scene_clips, plans)]
        return CompositeVideoClip(placed, size=canvas, bg_color=tuple(bg)).with_duration(total)
    return concatenate_videoclips([c.without_audio() for c in scene_clips], method="chain").with_duration(total)


def render(args: argparse.Namespace) -> Path:
    t0 = time.time()
    require_binary("ffmpeg")
    require_binary("ffprobe")
    storyboard_path = args.storyboard.resolve()
    if args.generate_images and not args.dry_run:
        from image_generator import generate_images

        ids = [s.strip() for s in args.scenes.split(",")] if args.scenes else None
        generate_images(storyboard_path, ids, args.force_images, args.comfy_url, args.comfy_dir)
    sb, base_dir = load_storyboard(storyboard_path)
    proj = sb["project"]

    if args.scenes:
        wanted = [s.strip() for s in args.scenes.split(",") if s.strip()]
        missing = set(wanted) - {s["id"] for s in sb["scenes"]}
        if missing:
            raise PipelineError(f"--scenes: unknown id(s): {', '.join(sorted(missing))}")
        sb["scenes"] = [s for s in sb["scenes"] if s["id"] in wanted]

    errors = validate_storyboard(sb, base_dir, args.allow_missing)
    if errors:
        raise PipelineError("Storyboard validation failed:\n  - " + "\n  - ".join(errors))

    canvas = tuple(int(v) for v in proj["resolution"])
    if args.draft:
        canvas = (canvas[0] // 2 // 2 * 2, canvas[1] // 2 // 2 * 2)
    fps = float(proj["fps"])

    # 1) Voiceovers
    from voiceover_generator import (
        estimate_speech_seconds,
        generate_for_storyboard,
        lines_from_storyboard,
        proportional_boundaries,
    )

    if args.dry_run:
        voices = {}
        for line in lines_from_storyboard(sb):
            d = estimate_speech_seconds(line.text, line.rate)
            voices[line.id] = {"text": line.text, "duration": d, "audio": "",
                               "boundaries": proportional_boundaries(line.text, d), "fallback": True}
    else:
        voices = generate_for_storyboard(sb, TEMP_DIR / "voiceovers", fallback=args.tts_fallback,
                                         force=args.force_tts, proxy=args.proxy)

    # 2) Timeline
    plans = plan_timeline(sb, base_dir, voices)
    print_timeline(plans)
    output = Path(args.output).resolve() if args.output else resolve_path(base_dir, proj["output"])
    if args.draft:
        output = output.with_name(output.stem + "_draft" + output.suffix)
    timeline = timeline_dict(sb, plans, canvas, storyboard_path)
    timeline_path = save_json(TEMP_DIR / f"{output.stem}_timeline.json", timeline)
    if args.dry_run:
        log.info("Dry run complete (durations are estimates). Timeline: %s", timeline_path)
        return timeline_path

    # 3) Scenes + audio
    log.info("Building %d scene(s) at %dx%d @ %g fps", len(plans), canvas[0], canvas[1], fps)
    bg = parse_color(proj["background_color"])
    scene_clips = []
    for i, plan in enumerate(plans):
        log.info("  scene %-14s %6.2fs  %s", plan.id, plan.duration,
                 plan.source.name if plan.source else "(placeholder)")
        scene_clips.append(build_scene(plan, sb, canvas, plans[i + 1] if i + 1 < len(plans) else None))
    total = plans[-1].end
    video = assemble(scene_clips, plans, canvas, bg)
    audio = build_audio(sb, plans, scene_clips, base_dir, total)
    video = video.with_audio(audio) if audio is not None else video.without_audio()

    # 4) Lossless master
    ensure_dir(TEMP_DIR)
    master = TEMP_DIR / f"{output.stem}_master.mkv"
    log.info("Rendering lossless RGB master -> %s", master)
    video.write_videofile(
        str(master), fps=fps, codec="libx264rgb", preset="ultrafast" if args.draft else "veryfast",
        pixel_format="rgb24", ffmpeg_params=["-qp", "0"],
        audio=audio is not None, audio_codec="pcm_s16le", audio_fps=48000,
        temp_audiofile_path=str(TEMP_DIR), threads=args.threads, logger="bar",
    )

    # 5) Subtitles + final encode
    from subtitle_generator import cues_from_timeline, final_encode, write_ass, write_srt

    sub_cfg = sb["subtitles"]
    ass_path = srt_path = None
    if sub_cfg["enabled"]:
        cues = cues_from_timeline(timeline, sub_cfg)
        if cues:
            ass_path = write_ass(cues, TEMP_DIR / f"{output.stem}.ass", sub_cfg, canvas, sb["speakers"])
            if sub_cfg["export_srt"] or sub_cfg["soft_subtitles"]:
                srt_path = write_srt(cues, output.with_suffix(".srt"), sub_cfg["show_speaker"])
            log.info("Subtitles: %d cue(s)%s", len(cues), f", SRT -> {srt_path}" if srt_path else "")
    final_encode(
        master, output,
        ass_path=ass_path if sub_cfg["enabled"] and sub_cfg["burn_in"] else None,
        srt_path=srt_path if sub_cfg["soft_subtitles"] else None,
        fps=fps,
        crf=int(args.crf if args.crf is not None else (23 if args.draft else proj["crf"])),
        preset="veryfast" if args.draft else proj["preset"],
        tune=proj["tune"], audio_bitrate=proj["audio_bitrate"], loudness_lufs=proj.get("loudness_lufs"),
    )

    write_audio_credits(sb, base_dir, output)
    for c in scene_clips:
        c.close()
    video.close()
    if not args.keep_temp:
        master.unlink(missing_ok=True)
    placeholders = [p.id for p in plans if p.voice and p.voice.get("fallback")]
    if placeholders:
        log.warning("Narration for %s is SILENT placeholder audio (edge-tts was unreachable).",
                    ", ".join(placeholders))
    log.info("Done in %.1fs -> %s (%s)", time.time() - t0, output, format_seconds(total))
    return output


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a storyboard JSON into a finished anime MP4.")
    p.add_argument("storyboard", type=Path, help="Storyboard JSON (see storyboard_template.json).")
    p.add_argument("-o", "--output", type=Path, help="Override project.output.")
    p.add_argument("--draft", action="store_true", help="Half resolution, fast presets, for previews.")
    p.add_argument("--dry-run", action="store_true", help="Validate and print the timeline only (no TTS, no render).")
    p.add_argument("--scenes", help="Comma-separated scene ids to render (subset).")
    p.add_argument("--allow-missing", action="store_true", help="Use placeholder cards for missing clips.")
    p.add_argument("--tts-fallback", choices=["none", "silence"], default="none",
                   help="If edge-tts is unreachable: fail (none) or use silent placeholder audio.")
    p.add_argument("--force-tts", action="store_true", help="Ignore the voiceover cache.")
    p.add_argument("--proxy", help="HTTP proxy for edge-tts.")
    p.add_argument("--crf", type=int, help="Override final x264 CRF (lower = higher quality).")
    p.add_argument("--threads", type=int, help="ffmpeg encoder threads.")
    p.add_argument("--keep-temp", action="store_true", help="Keep the lossless master in temp/.")
    p.add_argument("--generate-images", action="store_true",
                   help="First generate/refresh images for scenes with a 'generate' block via local ComfyUI.")
    p.add_argument("--force-images", action="store_true", help="With --generate-images: regenerate all.")
    p.add_argument("--comfy-url", help="ComfyUI URL (default $COMFYUI_URL or http://127.0.0.1:8189).")
    p.add_argument("--comfy-dir", help="ComfyUI folder to auto-start if not running (default $COMFYUI_DIR).")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        render(args)
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
