# Anime Video Pipeline

A local, scriptable pipeline that turns a **JSON storyboard** plus your clips and images
(from Stable Diffusion, AnimateDiff or anything else) into a finished **1080p / 24 fps MP4**.
It adds neural narration, anime color grading, Ken Burns camera moves, speed lines, title
cards, burned-in anime-style subtitles and ducked background music.

```
storyboard.json ──► voiceover_generator ──► timeline ──► scenes (cinematic_fx) ──► lossless master
                     (edge-tts, cached)      (durations,   fit+Ken Burns, grade,      (libx264rgb qp0
                                             transitions)  speed lines, overlays       + PCM audio)
                                                                                           │
            output/<name>.mp4  ◄── final_encode: burn ASS subs → BT.709 → H.264 CRF16 ◄──┘
            output/<name>.srt
```

---

## Read this first: the honest limits

| Claim | Reality |
|---|---|
| **Zero cost** | All software is free and open source. edge-tts is free and needs no API key, but it is an *unofficial* client for Microsoft Edge's online Read-Aloud service. Microsoft can rate-limit, change or block it at any time, and there is no SLA. Its terms for use in published videos are not clearly documented, so check them yourself before monetizing. |
| **Local** | Everything runs locally **except TTS**, which needs internet access to `speech.platform.bing.com`. Offline, use `--tts-fallback silence`: every other stage still runs, with silent placeholder narration of estimated length. |
| **No quality degradation** | There is no *avoidable* generation loss. Scenes go to a lossless RGB master, then there is exactly **one** lossy encode (H.264 CRF 16, `tune=animation`, BT.709). Any H.264 delivery file is lossy, and your SD/AnimateDiff clips already are. Upscaling 512–768 px AI output to 1080p cannot add detail; use a dedicated upscaler first (see below). |
| **Speed** | Rendering is CPU-bound Python. Measured on a 4-core cloud VM, the 20 s demo took **221 s end to end at 1080p** (~0.4 s/frame including the lossless master and the final x264 `slow` encode), roughly **11 minutes of render per minute of video**. `--draft` (960×540) took 52 s. |

---

## 1. Setup

### Requirements
- Python **3.10+**
- **FFmpeg** with `libass`, `libx264` and `libx264rgb` (standard in most builds)

```bash
# Ubuntu / Debian
sudo apt install ffmpeg fonts-noto-cjk
# macOS (Homebrew)
brew install ffmpeg
# Windows: install a "full" build from https://www.gyan.dev/ffmpeg/builds/ and add its bin/ to PATH
```

Check that FFmpeg can burn subtitles:
```bash
ffmpeg -hide_banner -filters | grep -w ass
```

### Python environment
```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`moviepy` **2.x** is required. Code written for moviepy 1.x (`moviepy.editor`, `set_duration`,
`subclip`) will not run here, and the reverse is also true.

### Smoke test (no assets of your own needed)
```bash
python scripts/make_demo_assets.py                                         # procedural placeholder art + BGM
python scripts/video_orchestrator.py storyboard_template.json --dry-run    # validate + print timeline
python scripts/video_orchestrator.py storyboard_template.json --draft      # fast 540p preview
python scripts/video_orchestrator.py storyboard_template.json              # full 1080p render
```
Output: `output/sakura_horizon.mp4` and `output/sakura_horizon.srt`.
Without internet access, add `--tts-fallback silence`.

---

## 2. The scripts

All scripts are run from the repo root as `python scripts/<name>.py`. Each one has `--help`.

### `video_orchestrator.py`: main entry point
```bash
python scripts/video_orchestrator.py STORYBOARD.json [options]
```
| Option | Effect |
|---|---|
| `--dry-run` | Validate and print the timeline using *estimated* narration lengths. No network, no render. |
| `--draft` | Half resolution and fast presets. Writes `<name>_draft.mp4`. |
| `--scenes id1,id2` | Render only these scenes. |
| `-o PATH` | Override `project.output`. |
| `--allow-missing` | Replace missing clips with labelled placeholder cards. |
| `--tts-fallback silence` | If edge-tts fails, continue with silent narration of estimated length. |
| `--force-tts` | Ignore the voiceover cache. |
| `--proxy URL` | HTTP proxy for edge-tts. |
| `--crf N` | Final quality (lower is better and larger; 14–18 is sensible). |
| `--keep-temp` | Keep the lossless master `temp/<name>_master.mkv` (large). |

Also written on every run: `temp/<name>_timeline.json`, with the exact start/end of every scene
and narration line. Use it to regenerate subtitles or debug timing.

### `voiceover_generator.py`: neural TTS
```bash
python scripts/voiceover_generator.py --text "Hello, world." --out temp/hello.wav
python scripts/voiceover_generator.py --json storyboard_template.json            # all scene narrations
python scripts/voiceover_generator.py --json lines.json --voice ja-JP-NanamiNeural
python scripts/voiceover_generator.py --list-voices --locale ja-JP
```
`--json` accepts a storyboard, a list (`["line one", {"id": "x", "text": "...", "voice": "..."}]`)
or `{"defaults": {...}, "lines": [...]}`. Each line produces a 48 kHz WAV and a JSON sidecar
with duration and per-sentence timings, which drive subtitle timing. Results are cached by a
hash of text, voice, rate, pitch and volume. Prosody formats: `rate "+10%"`, `pitch "+15Hz"`,
`volume "-5%"`.

### `cinematic_fx.py`: effects on a single clip
```bash
# preview one frame (fast way to tune a look)
python scripts/cinematic_fx.py assets/images/scene_01.png --duration 5 --frame 2.5 \
    --kb-preset push_in --grade anime_vivid -o temp/look.png
