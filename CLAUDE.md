# CLAUDE.md — Anime Video Pipeline

Project manual for Claude Code sessions. Read this before changing anything.

## What this repo is
A local Python pipeline that turns a JSON storyboard (scene order, clip/image/frame-sequence
paths, narration text, FX toggles, text overlays) into a finished **1920×1080, 24 fps H.264 MP4**
with neural voiceover (edge-tts), anime color grading, Ken Burns camera moves, speed lines,
burned-in ASS subtitles and ducked BGM.

## Layout
```
scripts/
  pipeline_utils.py       shared helpers: paths, JSON, ffprobe, colors, easing, fonts, BT.709 consts
  voiceover_generator.py  edge-tts -> 48 kHz WAV + sentence-timing JSON sidecar (cached by hash)
  cinematic_fx.py         camera (fit + Ken Burns), color grade presets, speed lines, source loader
  subtitle_generator.py   cue building, SRT/ASS writers, final ffmpeg encode (burn-in)
  video_orchestrator.py   main entry: storyboard -> timeline -> scenes -> master -> final MP4
  make_demo_assets.py     procedural placeholder assets so the template renders out of the box
  comfy_client.py         stdlib HTTP client for ComfyUI (/prompt, /history, /view, /object_info) + auto-start
  image_generator.py      storyboard `generate` blocks -> SDXL (+hires) graphs -> assets/generated/*.png + sidecars
  setup_models.py         resumable Hugging Face model download into $COMFYUI_DIR + safetensors integrity check
  video_generator.py      keyframe -> Wan 2.2 TI2V-5B image-to-video (graph = official template + start_image);
                          frames returned as lossless PNGs into a folder (usable as a scene clip) + MP4 preview
make_video.cmd            Windows one-command entry: orchestrator --generate-images
my_story.json             full example storyboard using only generated images
promo/                    Remotion + Three.js + GSAP promo toolkit (TitleCard, Teaser, CountdownReel); see
                          promo/README.md. Rule: every pixel is a pure function of useCurrentFrame()
                          (no Math.random, no r3f useFrame, GSAP only via useGsapTimeline seek). Fonts are
                          bundled in promo/public/fonts. Verify with `npx tsc --noEmit` and
                          `npx remotion still ... --gl=swangle --browser-executable=<headless chrome>`.
storyboard_template.json  reference storyboard; every supported key appears here
assets/ {clips,images,audio/bgm,fonts}   inputs (fonts/ is passed to libass as fontsdir)
temp/                     voiceover cache, timeline JSON, ASS, lossless master (gitignored)
output/                   final MP4 + SRT (gitignored)
```

