# Studio — Local Setup & Layout

Everything runs on one Windows laptop (RTX 5060 Laptop 8 GB, 16 GB RAM). All tools are free.
**Status legend:** ✅ built and tested · 🧪 built, waiting for its first real run on your laptop · 📋 planned, not built yet

## Folder layout
```
C:\Users\<you>\
├─ anime-video-pipeline\            ← this repo (code, scripts, storyboards; pushed to GitHub)
│  ├─ scripts\                      ← Python pipeline (images, video, voices, subtitles, assembly)
│  ├─ series\<show>\                ← a show: bible.md, ep01.script.json, later ep02… (none yet)
│  ├─ assets\generated\             ← AI keyframes + clips (gitignored, can be regenerated)
│  ├─ output\                       ← finished episodes (gitignored)
│  └─ docs\STUDIO_SETUP.md          ← this file
├─ comfyui-anime\                   ← ComfyUI for this show (models live here, never in git)
│  └─ models\{checkpoints,diffusion_models,text_encoders,vae,loras,ipadapter}
└─ VOICEVOX\                        ← (📋) Japanese voice engine, if adopted
```

## Tools and status
| Stage | Tool | Status | Notes |
|---|---|---|---|
| Keyframes | ComfyUI + Animagine XL 4.0 | ✅ | `make_video` / `image_generator.py`, tested on your laptop (~55 s/image) |
| Motion | Wan 2.2 TI2V-5B | ✅ (slow) | Phase 0 passed on your laptop: **38.1 min for one 5 s clip** (first run, incl. model load). `episode_producer.py motion` batches cuts in one session |
| Assembly | Python pipeline (moviepy + ffmpeg) | ✅ | lossless master, a single final encode |
| English voices | edge-tts (current) → Kokoro-82M | ✅ / 📋 | Kokoro 0.9.4, Apache-2.0, **needs Python ≤ 3.12** |
| Japanese voices | VOICEVOX engine via `scripts/setup_voicevox.py` | 🧪 | Kokoro has few Japanese voices, and its Japanese extra is hard to install on Windows |
| Subtitles | built from the known script + TTS timings | ✅ | No speech recognition needed: we already know every word and its timing |
| Subtitle QA | faster-whisper 1.2.1 (MIT) | 📋 | Used to *catch mispronunciations* (transcribe the TTS, compare with the script) |
| Voice conversion | Ultimate RVC | ⛔ deferred | See below |
| Line-art ControlNet / AnimateDiff | ComfyUI nodes | ⛔ deferred | Only after the Wan test shows where consistency breaks |

## Why RVC is deferred (read before training anything)
RVC converts a voice **into a target voice it was trained on**. That needs roughly 10+ minutes of clean
recordings of the target voice. You have no voice actors and won't record yourself, so the only
sources would be other people's voices: real seiyuu, anime clips, or community RVC models trained
on them. **Using those on a monetized channel is a copyright and personality-rights problem**, and
it is exactly what YouTube's policies target. Converting one synthetic voice into another synthetic
voice adds artifacts without adding anything. Revisit RVC only with consented recordings, for
example a paid voice actor once the channel earns.

## Setup commands (Windows PowerShell)
Already done: Git, Python 3.12, FFmpeg, the repo venv, ComfyUI at `comfyui-anime`, `COMFYUI_DIR`,
and Animagine.

```powershell
# 1. Phase 0: video model (~17 GB) + test clip
cd $HOME\anime-video-pipeline
git pull
.venv\Scripts\python scripts\setup_models.py --model wan2.2-ti2v-5b
.venv\Scripts\python scripts\video_generator.py assets\generated\s02_hikari.png --prompt "anime style, her scarf and hair sway in the wind, rain falls, slow camera push-in"
```
Voice-stack setup commands will be added here once the Kokoro and VOICEVOX integration is built and
tested. They aren't listed yet because neither has been run on your machine.

## Production order for a new show
1. **Characters**: write `series\<show>\character_refs.json`, generate candidates, pick, lock:
   ```powershell
   .venv\Scripts\python scripts\image_generator.py series\<show>\character_refs.json
   .venv\Scripts\python scripts\contact_sheet.py assets\generated\<show>\refs -o output\review\refs_sheet.png
   .venv\Scripts\python scripts\lock_refs.py series\<show>\locked_refs.json
   ```
2. **One segment end to end** as the quality bar, before writing more:
   ```powershell
   .venv\Scripts\python scripts\episode_producer.py series\<show>\ep01.script.json keyframes --segment <id>
   .venv\Scripts\python scripts\episode_producer.py series\<show>\ep01.script.json motion --segment <id>
   .venv\Scripts\python scripts\episode_producer.py series\<show>\ep01.script.json assemble --segment <id> --draft
   ```
   Before any GPU run: restart or close GPU/RAM-heavy apps (see docs/ROADMAP.md, measured facts).
