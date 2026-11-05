#!/usr/bin/env python3
"""Cinematic effects for anime clips (moviepy 2.x + numpy + Pillow).

Effects
  * Camera  : fit-to-canvas (cover/contain) + Ken Burns zoom/pan, done as ONE
              sub-pixel resample per frame (no double-scaling, no jitter).
  * Grade   : anime color grading presets (saturation, contrast, split-toning,
              bloom, vignette) blended by an intensity factor.
  * Lines   : procedurally generated speed lines (radial "focus lines",
              horizontal or vertical motion streaks) that flicker on twos.

Library use (from the orchestrator):
  clip = apply_scene_fx(clip, fx_cfg, canvas=(1920, 1080), fit="cover")

CLI examples:
  python scripts/cinematic_fx.py assets/images/scene_01.png -o output/fx_test.mp4 \
      --duration 5 --kb-preset push_in --grade anime_vivid
  python scripts/cinematic_fx.py assets/clips/scene_02.mp4 -o output/fx_lines.mp4 \
      --speed-lines radial --grade cinematic_teal_orange
  python scripts/cinematic_fx.py assets/images/scene_01.png --frame 2.5 -o output/still.png \
      --duration 5 --grade anime_pastel
"""

from __future__ import annotations

import argparse
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from pipeline_utils import (
    BT709_TAGS,
    BT709_VF,
    PipelineError,
    classify_media,
    ease,
    get_logger,
    list_frames,
    parse_color,
    setup_logging,
)

log = get_logger("fx")

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #
KEN_BURNS_PRESETS: dict[str, dict] = {
    "push_in":    {"zoom_start": 1.00, "zoom_end": 1.12, "pan_start": [0.50, 0.50], "pan_end": [0.50, 0.48]},
    "pull_out":   {"zoom_start": 1.14, "zoom_end": 1.00, "pan_start": [0.50, 0.46], "pan_end": [0.50, 0.50]},
    "pan_left":   {"zoom_start": 1.10, "zoom_end": 1.10, "pan_start": [0.62, 0.50], "pan_end": [0.38, 0.50]},
    "pan_right":  {"zoom_start": 1.10, "zoom_end": 1.10, "pan_start": [0.38, 0.50], "pan_end": [0.62, 0.50]},
    "pan_up":     {"zoom_start": 1.12, "zoom_end": 1.12, "pan_start": [0.50, 0.62], "pan_end": [0.50, 0.38]},
    "pan_down":   {"zoom_start": 1.12, "zoom_end": 1.12, "pan_start": [0.50, 0.38], "pan_end": [0.50, 0.62]},
    "drift":      {"zoom_start": 1.04, "zoom_end": 1.08, "pan_start": [0.48, 0.51], "pan_end": [0.53, 0.49]},
    # Action presets: fast start, hard landing (ease_out). Best on short cuts (0.5-1.5 s).
    "crash_zoom": {"zoom_start": 1.00, "zoom_end": 1.35, "pan_start": [0.50, 0.50], "pan_end": [0.50, 0.46],
                   "easing": "ease_out"},
    "slam_out":   {"zoom_start": 1.30, "zoom_end": 1.00, "pan_start": [0.50, 0.45], "pan_end": [0.50, 0.50],
                   "easing": "ease_out"},
    "whip_left":  {"zoom_start": 1.18, "zoom_end": 1.18, "pan_start": [0.80, 0.50], "pan_end": [0.25, 0.50],
                   "easing": "ease_out"},
    "whip_right": {"zoom_start": 1.18, "zoom_end": 1.18, "pan_start": [0.20, 0.50], "pan_end": [0.75, 0.50],
                   "easing": "ease_out"},
}
KEN_BURNS_DEFAULTS = {"enabled": False, "preset": None, "easing": "ease_in_out", **KEN_BURNS_PRESETS["push_in"]}

