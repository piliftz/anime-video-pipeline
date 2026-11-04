# Pipeline Roadmap: from "AI reel" to broadcast-anime feel

Logged after the cold-open draft (2026-11-03). Feedback: *the motion feels like an AI short/reel
(smooth, floaty interpolation), not snappy 2D anime acting.* That's accurate: Wan produces 24
distinct, smoothly interpolated frames per second, while TV anime is mostly drawn **on twos or
threes** (12 or 8 drawings per second) with held poses, and snaps between key poses.

Priorities weigh **feel gained** against **effort and GPU cost on an 8 GB laptop**.
Measured baseline: **23.6 s of GPU time per Wan frame**, so the 7-cut cold open took 4 h.

## 1. Anime timing: on twos/threes + holds — **do first** (cheap, big win)
- **What:** show each generated frame for 2 or 3 video frames (`on: 2` → 12 drawings/s,
  `on: 3` → 8/s), plus optional "hold" ranges where a pose freezes, then snaps.
- **Where:** a deterministic time remap in the assembly (same mechanism as `fit_duration`), set per
  cut: `"fx": {"anime_timing": {"on": 2}}`. No GPU cost, fully reversible.
- **Possible bonus, an experiment rather than a promise:** because on-threes shows only every
  third frame, Wan could generate about a third of the frames and have each held for 3 frames.
  Motion would play about 3× slower, so motion prompts must ask for faster or bigger action. If
  this holds up, the per-cut GPU cost drops roughly 2–3×, which matters more than anything else
  here for the 8-days-per-episode problem.
- **Risk:** low. Worst case it looks choppy, and we switch it off per cut.

## 2. Post-processing in assembly — **do second** (cheap)
- **Impact frames:** port the Remotion `Impact` effect (1-frame inverted B&W, white flash,
  decaying shake) into `cinematic_fx` so hits land on cuts like c007. Easy; the logic already exists.
- **Smear/snap cuts:** shorten cut-ins to 4–8 frames at action peaks, and use hard cuts on the
  beat instead of easing.
- **Speed lines:** already available (`fx.speed_lines`). Use them more, on action cuts only.
- **Line-art enhancement (experimental):** darken detected edges (XDoG / edge-detect overlay)
  for a crisper ink look. **Risk:** on AI frames this often amplifies noise and boils between
  frames. Prototype on 3 cuts before adopting.

## 3. Pose / ControlNet acting — **later** (expensive, needs source material)
- **Keyframe poses (feasible now):** an SDXL OpenPose ControlNet for Animagine keyframes, so a
  character hits an exact acting pose (slumped, pointing, flinching) instead of whatever the seed
  gives. Needs a ControlNet model (about 2.5 GB) and pose skeletons drawn or extracted from
  reference photos.
- **Pose-driven motion (hard on 8 GB):** a pose-control Wan variant driven by a pose *video*.
  Needs (a) a model that fits in 8 GB, which I haven't verified, and (b) **a reference performance
  for every shot**, e.g. phone video of someone acting it out, converted to pose skeletons. That's a
  production step per cut, not a setting. Revisit after 1 and 2 show how far cheap timing gets us.

## 4. Speed (blocks full episodes)
At 23.6 s/frame a fully animated 20-minute episode is about 190 GPU hours. To benchmark on the
laptop, one change at a time, with the same cut: 960×544 output; `fp8_e4m3fn_fast` weights; the
CUDA 13 PyTorch build ComfyUI asked for; fewer steps; whether thermal throttling explains why the
batch was slower per frame than the single test. Combined with item 1's bonus, this decides
episode length and how many cuts per episode can move.

## Done
- 2026-11-04: **Soundtrack + SFX layer**: license manifest with allow-list and auto credits; Freesound CC0
  fetcher with audition step; multi-cue music (scene-placed, fades, ducking, silence drops) and timed SFX in
  the orchestrator; cold-open sound design (drone c001-c006, hard stop into the c007 hit, silence under c008,
  title sting c009). Mix verified per cut on test tones.
- 2026-11-03: **Anime timing** (`fx.anime_timing.on` 2/3; drawing steps, camera stays smooth; speed lines
  follow the same beat) and **impact frames** (`fx.impact`: inverted B&W hit frame, flash, settling
  shake). Verified frame-by-frame in the lossless master. Cold open: twos everywhere, threes + impact on
  c007; Gōda's c003 line split across c003/c004 to remove a 5 s freeze.
  Not yet tried: generating fewer Wan frames for on-threes cuts (the possible GPU saving in item 1).
- 2026-11-03: VOICEVOX Japanese dub (`voicevox_client.py`, engine `voicevox` per speaker,
  per-line `ja_style`, EN subtitles under JA audio via `subtitle_text`, auto credits + terms).
- 2026-11-03: Title card (c009) rendered from `promo/` as PNG frames and cut into the cold open.