# render a clip
python scripts/cinematic_fx.py assets/clips/scene_02.mp4 -o output/fx.mp4 \
    --grade cinematic_teal_orange --speed-lines radial
```
- **Camera:** `--fit cover|contain|stretch`, `--kb-preset` (`push_in`, `pull_out`, `pan_left`,
  `pan_right`, `pan_up`, `pan_down`, `drift`), or a manual `--zoom 1.0 1.15 --pan 0.5 0.5 0.6 0.45`.
  Fit and Ken Burns are one sub-pixel resample per frame, so motion stays smooth without a
  double-scaling blur. Use sources larger than 1080p if you zoom, otherwise the zoom softens the image.
- **Grade presets:** `anime_vivid`, `anime_pastel`, `cinematic_teal_orange`, `night_blue`,
  `golden_hour`, `none`. Any parameter can be overridden in the storyboard (see §3).
- **Speed lines:** `radial` (focus lines), `horizontal`, `vertical`. They are generated
  procedurally and animated "on twos" (12 fps) by default.

### `subtitle_generator.py`: SRT/ASS and burn-in
```bash
python scripts/subtitle_generator.py from-timeline temp/sakura_horizon_timeline.json
python scripts/subtitle_generator.py from-srt my.srt -o temp/my.ass --storyboard storyboard_template.json
python scripts/subtitle_generator.py burn -i in.mp4 -s temp/my.ass -o output/subbed.mp4
```
The style has a bold white fill, a thick colored outline (per speaker), a soft shadow, a slight
edge blur, 120 ms fades and a small speaker-name tag. Long sentences are wrapped and split into
cues of at most `max_lines` lines. `from-srt` recognises `Name: text` lines as speakers.

### `make_demo_assets.py`
Generates the placeholder images, clip, frame sequence and BGM that the template refers to.
They are for testing only. `--force` regenerates them.

---

## 3. Storyboard reference

Start from `storyboard_template.json`, which uses every feature. Paths are resolved **relative
to the storyboard file**. Unspecified keys fall back to the defaults below.

### Top level
| Key | Purpose |
|---|---|
| `project` | `title`, `output`, `resolution` [1920,1080], `fps` 24, `fit` (`cover`/`contain`/`stretch`), `background_color`, `crf` 16, `preset` `slow`, `tune` `animation`, `audio_bitrate` `192k`, `loudness_lufs` −14 (final mix normalized to YouTube's playback loudness with a −1 dBTP ceiling; `null` disables) |
| `voice_defaults` | `voice`, `rate`, `pitch`, `volume` for every narration line |
| `speakers` | `{ "Name": { voice, rate, pitch, volume, subtitle_color } }`, used when a narration sets `speaker`. For Japanese voices: `{ "engine": "voicevox", "voicevox": { "speaker": "青山龍星", "style": "ノーマル", "speed": 0.95 } }` (install the engine with `python scripts/setup_voicevox.py`, which needs no desktop app; list voices with `python scripts/voicevox_client.py`). For a cloned character voice (local, free, Apache-2.0): `{ "engine": "qwen3", "qwen3": { "voice": "hero", "delivery": "calm", "seed": 1, "language": "English", "sentence_pause": 0.35 } }`; see *Qwen3 character voices* below |
| `audio` | `narration_volume`, `clip_audio_volume`, `bgm { path, volume, loop, duck_to, duck_fade, fade_in, fade_out }`. `duck_to` 0.35 means BGM drops to 35% while someone speaks. |
| `transitions.default` | `{ "type": "cut" \| "fade_black" \| "crossfade", "duration": s }` |
| `subtitles` | `enabled`, `burn_in`, `export_srt`, `soft_subtitles` (adds a selectable track), `show_speaker`, `max_chars_per_line`, `max_lines`, `min_cue_duration`, `style { font, font_size, bold, primary_color, outline_color, outline, shadow, shadow_alpha, blur, margin_v, margin_h, alignment, fade_in_ms, fade_out_ms }` |
| `fx_defaults` | Default `fx` block merged into every scene |
| `scenes` | Ordered by `order`, then by position in the file |

### Scene
| Key | Default | Notes |
|---|---|---|
| `id` | `scene_NN` | Unique; also names the voiceover cache file |
| `order` | file position | |
| `clip` | | A video (`.mp4/.mov/.webm/.gif`…), an image (`.png/.jpg/.webp`…), or a **directory of frames** (AnimateDiff) |
| `duration` | auto | If omitted: `max(min_duration, narration offset + length + tail)`; with no narration, the clip's own length |
| `min_duration` | 2.0 | |
| `clip_start` / `clip_end` | 0 / end | Trim the source (seconds) |
| `playback_speed` | 1.0 | 0.5 = slow motion |
| `extend` | `loop` | When the clip is shorter than the scene: `loop`, `boomerang` (ping-pong; suits AnimateDiff loops), `freeze` |
| `sequence_fps` | 8 | Frame rate of a frame directory |
| `fit` | project | Per-scene override |
| `keep_clip_audio` | false | Mix the clip's own audio at `audio.clip_audio_volume` |
| `transition_in` | default | Transition from the previous scene |
| `narration` | none | `{ text, speaker, voice, rate, pitch, volume, offset 0.3, tail 0.6, subtitle true, subtitle_text null }`. `subtitle_text` shows different text than is spoken, e.g. English subtitles under a Japanese dub |
| `fx` | `fx_defaults` | See below |
| `text_overlays` | [] | See below |

### `fx`
```json
"fx": {
  "ken_burns":   { "enabled": true, "preset": "push_in", "zoom_start": 1.0, "zoom_end": 1.12,
                   "pan_start": [0.5, 0.5], "pan_end": [0.5, 0.48], "easing": "ease_in_out" },
  "color_grade": { "enabled": true, "preset": "anime_vivid", "intensity": 0.85,
                   "saturation": 1.3, "contrast": 1.1, "bloom": 0.2, "vignette": 0.2 },
  "speed_lines": { "enabled": true, "style": "radial", "count": 90, "color": "#FFFFFF",
                   "opacity": 0.55, "inner_radius": 0.38, "center": [0.5, 0.5],
                   "min_width": 2, "max_width": 9, "flicker_fps": 12, "start": 1.0, "end": null, "seed": 7 },
  "fade_in": 0.5,
  "fade_out": 0.5
}
```
- `anime_timing: { "enabled": true, "on": 2 }` holds each frame of a video/frame-sequence clip for 2 ("on twos",
  12 drawings/s) or 3 ("on threes", 8/s) frames for a drawn-anime cadence. It applies to the drawing only;
  Ken Burns camera moves stay smooth.
- `impact: { "enabled": true, "at": [0.0], "shake": 24, "settle": 0.5, "invert": true, "flash": true }`:
  anime impact hit at each listed second. One inverted high-contrast B&W frame, a white flash, then a
  camera shake that settles over `settle` seconds.
- Action camera presets: `crash_zoom`, `slam_out`, `whip_left`, `whip_right`. They start fast and land hard
  (ease_out), and are made for short 0.5–1.5 s cuts.
- `shake: { "enabled": true, "amount": 10, "fps": 12 }` adds a continuous energy rumble (pixels at 1080p,
  changing `fps` times per second like anime camera shake). Speed lines stay steady on top of it.
- `impact.flash_color`: the flash after the hit frame, e.g. `"#FF7A1A"` (ember) or `"#19E6FF"` (cyan).
- `blackout: { "enabled": true, "start": 0.3, "end": 2.2, "brightness": 0.15, "saturation": 0.0, "steps": 3,
  "easing": "ease_in" }` ramps brightness and saturation from 1.0 down to the targets between `start` and `end`
  (seconds into the cut), then holds. `steps > 0` makes it stepped (lights dying bank by bank); `0` is smooth.
  Use `saturation` alone for an energy drain (color to gray), `brightness` for lights out. It runs after the grade
  and before speed lines and impacts, so a hit flash stays bright over a dark frame.
- Ken Burns zoom must be ≥ 1.0, and panning only has an effect when zoom > 1 because there is
  nothing to pan across otherwise. Easings: `linear`, `ease_in`, `ease_out`, `ease_in_out`, `sine`.
- Grade overrides: `saturation`, `contrast`, `brightness`, `gamma`, `black_lift`,
  `shadow_tint` [r,g,b], `highlight_tint` [r,g,b], `bloom`, `bloom_threshold`, `bloom_radius`,
  `vignette`, `intensity` (0 = off, 1 = full).

### `text_overlays[]`
```json
{ "text": "EPISODE 1", "start": 0.5, "end": 3.0, "position": "center",
  "font": "NotoSansCJK-Bold.ttc", "font_size": 110, "color": "#FFFFFF",
  "stroke_color": "#D6336C", "stroke_width": 8, "shadow_offset": [4, 6], "shadow_opacity": 0.55,
  "box_color": null, "box_opacity": 0.5, "align": "center", "max_width": 0.8,
  "animation": "slide_up", "fade_in": 0.5, "fade_out": 0.5 }
