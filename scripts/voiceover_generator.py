#!/usr/bin/env python3
"""Neural voiceover generation with edge-tts.

NOTE: edge-tts is free and needs no API key, but it is NOT offline. It sends
text to Microsoft's online Edge "Read Aloud" service. Use ``--fallback silence``
to keep the rest of the pipeline working offline (silent placeholder tracks of
estimated length).

Outputs, per line:
  <out_dir>/<id>_<hash>.wav    48 kHz PCM, ready for editing
  <out_dir>/<id>_<hash>.json   sidecar: text, voice, duration, sentence timings

Examples:
  python scripts/voiceover_generator.py --text "Hello there." --out temp/hello.wav
  python scripts/voiceover_generator.py --json storyboard_template.json
  python scripts/voiceover_generator.py --json lines.json --voice ja-JP-NanamiNeural
  python scripts/voiceover_generator.py --list-voices --locale ja-JP
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import re
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_utils import (
    TEMP_DIR,
    PipelineError,
    ensure_dir,
    get_logger,
    load_json,
    media_duration,
    require_binary,
    save_json,
    setup_logging,
)

log = get_logger("voiceover")

DEFAULT_VOICE = "en-US-AnaNeural"
TICKS_PER_SECOND = 10_000_000  # edge-tts offsets are in 100 ns units
SAMPLE_RATE = 48_000
FALLBACK_WORDS_PER_SECOND = 2.6
FALLBACK_CJK_CHARS_PER_SECOND = 7.0
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？…])\s*")


@dataclass
class VoiceLine:
    id: str
    text: str
    voice: str = DEFAULT_VOICE
    rate: str = "+0%"
    pitch: str = "+0Hz"
    volume: str = "+0%"
    speaker: str | None = None
    extra: dict = field(default_factory=dict)
    engine: str = "edge"                              # "edge" (edge-tts) or "voicevox"
    vv: dict = field(default_factory=dict)            # voicevox: speaker, style, speed, pitch, intonation, ...

    def cache_key(self) -> str:
        raw = "|".join([self.text, self.voice, self.rate, self.pitch, self.volume])
        if self.engine != "edge":
            raw += "|" + self.engine + "|" + json.dumps(self.vv, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]

    def stem(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", self.id).strip("_") or "line"
        return f"{safe}_{self.cache_key()}"


# --------------------------------------------------------------------------- #
# Parsing inputs
# --------------------------------------------------------------------------- #
def _validate_prosody(value: str, unit: str, name: str) -> str:
    if not re.fullmatch(rf"[+-]\d+{unit}", value):
        raise PipelineError(f"{name} must look like '+10{unit}' or '-5{unit}', got {value!r}")
    return value


def make_line(entry: dict, defaults: dict, index: int) -> VoiceLine | None:
    text = (entry.get("text") or "").strip()
    if not text:
        return None
    line = VoiceLine(
        id=str(entry.get("id") or f"line_{index:03d}"),
        text=text,
        voice=entry.get("voice") or defaults.get("voice") or DEFAULT_VOICE,
        rate=_validate_prosody(entry.get("rate") or defaults.get("rate") or "+0%", "%", "rate"),
        pitch=_validate_prosody(entry.get("pitch") or defaults.get("pitch") or "+0Hz", "Hz", "pitch"),
        volume=_validate_prosody(entry.get("volume") or defaults.get("volume") or "+0%", "%", "volume"),
        speaker=entry.get("speaker"),
        engine=entry.get("engine") or defaults.get("engine") or "edge",
        vv={**(defaults.get("voicevox") or {}), **(entry.get("voicevox") or {})},
    )
    if line.engine not in ("edge", "voicevox"):
        raise PipelineError(f"[{line.id}] unknown TTS engine '{line.engine}' (use edge or voicevox).")
    if line.engine == "voicevox" and not line.vv.get("speaker"):
        raise PipelineError(f"[{line.id}] engine voicevox needs voicevox.speaker (a VOICEVOX character name).")
    return line


def lines_from_storyboard(storyboard: dict) -> list[VoiceLine]:
    defaults = storyboard.get("voice_defaults", {})
    speakers = storyboard.get("speakers", {})
    lines: list[VoiceLine] = []
    for i, scene in enumerate(storyboard.get("scenes", []), start=1):
        narration = scene.get("narration") or {}
        if not narration.get("text"):
            continue
        speaker_cfg = speakers.get(narration.get("speaker") or "", {})
        merged_defaults = {**defaults, **{k: v for k, v in speaker_cfg.items() if k in ("voice", "rate", "pitch", "volume", "engine", "voicevox")}}
        entry = {**narration, "id": scene.get("id") or f"scene_{i:02d}"}
        line = make_line(entry, merged_defaults, i)
        if line:
            lines.append(line)
    return lines


def lines_from_json(data) -> list[VoiceLine]:
    """Accept a storyboard, a list of line dicts, or {"defaults":..., "lines": [...]}."""
    if isinstance(data, dict) and "scenes" in data:
        return lines_from_storyboard(data)
    defaults: dict = {}
    entries = data
    if isinstance(data, dict):
        defaults = data.get("defaults") or data.get("voice_defaults") or {}
        entries = data.get("lines")
    if not isinstance(entries, list):
        raise PipelineError("JSON must be a storyboard, a list of lines, or an object with a 'lines' list.")
    lines = []
    for i, entry in enumerate(entries, start=1):
        if isinstance(entry, str):
            entry = {"text": entry}
        line = make_line(entry, defaults, i)
        if line:
            lines.append(line)
    return lines


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #
def _mp3_to_wav(src: Path, dst: Path) -> None:
    require_binary("ffmpeg")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(SAMPLE_RATE),
         "-c:a", "pcm_s16le", str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise PipelineError(f"ffmpeg failed converting {src} -> {dst}: {proc.stderr.strip()}")


TRIM_THRESHOLD_DBFS = -45.0
TRIM_PAD_SECONDS = 0.04


def trim_silence(wav_path: Path) -> tuple[float, float]:
    """Cut leading/trailing silence from a mono 16-bit WAV in place (keeps a 40 ms pad).

    TTS engines pad lines with silence; on 1-2 s action cuts that padding alone stretches the cut.
    Returns (seconds removed at the start, seconds removed at the end).
    """
    import numpy as np

    with wave.open(str(wav_path), "rb") as wf:
        params = wf.getparams()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    if params.nchannels != 1 or params.sampwidth != 2 or data.size == 0:
        return 0.0, 0.0
    threshold = 32768 * 10 ** (TRIM_THRESHOLD_DBFS / 20)
    loud = np.flatnonzero(np.abs(data.astype(np.int32)) > threshold)
    if loud.size == 0:
        return 0.0, 0.0
    pad = int(TRIM_PAD_SECONDS * params.framerate)
    start, end = max(0, int(loud[0]) - pad), min(data.size, int(loud[-1]) + 1 + pad)
    if start == 0 and end == data.size:
        return 0.0, 0.0
    with wave.open(str(wav_path), "wb") as wf:
        wf.setparams(params)
        wf.writeframes(data[start:end].tobytes())
    return start / params.framerate, (data.size - end) / params.framerate


def _shift_boundaries(boundaries: list[dict], lead: float, duration: float) -> list[dict]:
    out = []
    for b in boundaries:
        s = min(duration, max(0.0, b["start"] - lead))
        e = min(duration, max(s, b["end"] - lead))
        out.append({**b, "start": round(s, 3), "end": round(e, 3)})
    return out


def _write_silence(dst: Path, seconds: float) -> None:
    frames = int(round(seconds * SAMPLE_RATE))
    with wave.open(str(dst), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"\x00\x00" * frames)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_RE.split(text) if p.strip()]
    return parts or [text.strip()]


def estimate_speech_seconds(text: str, rate: str = "+0%") -> float:
    cjk = len(_CJK_RE.findall(text))
    words = len(re.sub(_CJK_RE, " ", text).split())
    seconds = words / FALLBACK_WORDS_PER_SECOND + cjk / FALLBACK_CJK_CHARS_PER_SECOND
    seconds += 0.35 * max(0, len(split_sentences(text)) - 1)  # sentence pauses
    rate_pct = int(rate.rstrip("%")) if rate else 0
    seconds /= max(0.25, 1 + rate_pct / 100)
    return max(1.0, round(seconds, 3))


def proportional_boundaries(text: str, duration: float) -> list[dict]:
    """Spread sentences over *duration* proportionally to character count."""
    sentences = split_sentences(text)
    total = sum(len(s) for s in sentences) or 1
    t, out = 0.0, []
    for s in sentences:
        span = duration * len(s) / total
        out.append({"start": round(t, 3), "end": round(t + span, 3), "text": s})
        t += span
    return out


async def _synthesize_edge(line: VoiceLine, mp3_path: Path, proxy: str | None) -> list[dict]:
    import edge_tts  # imported lazily so --help works without the package

    communicate = edge_tts.Communicate(
        line.text, line.voice, rate=line.rate, pitch=line.pitch, volume=line.volume,
        boundary="SentenceBoundary", proxy=proxy,
    )
    boundaries: list[dict] = []
    got_audio = False
    with mp3_path.open("wb") as fh:
        async for chunk in communicate.stream():
            ctype = chunk.get("type")
            if ctype == "audio":
                fh.write(chunk["data"])
                got_audio = True
            elif ctype in ("SentenceBoundary", "WordBoundary"):
                start = chunk["offset"] / TICKS_PER_SECOND
                end = (chunk["offset"] + chunk["duration"]) / TICKS_PER_SECOND
                boundaries.append({"start": round(start, 3), "end": round(end, 3), "text": chunk["text"]})
    if not got_audio:
        raise PipelineError(f"edge-tts returned no audio for '{line.id}'")
    return boundaries


_VV_CLIENT = None  # set by synthesize_all while a VOICEVOX engine session is open


def _synthesize_voicevox(line: VoiceLine, raw_wav: Path) -> list[dict]:
    """Blocking VOICEVOX call (run in a worker thread). Records the resolved style in line.vv."""
    if _VV_CLIENT is None:
        raise PipelineError("VOICEVOX engine session is not open.")
    style_id, uuid, used = _VV_CLIENT.resolve(line.vv["speaker"], line.vv.get("style"))
    raw_wav.write_bytes(_VV_CLIENT.synthesize(line.text, style_id, line.vv))
    line.vv = {**line.vv, "style_used": used, "style_id": style_id, "speaker_uuid": uuid}
    return []  # sentence timings are spread proportionally (VOICEVOX gives per-mora data, not sentence events)


async def synthesize_line(
    line: VoiceLine,
    out_dir: Path,
    fallback: str = "none",
    force: bool = False,
    retries: int = 3,
    proxy: str | None = None,
) -> dict:
    """Synthesize one line (cached). Returns the sidecar dict."""
    ensure_dir(out_dir)
    wav_path = out_dir / f"{line.stem()}.wav"
    meta_path = out_dir / f"{line.stem()}.json"
    if not force and wav_path.is_file() and meta_path.is_file():
        meta = load_json(meta_path)
        # Re-synthesize cached silent placeholders once a real voice is possible.
        if not meta.get("fallback") or fallback == "silence":
            if not meta.get("fallback") and not meta.get("trimmed"):  # cached before trimming existed
                lead, tail = trim_silence(wav_path)
                duration = media_duration(wav_path)
                meta.update(duration=round(duration, 3), trimmed=True,
                            boundaries=_shift_boundaries(meta.get("boundaries", []), lead, duration))
                save_json(meta_path, meta)
                log.info("trimmed  %-14s -%.2fs lead, -%.2fs tail (cached line)", line.id, lead, tail)
            log.info("cached   %-14s %.2fs  %s", line.id, meta["duration"], wav_path.name)
            return meta

    mp3_path = out_dir / f"{line.stem()}.{'mp3' if line.engine == 'edge' else 'raw.wav'}"
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if line.engine == "voicevox":
                boundaries = await asyncio.to_thread(_synthesize_voicevox, line, mp3_path)
            else:
                boundaries = await _synthesize_edge(line, mp3_path, proxy)
            _mp3_to_wav(mp3_path, wav_path)
            mp3_path.unlink(missing_ok=True)
            lead, tail = trim_silence(wav_path)
            duration = media_duration(wav_path)
            boundaries = _shift_boundaries(boundaries, lead, duration) if boundaries else \
                proportional_boundaries(line.text, duration)
            meta = {
                "id": line.id, "text": line.text, "speaker": line.speaker, "voice": line.voice,
                "rate": line.rate, "pitch": line.pitch, "volume": line.volume,
                "audio": str(wav_path), "duration": round(duration, 3),
                "boundaries": boundaries, "fallback": False, "engine": line.engine, "voicevox": line.vv or None,
                "trimmed": True, "trimmed_seconds": [round(lead, 3), round(tail, 3)],
            }
            save_json(meta_path, meta)
            who = f"VOICEVOX {line.vv.get('speaker')}/{line.vv.get('style_used', '')}" if line.engine == "voicevox" else line.voice
            log.info("voiced   %-14s %.2fs  %s (%s; trimmed %.2fs)",
                     line.id, duration, wav_path.name, who, lead + tail)
            return meta
        except Exception as exc:  # network errors, service hiccups, bad voice names
            last_error = exc
            mp3_path.unlink(missing_ok=True)
            if attempt < retries:
                wait = 2 ** attempt
                log.warning("%s failed for '%s' (attempt %d/%d): %s -- retrying in %ds",
                            line.engine, line.id, attempt, retries, exc, wait)
                await asyncio.sleep(wait)

    if fallback != "silence":
        raise PipelineError(
            f"Voiceover for '{line.id}' ({line.engine}) failed after {retries} attempts: {last_error}\n"
            + ("Start the VOICEVOX app or set VOICEVOX_RUN. "
               if line.engine == "voicevox" and "Cannot reach" in str(last_error) else "" if line.engine == "voicevox" else
               "edge-tts needs internet access to speech.platform.bing.com. ") +
            "Re-run with --tts-fallback silence (orchestrator) / --fallback silence to continue offline."
        )
    duration = estimate_speech_seconds(line.text, line.rate)
    _write_silence(wav_path, duration)
    meta = {
        "id": line.id, "text": line.text, "speaker": line.speaker, "voice": line.voice,
        "rate": line.rate, "pitch": line.pitch, "volume": line.volume,
        "audio": str(wav_path), "duration": duration,
        "boundaries": proportional_boundaries(line.text, duration),
        "fallback": True, "error": str(last_error),
    }
    save_json(meta_path, meta)
    log.warning("SILENT   %-14s %.2fs  placeholder (edge-tts unavailable: %s)", line.id, duration, last_error)
    return meta


async def synthesize_all(
    lines: list[VoiceLine],
    out_dir: Path,
    fallback: str = "none",
    force: bool = False,
    concurrency: int = 4,
    proxy: str | None = None,
) -> dict[str, dict]:
    global _VV_CLIENT
    sem = asyncio.Semaphore(max(1, concurrency))

    async def run(line: VoiceLine) -> tuple[str, dict]:
        async with sem:
            return line.id, await synthesize_line(line, out_dir, fallback, force, proxy=proxy)

    def cached(line: VoiceLine) -> bool:
        meta = out_dir / f"{line.stem()}.json"
        return not force and meta.is_file() and not load_json(meta).get("fallback")

    # Open the VOICEVOX engine only if some VOICEVOX line actually needs synthesis.
    need_vv = any(l.engine == "voicevox" and not cached(l) for l in lines)
    with contextlib.ExitStack() as stack:
        if need_vv:
            from voicevox_client import VoicevoxServer

            try:
                _VV_CLIENT = stack.enter_context(VoicevoxServer())
            except PipelineError:
                if fallback != "silence":
                    raise
                log.warning("VOICEVOX engine unavailable; Japanese lines will be silent placeholders.")
        try:
            results = await asyncio.gather(*(run(l) for l in lines))
        finally:
            _VV_CLIENT = None
    return dict(results)


def generate_for_storyboard(
    storyboard: dict,
    out_dir: Path = TEMP_DIR / "voiceovers",
    fallback: str = "none",
    force: bool = False,
    proxy: str | None = None,
) -> dict[str, dict]:
    """Synchronous entry point used by the orchestrator. Keyed by scene id."""
    lines = lines_from_storyboard(storyboard)
    if not lines:
        return {}
    log.info("Generating %d voiceover line(s) -> %s", len(lines), out_dir)
    return asyncio.run(synthesize_all(lines, out_dir, fallback, force, proxy=proxy))


async def list_voices(locale: str | None) -> list[dict]:
    import edge_tts

    voices = await edge_tts.list_voices()
    if locale:
        voices = [v for v in voices if v["Locale"].lower().startswith(locale.lower())]
    return sorted(voices, key=lambda v: v["ShortName"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate neural voiceovers with edge-tts.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="Single line of text to speak.")
    src.add_argument("--json", type=Path, help="Storyboard JSON or a JSON list of lines.")
    src.add_argument("--list-voices", action="store_true", help="List available voices and exit.")
    p.add_argument("--locale", help="Filter --list-voices by locale prefix, e.g. ja-JP or en.")
    p.add_argument("--out", type=Path, help="Output .wav path (only with --text).")
    p.add_argument("--out-dir", type=Path, default=TEMP_DIR / "voiceovers", help="Output directory.")
    p.add_argument("--voice", help=f"Voice short name (default {DEFAULT_VOICE}).")
    p.add_argument("--rate", help="Speaking rate, e.g. +10%% or -5%%.")
    p.add_argument("--pitch", help="Pitch shift, e.g. +20Hz.")
    p.add_argument("--volume", help="Volume, e.g. +0%%.")
    p.add_argument("--fallback", choices=["none", "silence"], default="none",
                   help="What to do if edge-tts is unreachable.")
    p.add_argument("--proxy", help="HTTP proxy URL for edge-tts.")
    p.add_argument("--force", action="store_true", help="Ignore cache and re-synthesize.")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        if args.list_voices:
            for v in asyncio.run(list_voices(args.locale)):
                print(f"{v['ShortName']:<40} {v['Gender']:<7} {v['Locale']}")
            return 0

        overrides = {k: getattr(args, k) for k in ("voice", "rate", "pitch", "volume") if getattr(args, k)}
        if args.text:
            line = make_line({"id": "line", "text": args.text, **overrides}, {}, 1)
            if line is None:
                raise PipelineError("--text is empty.")
            out_dir = args.out.parent if args.out else args.out_dir
            meta = asyncio.run(synthesize_line(line, out_dir, args.fallback, args.force, proxy=args.proxy))
            if args.out:
                src = Path(meta["audio"])
                args.out.parent.mkdir(parents=True, exist_ok=True)
                if src.resolve() != args.out.resolve():
                    src.replace(args.out)
                    Path(src.with_suffix(".json")).replace(args.out.with_suffix(".json"))
                print(args.out)
            else:
                print(meta["audio"])
            return 0

        data = load_json(args.json)
        lines = lines_from_json(data)
        for line in lines:
            for k, v in overrides.items():
                setattr(line, k, v)
        if not lines:
            raise PipelineError(f"No narration text found in {args.json}")
        results = asyncio.run(synthesize_all(lines, args.out_dir, args.fallback, args.force,
                                             args.concurrency, args.proxy))
        manifest = save_json(args.out_dir / "manifest.json", results)
        print(f"{len(results)} voiceover(s) written. Manifest: {manifest}")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
