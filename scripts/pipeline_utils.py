"""Shared helpers for the anime video pipeline.

Everything here is dependency-light (stdlib + Pillow) so that every other
module can import it without pulling in moviepy.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMP_DIR = PROJECT_ROOT / "temp"
OUTPUT_DIR = PROJECT_ROOT / "output"
ASSETS_DIR = PROJECT_ROOT / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

# moviepy pipes RGB to ffmpeg; without an explicit matrix ffmpeg converts with
# BT.601 and leaves the file untagged, which players render with a hue shift on
# HD content. These params force a correct, tagged BT.709 conversion.
BT709_VF = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
BT709_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


class PipelineError(RuntimeError):
    """Raised for user-facing pipeline failures (bad config, missing tools...)."""


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%H:%M:%S",
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


log = get_logger("utils")


# --------------------------------------------------------------------------- #
# External binaries
# --------------------------------------------------------------------------- #
def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PipelineError(
            f"'{name}' was not found on PATH. Install FFmpeg (it ships ffmpeg and "
            f"ffprobe) and make sure it is on PATH. See README.md > Setup."
        )
    return path


def run_command(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    log.debug("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()[-2000:]}"
        )
    return proc


def ffprobe_json(path: Path) -> dict:
    require_binary("ffprobe")
    proc = run_command(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
    )
    return json.loads(proc.stdout)


def media_duration(path: Path) -> float:
    info = ffprobe_json(path)
    duration = info.get("format", {}).get("duration")
    if duration is None:
        for stream in info.get("streams", []):
            if stream.get("duration"):
                duration = stream["duration"]
                break
    if duration is None:
        raise PipelineError(f"Could not determine duration of {path}")
    return float(duration)


def has_audio_stream(path: Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in ffprobe_json(path).get("streams", []))


# --------------------------------------------------------------------------- #
# Files / JSON / paths
# --------------------------------------------------------------------------- #
def load_json(path: Path) -> Any:
    path = Path(path)
    if not path.is_file():
        raise PipelineError(f"JSON file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Invalid JSON in {path}: line {exc.lineno} col {exc.colno}: {exc.msg}") from exc


def save_json(path: Path, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def resolve_path(base_dir: Path, value: str | Path | None) -> Path | None:
    """Resolve *value* relative to *base_dir* unless it is absolute."""
    if value is None or value == "":
        return None
    p = Path(value).expanduser()
    return p if p.is_absolute() else (Path(base_dir) / p).resolve()


def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def deep_merge(base: dict, override: dict | None) -> dict:
    """Return a new dict: *override* merged recursively on top of *base*."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def classify_media(path: Path) -> str:
    """Return 'video', 'image', 'sequence' (directory of frames) or 'unknown'."""
    path = Path(path)
    if path.is_dir():
        return "sequence"
    ext = path.suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "unknown"


def list_frames(directory: Path) -> list[Path]:
    frames = sorted(
        p for p in Path(directory).iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS
    )
    if not frames:
        raise PipelineError(f"No image frames found in sequence directory {directory}")
    return frames


# --------------------------------------------------------------------------- #
# Colors
# --------------------------------------------------------------------------- #
def parse_color(value: Any, default: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    """Accept '#RRGGBB', '#RGB', [r, g, b] or None."""
    if value is None:
        return default
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return tuple(int(max(0, min(255, c))) for c in value[:3])  # type: ignore[return-value]
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) == 6:
            try:
                return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
            except ValueError:
                pass
    raise PipelineError(f"Unrecognised color value: {value!r}")


def ass_color(rgb: tuple[int, int, int], alpha: int = 0) -> str:
    """Convert RGB + alpha (0 = opaque, 255 = transparent) to ASS &HAABBGGRR."""
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


# --------------------------------------------------------------------------- #
# Easing
# --------------------------------------------------------------------------- #
def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


EASINGS = {
    "linear": lambda x: x,
    "ease_in": lambda x: x * x * x,
    "ease_out": lambda x: 1 - (1 - x) ** 3,
    # smootherstep: zero velocity *and* acceleration at both ends -> no jolt
    "ease_in_out": lambda x: x * x * x * (x * (x * 6 - 15) + 10),
    "sine": lambda x: 0.5 - 0.5 * math.cos(math.pi * x),
}


def ease(name: str, x: float) -> float:
    fn = EASINGS.get(name)
    if fn is None:
        raise PipelineError(f"Unknown easing '{name}'. Options: {', '.join(EASINGS)}")
    return fn(_clamp01(x))


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
_SYSTEM_FONT_DIRS = [
    FONTS_DIR,
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path.home() / ".fonts",
    Path.home() / ".local/share/fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path.home() / "Library/Fonts",
    Path("C:/Windows/Fonts"),
]
_FALLBACK_FONT_NAMES = [
    "NotoSansCJK-Bold", "NotoSansJP-Bold", "DejaVuSans-Bold", "Arial Bold", "arialbd",
    "Helvetica-Bold", "FreeSansBold", "LiberationSans-Bold", "Inter-Bold",
]


def _iter_font_files() -> Iterable[Path]:
    for d in _SYSTEM_FONT_DIRS:
        if d.is_dir():
            yield from (p for p in d.rglob("*") if p.suffix.lower() in {".ttf", ".otf", ".ttc"})


@lru_cache(maxsize=64)
def find_font(name_or_path: str | None) -> str | None:
    """Return a font file path for PIL, or None to use PIL's built-in font."""
    if name_or_path:
        candidate = Path(name_or_path).expanduser()
        if candidate.is_file():
            return str(candidate)
        local = FONTS_DIR / name_or_path
        if local.is_file():
            return str(local)
    wanted = [name_or_path] if name_or_path else []
    wanted += _FALLBACK_FONT_NAMES
    files = list(_iter_font_files())
    for want in wanted:
        key = Path(want).stem.lower().replace(" ", "").replace("_", "-")
        for f in files:
            if f.stem.lower().replace(" ", "").replace("_", "-") == key:
                return str(f)
    if shutil.which("fc-match") and name_or_path:
        proc = subprocess.run(
            ["fc-match", "-f", "%{file}", name_or_path], capture_output=True, text=True
        )
        if proc.returncode == 0 and Path(proc.stdout.strip()).is_file():
            return proc.stdout.strip()
    if files:
        return str(files[0])
    log.warning("No TrueType fonts found; falling back to Pillow's built-in font.")
    return None


def load_pil_font(name_or_path: str | None, size: int):
    from PIL import ImageFont

    path = find_font(name_or_path)
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def format_seconds(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m):02d}:{s:06.3f}"
