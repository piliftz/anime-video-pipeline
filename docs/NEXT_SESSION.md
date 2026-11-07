# Handoff: first local session (2026-11-06)

Read this, then `CLAUDE.md` ("Working with the owner") and `docs/LOCAL_ANIME_PIPELINE.md`, before doing anything.

## Where things stand
- The first show (Arc-Forge) was scrapped and removed; a **new story** will be written later. Don't write one yet.
- The owner rejected the old output as a "school project": no lip-sync, no character movement, no expressions, flat
  TTS. Paid cloud tools (Higgsfield, muapi) were researched and ruled out: the owner wants **local and free**, and
  accepts long render times.
- Chosen direction: **3D cel-shaded anime in Blender** (VRoid character, Rhubarb lip-sync, Eevee toon shading,
  Animagine backgrounds, this pipeline for sound/subtitles/final encode). See docs/LOCAL_ANIME_PIPELINE.md.
- The owner is showrunner only. This session runs **on the owner's laptop** (Windows, RTX 5060 Laptop 8 GB,
  16 GB RAM, repo at `C:\Users\adu94\anime-video-pipeline`), so run installs, scripts and renders yourself; don't
  hand the owner commands to paste unless something truly needs their hands (e.g. an installer's admin prompt).
- Before any GPU-heavy run: close GPU/RAM-heavy apps. Past failures (a ComfyUI crash, a 10-hour Wan stall on CUDA
  out-of-memory) were memory pressure from other apps and leftover python processes.

## First task: the 10-second test
The owner is designing one character in **VRoid Studio** and will export a `.vrm` file
(expected under `assets\characters\`, e.g. `hero.vrm`). Then:

1. Install and verify (check versions and licenses as you go; record exact versions here afterwards):
   Blender (supports the RTX 5060 / Blackwell GPU; verify), the **VRM add-on for Blender**, **Rhubarb Lip Sync**.
   **Installed and verified 2026-11-07:**
   - Blender **5.2.2 LTS** (`$BLENDER_EXE` = `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`, GPL).
     Headless Eevee render works on the RTX 5060 (OpenGL backend, driver 616.92); Cycles sees it via OptiX and CUDA.
   - VRM add-on **"VRM format" 4.10.4** (extensions.blender.org id `vrm`, MIT / GPL-3.0-or-later), installed with
     `blender --command extension install -s --enable vrm`; `bpy.ops.import_scene.vrm` is available headless.
   - Rhubarb Lip Sync **1.14.0** (`$RHUBARB_EXE` = `C:\Users\adu94\tools\Rhubarb-Lip-Sync-1.14.0-Windows\rhubarb.exe`,
     MIT; bundled deps BSD-2 / Boost / MIT). Zip sha256 `62fa416a…098d1fe3`. Takes 48 kHz mono WAV directly.
     Recognizers: `pocketSphinx` (English, ~2 s per 1.8 s line) and `phonetic` (language-independent: use for JA).
     Use `-f json --extendedShapes GHX`.
   **Steps 2-3 built and tested 2026-11-07 on a sample model** (pixiv's `VRM1_Constraint_Twist_Sample.vrm`
   from vrm-c/vrm-specification, VRM Public License 1.0, internal testing only, gitignored under
   `assets/characters/_samples/`): `scripts/character_shot.py` + `scripts/blender_vrm_shot.py`, spec
   `character_shot_template.json` → `output/lipsync_test/lipsync_test.mp4`. Verified in frames and audio: mouth
   cues line up with speech onsets (1.24 s, 4.00 s, end 5.94 s), expression change, fist gesture, nod, blink,
   camera punch, spring-bone hair/sleeves (needs the 2 s pre-roll, else sleeves stick out in T-pose).
   Eevee benchmark (48 frames, 1920x1080, 16 samples, on ones): first frame 1.30 s, then **0.56 s/frame**
   (0.54-0.59). The 10 s shot on twos: 120 drawings, 71 s of rendering; whole flow ~2.5 min incl. encode.
   Known gaps: MToon caps light intensity, so the character stays bright against night backgrounds (light
   colour is the only lever so far); the key light's cel shadow edge can cross the mouth; only two gestures
   (fist_to_chest, nod); English voice via edge-tts. When the owner's `hero.vrm` lands: copy the template,
   point `vrm` at it, render, show the owner.
   **Owner verdict 2026-11-07: tech APPROVED, look REJECTED.** Reworked the same line: 4 hard cuts (eyes close-up
   with head down, low/dutch 24 mm fist cut, low/dutch shouting close-up, extreme close-up on the eyes), anime
   closed/half/open mouth flaps (mouth_gain 1.2), brows/eyes via raw shape keys so the mouth stays free, key-pose
   gestures (fist_raise: anticipation, overshoot, hold; head_down), lighting through MToon (exposure 0.72, 40%
   tint toward the background mean, tight blue fresnel rim, face shading shift so no shadow edge on the face),
   Eevee depth of field per cut, text-free background bg_platform_night_b (character_backgrounds.json; c is a
   rooftop alternative). Render: 0.70 s/drawing with DoF and close-ups. Awaiting the owner's next verdict.
   **Owner verdict: REJECT - LACKS IMPACT** (v2 committed as 8997201). v3: warm saturated skin with blue only on
   the shadow side and rim (shade colour blend, toony 0.98, side key), fist cut at 18 mm with the fist ~1/3 of
   frame + impact frame + radial speed lines + shake on the 4.30 s apex (boom SFX on the same frame),
   overdriven angry brows (1.9) + narrowed eyes + gritted teeth between flaps, eye close-up cut to 1.85 s with
   a hard flag shadow across the upper face and emissive catchlights; shot now 8.1 s. Frame cache key now
   includes blender_vrm_shot.py (code changes used to be skipped).
2. Write a headless Blender script (`blender --background --python ...`) that:
   - imports the VRM, applies Eevee + toon (cel) shading with outlines;
   - places the character over an Animagine background (ComfyUI is at `C:\Users\adu94\comfyui-anime`,
     `COMFYUI_DIR` is set; `scripts/image_generator.py` makes backgrounds);
   - lip-syncs one voice line: synthesize it with `scripts/voiceover_generator.py`, run Rhubarb on the WAV, map
     Rhubarb mouth shapes to the model's VRM mouth blendshapes (A/I/U/E/O + closed);
   - changes the facial expression mid-line (VRM expression presets) and adds one simple gesture;
   - renders 10 s at 24 fps to PNG frames (consider rendering on twos, matching `fx.anime_timing`).
3. Feed the frames + voice through this pipeline (a storyboard with the frame folder as a clip, narration, sfx) so
   sound, subtitles and the single final encode are reused.
4. Show the owner the result with the showrunner command menu (REJECT - LACKS IMPACT / ADJUST PACE /
   APPROVED - EXECUTE RENDER). If they judge it still "school project", stop and report honestly.

Keep new code to the repo's rules in CLAUDE.md (pathlib, PipelineError, no stubs, verify by inspecting frames).

## Open research (in parallel, report before adopting)
- Free, **commercially licensed**, expressive local TTS (the voice is the weakest link). Test by ear.
- Music: MIDI + FluidSynth vs. CC0 tracks vs. a local music model, compared by ear.
