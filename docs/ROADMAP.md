# Pipeline Roadmap

## Status (2026-11-06): starting a new show
The first show (Arc-Forge: Ignition) was removed at the owner's request. Its bible, scripts, character refs and the
Remotion promo toolkit are recoverable from git history: `git checkout 5a0bcfe -- series/arc_forge promo`.
The pipeline code (voices, audio licensing, subtitles, assembly, FX, ComfyUI/Wan clients) stays and is show-agnostic.

**Plan for the redo:** [LOCAL_ANIME_PIPELINE.md](LOCAL_ANIME_PIPELINE.md) (studio pipeline mapped to free local tools).

## Why it was scrapped: owner feedback on the finished drafts
"Doesn't feel like a popular anime; looks like a school project." Specifically:
1. **No lip-sync** (the production rule hid mouths: backs, eyes, hands, silhouettes).
2. **No character movement or acting**: mostly stills with camera moves; Wan used for 3-4 shots per minute.
3. **No emotion in faces**: expressions came from single SDXL keyframes, not performances.
4. **Flat dialogue delivery**: edge-tts/VOICEVOX read lines like a narrator, with no acting.
5. Art/consistency, story/pacing and sound were also named.

Any redo has to answer 1-4 directly. They are limits of the *approach* (local stills + a 5B video model + TTS), not
bugs. A new story alone will not fix them.

## Measured facts to plan with (RTX 5060 Laptop 8 GB, 16 GB RAM)
- SDXL keyframe (Animagine 4.0, 28 + 20 hires steps): **~47-60 s** with other apps closed; 55-178 s and one crash
  with many GPU/RAM-heavy apps open.
- Wan 2.2 TI2V-5B, 1280x704: **~25-26 s per frame** when healthy (73 frames ~ 32 min). Outliers (108 s/frame, a
  10-hour stall) were memory: idle python processes and desktop apps holding GPU memory. Restart + close apps first.
- A fully Wan-animated 20 min episode ~ 190 GPU hours. Not viable weekly on this laptop.
- Wan handled motion (fire, energy, rising crystals) but failed **global lighting/colour changes** (2/2) and hands.
- edge-tts pads lines with silence; lines are now trimmed. Full stops add pauses: one sentence per action line.

## Open engineering items (show-agnostic)
- Speed benchmarks: 960x544, `fp8_e4m3fn_fast`, fewer steps, fewer frames on threes (held 3x).
- Lip-sync and expressive voices: no local, free solution has been tested here. Must be researched before a redo
  promises them.
- Two-pass loudnorm for exact -14 LUFS on short clips (single pass lands within ~1-1.5 LU).

## Done (pipeline features, all reusable)
- `fx.blackout` (lights-out / colour drain), action camera presets (crash_zoom, slam_out, whip_*), continuous
  `shake`, `impact` frames with coloured flashes, anime timing on twos/threes.
- Audio: license manifest + allow-list, Freesound CC0 fetcher (SFX + music/ambience beds), multi-cue music with
  ducking and drops, timed SFX, -14 LUFS loudness normalization, auto credits.
- Voices: edge-tts + VOICEVOX (JA dub, styles, credits/terms), silence trimming, subtitle_text (dub + other-language
  subs).
- Production: episode_producer (keyframes -> Wan -> assemble, resumable), per-segment output folders, automatic
  tidy/clean, ComfyUI crash diagnostics, dropped-job detection.