## Environment setup
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ffmpeg -hide_banner -filters | grep -w ass      # must list the libass 'ass' filter
python scripts/make_demo_assets.py
```
System deps: Python ≥ 3.10, FFmpeg + ffprobe on PATH built with libass, libx264, libx264rgb.

## Commands
```bash
python scripts/video_orchestrator.py storyboard_template.json --dry-run       # validate + timeline, no TTS/render
python scripts/video_orchestrator.py storyboard_template.json --draft         # 960x540 fast preview
python scripts/video_orchestrator.py storyboard_template.json                 # full quality
python scripts/video_orchestrator.py storyboard_template.json --tts-fallback silence   # offline
python scripts/video_orchestrator.py storyboard_template.json --scenes scene_02 -o output/s2.mp4
python scripts/voiceover_generator.py --list-voices --locale ja-JP
python scripts/cinematic_fx.py assets/images/scene_01.png --frame 2 -o temp/still.png --duration 5 --grade anime_vivid
python scripts/subtitle_generator.py from-timeline temp/sakura_horizon_timeline.json
python scripts/video_orchestrator.py my_story.json --generate-images --draft     # needs ComfyUI ($COMFYUI_DIR)
python scripts/image_generator.py my_story.json --scenes s02_hikari --force
python scripts/setup_models.py --verify
python scripts/setup_models.py --model wan2.2-ti2v-5b
python scripts/video_generator.py assets/generated/s02_hikari.png --prompt "..." [--length 121 --size 1280x704]
```
ComfyUI is optional: everything except `--generate-images` works without it. There is no real
ComfyUI in the cloud sandbox; test the client against a mock that implements the same HTTP routes
and validates graphs against real node input names (check ComfyUI's `nodes.py` on GitHub when
changing graphs).
Verification after any code change (there is no test suite yet):
```bash
python -m py_compile scripts/*.py && python -m pyflakes scripts/
python scripts/video_orchestrator.py storyboard_template.json --dry-run
python scripts/video_orchestrator.py storyboard_template.json --draft --tts-fallback silence
```
Then extract frames (`ffmpeg -ss T -i out.mp4 -frames:v 1 f.png`) and look at them. Do not claim
a visual change works without inspecting frames.

## Architecture rules (do not break these)
- **moviepy 2.x API only** (`with_duration`, `subclipped`, `with_effects`, `transform`,
  `image_transform`, `time_transform`). `moviepy.editor`, `set_*`, `fl*`, `subclip` are 1.x and fail.
- **Exactly one lossy encode.** Scenes render to a lossless RGB master
  (`libx264rgb -qp 0`, PCM) in `temp/`; `subtitle_generator.final_encode` does the only lossy pass.
  Never add an intermediate lossy write.
- **Color:** the final pass converts RGB → yuv420p with `out_color_matrix=bt709` and tags
  bt709. Any direct moviepy MP4 write must use `BT709_VF` + `BT709_TAGS` from `pipeline_utils`.
- **Single resample for camera moves:** fit + Ken Burns are one `Image.resize(box=...)` call.
  Don't chain `Resize` + `Crop` effects; that double-filters and jitters.
- **Frame math is planar float32** in `grade_frame` (3,H,W). Interleaved (H,W,3) broadcasting
  measured ~4× slower. Keep per-frame work vectorized; no per-pixel Python loops.
- Time remapping (loop/boomerang/freeze) is explicit and clamped in `fit_duration`; moviepy's
  `Loop`/`TimeSymmetrize` crash `ImageSequenceClip` via out-of-range `t`.
- Storyboard paths resolve **relative to the storyboard file's directory**.
- New storyboard keys need: a default in the relevant `*_DEFAULTS` dict, validation in
  `validate_storyboard`, an example in `storyboard_template.json`, and a README entry.
- ComfyUI graphs use only core nodes (CheckpointLoaderSimple, CLIPTextEncode, EmptyLatentImage, KSampler,
  VAEDecode/Encode, ImageScaleBy, SaveImage; for Wan also UNETLoader, ModelSamplingSD3, CLIPLoader, VAELoader,
  LoadImage via /upload/image, Wan22ImageToVideoLatent). Video frames come back via SaveImage, NOT SaveVideo
  (its inputs change often). Validate checkpoint/sampler/scheduler names via `/object_info`
  before queueing. Never overwrite an image that has no `.png.json` sidecar (user-supplied) without --force.
- Every module is runnable as a script (`python scripts/x.py`) and imports siblings by bare
  name (`from pipeline_utils import ...`). Don't convert to package-relative imports without
  updating every entry point.

## Coding style
- Python 3.10+, `from __future__ import annotations`, type hints on public functions.
- Line length ≤ 120. f-strings. `pathlib.Path`, never string path joins.
- User-facing failures raise `PipelineError` with an actionable message; CLIs catch it and
  return exit code 1. No bare `except:`; broad `except Exception` only around network calls.
- Logging via `get_logger(name)`; `print` only for final CLI results.
- Heavy imports (moviepy, edge_tts, ffmpeg) go inside functions so `--help` and `--dry-run` stay fast.
- No placeholder or stub code. If a feature is not implemented, it's not in the schema.

## Known limits (be honest about these to the user)
- edge-tts is **not offline**: it calls Microsoft's online Read-Aloud service (unofficial, no SLA,
  can break or be rate-limited). `--tts-fallback silence` lets everything else run offline.
- Rendering is CPU-bound, single-threaded Python frame generation: the 20 s demo took 221 s at
  1080p on 4 cores (~11× realtime); `--draft` took 52 s. FX alone cost ~0.2 s/frame.
- "No quality loss" means no *avoidable* generation loss; the final H.264 (CRF 16) is still lossy,
  and your source clips already are.
- Speaker tags / CJK subtitles need a font with the right glyphs (e.g. Noto Sans CJK) installed
  or dropped into `assets/fonts/`.

## Git
Rendered media, temp files and model weights are gitignored. Commit code, storyboards and small
reference assets only.
