#!/usr/bin/env python3
"""Generate synthetic demo assets so storyboard_template.json renders out of the box.

Creates (procedurally, no downloads):
  assets/images/scene_01.png     sunrise hills (still, for Ken Burns)
  assets/clips/scene_02.mp4      3 s animated city run (video clip)
  assets/clips/scene_03/*.png    16-frame petal loop at 8 fps (AnimateDiff-style frame dir)
  assets/images/scene_04.png     night festival sky (still)
  assets/audio/bgm/theme.wav     24 s synthesized pad + plucks (BGM)

These are placeholders for pipeline testing, not art. Replace them with your
Stable Diffusion / AnimateDiff outputs (see README.md).

  python scripts/make_demo_assets.py [--force]
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from pipeline_utils import ASSETS_DIR, ensure_dir, get_logger, setup_logging

log = get_logger("demo")
RNG = np.random.default_rng(42)


def vertical_gradient(w: int, h: int, top, bottom) -> Image.Image:
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    arr = np.asarray(top, np.float32) * (1 - t) + np.asarray(bottom, np.float32) * t
    return Image.fromarray(np.repeat(arr, w, axis=1).astype(np.uint8))


def hills(draw: ImageDraw.ImageDraw, w: int, h: int, base: float, amp: float, color, phase: float) -> None:
    xs = np.linspace(0, w, 200)
    ys = h * base + amp * h * np.sin(xs / w * 2 * np.pi * 1.3 + phase) * np.cos(xs / w * 3.1 + phase * 0.7)
    draw.polygon([(0, h)] + list(zip(xs, ys)) + [(w, h)], fill=color)


def scene_01(path: Path) -> None:
    w, h = 2304, 1296  # larger than 1080p so the Ken Burns zoom stays sharp
    img = vertical_gradient(w, h, (255, 170, 190), (255, 228, 170))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([w * 0.58, h * 0.22, w * 0.78, h * 0.22 + w * 0.2], fill=(255, 250, 220, 255))
    img.paste(glow.filter(ImageFilter.GaussianBlur(40)), (0, 0), glow.filter(ImageFilter.GaussianBlur(40)))
    img.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(img)
    hills(d, w, h, 0.62, 0.05, (186, 120, 170), 0.3)
    hills(d, w, h, 0.72, 0.04, (120, 80, 140), 1.9)
    hills(d, w, h, 0.84, 0.03, (60, 40, 90), 4.0)
    for _ in range(140):  # petals
        x, y, r = RNG.uniform(0, w), RNG.uniform(0, h), RNG.uniform(5, 14)
        d.ellipse([x - r, y - r * 0.6, x + r, y + r * 0.6], fill=(255, 205, 222))
    img.save(path)


def scene_04(path: Path) -> None:
    w, h = 2304, 1296
    img = vertical_gradient(w, h, (12, 14, 48), (60, 30, 90))
    d = ImageDraw.Draw(img)
    for _ in range(500):
        x, y, r = RNG.uniform(0, w), RNG.uniform(0, h * 0.7), RNG.uniform(0.8, 2.6)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 230))
    d.ellipse([w * 0.15, h * 0.12, w * 0.15 + 220, h * 0.12 + 220], fill=(255, 246, 210))
    hills(d, w, h, 0.80, 0.02, (20, 12, 40), 2.2)
    for i in range(14):  # lantern string
        x = w * (0.08 + i * 0.065)
        y = h * 0.55 + 40 * np.sin(i * 0.7)
        lantern = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(lantern).ellipse([x - 34, y - 46, x + 34, y + 46], fill=(255, 120, 60, 255))
        img.paste(lantern.filter(ImageFilter.GaussianBlur(18)), (0, 0), lantern.filter(ImageFilter.GaussianBlur(18)))
        ImageDraw.Draw(img).ellipse([x - 26, y - 36, x + 26, y + 36], fill=(255, 170, 90))
    img.save(path)


def scene_02(path: Path) -> None:
    from moviepy import ImageSequenceClip

    w, h, fps, seconds = 1280, 720, 24, 3
    sky = vertical_gradient(w, h, (120, 190, 255), (230, 240, 255))
    buildings = [(RNG.uniform(60, 160), RNG.uniform(0.25, 0.6), tuple(int(c) for c in RNG.integers(70, 140, 3)))
                 for _ in range(40)]
    frames = []
    for f in range(fps * seconds):
        img = sky.copy()
        d = ImageDraw.Draw(img)
        x = -((f * 22) % 4000)
        for bw, bh, col in buildings * 2:
            d.rectangle([x, h * (1 - bh), x + bw, h], fill=col)
            for wy in range(int(h * (1 - bh)) + 20, h - 20, 40):
                d.rectangle([x + 14, wy, x + bw - 14, wy + 14], fill=(255, 236, 160))
            x += bw + 10
        bob = 14 * abs(np.sin(f / fps * 2 * np.pi * 2.5))
        cx, cy = w * 0.45, h * 0.62 - bob
        d.ellipse([cx - 60, cy - 160, cx + 60, cy - 40], fill=(255, 224, 200))      # head
        d.polygon([(cx - 70, cy - 40), (cx + 70, cy - 40), (cx + 90, cy + 120), (cx - 90, cy + 120)],
                  fill=(214, 51, 108))                                               # body
        d.ellipse([cx - 70, cy - 190, cx + 80, cy - 90], fill=(60, 40, 90))         # hair
        frames.append(np.asarray(img))
    ImageSequenceClip(frames, fps=fps).write_videofile(
        str(path), codec="libx264", preset="medium", ffmpeg_params=["-crf", "16"],
        pixel_format="yuv420p", audio=False, logger=None,
    )


def scene_03(directory: Path) -> None:
    w, h, n = 1024, 576, 16
    ensure_dir(directory)
    for old in directory.glob("*.png"):
        old.unlink()
    base = vertical_gradient(w, h, (255, 214, 230), (190, 160, 230))
    ImageDraw.Draw(base).rectangle([0, h * 0.78, w, h], fill=(120, 90, 150))
    petals = [(RNG.uniform(0, w), RNG.uniform(0, h), RNG.uniform(6, 16), RNG.uniform(0, 2 * np.pi)) for _ in range(90)]
    for i in range(n):
        img = base.copy()
        d = ImageDraw.Draw(img)
        p = i / n
        for x, y, r, ph in petals:
            px = (x + 60 * p + 18 * np.sin(2 * np.pi * p + ph)) % w
            py = (y + h * 0.5 * p) % h
            d.ellipse([px - r, py - r * 0.55, px + r, py + r * 0.55], fill=(255, 190, 210))
        img.save(directory / f"frame_{i:04d}.png")


def theme(path: Path, seconds: float = 24.0, sr: int = 48_000) -> None:
    t = np.arange(int(seconds * sr)) / sr
    chords = [[220.0, 277.18, 329.63], [196.0, 246.94, 293.66], [174.61, 220.0, 261.63], [196.0, 246.94, 329.63]]
    out = np.zeros_like(t)
    seg = seconds / len(chords)
    for i, chord in enumerate(chords):
        m = (t >= i * seg) & (t < (i + 1) * seg)
        local = t[m] - i * seg
        env = np.minimum(1, local / 0.8) * np.minimum(1, (seg - local) / 0.8)
        for f in chord:
            out[m] += env * (np.sin(2 * np.pi * f * t[m]) + 0.3 * np.sin(2 * np.pi * 2 * f * t[m])) / 6
    for k in range(int(seconds * 2)):  # plucks on eighth notes
        start = int(k * 0.5 * sr)
        n = int(0.4 * sr)
        if start + n > len(t):
            break
        f = chords[int(k * 0.5 // seg) % len(chords)][k % 3] * 2
        tt = np.arange(n) / sr
        out[start:start + n] += 0.12 * np.sin(2 * np.pi * f * tt) * np.exp(-tt * 9)
    out = out / max(1e-9, np.abs(out).max()) * 0.6
    stereo = (np.stack([out, np.roll(out, 240)], axis=1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())


def impact_sfx(path: Path, sr: int = 48_000) -> None:
    """Synthesized hit: low sine drop + noise burst with a fast decay (about 1.2 s)."""
    n = int(1.2 * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(7)
    boom = np.sin(2 * np.pi * (90 - 50 * t) * t) * np.exp(-t * 4)
    crack = rng.standard_normal(n) * np.exp(-t * 25) * 0.6
    out = boom + crack
    out = out / np.abs(out).max() * 0.85
    stereo = (np.stack([out, out], axis=1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())


DEMO_AUDIO = {  # registered in the audio license manifest as project-generated
    "theme.wav": ("demo_theme", "music"),
    "demo_impact.wav": ("demo_impact", "sfx"),
}


def register_demo_audio(paths: list[Path]) -> None:
    from audio_manifest import add, load_manifest, rel_key

    files = load_manifest()["files"]
    for path in paths:
        if path.is_file() and rel_key(path) not in files:
            name, kind = DEMO_AUDIO[path.name]
            add(path, name=name, kind=kind, source="generated", license_="generated", author="make_demo_assets.py",
                url=None, attribution=None)
            log.info("registered %s in the audio manifest", path.relative_to(ASSETS_DIR.parent))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--force", action="store_true", help="Overwrite existing demo assets.")
    args = p.parse_args(argv)
    setup_logging()
    jobs = [
        (ASSETS_DIR / "images" / "scene_01.png", scene_01),
        (ASSETS_DIR / "clips" / "scene_02.mp4", scene_02),
        (ASSETS_DIR / "clips" / "scene_03", scene_03),
        (ASSETS_DIR / "images" / "scene_04.png", scene_04),
        (ASSETS_DIR / "audio" / "bgm" / "theme.wav", theme),
        (ASSETS_DIR / "audio" / "sfx" / "demo_impact.wav", impact_sfx),
    ]
    for target, fn in jobs:
        exists = target.is_dir() and any(target.glob("*.png")) if fn is scene_03 else target.exists()
        if exists and not args.force:
            log.info("exists   %s (use --force to regenerate)", target.relative_to(ASSETS_DIR.parent))
            continue
        ensure_dir(target.parent)
        fn(target)
        log.info("created  %s", target.relative_to(ASSETS_DIR.parent))
    register_demo_audio([ASSETS_DIR / "audio" / "bgm" / "theme.wav", ASSETS_DIR / "audio" / "sfx" / "demo_impact.wav"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