```
`position` accepts `center`, `top`, `bottom`, `top_left`, `top_right`, `bottom_left`,
`bottom_right`, or `[x, y]` for the overlay's center: floats from 0 to 1 are fractions of the
frame, integers are pixels. Sizes are given for 1080p and scale automatically in draft mode.
Keep overlays away from the bottom-center area, where subtitles go.

---

## 4. Automated image generation (local ComfyUI)

The pipeline can generate every scene's image for you with a **local ComfyUI** (your GPU, no
cloud costs) and then render the video, in one command.

### One-time setup (Windows PowerShell example)
```powershell
# 1. Tell the pipeline where ComfyUI lives (persists; open a NEW terminal afterwards)
setx COMFYUI_DIR "C:\Users\<you>\comfyui-anime"
# 2. Download + verify the anime model (Animagine XL 4.0, ~6.9 GB, resumable)
.venv\Scripts\python scripts\setup_models.py
```
`setup_models.py --verify` checks every `.safetensors` file under ComfyUI's `models/` for
truncated downloads. Re-running an interrupted download resumes it.

### Every video
```powershell
make_video my_story.json --draft     # images (only new/changed scenes) + 540p preview
make_video my_story.json             # images + full 1080p render
```
`make_video.cmd` runs `video_orchestrator.py ... --generate-images`. On macOS/Linux use
`python scripts/video_orchestrator.py my_story.json --generate-images`.

What happens:
1. If ComfyUI isn't already running at `$COMFYUI_URL` (default `http://127.0.0.1:8189`), it is
   started from `$COMFYUI_DIR` in the background (log: `temp/comfyui_server.log`), then stopped
   when the images are done, which frees the GPU.
