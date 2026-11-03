#!/usr/bin/env python3
"""Install the VOICEVOX engine (Japanese TTS) without the Japanese-only desktop app.

Downloads the official Windows CPU engine package from the VOICEVOX/voicevox_engine GitHub
release, extracts it, registers it for this pipeline (VOICEVOX_RUN), and checks that it speaks.

Facts from the engine's own release workflow (.github/workflows/build-engine.yml):
  - package name: voicevox_engine-windows-cpu-<version>
  - "<package>.vvpp" is a plain ZIP of the engine folder (run.exe + voice models included);
    above 1.9 GB it is split into "<package>.001.vvppp", "<package>.002.vvppp", ... and
    "<package>.vvpp.txt" lists the parts. Parts are raw byte splits, so concatenating them
    gives the ZIP back. No 7-Zip needed.
The CPU build is used on purpose: speech is fast enough on CPU, and it leaves the GPU to ComfyUI.

  python scripts/setup_voicevox.py                 # latest release
  python scripts/setup_voicevox.py --version 0.25.0
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from pipeline_utils import PipelineError, ensure_dir, get_logger, setup_logging

log = get_logger("vvsetup")

REPO = "VOICEVOX/voicevox_engine"
TARGET = "windows-cpu"


def default_install_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "voicevox_engine" if local else Path.home() / "voicevox_engine"


def _get(url: str, timeout: int = 30) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "anime-video-pipeline-setup",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.geturl()


def latest_version(api: str, web: str) -> str:
    try:
        data, _ = _get(f"{api}/repos/{REPO}/releases/latest")
        return json.loads(data)["tag_name"]
    except (urllib.error.URLError, KeyError, ValueError) as exc:
        log.warning("GitHub API unavailable (%s); falling back to the release page redirect.", exc)
    try:
        _, final = _get(f"{web}/{REPO}/releases/latest")
    except urllib.error.URLError as exc:
        raise PipelineError(f"Cannot reach GitHub to find the latest VOICEVOX engine ({exc}). "
                            "Check your internet connection or pass --version.") from exc
    if "/tag/" not in final:
        raise PipelineError(f"Unexpected release redirect: {final}. Pass --version explicitly.")
    return final.rsplit("/tag/", 1)[1].strip("/")


def part_names(web: str, version: str, package: str) -> list[str]:
    """Read '<package>.vvpp.txt' (the release's own list of VVPP parts)."""
    url = f"{web}/{REPO}/releases/download/{version}/{package}.vvpp.txt"
    try:
        data, _ = _get(url)
    except urllib.error.HTTPError as exc:
        raise PipelineError(f"Release {version} has no {package}.vvpp.txt (HTTP {exc.code}). "
                            f"Check that version {version} exists and ships a {TARGET} build.") from exc
    names = [Path(line.strip()).name for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
    names = [n for n in names if n.endswith((".vvpp", ".vvppp"))]
    if not names:
        raise PipelineError(f"{package}.vvpp.txt lists no package parts.")
    return sorted(names)


def install(version: str | None, root: Path, api: str, web: str, keep_download: bool) -> Path:
    from setup_models import download  # resumable downloader with retries

    version = version or latest_version(api, web)
    package = f"voicevox_engine-{TARGET}-{version}"
    dest = root / version
    existing = next(dest.rglob("run.exe"), None) if dest.is_dir() else None
    if existing:
        log.info("Already installed: %s", existing)
        return existing
    parts = part_names(web, version, package)
    dl_dir = ensure_dir(root / "_download" / version)
    log.info("VOICEVOX engine %s (%s): %d part(s): %s", version, TARGET, len(parts), ", ".join(parts))
    for name in parts:
        target = dl_dir / name
        if target.is_file():
            log.info("have     %s", name)
            continue
        log.info("download %s", name)
        download(f"{web}/{REPO}/releases/download/{version}/{name}", target, 0)
    zip_path = dl_dir / f"{package}.zip"
    with zip_path.open("wb") as out:  # split parts are raw byte ranges of one ZIP
        for name in parts:
            with (dl_dir / name).open("rb") as part:
                shutil.copyfileobj(part, out, 16 * 1024 * 1024)
    log.info("extracting -> %s", dest)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = [n for n in zf.namelist() if n.startswith(("/", "\\")) or ".." in Path(n).parts]
            if bad:
                raise PipelineError(f"Refusing to extract unsafe paths from the package: {bad[:3]}")
            zf.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise PipelineError(f"The downloaded package is not a valid ZIP ({exc}). Delete {dl_dir} and retry.") from exc
    run_exe = next(dest.rglob("run.exe"), None)
    if run_exe is None:
        raise PipelineError(f"run.exe not found after extracting to {dest}.")
    if not keep_download:
        shutil.rmtree(dl_dir, ignore_errors=True)
    return run_exe


def register(run_exe: Path) -> None:
    os.environ["VOICEVOX_RUN"] = str(run_exe)
    if sys.platform == "win32":
        subprocess.run(["setx", "VOICEVOX_RUN", str(run_exe)], check=False, capture_output=True)
        log.info("Saved VOICEVOX_RUN=%s (new terminals will see it).", run_exe)
    else:
        log.info("Set VOICEVOX_RUN=%s in your shell profile to make it permanent.", run_exe)


def verify() -> int:
    from voicevox_client import VoicevoxServer

    with VoicevoxServer(startup_timeout=180) as vv:
        speakers = vv.speakers()
    print(f"VOICEVOX engine OK: {len(speakers)} characters installed.")
    return len(speakers)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Install the VOICEVOX engine (Windows CPU build) for this pipeline.")
    p.add_argument("--version", help="Engine release tag (default: latest).")
    p.add_argument("--dir", type=Path, default=default_install_root(), help="Install root.")
    p.add_argument("--keep-download", action="store_true", help="Keep the downloaded package parts.")
    p.add_argument("--no-verify", action="store_true", help="Skip starting the engine afterwards.")
    p.add_argument("--github-api", default="https://api.github.com", help=argparse.SUPPRESS)
    p.add_argument("--github-web", default="https://github.com", help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose)
    try:
        run_exe = install(args.version, args.dir, args.github_api.rstrip("/"), args.github_web.rstrip("/"),
                          args.keep_download)
        register(run_exe)
        if not args.no_verify:
            verify()
        print(f"Installed: {run_exe}")
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted; re-run to resume the download.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
