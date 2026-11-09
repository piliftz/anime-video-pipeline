#!/usr/bin/env python3
"""Voiceover generation: edge-tts (online), VOICEVOX (local, Japanese) and Qwen3-TTS voice clones (local).

NOTE: edge-tts is free and needs no API key, but it is NOT offline. It sends
text to Microsoft's online Edge "Read Aloud" service. Use ``--fallback silence``
to keep the rest of the pipeline working offline (silent placeholder tracks of
estimated length).

Qwen3 lines (engine "qwen3") clone a registered character voice, assets/voices/<voice>_ref.json, from the
reference clip for the requested delivery (calm, shout, ...). They run in a separate Python env
($QWEN3_TTS_PYTHON) through scripts/qwen3_tts_worker.py, one model load per render.

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
import os
import re
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from pipeline_utils import (
    ASSETS_DIR,
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
ENGINES = ("edge", "voicevox", "qwen3")
VOICE_KEYS = ("voice", "rate", "pitch", "volume", "engine", "voicevox", "qwen3")  # speaker keys that set the voice
VOICES_DIR = ASSETS_DIR / "voices"
QWEN3_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
QWEN3_DEFAULTS = {"voice": None, "delivery": None, "seed": 1, "language": "English", "sentence_pause": 0.35}
QWEN3_WORKER = Path(__file__).resolve().parent / "qwen3_tts_worker.py"


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
    engine: str = "edge"                              # "edge" (edge-tts), "voicevox" or "qwen3"
    vv: dict = field(default_factory=dict)            # voicevox: speaker, style, speed, pitch, intonation, ...
    qw: dict = field(default_factory=dict)            # qwen3, resolved: voice, delivery, seed, ref_audio, ref_sha...

    def cache_key(self) -> str:
        raw = "|".join([self.text, self.voice, self.rate, self.pitch, self.volume])
        if self.engine != "edge":
            settings = self.qw if self.engine == "qwen3" else self.vv
            raw += "|" + self.engine + "|" + json.dumps(settings, sort_keys=True, ensure_ascii=False)
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


def resolve_qwen3_voice(cfg: dict, text: str, line_id: str) -> dict:
    """Merge qwen3 settings with defaults and resolve reference clips from assets/voices/<voice>_ref.json.

    The registry's ``engine.refs`` maps a delivery name to {audio (relative to the registry file), text (its exact
    transcript)}. ``delivery`` is one name for the whole line, or a list with one name per sentence (the last one
    repeats), e.g. ["calm", "shout"]: quiet setup, shouted payoff. Clip hashes go into the cache key, so
    replacing a reference re-voices its lines.
    """
    cfg = {**QWEN3_DEFAULTS, **cfg}
    voice, delivery = cfg.get("voice"), cfg.get("delivery")
    if not voice:
        raise PipelineError(f"[{line_id}] engine qwen3 needs qwen3.voice (a name in assets/voices/<name>_ref.json).")
    registry = VOICES_DIR / f"{voice}_ref.json"
    if not registry.is_file():
        raise PipelineError(f"[{line_id}] qwen3 voice '{voice}' is not registered: {registry} is missing.")
    engine = load_json(registry).get("engine") or {}
    refs = engine.get("refs") or {}
    if not refs:
        raise PipelineError(f"[{line_id}] {registry.name} has no engine.refs (delivery -> reference clip).")
    if not delivery:
        if len(refs) != 1:
            raise PipelineError(f"[{line_id}] qwen3 voice '{voice}' needs qwen3.delivery: one of {sorted(refs)}.")
        delivery = next(iter(refs))
    names = [delivery] if isinstance(delivery, str) else list(delivery)
    if not names or not all(isinstance(n, str) for n in names):
        raise PipelineError(f"[{line_id}] qwen3.delivery must be a name or a list of names, got {delivery!r}.")
    sentences = split_sentences(text) if len(names) > 1 else [text]
    segments = []
    for i, sentence in enumerate(sentences):
        name = names[min(i, len(names) - 1)]
        if name not in refs:
            raise PipelineError(f"[{line_id}] qwen3 voice '{voice}' has no delivery '{name}' "
                                f"(registered: {sorted(refs)}).")
        ref = refs[name]
        audio = (registry.parent / ref["audio"]).resolve()
        if not audio.is_file():
            raise PipelineError(f"[{line_id}] reference clip for {voice}/{name} is missing: {audio} "
                                "(voice WAVs are gitignored; copy them onto this machine).")
        if not (ref.get("text") or "").strip():
            raise PipelineError(f"[{line_id}] {registry.name}: refs.{name}.text (the clip's transcript) is empty.")
        segments.append({"text": sentence, "delivery": name, "ref_audio": str(audio), "ref_text": ref["text"].strip(),
                         "ref_sha": hashlib.sha1(audio.read_bytes()).hexdigest()[:12]})
    return {"voice": voice, "delivery": delivery, "seed": int(cfg["seed"]), "language": cfg["language"],
            "sentence_pause": float(cfg["sentence_pause"]), "model": engine.get("model_id") or QWEN3_MODEL,
            "segments": segments}


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
    if line.engine not in ENGINES:
        raise PipelineError(f"[{line.id}] unknown TTS engine '{line.engine}' (use {', '.join(ENGINES)}).")
    if line.engine == "qwen3":
        line.qw = resolve_qwen3_voice({**(defaults.get("qwen3") or {}), **(entry.get("qwen3") or {})}, text,
                                      line.id)
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
        merged_defaults = {**defaults, **{k: v for k, v in speaker_cfg.items() if k in VOICE_KEYS}}
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


_QW_ERRORS: dict[str, str] = {}  # line id -> error from the last Qwen3 worker batch
_QW_BOUNDARIES: dict[str, list[dict]] = {}  # line id -> sentence timings measured by the worker


def qwen3_python() -> Path:
    exe = os.environ.get("QWEN3_TTS_PYTHON")
    if not exe:
        raise PipelineError("QWEN3_TTS_PYTHON is not set: point it at the python.exe of the Qwen3-TTS env "
                            "(e.g. C:\\Users\\you\\tools\\tts\\qwen3\\Scripts\\python.exe).")
    if not Path(exe).is_file():
        raise PipelineError(f"QWEN3_TTS_PYTHON points at a missing file: {exe}")
    return Path(exe)


def run_qwen3_batch(lines: list[VoiceLine], out_dir: Path) -> None:
    """Voice every pending Qwen3 line in ONE worker process per model (it loads once). Writes <stem>.raw.wav;
    per-line failures land in _QW_ERRORS. Raises PipelineError if the worker cannot run at all."""
    _QW_ERRORS.clear()
    _QW_BOUNDARIES.clear()
    exe = qwen3_python()
    job_dir = ensure_dir(out_dir / "qwen3_jobs")
    for model in sorted({l.qw["model"] for l in lines}):
        batch = [l for l in lines if l.qw["model"] == model]
        tag = hashlib.sha1("|".join(l.stem() for l in batch).encode()).hexdigest()[:10]
        job, result = job_dir / f"job_{tag}.json", job_dir / f"result_{tag}.json"
        result.unlink(missing_ok=True)
        save_json(job, {"model": model, "lines": [
            {"id": l.id, "out": str(out_dir / f"{l.stem()}.raw.wav"), "language": l.qw["language"],
             "seed": l.qw["seed"], "sentence_pause": l.qw["sentence_pause"],
             "segments": [{k: seg[k] for k in ("text", "ref_audio", "ref_text")} for seg in l.qw["segments"]]}
            for l in batch]})
        env = {**os.environ, "HF_HUB_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
        if os.environ.get("QWEN3_TTS_HF_HOME"):
            env["HF_HOME"] = os.environ["QWEN3_TTS_HF_HOME"]
        log.info("Qwen3-TTS: voicing %d line(s) with %s", len(batch), model)
        # When RAM is tight, Windows can fail to page in the memory-mapped weights while the model loads, and the
        # worker dies natively (0xC0000005, in torch storage __getitem__). The files are fine and a retry usually
        # loads, so allow a few attempts; -X faulthandler puts the native crash site in stderr.
        attempts = 3
        for attempt in range(1, attempts + 1):
            proc = subprocess.run([str(exe), "-I", "-X", "faulthandler", str(QWEN3_WORKER), str(job), str(result)],
                                  env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.is_file() or attempt == attempts:
                break
            log.warning("Qwen3-TTS worker crashed (exit %d, attempt %d/%d; usually low free RAM while loading); "
                        "retrying in 5 s.", proc.returncode, attempt, attempts)
            time.sleep(5)
        for row in proc.stdout.splitlines():
            if row.startswith("qwen3 worker:"):
                log.info("%s", row)
        if not result.is_file():
            tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-8:])
            raise PipelineError(f"Qwen3-TTS worker failed (exit {proc.returncode}) after {attempts} attempts. Check "
                                f"QWEN3_TTS_PYTHON / QWEN3_TTS_HF_HOME and that {model} is downloaded; an access "
                                f"violation while loading usually means too little free RAM (close big apps).\n{tail}")
        res = load_json(result)
        _QW_ERRORS.update(res.get("errors") or {})
        _QW_BOUNDARIES.update(res.get("boundaries") or {})


def _synthesize_qwen3(line: VoiceLine, raw_wav: Path) -> list[dict]:
    """The batch already ran; pick up its output for this line."""
    if line.id in _QW_ERRORS:
        raise PipelineError(f"Qwen3-TTS: {_QW_ERRORS[line.id]}")
    if not raw_wav.is_file():
        raise PipelineError("Qwen3-TTS worker produced no audio for this line.")
    return _QW_BOUNDARIES.get(line.id, [])  # measured per sentence when the line is split, else proportional


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
            elif line.engine == "qwen3":
                boundaries = _synthesize_qwen3(line, mp3_path)
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
                "qwen3": line.qw or None,
                "trimmed": True, "trimmed_seconds": [round(lead, 3), round(tail, 3)],
            }
            save_json(meta_path, meta)
            who = (f"VOICEVOX {line.vv.get('speaker')}/{line.vv.get('style_used', '')}" if line.engine == "voicevox"
                   else f"Qwen3 {line.qw['voice']}/{'+'.join(s['delivery'] for s in line.qw['segments'])} "
                        f"seed {line.qw['seed']}"
                   if line.engine == "qwen3" else line.voice)
            log.info("voiced   %-14s %.2fs  %s (%s; trimmed %.2fs)",
                     line.id, duration, wav_path.name, who, lead + tail)
            return meta
        except Exception as exc:  # network errors, service hiccups, bad voice names
            last_error = exc
            mp3_path.unlink(missing_ok=True)
            if line.engine == "qwen3":
                break  # local batch output: retrying cannot change it
            if attempt < retries:
                wait = 2 ** attempt
                log.warning("%s failed for '%s' (attempt %d/%d): %s -- retrying in %ds",
                            line.engine, line.id, attempt, retries, exc, wait)
                await asyncio.sleep(wait)

    if fallback != "silence":
        raise PipelineError(
            f"Voiceover for '{line.id}' ({line.engine}) failed: {last_error}\n"
            + ("Start the VOICEVOX app or set VOICEVOX_RUN. "
               if line.engine == "voicevox" and "Cannot reach" in str(last_error) else "" if line.engine != "edge" else
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
    log.warning("SILENT   %-14s %.2fs  placeholder (%s unavailable: %s)", line.id, duration, line.engine,
                last_error)
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
        pending_qw = [l for l in lines if l.engine == "qwen3" and not cached(l)]
        if pending_qw:
            try:
                await asyncio.to_thread(run_qwen3_batch, pending_qw, out_dir)
            except PipelineError as exc:
                if fallback != "silence":
                    raise
                log.warning("Qwen3-TTS unavailable (%s); its lines will be silent placeholders.", exc)
                _QW_ERRORS.update({l.id: str(exc) for l in pending_qw})
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
