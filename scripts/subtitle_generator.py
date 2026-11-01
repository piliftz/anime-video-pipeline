#!/usr/bin/env python3
"""Anime-style subtitles: build SRT + styled ASS, and burn them into video.

The ASS output uses a heavy colored outline, soft drop shadow, slight edge blur
and quick fades (typical fansub/broadcast anime look), with optional per-speaker
outline colors and a small speaker-name tag above the line.

Subcommands:
  from-timeline  Build subtitles from temp/<name>_timeline.json (written by the orchestrator).
  from-srt       Restyle an existing .srt into anime-styled .ass.
  burn           Burn .ass/.srt subtitles into a video (single high-quality encode).

Examples:
  python scripts/subtitle_generator.py from-timeline temp/my_anime_short_timeline.json
  python scripts/subtitle_generator.py from-srt subs.srt -o temp/subs.ass --storyboard storyboard_template.json
  python scripts/subtitle_generator.py burn -i output/clean.mp4 -s temp/subs.ass -o output/subbed.mp4
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from pipeline_utils import (
    FONTS_DIR,
    PipelineError,
    ass_color,
    deep_merge,
    ensure_dir,
    get_logger,
    has_audio_stream,
    load_json,
    parse_color,
    require_binary,
    setup_logging,
)

log = get_logger("subs")

SUBTITLE_DEFAULTS: dict = {
    "enabled": True,
    "burn_in": True,
    "export_srt": True,
    "soft_subtitles": False,     # also mux the SRT as a selectable mov_text track
    "show_speaker": True,
    "max_chars_per_line": 42,
    "max_lines": 2,
    "min_cue_duration": 0.8,
    "gap": 0.05,                 # seconds kept free between consecutive cues
    "style": {
        "font": "DejaVu Sans",
        "font_size": 54,
        "bold": True,
        "italic": False,
        "primary_color": "#FFFFFF",
        "outline_color": "#2B1B5A",
        "shadow_color": "#000000",
        "shadow_alpha": 110,     # 0 opaque .. 255 invisible
        "outline": 4.5,
        "shadow": 2.0,
        "blur": 0.6,
        "spacing": 0.5,
        "margin_v": 64,
        "margin_h": 120,
        "alignment": 2,          # numpad layout: 2 = bottom center, 8 = top center
        "fade_in_ms": 120,
        "fade_out_ms": 120,
        "speaker_font_scale": 0.7,
    },
}


@dataclass
class Cue:
    start: float
    end: float
    text: str
    speaker: str | None = None


# --------------------------------------------------------------------------- #
# Cue construction
# --------------------------------------------------------------------------- #
def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[぀-ヿ㐀-䶿一-鿿가-힯]", text))


def wrap_text(text: str, max_chars: int) -> list[str]:
    text = " ".join(text.split())
    if _has_cjk(text) and " " not in text:
        return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]
    return textwrap.wrap(text, width=max_chars, break_long_words=True) or [""]


def split_segment(start: float, end: float, text: str, speaker: str | None, cfg: dict) -> list[Cue]:
    """Break one timed sentence into readable cues of <= max_lines lines each."""
    lines = wrap_text(text, int(cfg["max_chars_per_line"]))
    per_cue = max(1, int(cfg["max_lines"]))
    chunks = ["\n".join(lines[i:i + per_cue]) for i in range(0, len(lines), per_cue)]
    total = sum(len(c) for c in chunks) or 1
    cues, t = [], start
    for chunk in chunks:
        span = (end - start) * len(chunk) / total
        cues.append(Cue(t, t + span, chunk, speaker))
        t += span
    return cues


def normalize_cues(cues: list[Cue], cfg: dict, total_duration: float | None = None) -> list[Cue]:
    """Sort, enforce minimum duration, remove overlaps, clamp to the video length."""
    cues = sorted((c for c in cues if c.text.strip()), key=lambda c: c.start)
    min_d, gap = float(cfg["min_cue_duration"]), float(cfg["gap"])
    for i, cue in enumerate(cues):
        if cue.end - cue.start < min_d:
            cue.end = cue.start + min_d
        if i + 1 < len(cues) and cue.end > cues[i + 1].start - gap:
            cue.end = max(cue.start + 0.2, cues[i + 1].start - gap)
        if total_duration is not None:
            cue.end = min(cue.end, total_duration)
    return [c for c in cues if c.end > c.start]


def cues_from_timeline(timeline: dict, cfg: dict) -> list[Cue]:
    cues: list[Cue] = []
    for scene in timeline.get("scenes", []):
        nar = scene.get("narration")
        if not nar or not nar.get("text") or not nar.get("subtitle", True):
            continue
        base = float(nar["start"])
        bounds = nar.get("boundaries") or [{"start": 0.0, "end": nar["duration"], "text": nar["text"]}]
        for b in bounds:
            cues.extend(split_segment(base + b["start"], base + b["end"], b["text"], nar.get("speaker"), cfg))
    return normalize_cues(cues, cfg, timeline.get("duration"))


# --------------------------------------------------------------------------- #
# SRT
# --------------------------------------------------------------------------- #
def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[Cue], path: Path, show_speaker: bool = True) -> Path:
    ensure_dir(path.parent)
    blocks = []
    for i, c in enumerate(cues, start=1):
        text = f"{c.speaker}: {c.text}" if show_speaker and c.speaker else c.text
        blocks.append(f"{i}\n{_srt_time(c.start)} --> {_srt_time(c.end)}\n{text}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


_SRT_TIME = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"


def parse_srt(path: Path) -> list[Cue]:
    raw = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.strip().split("\n")
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        m = re.match(rf"\s*{_SRT_TIME}\s*-->\s*{_SRT_TIME}", lines[idx])
        if not m:
            continue
        g = m.groups()

        def secs(h: str, mi: str, s: str, frac: str) -> float:
            return int(h) * 3600 + int(mi) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000

        start, end = secs(*g[0:4]), secs(*g[4:8])
        text = "\n".join(lines[idx + 1:]).strip()
        speaker = None
        sm = re.match(r"^([^\s:：]{1,24})[:：]\s+(.+)$", text, flags=re.S)
        if sm:
            speaker, text = sm.group(1), sm.group(2)
        cues.append(Cue(start, end, re.sub(r"<[^>]+>", "", text), speaker))
    if not cues:
        raise PipelineError(f"No cues parsed from {path}")
    return cues


# --------------------------------------------------------------------------- #
# ASS
# --------------------------------------------------------------------------- #
def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "⧵").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _style_name(speaker: str | None) -> str:
    if not speaker:
        return "Default"
    return "SPK_" + re.sub(r"[^A-Za-z0-9_]", "_", speaker)


def _style_line(name: str, s: dict, outline_rgb: tuple[int, int, int]) -> str:
    fields = [
        name, s["font"], str(int(s["font_size"])),
        ass_color(parse_color(s["primary_color"])),
        ass_color(parse_color(s["primary_color"])),
        ass_color(outline_rgb),
        ass_color(parse_color(s["shadow_color"]), int(s["shadow_alpha"])),
        "-1" if s["bold"] else "0", "-1" if s["italic"] else "0", "0", "0",
        "100", "100", str(s["spacing"]), "0", "1",
        str(s["outline"]), str(s["shadow"]), str(int(s["alignment"])),
        str(int(s["margin_h"])), str(int(s["margin_h"])), str(int(s["margin_v"])), "1",
    ]
    return "Style: " + ",".join(fields)


def build_ass(cues: list[Cue], cfg: dict, size: tuple[int, int], speakers: dict | None = None) -> str:
    s = cfg["style"]
    speakers = speakers or {}
    W, H = size
    # Scale pixel-based style values when rendering at non-1080p (e.g. draft mode).
    k = H / 1080.0
    s = {**s, "font_size": s["font_size"] * k, "outline": round(s["outline"] * k, 2),
         "shadow": round(s["shadow"] * k, 2), "margin_v": s["margin_v"] * k, "margin_h": s["margin_h"] * k}
    default_outline = parse_color(s["outline_color"])

    styles = {"Default": _style_line("Default", s, default_outline)}
    for cue in cues:
        name = _style_name(cue.speaker)
        if name not in styles:
            spk = speakers.get(cue.speaker or "", {})
            styles[name] = _style_line(name, s, parse_color(spk.get("subtitle_color"), default_outline))

    head = [
        "[Script Info]",
        "; Generated by subtitle_generator.py",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        *styles.values(),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    tag_size = int(s["font_size"] * s["speaker_font_scale"])
    for cue in cues:
        name = _style_name(cue.speaker)
        prefix = f"{{\\fad({int(s['fade_in_ms'])},{int(s['fade_out_ms'])})\\blur{s['blur']}}}"
        body = _ass_escape(cue.text)
        if cfg.get("show_speaker", True) and cue.speaker:
            spk = speakers.get(cue.speaker, {})
            tag_color = ass_color(parse_color(spk.get("subtitle_color"), default_outline))
            # Speaker tag: smaller, colored fill with white outline, on its own line.
            body = (f"{{\\fs{tag_size}\\1c{tag_color}&\\3c&H00FFFFFF&\\bord{max(1.0, s['outline'] * 0.6):.1f}}}"
                    f"{_ass_escape(cue.speaker)}{{\\r}}\\N{body}")
        events.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},{name},"
                      f"{_ass_escape(cue.speaker or '')},0,0,0,,{prefix}{body}")
    return "\n".join(head + events) + "\n"


def write_ass(cues: list[Cue], path: Path, cfg: dict, size: tuple[int, int], speakers: dict | None = None) -> Path:
    ensure_dir(path.parent)
    path.write_text(build_ass(cues, cfg, size, speakers), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Encoding / burn-in
# --------------------------------------------------------------------------- #
def final_encode(
    src: Path,
    output: Path,
    ass_path: Path | None = None,
    srt_path: Path | None = None,
    fps: float = 24,
    crf: int = 16,
    preset: str = "slow",
    tune: str | None = "animation",
    audio_bitrate: str = "192k",
    fonts_dir: Path | None = FONTS_DIR,
    overwrite: bool = True,
) -> Path:
    """Single final H.264 encode: [burn ASS] -> BT.709 yuv420p -> MP4 (+optional soft SRT)."""
    import ffmpeg  # ffmpeg-python handles filter-argument escaping for us

    require_binary("ffmpeg")
    src, output = Path(src).resolve(), Path(output).resolve()
    ensure_dir(output.parent)
    inp = ffmpeg.input(str(src))
    video = inp.video
    # Paths inside a filtergraph need ffmpeg's two-level escaping, which breaks on Windows
    # drive letters (C:\...). So ffmpeg runs *inside* the subtitle's folder and the filter
    # only ever sees a bare filename plus a relative, forward-slash fonts path.
    workdir = None
    if ass_path is not None:
        ass_path = Path(ass_path).resolve()
        workdir = ass_path.parent
        kwargs = {}
        if fonts_dir and Path(fonts_dir).is_dir() and any(Path(fonts_dir).iterdir()):
            try:
                kwargs["fontsdir"] = Path(os.path.relpath(Path(fonts_dir).resolve(), workdir)).as_posix()
            except ValueError:  # Windows: fonts dir on a different drive than temp/
                log.warning("assets/fonts is on another drive than %s; using system fonts only.", workdir)
        video = video.filter("ass", ass_path.name, **kwargs)
    # RGB master -> BT.709 limited-range 4:2:0 (see pipeline_utils.BT709_VF for why).
    video = video.filter("scale", out_color_matrix="bt709", out_range="tv").filter("format", "yuv420p")

    streams = [video]
    out_kwargs = {
        "vcodec": "libx264", "crf": crf, "preset": preset, "r": fps,
        "movflags": "+faststart", "profile:v": "high",
        "colorspace": "bt709", "color_primaries": "bt709", "color_trc": "bt709", "color_range": "tv",
    }
    if tune:
        out_kwargs["tune"] = tune
    if has_audio_stream(src):
        streams.append(inp.audio)
        out_kwargs.update({"acodec": "aac", "audio_bitrate": audio_bitrate})
    if srt_path is not None:
        streams.append(ffmpeg.input(str(srt_path)))
        out_kwargs.update({"scodec": "mov_text"})
    out = ffmpeg.output(*streams, str(output), **out_kwargs).global_args("-hide_banner", "-loglevel", "warning", "-stats")
    log.info("Encoding %s (crf=%s, preset=%s%s)", output, crf, preset, ", burning subtitles" if ass_path else "")
    cmd = out.compile(overwrite_output=overwrite)
    log.debug("ffmpeg (cwd=%s): %s", workdir, " ".join(cmd))
    proc = subprocess.run(cmd, cwd=workdir)
    if proc.returncode != 0:
        raise PipelineError(f"ffmpeg final encode failed for {output} (exit {proc.returncode}); see the ffmpeg "
                            "messages above.")
    return output


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _subtitle_cfg(storyboard_path: Path | None) -> tuple[dict, dict]:
    if storyboard_path:
        sb = load_json(storyboard_path)
        return deep_merge(SUBTITLE_DEFAULTS, sb.get("subtitles")), sb.get("speakers", {})
    return deep_merge(SUBTITLE_DEFAULTS, {}), {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate and burn anime-style subtitles.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("from-timeline", help="Build SRT+ASS from an orchestrator timeline JSON.")
    t.add_argument("timeline", type=Path)
    t.add_argument("--storyboard", type=Path, help="Storyboard for subtitle style/speakers (default: path in timeline).")
    t.add_argument("--srt", type=Path, help="SRT output path (default next to timeline).")
    t.add_argument("--ass", type=Path, help="ASS output path (default next to timeline).")

    s = sub.add_parser("from-srt", help="Convert an SRT into anime-styled ASS.")
    s.add_argument("srt", type=Path)
    s.add_argument("-o", "--output", type=Path, required=True)
    s.add_argument("--storyboard", type=Path, help="Take style + speaker colors from this storyboard.")
    s.add_argument("--resolution", default="1920x1080")

    b = sub.add_parser("burn", help="Burn subtitles into a video.")
    b.add_argument("-i", "--input", type=Path, required=True)
    b.add_argument("-s", "--subs", type=Path, required=True, help=".ass or .srt")
    b.add_argument("-o", "--output", type=Path, required=True)
    b.add_argument("--storyboard", type=Path, help="Style source when --subs is an .srt.")
    b.add_argument("--fps", type=float, default=24)
    b.add_argument("--crf", type=int, default=16)
    b.add_argument("--preset", default="slow")
    b.add_argument("--soft", action="store_true", help="Also mux the SRT as a soft subtitle track.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        if args.cmd == "from-timeline":
            timeline = load_json(args.timeline)
            sb_path = args.storyboard or (Path(timeline["storyboard"]) if timeline.get("storyboard") else None)
            cfg, speakers = _subtitle_cfg(sb_path)
            cues = cues_from_timeline(timeline, cfg)
            stem = args.timeline.with_name(args.timeline.stem.replace("_timeline", ""))
            srt = write_srt(cues, args.srt or stem.with_suffix(".srt"), cfg["show_speaker"])
            ass = write_ass(cues, args.ass or stem.with_suffix(".ass"), cfg, tuple(timeline["canvas"]), speakers)
            print(f"{len(cues)} cues -> {srt}, {ass}")
        elif args.cmd == "from-srt":
            cfg, speakers = _subtitle_cfg(args.storyboard)
            w, h = (int(x) for x in args.resolution.lower().split("x"))
            cues = normalize_cues(parse_srt(args.srt), cfg)
            print(write_ass(cues, args.output, cfg, (w, h), speakers))
        elif args.cmd == "burn":
            if not args.input.is_file():
                raise PipelineError(f"Input video not found: {args.input}")
            ass_path, srt_path = args.subs, None
            if args.subs.suffix.lower() == ".srt":
                from pipeline_utils import ffprobe_json

                vs = next(s for s in ffprobe_json(args.input)["streams"] if s["codec_type"] == "video")
                cfg, speakers = _subtitle_cfg(args.storyboard)
                ass_path = write_ass(normalize_cues(parse_srt(args.subs), cfg), args.subs.with_suffix(".styled.ass"),
                                     cfg, (int(vs["width"]), int(vs["height"])), speakers)
                srt_path = args.subs if args.soft else None
            final_encode(args.input, args.output, ass_path=ass_path, srt_path=srt_path,
                         fps=args.fps, crf=args.crf, preset=args.preset)
            print(args.output)
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