GRADE_PRESETS: dict[str, dict] = {
    "none": {},
    "anime_vivid": {
        "saturation": 1.25, "contrast": 1.08, "brightness": 0.02, "gamma": 0.97, "black_lift": 0.0,
        "shadow_tint": [0.00, 0.02, 0.06], "highlight_tint": [0.04, 0.02, -0.01],
        "bloom": 0.18, "bloom_threshold": 0.75, "bloom_radius": 18, "vignette": 0.18,
    },
    "anime_pastel": {
        "saturation": 1.05, "contrast": 0.92, "brightness": 0.04, "gamma": 0.95, "black_lift": 0.04,
        "shadow_tint": [0.02, 0.00, 0.04], "highlight_tint": [0.05, 0.01, 0.03],
        "bloom": 0.25, "bloom_threshold": 0.65, "bloom_radius": 24, "vignette": 0.10,
    },
    "cinematic_teal_orange": {
        "saturation": 1.10, "contrast": 1.12, "brightness": 0.0, "gamma": 1.0, "black_lift": 0.01,
        "shadow_tint": [-0.03, 0.03, 0.06], "highlight_tint": [0.06, 0.02, -0.04],
        "bloom": 0.10, "bloom_threshold": 0.80, "bloom_radius": 16, "vignette": 0.25,
    },
    "night_blue": {
        "saturation": 0.85, "contrast": 1.05, "brightness": -0.05, "gamma": 1.05, "black_lift": 0.02,
        "shadow_tint": [0.00, 0.02, 0.10], "highlight_tint": [-0.02, 0.00, 0.04],
        "bloom": 0.30, "bloom_threshold": 0.70, "bloom_radius": 22, "vignette": 0.35,
    },
    "golden_hour": {
        "saturation": 1.15, "contrast": 1.04, "brightness": 0.02, "gamma": 0.96, "black_lift": 0.01,
        "shadow_tint": [0.03, 0.00, -0.02], "highlight_tint": [0.08, 0.04, -0.03],
        "bloom": 0.30, "bloom_threshold": 0.60, "bloom_radius": 26, "vignette": 0.20,
    },
}
GRADE_NEUTRAL = {
    "saturation": 1.0, "contrast": 1.0, "brightness": 0.0, "gamma": 1.0, "black_lift": 0.0,
    "shadow_tint": [0.0, 0.0, 0.0], "highlight_tint": [0.0, 0.0, 0.0],
    "bloom": 0.0, "bloom_threshold": 0.75, "bloom_radius": 18, "vignette": 0.0,
}
GRADE_DEFAULTS = {"enabled": False, "preset": "anime_vivid", "intensity": 1.0}