2. Each scene with a `generate` block gets an SDXL image plus a hi-res second pass
   (1344×768 → 2016×1152), saved to `assets/generated/<scene_id>.png` with a `.png.json`
   sidecar recording the prompt, seed, model and settings.
3. **Incremental:** only scenes whose prompt or settings changed are regenerated. Images you put
   there yourself (no sidecar) are never overwritten unless you add `--force-images`.
4. The video renders as usual.

Generate images only: `python scripts/image_generator.py my_story.json [--scenes s02 --force]`.
List the models ComfyUI sees: `python scripts/image_generator.py --list-checkpoints`.

### Storyboard keys
```json
"image_generation": { "checkpoint": "animagine", "width": 1344, "height": 768, "steps": 28, "cfg": 5.0,
                      "sampler": "euler_ancestral", "scheduler": "normal", "seed": 1000, "rating": "safe",
                      "quality_tags": "...", "negative": "...",
                      "hires": { "enabled": true, "scale": 1.5, "denoise": 0.4, "steps": 20 } },
"characters": { "Hikari": { "tags": "long black hair, brown eyes, red scarf, school uniform" } },
"scenes": [ { "id": "s02", "generate": { "characters": ["Hikari"],
              "prompt": "1girl, solo, holding umbrella, train station, night, rain" } } ]
```
- A scene with `generate` and no `clip` uses `assets/generated/<id>.png` (`output_dir`).
- The final prompt is assembled as counts (`1girl`…) → character tags → scene tags → rating →
  quality tags, which is Animagine's recommended order. Use **Danbooru-style tags**, not sentences.
- `checkpoint` matches by substring. If unset, the first checkpoint containing "animagine" is used.
- Per-scene overrides: `negative` (appended), `seed`, `width`, `height`, `rating`.
- Seeds are deterministic (base seed + scene id), so re-running reproduces the same image.
  Change a scene's `seed` to get a different take.

### Honest limits
- **Character consistency is approximate.** Reusing the same `characters` tags keeps hair, eye
  color and outfit stable, but faces still vary between scenes. Locking a face takes a character
  LoRA or IP-Adapter, which this automation does not wire up yet.
- **Two characters in one image bleed together:** the model can swap hair colors or outfits
  between them. Prefer one character per shot, or regenerate with a different `seed`.
- Hands and text inside images are unreliable (a known Animagine limitation).
- With 8 GB of VRAM, expect very roughly 30–90 s per image including the hi-res pass. Lower
  `hires.scale` or disable it if you run out of GPU memory.

`my_story.json` is a complete 5-scene example that uses only generated images.

### Image-to-video (experimental): Wan 2.2 TI2V-5B
Animates a keyframe into a real moving clip (default 5 s, 1280×704, 24 fps).
```powershell
.venv\Scripts\python scripts\setup_models.py --model wan2.2-ti2v-5b          # ~17 GB, resumable
.venv\Scripts\python scripts\video_generator.py assets\generated\s02_hikari.png --prompt "anime style, scarf and hair sway in the wind, rain falls, slow camera push-in"
```
Output: lossless frames in `assets/generated/clips/<name>/` (usable directly as a scene `clip`),
plus `output/<name>_wan_preview.mp4`. Write the prompt as plain English describing the scene **and
the motion/camera**. `--length` must be 4n+1 (49 ≈ 2 s, 121 = 5 s); `--size 960x544` is about 2×
faster. Model weights load as fp8 by default to fit 8 GB of VRAM and 16 GB of RAM. Speed and quality
on an 8 GB laptop GPU are still being measured.

