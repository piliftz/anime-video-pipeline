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
