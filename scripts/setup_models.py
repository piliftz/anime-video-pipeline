#!/usr/bin/env python3
"""Download and verify the models the pipeline needs, into a ComfyUI folder.

- Resolves the right file from the Hugging Face API (no hard-coded filenames).
- Resumable: an interrupted download continues from where it stopped (*.part).
- Verifies every .safetensors file is complete (header vs. actual size).

Examples (Windows PowerShell):
  .venv\\Scripts\\python scripts\\setup_models.py --comfy-dir C:\\Users\\you\\comfyui-anime
  .venv\\Scripts\\python scripts\\setup_models.py --comfy-dir C:\\Users\\you\\comfyui-anime --verify
  .venv\\Scripts\\python scripts\\setup_models.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from pipeline_utils import PipelineError, ensure_dir, get_logger, setup_logging

log = get_logger("models")

HF = "https://huggingface.co"
MODELS = {
    "animagine-xl-4.0": {
        "repo": "cagliostrolab/animagine-xl-4.0",
        "folder": "checkpoints",
        "license": "CreativeML Open RAIL++-M (commercial use allowed with its conditions)",
        "about": "SDXL anime checkpoint; default for this pipeline.",
    },
    # Files and folders exactly as listed in ComfyUI's official template video_wan2_2_5B_ti2v.json.
    "wan2.2-ti2v-5b": {
        "files": [
            ("Comfy-Org/Wan_2.2_ComfyUI_Repackaged", "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors",
             "diffusion_models"),
            ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
             "text_encoders"),
            ("Comfy-Org/Wan_2.2_ComfyUI_Repackaged", "split_files/vae/wan2.2_vae.safetensors", "vae"),
        ],
        "license": "Apache 2.0 (Wan 2.2)",
        "about": "Wan 2.2 TI2V-5B image-to-video (~17 GB total: model 9.3 + text encoder 6.3 + VAE 1.3).",
    },
}
DEFAULT_MODELS = ["animagine-xl-4.0"]
CHUNK = 4 * 1024 * 1024


def verify_safetensors(path: Path) -> tuple[bool, int, int]:
    """Return (complete, actual_size, expected_size) from the safetensors header."""
    have = path.stat().st_size
    try:
        with path.open("rb") as f:
            n = struct.unpack("<Q", f.read(8))[0]
            if n <= 0 or n > 100_000_000:
                return False, have, -1
            header = json.loads(f.read(n))
    except (OSError, struct.error, ValueError):
        return False, have, -1
    ends = [v["data_offsets"][1] for k, v in header.items() if k != "__metadata__"]
    need = 8 + n + (max(ends) if ends else 0)
    return have == need, have, need


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "anime-video-pipeline"})
    token = os.environ.get("HF_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PipelineError(f"{url}: access denied (HTTP {exc.code}). The model may be gated: accept its terms "
                                "on huggingface.co and set HF_TOKEN.") from exc
        raise PipelineError(f"{url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise PipelineError(f"Cannot reach Hugging Face ({exc.reason}). Check your internet connection.") from exc


def resolve_file(repo: str) -> tuple[str, int]:
    """Pick the single-file checkpoint: the largest .safetensors at the repo root."""
    info = _get_json(f"{HF}/api/models/{repo}?blobs=true")
    files = [(s["rfilename"], int(s.get("size") or 0)) for s in info.get("siblings", [])
             if s["rfilename"].endswith(".safetensors") and "/" not in s["rfilename"]]
    if not files:
        raise PipelineError(f"No root-level .safetensors file in {repo}.")
    return max(files, key=lambda f: f[1])


def download(url: str, dest: Path, expected: int, retries: int = 8) -> None:
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        if expected and have >= expected:
            break
        req = urllib.request.Request(url, headers={"User-Agent": "anime-video-pipeline"})
        if os.environ.get("HF_TOKEN"):
            req.add_header("Authorization", f"Bearer {os.environ['HF_TOKEN']}")
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:      # server ignored Range: start over
                    have = 0
                mode = "ab" if have else "wb"
                total = expected or (have + int(r.headers.get("Content-Length", 0)))
                t0, last = time.time(), 0.0
                done = have
                with part.open(mode) as f:
                    while True:
                        chunk = r.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last > 1.0:
                            speed = (done - have) / max(1e-6, now - t0) / 1e6
                            pct = 100 * done / total if total else 0
                            eta = (total - done) / (speed * 1e6) if speed > 0 and total else 0
                            print(f"\r  {done / 1e9:6.2f} / {total / 1e9:.2f} GB  {pct:5.1f}%  "
                                  f"{speed:5.1f} MB/s  ETA {eta / 60:4.1f} min ", end="", flush=True)
                            last = now
                print()
            if not expected or part.stat().st_size >= expected:
                break
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            wait = min(60, 2 ** attempt)
            print()
            log.warning("Download interrupted (%s); resuming in %ds (attempt %d/%d)", exc, wait, attempt, retries)
            time.sleep(wait)
    else:
        raise PipelineError(f"Download failed after {retries} attempts; re-run the same command to resume.")
    part.replace(dest)


def file_size(repo: str, path: str, _cache: dict = {}) -> int:
    if repo not in _cache:
        info = _get_json(f"{HF}/api/models/{repo}?blobs=true")
        _cache[repo] = {s["rfilename"]: int(s.get("size") or 0) for s in info.get("siblings", [])}
    if path not in _cache[repo]:
        raise PipelineError(f"{path} is no longer in {repo} on Hugging Face; the repo layout changed.")
    return _cache[repo][path]


def install(name: str, comfy_dir: Path) -> list[Path]:
    spec = MODELS[name]
    log.info("Model %s: %s  [license: %s]", name, spec["about"], spec["license"])
    if "files" in spec:
        return [fetch(comfy_dir, repo, path, file_size(repo, path), folder, name)
                for repo, path, folder in spec["files"]]
    filename, size = resolve_file(spec["repo"])
    return [fetch(comfy_dir, spec["repo"], filename, size, spec["folder"], name)]


def fetch(comfy_dir: Path, repo: str, repo_path: str, size: int, folder_name: str, name: str) -> Path:
    folder = ensure_dir(comfy_dir / "models" / folder_name)
    filename = repo_path.rsplit("/", 1)[-1]
    dest = folder / filename
    if dest.exists():
        ok, have, need = verify_safetensors(dest)
        if ok:
            log.info("present  %s (%.2f GB, verified)", dest, have / 1e9)
            return dest
        log.warning("%s is incomplete (%.2f of %.2f GB); re-downloading.", dest.name, have / 1e9, need / 1e9)
        dest.rename(dest.with_name(dest.name + ".part"))
    log.info("Downloading %s (%s, %.2f GB) -> %s", name, filename, size / 1e9, folder)
    download(f"{HF}/{repo}/resolve/main/{repo_path}", dest, size)
    ok, have, need = verify_safetensors(dest)
    if not ok:
        raise PipelineError(f"{dest} failed verification ({have} of {need} bytes). Delete it and re-run.")
    log.info("verified %s (%.2f GB)", dest.name, have / 1e9)
    return dest


def verify_all(comfy_dir: Path) -> bool:
    files = sorted((comfy_dir / "models").rglob("*.safetensors"))
    if not files:
        print(f"No .safetensors files under {comfy_dir / 'models'}")
        return True
    all_ok = True
    for f in files:
        ok, have, need = verify_safetensors(f)
        all_ok &= ok
        print(f"{'OK    ' if ok else 'BROKEN'}  {f.relative_to(comfy_dir)}  {have / 1e9:.2f} of {need / 1e9:.2f} GB")
    return all_ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Download/verify models for the anime pipeline.")
    p.add_argument("--comfy-dir", type=Path, default=os.environ.get("COMFYUI_DIR"),
                   help="ComfyUI folder (default $COMFYUI_DIR).")
    p.add_argument("--model", action="append", choices=list(MODELS), help="Model(s) to install (default: all).")
    p.add_argument("--verify", action="store_true", help="Only check existing .safetensors files.")
    p.add_argument("--list", action="store_true", help="List known models.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        if args.list:
            for k, v in MODELS.items():
                print(f"{k:<18} {v['about']}  [{v['license']}]")
            return 0
        if not args.comfy_dir:
            raise PipelineError("Pass --comfy-dir or set COMFYUI_DIR to your ComfyUI folder.")
        comfy_dir = Path(args.comfy_dir).expanduser().resolve()
        if not (comfy_dir / "main.py").is_file():
            raise PipelineError(f"{comfy_dir} does not look like a ComfyUI folder (no main.py).")
        if args.verify:
            return 0 if verify_all(comfy_dir) else 1
        for name in args.model or DEFAULT_MODELS:
            install(name, comfy_dir)
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        print()
        log.error("Interrupted. Re-run the same command to resume the download.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