### Music and sound effects (license-checked)
Every audio file must be registered in `assets/audio/manifest.json` with its source and license.
Renders refuse unregistered files and licenses that are unsafe for monetized videos (NC, ND, SA, unknown).
```powershell
# Sound effects from Freesound, CC0 only. Free API key: https://freesound.org/apiv2/apply
setx FREESOUND_API_KEY "your-key"        # once, then open a new terminal
.venv\Scripts\python scripts\fetch_sfx.py search "handcuffs chain clink" --name chain_clink   # 3 candidates to listen to
.venv\Scripts\python scripts\fetch_sfx.py keep chain_clink 2
.venv\Scripts\python scripts\fetch_sfx.py script series\<show>\ep01.script.json   # every SFX a script needs
# CC0 music beds from Freesound work the same way (no YouTube channel needed):
.venv\Scripts\python scripts\fetch_sfx.py search "dark ambient drone" --name tension_drone --min-duration 30 --max-duration 180
.venv\Scripts\python scripts\fetch_sfx.py keep tension_drone 1 --kind music
# Music you downloaded elsewhere (e.g. YouTube Studio > Audio Library, which needs a YouTube channel):
.venv\Scripts\python scripts\audio_manifest.py add assets\audio\music\tension_drone.mp3 --name tension_drone ^
    --kind music --source youtube_audio_library --license "YouTube Audio Library" --attribution "<if the library shows one>"
.venv\Scripts\python scripts\audio_manifest.py check
```
Storyboard keys: `audio.music: [{ file, start_scene, end_scene (or start/end seconds), volume, loop, fade_in,
fade_out, duck_to, duck_fade, drops: [{start_scene, end_scene}] }]`, where `drops` cut the music to silence
for a dramatic beat; scene `sfx: [{ file, at, volume }]`; `audio.require_license` (default true). Each render
writes `output/<name>_credits.txt` with the attribution lines your audio requires. Audio files are gitignored,
so licensed tracks are never pushed to your repository; only the manifest is.

### Qwen3 character voices

`engine: "qwen3"` clones a registered character voice locally with Qwen3-TTS 1.7B Base (Apache-2.0, ~2.3x
realtime and 4.2 GB VRAM on an RTX 5060). A voice is `assets/voices/<voice>_ref.json`, whose `engine.refs` maps a
delivery name to a reference clip and its exact transcript (`audio` is relative to that file):

```json
{"engine": {"model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
            "refs": {"calm":  {"audio": "hero_calm_seed3.wav",  "text": "I'm not running anymore. ..."},
                     "shout": {"audio": "hero_shout_seed3.wav", "text": "Get out of my way!"}}}}
```

The clone copies the voice **and the delivery** from the clip; there are no text instructions, and `rate` /
`pitch` / `volume` do not apply. `qwen3` keys: `voice`, `delivery` (one name, or one per sentence with the last
repeating, e.g. `["calm", "shout"]` for a quiet setup and a shouted payoff; split lines get measured sentence
timings), `seed` 1, `language` `English`, `sentence_pause` 0.35 s. In episode scripts a line's `<lang>_style` sets
the delivery. Clip hashes are in the voice cache key, so replacing a reference re-voices its lines.

The model runs in its own Python env, called once per render so it loads once (`scripts/qwen3_tts_worker.py`):
set `QWEN3_TTS_PYTHON` to that env's `python.exe` and `QWEN3_TTS_HF_HOME` to the Hugging Face cache holding the
model. Voice WAVs are gitignored; the JSON recipe is committed.

