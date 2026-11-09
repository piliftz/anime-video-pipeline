#!/usr/bin/env python3
"""Qwen3-TTS voice-clone worker. Runs inside the Qwen3 Python env ($QWEN3_TTS_PYTHON), not the pipeline's.

voiceover_generator writes a job file and calls this once per render, so the model loads once for all lines:
  {"model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
   "lines": [{"id", "out", "language", "seed", "sentence_pause",
              "segments": [{"text", "ref_audio", "ref_text"}, ...]}, ...]}
Each segment (a sentence, or the whole line) is cloned in ICL mode (reference audio + its exact transcript): the
voice AND the delivery (calm, shout, ...) come from that segment's reference clip; Qwen3 Base takes no text
instructions. Segments are joined with sentence_pause seconds of silence. Writes 16-bit mono WAVs (24 kHz) to
each "out" path and a result file {"ok": [ids], "errors": {id: message}, "boundaries": {id: [{start, end, text}]}}
(boundaries only for lines with more than one segment).

Usage: <qwen3 env python> scripts/qwen3_tts_worker.py job.json result.json
Needs: torch (CUDA build matching the GPU), qwen-tts, soundfile, numpy; the model in the HF cache (HF_HOME).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def run(job_path: Path, result_path: Path) -> int:
    import numpy as np
    import soundfile as sf
    import torch
    from huggingface_hub import snapshot_download
    from qwen_tts import Qwen3TTSModel

    job = json.loads(job_path.read_text(encoding="utf-8"))
    model_id = job["model"]
    local = model_id if Path(model_id).is_dir() else snapshot_download(model_id, local_files_only=True)
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device != "cpu" else torch.float32
    t0 = time.perf_counter()
    model = Qwen3TTSModel.from_pretrained(local, device_map=device, dtype=dtype, attn_implementation="sdpa")
    print(f"qwen3 worker: loaded {model_id} on {device} in {time.perf_counter() - t0:.1f}s", flush=True)

    prompts: dict[tuple[str, str], object] = {}
    ok: list[str] = []
    errors: dict[str, str] = {}
    boundaries: dict[str, list[dict]] = {}
    for line in job["lines"]:
        try:
            t = time.perf_counter()
            parts, spans, sr, cursor = [], [], 24000, 0.0
            for i, seg in enumerate(line["segments"]):
                key = (seg["ref_audio"], seg["ref_text"])
                if key not in prompts:
                    prompts[key] = model.create_voice_clone_prompt(ref_audio=key[0], ref_text=key[1])
                torch.manual_seed(int(line.get("seed", 1)))
                wavs, sr = model.generate_voice_clone(text=seg["text"], language=line.get("language") or "English",
                                                      voice_clone_prompt=prompts[key])
                part = np.asarray(wavs[0], dtype=np.float32)
                if part.size == 0:
                    raise RuntimeError(f"model returned no audio for segment {i + 1}")
                if parts:
                    gap = np.zeros(int(sr * float(line.get("sentence_pause", 0.35))), np.float32)
                    parts.append(gap)
                    cursor += gap.size / sr
                parts.append(part)
                end = cursor + part.size / sr
                spans.append({"start": round(cursor, 3), "end": round(end, 3), "text": seg["text"]})
                cursor = end
            wav = np.concatenate(parts)
            if len(spans) > 1:
                boundaries[line["id"]] = spans
            Path(line["out"]).parent.mkdir(parents=True, exist_ok=True)
            sf.write(line["out"], np.clip(wav, -1.0, 1.0), sr, subtype="PCM_16")
            ok.append(line["id"])
            print(f"qwen3 worker: {line['id']}: {wav.size / sr:.2f}s audio in {time.perf_counter() - t:.1f}s",
                  flush=True)
        except Exception as exc:  # one bad line must not lose the rest of the batch
            errors[line["id"]] = f"{type(exc).__name__}: {exc}"
            print(f"qwen3 worker: {line['id']} FAILED: {errors[line['id']]}", flush=True)
    result_path.write_text(json.dumps({"ok": ok, "errors": errors, "boundaries": boundaries}, indent=2),
                           encoding="utf-8")
    return 0 if not errors else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__)
        return 2
    return run(Path(args[0]), Path(args[1]))


if __name__ == "__main__":
    raise SystemExit(main())