SPEED_LINE_DEFAULTS = {
    "enabled": False,
    "style": "radial",        # radial | horizontal | vertical
    "count": 90,
    "color": "#FFFFFF",
    "opacity": 0.55,
    "inner_radius": 0.38,     # radial: clear zone as fraction of half-diagonal
    "center": [0.5, 0.5],     # radial: focus point (normalized)
    "min_width": 2.0,         # px at 1080p (scaled with canvas height)
    "max_width": 9.0,
    "flicker_fps": 12,        # 12 = animated "on twos" at 24 fps
    "variants": 6,
    "start": None,            # seconds inside the scene; None = scene start
    "end": None,              # None = scene end
    "seed": 7,
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _to_uint8(frame: np.ndarray) -> np.ndarray:
    if frame.dtype == np.uint8:
        return frame
    if np.issubdtype(frame.dtype, np.floating) and frame.max() <= 1.0:
        frame = frame * 255.0
    return np.clip(frame, 0, 255).astype(np.uint8)


def _rgb(frame: np.ndarray) -> np.ndarray:
    frame = _to_uint8(frame)
    if frame.ndim == 2:
        frame = np.stack([frame] * 3, axis=-1)
    return frame[..., :3]


def flatten_alpha(clip, background=(0, 0, 0)):
    """Composite a clip that has a mask onto a solid background (opaque output)."""
    if clip.mask is None:
        return clip
    from moviepy import ColorClip, CompositeVideoClip

    bg = ColorClip(clip.size, color=tuple(background), duration=clip.duration)
    return CompositeVideoClip([bg, clip.with_position((0, 0))], size=clip.size).with_duration(clip.duration)


# --------------------------------------------------------------------------- #
# Camera: fit + Ken Burns
# --------------------------------------------------------------------------- #
def resolve_ken_burns(cfg: dict | None) -> dict:
    cfg = dict(cfg or {})
    merged = dict(KEN_BURNS_DEFAULTS)
    preset = cfg.get("preset")
    if preset:
        if preset not in KEN_BURNS_PRESETS:
            raise PipelineError(f"Unknown ken_burns preset '{preset}'. Options: {', '.join(KEN_BURNS_PRESETS)}")
        merged.update(KEN_BURNS_PRESETS[preset])
    merged.update({k: v for k, v in cfg.items() if v is not None})
    for key in ("zoom_start", "zoom_end"):
        if float(merged[key]) < 1.0:
            raise PipelineError(f"ken_burns.{key} must be >= 1.0 (got {merged[key]}); <1 would reveal borders.")
    return merged


def _base_box(sw: int, sh: int, W: int, H: int, fit: str) -> tuple[float, float, float, float]:
    """Source-space box that maps onto the full canvas for the given fit mode."""
    target = W / H
    source = sw / sh
    if fit == "stretch":
        return 0.0, 0.0, float(sw), float(sh)
    if fit == "cover":
        if source > target:  # too wide: crop sides
            bw = sh * target
            return (sw - bw) / 2, 0.0, (sw + bw) / 2, float(sh)
        bh = sw / target     # too tall: crop top/bottom
        return 0.0, (sh - bh) / 2, float(sw), (sh + bh) / 2
    if fit == "contain":
        if source > target:  # letterbox top/bottom
            bh = sw / target
            return 0.0, (sh - bh) / 2, float(sw), (sh + bh) / 2
        bw = sh * target     # pillarbox sides
        return (sw - bw) / 2, 0.0, (sw + bw) / 2, float(sh)
    raise PipelineError(f"Unknown fit mode '{fit}' (use cover, contain or stretch)")


def _kb_box(base, zoom: float, cx: float, cy: float):
    x0, y0, x1, y1 = base
    bw, bh = (x1 - x0) / zoom, (y1 - y0) / zoom
    # Clamp the center so the zoomed window always stays inside the base box.
    px = min(max(x0 + cx * (x1 - x0), x0 + bw / 2), x1 - bw / 2)
    py = min(max(y0 + cy * (y1 - y0), y0 + bh / 2), y1 - bh / 2)
    return px - bw / 2, py - bh / 2, px + bw / 2, py + bh / 2


def camera_transform(clip, canvas: tuple[int, int], fit: str = "cover",
                     ken_burns: dict | None = None, background=(0, 0, 0)):
    """Fit *clip* to *canvas* and optionally add a Ken Burns move (single resample)."""
    W, H = canvas
    clip = flatten_alpha(clip, background)
    sw, sh = clip.size
    kb = resolve_ken_burns(ken_burns) if ken_burns and ken_burns.get("enabled") else None
    if kb is None and (sw, sh) == (W, H):
        return clip
    if kb is not None and not clip.duration:
        raise PipelineError("Ken Burns needs a clip with a known duration.")

    base = _base_box(sw, sh, W, H, fit)
    duration = clip.duration or 1.0
    bg = tuple(int(c) for c in background)

    def box_at(t: float):
        if kb is None:
            return base
        p = ease(kb["easing"], t / duration)
        zoom = kb["zoom_start"] + (kb["zoom_end"] - kb["zoom_start"]) * p
        cx = kb["pan_start"][0] + (kb["pan_end"][0] - kb["pan_start"][0]) * p
        cy = kb["pan_start"][1] + (kb["pan_end"][1] - kb["pan_start"][1]) * p
        return _kb_box(base, zoom, cx, cy)

    # 'contain' boxes extend past the source; pad once per frame so the box is valid.
    pad_x = int(np.ceil(max(0.0, -base[0])))
    pad_y = int(np.ceil(max(0.0, -base[1])))

    def frame_fn(get_frame, t):
        img = Image.fromarray(_rgb(get_frame(t)))
        x0, y0, x1, y1 = box_at(t)
        if pad_x or pad_y:
            padded = Image.new("RGB", (sw + 2 * pad_x, sh + 2 * pad_y), bg)
            padded.paste(img, (pad_x, pad_y))
            img = padded
            x0, x1, y0, y1 = x0 + pad_x, x1 + pad_x, y0 + pad_y, y1 + pad_y
        # Separable resample with a sub-pixel source box: smooth motion, proper
        # antialiasing on downscale, and ~5x faster than Image.transform.
        out = img.resize((W, H), Image.Resampling.BICUBIC, box=(x0, y0, x1, y1))
        return np.asarray(out)

    return clip.transform(frame_fn, apply_to=[])


# --------------------------------------------------------------------------- #
# Color grading
# --------------------------------------------------------------------------- #
def resolve_grade(cfg: dict | None) -> dict:
    cfg = {**GRADE_DEFAULTS, **(cfg or {})}
    preset = cfg.get("preset") or "none"
    if preset not in GRADE_PRESETS:
        raise PipelineError(f"Unknown color_grade preset '{preset}'. Options: {', '.join(GRADE_PRESETS)}")
    params = {**GRADE_NEUTRAL, **GRADE_PRESETS[preset]}
    params.update({k: v for k, v in cfg.items() if k in GRADE_NEUTRAL and v is not None})
    params["intensity"] = float(max(0.0, min(1.0, cfg.get("intensity", 1.0))))
    return params


@lru_cache(maxsize=8)
def _vignette_mask(h: int, w: int, strength: float) -> np.ndarray:
    y, x = np.ogrid[-1:1:complex(0, h), -1:1:complex(0, w)]
    r = np.sqrt((x * (w / h)) ** 2 + y ** 2) / np.sqrt((w / h) ** 2 + 1)
    falloff = np.clip((r - 0.45) / 0.55, 0.0, 1.0) ** 2
    return np.ascontiguousarray((1.0 - strength * falloff).astype(np.float32))  # (H, W)


@lru_cache(maxsize=16)
def _tone_lut(black_lift: float, contrast: float, brightness: float, gamma: float) -> np.ndarray:
    """Per-channel tone curve (lift -> contrast -> brightness -> gamma) as a 256-entry LUT."""
    x = np.arange(256, dtype=np.float32) / 255.0
    x = black_lift + x * (1 - black_lift)
    x = np.clip((x - 0.5) * contrast + 0.5 + brightness, 0, 1)
    if gamma != 1.0:
        x = np.power(x, gamma)
    return x.astype(np.float32)


def _bloom(planar: np.ndarray, threshold: float, radius: float) -> np.ndarray:
    """Bright-pass glow at quarter resolution. Input/output are planar (3, H, W) in 0..1."""
    _, h, w = planar.shape
    # Average 2 offset 1/4-res samplings: cheap, and the blur that follows hides the rest.
    a, b = planar[:, ::4, ::4], planar[:, 2::4, 2::4]
    hh, ww = min(a.shape[1], b.shape[1]), min(a.shape[2], b.shape[2])
    small = (a[:, :hh, :ww] + b[:, :hh, :ww]) * 0.5
    luma = LUMA[0] * small[0] + LUMA[1] * small[1] + LUMA[2] * small[2]
    mask = np.clip((luma - threshold) / max(1e-3, 1 - threshold), 0, 1)
    bright = Image.fromarray(np.ascontiguousarray((small * mask * 255).astype(np.uint8).transpose(1, 2, 0)))
    bright = bright.filter(ImageFilter.GaussianBlur(max(1.0, radius / 4)))
    up = Image.Image.split(bright.resize((w, h), Image.Resampling.BILINEAR))
    return np.stack([np.asarray(ch) for ch in up]).astype(np.float32) * (1 / 255.0)


def grade_frame(frame: np.ndarray, p: dict) -> np.ndarray:
    """Grade one RGB frame. Math runs on planar (3, H, W) float32 so that every
    broadcast is over contiguous memory (≈4x faster than interleaved H×W×3)."""
    src = _rgb(frame)
    lut = _tone_lut(float(p["black_lift"]), float(p["contrast"]), float(p["brightness"]), float(p["gamma"]))
    f = lut[src.transpose(2, 0, 1)]                       # (3, H, W), C-contiguous
    luma = LUMA[0] * f[0] + LUMA[1] * f[1] + LUMA[2] * f[2]
    sat = float(p["saturation"])
    if sat != 1.0:                                       # f = luma + (f - luma) * sat
        f *= sat
        f += luma * (1.0 - sat)
    shadow, highlight = p["shadow_tint"], p["highlight_tint"]
    if any(shadow) or any(highlight):
        inv2 = (1.0 - luma) ** 2
        l2 = luma * luma
        for c in range(3):
            if shadow[c]:
                f[c] += shadow[c] * inv2
            if highlight[c]:
                f[c] += highlight[c] * l2
    np.clip(f, 0, 1, out=f)
    if p["vignette"] > 0:
        f *= _vignette_mask(f.shape[1], f.shape[2], float(p["vignette"]))
    if p["bloom"] > 0:
        b = _bloom(f, p["bloom_threshold"], p["bloom_radius"])
        b *= p["bloom"]
        f -= f * b                                        # screen blend: f + b - f*b
        f += b
    f *= 255.0
    if p["intensity"] < 1.0:
        s = src.transpose(2, 0, 1).astype(np.float32)
        f -= s
        f *= p["intensity"]
        f += s
    f += 0.5
    np.clip(f, 0, 255, out=f)
    return np.ascontiguousarray(f.astype(np.uint8).transpose(1, 2, 0))


def color_grade(clip, cfg: dict | None):
    params = resolve_grade(cfg)
    if params["intensity"] <= 0:
        return clip
    return clip.image_transform(lambda fr: grade_frame(fr, params), apply_to=[])


# --------------------------------------------------------------------------- #
# Speed lines
# --------------------------------------------------------------------------- #
class SpeedLines:
    """Pre-renders a handful of speed-line alpha layers and cycles through them."""

    SUPERSAMPLE = 2

    def __init__(self, size: tuple[int, int], cfg: dict | None = None):
        self.cfg = {**SPEED_LINE_DEFAULTS, **(cfg or {})}
        if self.cfg["style"] not in ("radial", "horizontal", "vertical"):
            raise PipelineError("speed_lines.style must be radial, horizontal or vertical")
        self.W, self.H = size
        self.color = np.asarray(parse_color(self.cfg["color"]), np.float32)
        rng = np.random.default_rng(int(self.cfg["seed"]))
        self.layers = [self._render(rng) for _ in range(max(1, int(self.cfg["variants"])))]

    def _render(self, rng: np.random.Generator) -> np.ndarray:
        s = self.SUPERSAMPLE
        W, H = self.W * s, self.H * s
        scale = (self.H / 1080.0) * s
        img = Image.new("L", (W, H), 0)
        draw = ImageDraw.Draw(img)
        c = self.cfg
        count = int(c["count"])
        if c["style"] == "radial":
            cx, cy = c["center"][0] * W, c["center"][1] * H
            half_diag = 0.5 * np.hypot(W, H)
            r_outer = 2.2 * half_diag
            angles = rng.uniform(0, 2 * np.pi, count)
            for a in angles:
                r_in = half_diag * c["inner_radius"] * rng.uniform(1.0, 1.7)
                w = rng.uniform(c["min_width"], c["max_width"]) * scale * 4  # wide at the far edge
                d = np.array([np.cos(a), np.sin(a)])
                n = np.array([-d[1], d[0]])
                tip = (cx + d[0] * r_in, cy + d[1] * r_in)
                far = np.array([cx, cy]) + d * r_outer
                draw.polygon([tip, tuple(far + n * w / 2), tuple(far - n * w / 2)],
                             fill=int(rng.uniform(150, 255)))
        else:
            horizontal = c["style"] == "horizontal"
            span_main, span_cross = (W, H) if horizontal else (H, W)
            for _ in range(count):
                cross = rng.uniform(0, span_cross)
                length = rng.uniform(0.15, 0.6) * span_main
                start = rng.uniform(-0.2 * span_main, span_main)
                thick = rng.uniform(c["min_width"], c["max_width"]) * scale * 0.5
                mid = start + length * rng.uniform(0.3, 0.7)
                pts = [(start, cross), (mid, cross - thick / 2), (start + length, cross), (mid, cross + thick / 2)]
                if not horizontal:
                    pts = [(y, x) for x, y in pts]
                draw.polygon(pts, fill=int(rng.uniform(140, 255)))
        img = img.resize((self.W, self.H), Image.Resampling.LANCZOS)
        alpha = np.asarray(img, np.float32) / 255.0 * float(c["opacity"])
        # Only blend rows that contain line pixels (horizontal/vertical styles are sparse).
        rows = np.flatnonzero(alpha.max(axis=1) > 0)
        r0, r1 = (int(rows[0]), int(rows[-1]) + 1) if rows.size else (0, 0)
        a = alpha[r0:r1, :, None]
        return {"rows": (r0, r1), "keep": 1.0 - a, "add": self.color * a}

    def layer_at(self, t: float) -> dict:
        idx = int(t * float(self.cfg["flicker_fps"])) % len(self.layers)
        return self.layers[idx]

    def composite(self, frame: np.ndarray, t: float) -> np.ndarray:
        out = np.array(_rgb(frame), copy=True)
        layer = self.layer_at(t)
        r0, r1 = layer["rows"]
        if r1 > r0:
            band = out[r0:r1].astype(np.float32)
            band *= layer["keep"]
            band += layer["add"]
            band += 0.5
            np.clip(band, 0, 255, out=band)
            out[r0:r1] = band.astype(np.uint8)
        return out


def speed_lines(clip, cfg: dict | None):
    lines = SpeedLines(clip.size, cfg)
    start = lines.cfg["start"] or 0.0
    end = lines.cfg["end"] if lines.cfg["end"] is not None else float("inf")

    def frame_fn(get_frame, t):
        frame = get_frame(t)
        if start <= t < end:
            return lines.composite(frame, t)
        return _rgb(frame)

    return clip.transform(frame_fn, apply_to=[])


# --------------------------------------------------------------------------- #
# Combined entry point
# --------------------------------------------------------------------------- #
ANIME_TIMING_DEFAULTS = {"enabled": False, "on": 2}
IMPACT_DEFAULTS = {"enabled": False, "at": [0.0], "invert": True, "flash": True, "shake": 24.0, "settle": 0.5,
                   "seed": 11, "flash_color": "#FFFFFF"}  # e.g. "#FF7A1A" ember / "#19E6FF" cyan catalyst flash
SHAKE_DEFAULTS = {"enabled": False, "amount": 10.0, "fps": 12, "seed": 5}  # px at 1080p; offset changes `fps`/s
# Lights-out / energy-drain ramp: brightness and saturation go from 1.0 to the targets between start and end
# (seconds into the cut). steps > 0 makes it stepped (lights dying bank by bank); 0 is a smooth ramp.
BLACKOUT_DEFAULTS = {"enabled": False, "start": 0.0, "end": 1.0, "brightness": 0.15, "saturation": 0.0,
                     "steps": 0, "easing": "ease_in"}


def validate_timing_and_impact(fx: dict) -> None:
    timing = {**ANIME_TIMING_DEFAULTS, **(fx.get("anime_timing") or {})}
    if timing["enabled"] and int(timing["on"]) not in (1, 2, 3, 4):
        raise PipelineError(f"anime_timing.on must be 1-4 (2 = 'on twos', 3 = 'on threes'); got {timing['on']}")
    shake = {**SHAKE_DEFAULTS, **(fx.get("shake") or {})}
    if shake["enabled"] and (float(shake["amount"]) < 0 or float(shake["fps"]) <= 0):
        raise PipelineError("shake.amount must be >= 0 and shake.fps > 0")
    impact = {**IMPACT_DEFAULTS, **(fx.get("impact") or {})}
    if impact["enabled"]:
        parse_color(impact["flash_color"])
        hits = impact["at"]
        if not isinstance(hits, list) or not all(isinstance(h, (int, float)) and h >= 0 for h in hits):
            raise PipelineError(f"impact.at must be a list of seconds >= 0; got {hits!r}")
    blackout = {**BLACKOUT_DEFAULTS, **(fx.get("blackout") or {})}
    if blackout["enabled"]:
        if not 0 <= float(blackout["start"]) < float(blackout["end"]):
            raise PipelineError("blackout needs 0 <= start < end (seconds into the cut)")
        if not (0 <= float(blackout["brightness"]) <= 1 and 0 <= float(blackout["saturation"]) <= 1):
            raise PipelineError("blackout.brightness and blackout.saturation must be between 0 and 1")
        if int(blackout["steps"]) < 0:
            raise PipelineError("blackout.steps must be >= 0 (0 = smooth)")
        ease(blackout["easing"], 0.0)


def anime_timing(clip, cfg: dict | None, fps: float):
    """Hold each drawing for `on` frames (on twos = 12 drawings/s at 24 fps, on threes = 8).

    Applied to the source drawing BEFORE the camera transform, so Ken Burns moves stay smooth while the
    animation steps, the way TV anime composites limited animation under smooth camera work."""
    cfg = {**ANIME_TIMING_DEFAULTS, **(cfg or {})}
    on = int(cfg["on"])
    if not cfg["enabled"] or on <= 1 or clip.duration is None:  # (harmless on stills: nothing to hold)
        return clip
    step = on / float(fps)

    def remap(t):
        held = np.floor(np.asarray(t, dtype=np.float64) / step + 1e-6) * step
        return float(held) if np.ndim(held) == 0 else held

    return clip.time_transform(remap, apply_to=["mask"], keep_duration=True)  # default drops .duration


def impact_frames(clip, cfg: dict | None, fps: float):
    """Anime impact hits: 1 inverted high-contrast B&W frame, a white flash, then a decaying shake.

    Same look as promo/src/components/Fx.tsx `Impact`. Shake offsets are seeded per hit and frame, so renders
    are deterministic. The shake re-samples only the ~settle-seconds after each hit."""
    cfg = {**IMPACT_DEFAULTS, **(cfg or {})}
    if not cfg["enabled"] or not cfg["at"]:
        return clip
    hits = sorted(float(h) for h in cfg["at"])
    settle_frames = max(1, round(float(cfg["settle"]) * fps))
    flash_rgb = np.asarray(parse_color(cfg["flash_color"]), np.float32)
    W, H = clip.size

    def frame_fn(get_frame, t):
        frame = _rgb(get_frame(t))
        past = [h for h in hits if h <= t + 1e-6]
        if not past:
            return frame
        hit = past[-1]
        # Frames since the hit, rounded DOWN: cuts can start between video frames, and rounding to nearest
        # skipped frame 0 (the inverted hit frame) entirely.
        k = int(np.floor((t - hit) * fps + 1e-6))
        if k >= settle_frames:
            return frame
        out = frame
        decay = 1.0 - k / settle_frames
        amp = float(cfg["shake"]) * (H / 1080.0) * decay
        if amp > 0.5:
            rng = np.random.default_rng([int(cfg["seed"]), int(round(hit * 1000)), k])
            dx, dy = rng.uniform(-amp, amp, 2)
            zoom = 1.0 + 0.04 * decay  # zoom in slightly so the shifted frame never shows an edge
            bw, bh = W / zoom, H / zoom
            x0 = min(max((W - bw) / 2 + dx, 0.0), W - bw)
            y0 = min(max((H - bh) / 2 + dy, 0.0), H - bh)
            out = np.asarray(Image.fromarray(out).resize((W, H), Image.Resampling.BICUBIC,
                                                         box=(x0, y0, x0 + bw, y0 + bh)))
        if k == 0 and cfg["invert"]:
            gray = out.astype(np.float32) @ LUMA
            gray = np.clip((255.0 - gray - 128.0) * 4.0 + 128.0, 0, 255).astype(np.uint8)  # invert, contrast x4
            out = np.repeat(gray[..., None], 3, axis=2)
        elif cfg["flash"] and k in (1, 2):
            a = 0.85 if k == 1 else 0.35
            out = (out.astype(np.float32) * (1 - a) + flash_rgb * a + 0.5).astype(np.uint8)
        return out

    return clip.transform(frame_fn, apply_to=[])


def camera_shake(clip, cfg: dict | None):
    """Continuous energy rumble: a seeded random offset that changes `fps` times per second (stepped like
    anime camera shake), with a slight zoom so frame edges never show. Deterministic per (seed, step)."""
    cfg = {**SHAKE_DEFAULTS, **(cfg or {})}
    if not cfg["enabled"] or float(cfg["amount"]) <= 0:
        return clip
    W, H = clip.size
    amp = float(cfg["amount"]) * H / 1080.0
    zoom = 1.0 + 2.2 * amp / min(W, H)
    bw, bh = W / zoom, H / zoom

    def frame_fn(get_frame, t):
        frame = _rgb(get_frame(t))
        step = int(np.floor(t * float(cfg["fps"]) + 1e-6))
        dx, dy = np.random.default_rng([int(cfg["seed"]), step]).uniform(-amp, amp, 2)
        x0 = min(max((W - bw) / 2 + dx, 0.0), W - bw)
        y0 = min(max((H - bh) / 2 + dy, 0.0), H - bh)
        return np.asarray(Image.fromarray(frame).resize((W, H), Image.Resampling.BICUBIC,
                                                        box=(x0, y0, x0 + bw, y0 + bh)))

    return clip.transform(frame_fn, apply_to=[])


def blackout(clip, cfg: dict | None):
    """Ramp brightness and saturation down over [start, end]: lights going out, or a catalyst's color draining.

    Deterministic and cheap, so a lights-out beat doesn't need a video model (Wan rarely changes global lighting
    on request). Holds the final level after `end`."""
    cfg = {**BLACKOUT_DEFAULTS, **(cfg or {})}
    if not cfg["enabled"]:
        return clip
    start, end, steps = float(cfg["start"]), float(cfg["end"]), int(cfg["steps"])
    b_end, s_end = float(cfg["brightness"]), float(cfg["saturation"])

    def frame_fn(get_frame, t):
        frame = _rgb(get_frame(t))
        x = min(max((t - start) / (end - start), 0.0), 1.0)
        if steps > 0:
            x = np.floor(x * steps + 1e-6) / steps
        k = ease(cfg["easing"], x)
        if k <= 0:
            return frame
        bright, sat = 1.0 + (b_end - 1.0) * k, 1.0 + (s_end - 1.0) * k
        f = frame.astype(np.float32)
        luma = (f @ LUMA)[..., None]
        out = (luma + (f - luma) * sat) * bright
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)

    return clip.transform(frame_fn, apply_to=[])