### Character shots: lip-synced 3D cel-shaded characters (Blender + VRM + Rhubarb)
A VRoid character (`.vrm`, **VRM 1.0**) speaks one line in front of a background, rendered headless in Blender:
```powershell
blender --command extension install -s --enable vrm     # once: the VRM add-on (MIT/GPL)
# once: Rhubarb Lip Sync from github.com/DanielSWolf/rhubarb-lip-sync/releases; set BLENDER_EXE and RHUBARB_EXE
.venv\Scripts\python scripts\character_shot.py character_shot_template.json            # frames + final MP4
.venv\Scripts\python scripts\character_shot.py character_shot_template.json --frames-only
.venv\Scripts\python scripts\character_shot.py character_shot_template.json --benchmark 48   # Eevee s/frame
```
Steps: the line is voiced through `voiceover_generator` (same cache as the orchestrator, so audio and mouth
match), Rhubarb turns the WAV into mouth shapes (`line.recognizer`: `pocketSphinx` for English, `phonetic` for
Japanese), `blender_vrm_shot.py` keys lip-sync (Rhubarb A-H/X → VRM `aa ih ou ee oh`), VRM expressions (with
the spec's mouth/blink override rules), blinks, idle breathing/sway, gestures and the camera, renders with Eevee
on twos (`render.on`: each drawing held 2 frames like TV anime; held frames are hard links), then the frame
folder becomes the clip of a one-scene storyboard (narration, subtitles, `sfx`, `fx`) for the normal encode.
Frames are reused when the job is unchanged (`--force` re-renders); outputs land in `output/<id>/`, frames and a
debug `.blend` in `temp/character_shots/<id>/`.

Spec keys (`character_shot_template.json` shows all of them; paths are relative to the spec file):
`id`, `vrm`, `background` (image on a plane behind the character, sized to cover every cut), `duration`,
`render: {resolution, fps, on, samples}`, `line: {text, voice, rate, pitch, volume, engine, voicevox, qwen3, offset,
tail, recognizer, mouth_style: anime|visemes, mouth_gain, subtitle_text}`. `anime` mouth style = readable
closed / half / open flaps; `mouth_gain` above 1 opens wider than the model's own shapes (try 1.2-1.4).
`mouth_source`: `flaps` (default; anime) times the flaps from the voice's syllables: open on loud syllables, half on
soft ones or a syllable right after an open drawing, closed in the dips and on Rhubarb's M/B/P closures. Rhubarb
alone misreads shouted lines and can hold one half-open shape for a second. `rhubarb` uses Rhubarb's shapes, and
`visemes` style needs it. Either way each drawing gets the shape that covers most of its time on screen, drawn
`mouth_lead` 0.04 s (about a frame) before the sound, as animators do. `fx.impact.at[]` and
`fx.speed_lines.start/end` also take `{"at_sentence": n, "delay": s}`, so hits follow the voice when it changes.
`style` (all off by default; `character_shot_style_template.json` turns everything on) is a hybrid studio-anime
pass: `fx_on_ones` (camera, effects and ink move every frame while the character holds on `render.on`),
`line_art {px, color, crease_deg, taper, exclude}` (Grease Pencil Line Art ink, constant width in pixels per cut,
tapered ends; meshes named in `exclude` keep only their MToon outline, because a VRoid face's stacked eye layers
trace as scribbles), `ribbon {at|at_sentence+delay, grow, hold, fade, target, turns, layers[{radius, width, color,
strength, phase_deg}], line_art, light}` (an elemental motion trail spiralling around the path the target hand
travels, its head on the moving fist; `light` keys a cyan rim and shadow tint into the MToon materials while it's
up, since MToon ignores lamps), `embers {count, color, strength, size, rise, drift, near, ...}` (seeded glowing
particles between character and background) and `bloom {threshold, size, strength}` (compositor glare on pixels
above the threshold). Cost: ~1.5 s/frame at 1080p with everything on (vs ~0.7 s per drawing without).
`expressions: [{name | shape_keys: {key: weight}, weight, at|at_sentence+delay, ramp}]`: `name` is any VRM
expression on the model (`happy angry sad relaxed surprised`...; VRM override rules apply, and VRoid's emotions
also move the mouth), `shape_keys` drives raw keys such as VRoid's `Fcl_BRW_Angry` + `Fcl_EYE_Angry` (brows and
eyes only, so lip-sync stays free; values above 1 overdrive a shape, e.g. `Fcl_BRW_Angry: 1.9`). A cue with `when_mouth_closed: true` only shows between mouth flaps (gritted teeth: `Fcl_MTH_I` + `Fcl_MTH_E`). `gestures: [{name: fist_raise|fist_to_chest|head_down|nod, side,
at|at_sentence+delay, hold}]`, built from key poses: anticipation, overshoot strike, settle, held pose, release.
`cuts: [{at|at_sentence+delay, framing: extreme_closeup|closeup|medium_closeup|medium, target:
eyes|face|chest|right_hand|left_hand, lock, focal_mm, yaw_deg, low_angle_deg, dutch_deg, offset_x, offset_y,
push_in, fstop, face_shadow: {edge, face_shift}}]`: the camera hard-cuts at each `at`; `face_shadow` adds a hard cel shadow across the upper face (an invisible flag between key light and face; `edge` = metres above the eyes) for that cut only; `lock` = seconds into the cut at which the target is
measured (aim at where the fist ends up, not where it starts); `fstop` sets the background blur (Eevee depth of
field). `camera: {background_distance, punches: [{at|at_sentence, amount, duration}]}` (snap zoom-ins).
`lighting: {key_color, key_strength, key_direction: [forward, right, up], back_strength, ambient_color,
ambient_strength, exposure, lit_tint, match_background, shadow_color, shadow_tint, shade_darken, toony,
shading_shift, face_shading_shift, rim_color, rim_strength, rim_power, rim_lift, catchlight: {materials,
strength}, background_brightness}`: MToon ignores light intensity, so the look goes through the materials:
lit side = colour x `exposure` x `lit_tint` (warm; `match_background` > 0 pulls it toward the background),
shadow side blended toward `shadow_color` (blue), `toony` ~1 = hard terminator, `shading_shift` < 0 = more of
the body in shadow, `face_shading_shift` keeps the edge off the face, `rim_*` = coloured fresnel rim (neon),
`catchlight` makes eye-highlight materials (name contains e.g. `Highlight`, VRoid) emissive.
Impact frames, speed lines and shake for a hit come from the shot's `fx` (storyboard fx in shot seconds,
e.g. `speed_lines: {start, end, center}`, `impact: {at: [apex]}`); put the hit SFX on the same apex. `blink: {enabled,
interval, first, closeup_hold_max}` (no blinks inside close-up cuts shorter than this; default 2.5 s),
`strike_face: {enabled, shape_keys, gritted, ramp}` (house rule: `fist_raise`/`fist_to_chest` always wear
angry brows, open angry eyes and gritted teeth between flaps from wind-up to release; VRoid key names), `spring_bones` (hair/cloth physics, pre-rolled 2 s so it settles before frame 1),
`sfx: [{file, at|at_sentence, volume}]`, `fx`. Times: `at` = seconds into the shot; `at_sentence` = start of
that sentence (1-based) of the spoken line plus `delay`. Backgrounds for character shots are generated from
`character_backgrounds.json` (`python scripts/image_generator.py character_backgrounds.json`).
Measured on an RTX 5060 Laptop: 1080p, 16 samples, about 0.56 s per frame after the first (one medium shot).

