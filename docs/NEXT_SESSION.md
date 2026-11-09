# Handoff: first local session (2026-11-06)

Read this, then `CLAUDE.md` ("Working with the owner") and `docs/LOCAL_ANIME_PIPELINE.md`, before doing anything.

## UPDATE 2026-11-08: direction is now 2D cel, v4 APPROVED
The owner rejected the Blender/VRM style test ("THIS LOOKS 3D I WANT 2D"); the 3D sections below are history.
The **approved** look is the 2D cel prototype v4 (`output/cel_test/APPROVED_cel_v4.mp4`, code in
`prototypes/cel_shot/`, run from the repo root; it reads `character_shot_style_template.json` for voice and timing):
- One Animagine XL drawing per cut. Masked redraws (`cel_variants.py` + `cel_jobs*.json`) add mouth shapes, angry
  faces and water "boil" frames, pasted back so the pixels outside the mask never change.
- **Movement is rigid only.** The character is cut out with rembg `isnet-anime` (`cel_matte*.py`, venv
  `C:\Users\adu94\tools\rembg-env`; dark close-ups need a brightened copy). It is moved by tilt, shift and uniform
  scale over a push-pull-filled background plate. The owner rejected v3's bending warps as lens distortion: never
  bend a drawing. Extreme close-ups move the whole cel.
- Acting comes from the voice: an inhale before speaking, head nods on stressed-syllable onsets, and a beat (dip,
  then lift) before the strike. The strike lunges on ones with a smear; the impact frame and flash come from the
  orchestrator.
- Lip sync: the mouth is on screen whenever she speaks, with three mouth drawings per talking cut, flaps on twos
  (`drawn_mouth_cues`, lead 0.04 s). Cuts land in speech gaps.
- **Now a module:** `scripts/cel_shot.py` + `cel_shot_template.json`, which reproduces v4. Audio is
  bit-identical and frames are within 1/255. Speed: about 1.3 s per frame on CPU. `prototypes/cel_shot/` is
  kept as the record of v4.
- Not done yet:
  - character consistency across cuts (hair colour drifts);
  - hair motion via redrawn hair frames (not warps);
  - new poses (ControlNet).

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
   **APPROVED - EXECUTE RENDER (v3, commit 2e0a4fe): look locked.** Owner house rules, now defaults: strikes
   wear angry brows + gritted teeth (no half-lidded look); no blinks in short close-up holds. Next: wait for
   the owner's `assets/characters/hero.vrm`, then copy character_shot_template.json, point `vrm` at it, render.
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
- ~~Free, commercially licensed, expressive local TTS~~ **DONE 2026-11-08: hero voice LOCKED.** The hero is
  female. Voice = Qwen3-TTS 1.7B **Base** clone (Apache-2.0) from two owner-approved Qwen3 VoiceDesign takes:
  `assets/voices/hero_calm_seed3.wav` for quiet/bitter lines, `hero_shout_seed3.wav` for battle cries (the clone
  copies delivery from the ref; it takes no text instructions). Recipe + ref texts: `assets/voices/hero_ref.json`
  (WAVs gitignored). Runs in `C:\Users\adu94\tools\tts\qwen3` (torch cu128, HF_HOME=tools/tts/hf), ~2.3x realtime,
  4.2 GB VRAM. Rejected: edge-tts (can't shout), Chatterbox and IndexTTS-2.5 clones (lose her voice), raw
  VoiceDesign per line (voice drifts by seed). Never IndexTTS-2.0 (CC-BY-NC codec). **Wired in 2026-11-08:**
  `engine: "qwen3"`, `qwen3: {voice: "hero", delivery: "calm" | "shout" | ["calm", "shout"] per sentence}`
  (scripts/qwen3_tts_worker.py runs in $QWEN3_TTS_PYTHON; README "Qwen3 character voices"). Test render with the
  approved v3 shot: output/hero_voice_test/ (spec temp/hero_voice_test.json, fx/ECU retimed to her shorter line).
  **v2 APPROVED 2026-11-08** (owner: "ADJUST PACE, lip sync a little off" -> fixed -> "Approved"), now IS
  `character_shot_template.json`: her voice, offset 0.7, 0.6 s silent beat (head lifts, fist winds up), fist apex
  + impact + boom + punch + speed lines on the first shouted word, face cut +0.55, 1.5 s final eye close-up; all
  hits use `at_sentence` so they follow the voice. Lip-sync is `mouth_source: flaps` (syllable envelope; Rhubarb
  only for M/B/P closures) + `mouth_lead` 0.04: Rhubarb alone held one half-open shape through her whole shout.
  The template render (output/lipsync_test/) matches the approved test: audio bit-identical, video 56.8 dB PSNR.
  When hero.vrm lands: copy the template, point `vrm` at it, render. Known: with ~2 GB free RAM the Qwen3 worker can
  crash loading weights (0xC0000005); it retries 3x. An idle ComfyUI (4.8 GB) + Chrome (13.6 GB) were the big users.
- Music: MIDI + FluidSynth vs. CC0 tracks vs. a local music model, compared by ear.