def apply_scene_fx(clip, fx: dict | None, canvas: tuple[int, int], fit: str = "cover",
                   background=(0, 0, 0), fps: float = 24.0):
    """Apply anime timing -> camera -> shake -> grade -> blackout -> speed lines -> impact per a storyboard 'fx'."""
    fx = fx or {}
    clip = anime_timing(clip, fx.get("anime_timing"), fps)
    clip = camera_transform(clip, canvas, fit, fx.get("ken_burns"), background)
    clip = camera_shake(clip, fx.get("shake"))  # under grade + speed lines: lines stay screen-steady
    grade = fx.get("color_grade") or {}
    if grade.get("enabled"):
        clip = color_grade(clip, grade)
    clip = blackout(clip, fx.get("blackout"))
    lines = fx.get("speed_lines") or {}
    if lines.get("enabled"):
        timing = {**ANIME_TIMING_DEFAULTS, **(fx.get("anime_timing") or {})}
        if timing["enabled"] and int(timing["on"]) > 1 and "flicker_fps" not in lines:
            # Effects animate on the same beat as the drawings (on threes -> 8 changes per second).
            lines = {**lines, "flicker_fps": float(fps) / int(timing["on"])}
        clip = speed_lines(clip, lines)
    clip = impact_frames(clip, fx.get("impact"), fps)
    return clip