### 2D cel shots (the approved look): drawn cuts, rigid acting, lip sync
`cel_shot.py` makes a 2D anime shot from generated drawings. The owner rejected the 3D route as looking 3D, so
this is the main character-animation path now.
```powershell
# once: rembg in its own env (character cut-outs), plus Rhubarb as for character shots
python -m venv $HOME\tools\rembg-env; & $HOME\tools\rembg-env\Scripts\pip install "rembg[cpu]"
setx REMBG_PYTHON "$HOME\tools\rembg-env\Scripts\python.exe"
.venv\Scripts\python scripts\cel_shot.py cel_shot_template.json --dry-run          # validate, list drawings
.venv\Scripts\python scripts\cel_shot.py cel_shot_template.json --stills 0.5,3.2   # review PNGs only
.venv\Scripts\python scripts\cel_shot.py cel_shot_template.json                    # drawings, frames, MP4
```
How a shot is built:
1. The line is voiced exactly as in character shots.
2. Each cut gets an Animagine keyframe, built with `image_generator`'s own job, so it is cached the same way.
3. Each cut gets masked **redraws** for mouth shapes, faces and effect "boil" frames. A redraw is pasted back with
   `ImageCompositeMasked`, so every pixel outside its mask stays identical to its base. `fresh: true` blanks the
   mask first (`VAEEncodeForInpaint`), which can draw a new shape, e.g. turning an open mouth into a closed one.
   Without it, a partial repaint keeps the existing structure.
4. The character is cut out with rembg `isnet-anime`.
5. Frames are composited and encoded once by the orchestrator.

**Acting moves the cut-out as one rigid piece (tilt, shift, uniform scale) over a filled background plate. It
never bends a drawing:** the owner rejected bending warps as "lens distortion". Extreme close-ups move the whole
drawing.

The motion follows the voice:
- an inhale before she speaks;
- head nods on stressed syllables;
- a dip, then a look-up, just before the next cut;
- a stepped tremble while shouting.

Mouths flap from the voice envelope (`drawn_mouth_cues`); drawings and acting change on `on` (twos). The strike
moves every frame, with a smear. Camera, embers and bloom also move every frame. Camera and pose are one bilinear
resample. A shot takes about 1.3 s per frame on CPU, so about 3.5 min for 6.6 s, plus about 1 min per new drawing on the GPU.

Spec keys (`cel_shot_template.json` reproduces the approved demo):

- **Top level:**
  - `id`, `duration`, `fps`, `on`.
  - `line`: same keys as character shots.
  - `character: {name, tags}`: the tags go into every drawing.
  - `image_generation: {checkpoint, output_dir, rating}`.
  - `redraw: {steps, cfg, sampler, scheduler, quality_tags, negative}`.
  - `matte: {model, bright_gamma, edge, soften, grow}`.
  - `comp: {embers: {count, seed}, bloom: {threshold, strength}}`.
  - `sfx`, `fx`: as in character shots.
- **`cuts[]`:** the camera hard-cuts at each `at`.
  - `id`, `at`.
  - `keyframe: {id, prompt, seed}`.
  - `drawings: [{id, from: key|earlier id, shapes: [[ellipse|rect, [cx, cy, rx, ry]]], prompt, seed, denoise,
    fresh, feather}]`.
  - `mouth: {closed, half?, open}`: drawing ids or `key`. With no `half`, the open drawing is used.
  - `boil: {drawings, on, after: {at, on}}`: effect frames that cycle on threes, or on twos after the hit.
  - `layer`:
    - `matte`: cut-out;
    - `bright`: matte a brightened copy, for dark close-ups;
    - `none`: move the whole drawing.
  - `matte_ignore: [[x0, y0, x1, y1]]`: areas to drop from the cut-out, e.g. a lantern.
  - `acting: {type, ...}`, with these types:
    - `talk {pivot, head_down, lift_before_cut, nod, inhale_px, sway}`;
    - `strike {at, pivot = the fist, travel, lean_deg, windup_px, follow_px, lunge, settle, smear_taps}`;
    - `shout {scale, thrust, nod_px, tremble: [while speaking, after]}`;
    - `stare {scale, pant_px, period}`.
  - `camera: {zoom: [start, end] >= 1, center, slam: {at, amount, settle}}`.
  - `embers`: strength for this cut.

Times can be any of:
- seconds;
- `{"at_sentence": n, "delay": s}`;
- `{"after_speech": s}`.

Caching:
- Drawings carry `.png.json` sidecars and are redrawn only when their recipe or base image changes.
- Images without a sidecar are yours and are kept; `--force-drawings` replaces them.
- Frames are reused while nothing changed; `--force` re-renders them.
- Output goes to `output/<id>/`; work files go to `temp/cel_shots/<id>/`.

Limits:
- No new poses: turning, or a swinging arm, needs ControlNet.
- Hair doesn't sway, because there are no hair redraws yet.
- The character's look can drift between keyframes: lock the tags and check the cuts side by side.

## 5. Using Stable Diffusion and AnimateDiff output

The pipeline doesn't care which tool generated your media. It only needs files on disk.

**Stills (Automatic1111 / Forge / ComfyUI txt2img)**
- Generate at 16:9, for example 1344×768 for SDXL or 1024×576 for SD1.5, then upscale to at least
  **2304×1296** so Ken Burns zooms up to 1.2× stay sharp. Good anime upscalers are
  Real-ESRGAN `realesr-animevideov3` and `RealESRGAN_x4plus_anime_6B`, or hires-fix in the UI.
- Point `clip` at the PNG and enable `ken_burns`. A still with a camera move, narration and
  speed lines is the cheapest convincing anime shot.

**Animation (AnimateDiff in ComfyUI or A1111)**
- **Frame folder:** save frames (e.g. `frame_0001.png …`) and set `"clip": "assets/clips/shot_07"`
  with `"sequence_fps": 8`, which is AnimateDiff's usual output rate. Frames are sorted by filename.
- **Video file:** VideoHelperSuite's *Video Combine* node writes MP4 or GIF. Point `clip` at it.
- AnimateDiff clips are short (16–32 frames, about 2–4 s). Use `"extend": "boomerang"` or `"loop"`
  so they fill a narration-length scene.
- For smoother motion, interpolate to 24 fps first (the RIFE or FILM nodes in
  ComfyUI-Frame-Interpolation) and set `sequence_fps` to the new rate. The pipeline does not
  interpolate frames itself. It picks the nearest frame when converting 8 fps to 24 fps, which
  gives the classic "on threes" anime cadence.

**Keeping assets out of git**
```bash
ln -s ~/ComfyUI/output assets/comfy          # macOS/Linux; then use "clip": "assets/comfy/..."
```
Model weights (`*.safetensors`, `*.ckpt`) and rendered media are already gitignored.

---

## 6. Pushing to GitHub

```bash
git add -A
git commit -m "Initial anime video pipeline"
```
Create an empty repository on GitHub (no README or license, so there is nothing to merge), then:
```bash
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```
With the GitHub CLI, one command creates the repo and pushes:
```bash
gh repo create <your-repo> --private --source=. --remote=origin --push
```
`.gitignore` keeps `temp/`, `output/`, `*.mp4`, `*.wav`, generated demo images and model
weights out of the repository. If you want large binary assets under version control, use
Git LFS (`git lfs track "*.png"`) rather than committing them directly.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Cannot connect to host speech.platform.bing.com` | No internet, a firewall or proxy, or the service changed. Use `--proxy`, try later, or use `--tts-fallback silence`. Upgrade with `pip install -U edge-tts` if it was working before. |
| `No such filter: 'ass'` | Your FFmpeg build lacks libass. Install a full build. |
| Boxes (□) instead of Japanese text | Install Noto Sans CJK (or copy a `.ttf/.otf` into `assets/fonts/`) and set `subtitles.style.font` and the overlay `font` to it. |
| `AttributeError: ... has no attribute 'set_duration'` / `moviepy.editor` | moviepy 1.x code or install. Run `pip install "moviepy>=2.1,<3"`. |
| Colors look washed out or shifted in one player | The output is tagged BT.709 limited range. Compare in mpv or VLC; some web previews ignore the tags. |
| Render too slow | Use `--draft` while iterating, `--scenes` to render one shot, lower speed-line `count`, or turn bloom off with `"bloom": 0`. |
| Narration cut off or overlapping | Increase `narration.tail`, or remove `duration` so the scene sizes itself to the narration. |
| `ComfyUI is not running ... no ComfyUI folder is known` | Set `COMFYUI_DIR` (`setx COMFYUI_DIR "C:\path\to\ComfyUI"`, then open a new terminal), or start ComfyUI yourself first. |
| `ComfyUI exited during startup` | Read `temp/comfyui_server.log`. Usually a broken ComfyUI venv; start it manually once to see the error. |
| `out of GPU memory` during image generation | Lower `image_generation.hires.scale` (e.g. 1.25) or set `hires.enabled` to false. Close other GPU apps and the other ComfyUI. |
| `Checkpoint ... not found` | Run `python scripts/setup_models.py`, or check `--list-checkpoints` and fix `image_generation.checkpoint`. |

---

## License
Your code and storyboards are yours. Check the licenses of the models, LoRAs, fonts and music
you use. Generated BGM and demo art from `make_demo_assets.py` are free to use.