# --------------------------------------------------------------------------- #
# Source loading (shared with the orchestrator)
# --------------------------------------------------------------------------- #
def load_source(path: Path, duration: float | None = None, sequence_fps: float = 8.0,
                with_audio: bool = True):
    """Load a video, a still image, or a directory of frames as a moviepy clip."""
    from moviepy import ImageClip, ImageSequenceClip, VideoFileClip

    kind = classify_media(path)
    if not Path(path).exists():
        raise PipelineError(f"Source not found: {path}")
    if kind == "video":
        return VideoFileClip(str(path), audio=with_audio)
    if kind == "image":
        img = np.asarray(Image.open(path).convert("RGB"))
        return ImageClip(img, duration=duration or 5.0)
    if kind == "sequence":
        frames = [str(p) for p in list_frames(path)]
        first = Image.open(frames[0]).size
        bad = [f for f in frames if Image.open(f).size != first]
        if bad:
            raise PipelineError(f"All frames in {path} must share one size; e.g. {bad[0]} differs.")
        return ImageSequenceClip(frames, fps=sequence_fps, with_mask=False)
    raise PipelineError(f"Unsupported source type: {path}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_resolution(value: str) -> tuple[int, int]:
    try:
        w, h = value.lower().split("x")
        return int(w), int(h)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Resolution must look like 1920x1080") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Apply anime cinematic FX to a single clip or image.")
    p.add_argument("input", type=Path, help="Video, image, or directory of frames.")
    p.add_argument("-o", "--output", type=Path, required=True, help=".mp4 output (or .png with --frame).")
    p.add_argument("--duration", type=float, help="Duration for stills / trim length for videos.")
    p.add_argument("--resolution", type=_parse_resolution, default=(1920, 1080))
    p.add_argument("--fps", type=float, default=24)
    p.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    p.add_argument("--sequence-fps", type=float, default=8.0, help="FPS for frame directories (AnimateDiff default 8).")
    p.add_argument("--kb-preset", choices=list(KEN_BURNS_PRESETS), help="Enable Ken Burns with a preset.")
    p.add_argument("--zoom", nargs=2, type=float, metavar=("START", "END"), help="Ken Burns zoom (>=1).")
    p.add_argument("--pan", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"), help="Ken Burns pan centers (0-1).")
    p.add_argument("--easing", default="ease_in_out")
    p.add_argument("--grade", choices=list(GRADE_PRESETS), help="Color grade preset.")
    p.add_argument("--intensity", type=float, default=1.0)
    p.add_argument("--speed-lines", choices=["radial", "horizontal", "vertical"])
    p.add_argument("--lines-color", default="#FFFFFF")
    p.add_argument("--lines-opacity", type=float, default=0.55)
    p.add_argument("--frame", type=float, help="Render only the frame at this time to a PNG.")
    p.add_argument("--crf", type=int, default=16)
    p.add_argument("--preset", default="slow", help="x264 preset.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        clip = load_source(args.input, args.duration, args.sequence_fps)
        if args.duration and clip.duration and args.duration < clip.duration:
            clip = clip.subclipped(0, args.duration)
        fx: dict = {}
        if args.kb_preset or args.zoom or args.pan:
            kb = {"enabled": True, "preset": args.kb_preset, "easing": args.easing}
            if args.zoom:
                kb["zoom_start"], kb["zoom_end"] = args.zoom
            if args.pan:
                kb["pan_start"], kb["pan_end"] = args.pan[:2], args.pan[2:]
            fx["ken_burns"] = kb
        if args.grade:
            fx["color_grade"] = {"enabled": True, "preset": args.grade, "intensity": args.intensity}
        if args.speed_lines:
            fx["speed_lines"] = {"enabled": True, "style": args.speed_lines,
                                 "color": args.lines_color, "opacity": args.lines_opacity}
        out = apply_scene_fx(clip, fx, args.resolution, args.fit)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.frame is not None:
            Image.fromarray(_rgb(out.get_frame(args.frame))).save(args.output)
            log.info("Saved frame t=%.2fs -> %s", args.frame, args.output)
            return 0
        out.write_videofile(
            str(args.output), fps=args.fps, codec="libx264", preset=args.preset,
            audio_codec="aac", audio_bitrate="192k", pixel_format="yuv420p",
            ffmpeg_params=["-crf", str(args.crf), "-tune", "animation", "-vf", BT709_VF,
                           *BT709_TAGS, "-movflags", "+faststart"],
            logger="bar",
        )
        log.info("Wrote %s", args.output)
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
