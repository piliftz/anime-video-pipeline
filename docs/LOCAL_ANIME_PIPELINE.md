# Local Anime Production Plan (free, on one laptop)

Written 2026-11-06 after the first show was scrapped. Goal: shonen-style anime (think Naruto, Black Clover,
My Hero Academia) with **real movement, lip-sync, facial expressions and emotional delivery**, made **locally and
for free** on the owner's laptop (RTX 5060 Laptop 8 GB, 16 GB RAM). Long render times are acceptable.

Status legend: ✅ working in this repo · 🧪 proposed, not yet tested on the laptop · ⚠️ works but weak · ❌ unsolved

---

## 1. How the big studios do it (summary of the owner's research)

**Animation** (Studio Pierrot, Studio Bones and others) runs as a five-stage assembly line, with **10,000–15,000+
hand drawings per 24-minute episode** made by dozens of animators:

1. **Pre-production:** storyboard (*ekonte*) with framing, camera, dialogue timing for every second; character
   sheets (*settei*). Tools: paper, Toon Boom Storyboard Pro, Photoshop, Clip Studio Paint.
2. **Layout + key animation (*genga*):** senior animators draw the extreme poses. Tools: Wacom Cintiq, Clip Studio
   Paint EX, RETAS STUDIO (Stylos).
3. **In-betweening (*douga*) + ink & paint (*shiage*):** junior animators draw the frames between key poses; the
   paint team fills flat colours. Tools: OpenToonz, RETAS PaintMan.
4. **Backgrounds (*bijutsu*) + 3D:** painted backdrops (Photoshop, ~90%); 3D layouts for complex camera moves,
   crowds and vehicles (Maya, 3ds Max, Blender).
5. **Compositing (*satsu*):** layers stacked; lighting, blur, fog, particles, aura glows added. Tools: After Effects
   with OLM plugins.

**Sound production (*onkyō*)** has four pillars:

1. **Voice acting (*afreco*):** the cast records together *after* animation, syncing to mouth flaps
   (*kuchipaku*). Tools: Neumann U87 microphones, Avid Pro Tools.
2. **Music (BGM):** the director gives a music selection sheet; the composer writes themes (DAWs such as Logic Pro
   or Cubase, Kontakt or Spitfire libraries, plus live instruments).
3. **Sound effects (SE):** custom-designed, e.g. auras made by distorting fire or jet-engine recordings, energy
   blasts from synthesizers (Phase Plant, Serum), plus Foley.
4. **Final mix:** the sound director balances dialogue, music and effects so explosions never bury key lines and
   music swells land on impacts (Avid S6, Pro Tools, FabFilter, iZotope).

**What the software does NOT do:** none of these tools creates animation by itself. Human artists draw every frame.
Without animators, the drawing stages need a substitute.

---

## 2. The five animation stages, mapped to what we can do locally

| Studio stage | Who does it in a studio | Local substitute | Status |
|---|---|---|---|
| 1. Storyboard (*ekonte*) | Director | Claude writes it: shot list, framing, timing, dialogue (episode script JSON) | ✅ |
| 1. Character sheets (*settei*) | Designers | **VRoid Studio** (free, by pixiv): design with sliders; exports a 3D anime model with mouth shapes and expressions | 🧪 |
| 2. Layout + key animation (*genga*) | Senior animators | **Blender** (free), scripted by Claude: characters posed from motion-capture data, camera per storyboard shot | 🧪 |
| 3. In-betweening (*douga*) | Junior animators | Not needed in 3D: Blender computes every frame. Render on twos/threes for anime cadence (`anime_timing` exists) | ✅ timing |
| 3. Ink & paint (*shiage*) | Paint team | **Toon (cel) shading** in Blender: flat colours and outlines, automatic | 🧪 |
| Dialogue lip-sync (*kuchipaku*) | Animators | **Rhubarb Lip Sync** (free, open source): voice line → mouth shapes (A/I/U/E/O), automatic | 🧪 |
| 4. Backgrounds (*bijutsu*) | Background painters | **Animagine XL on ComfyUI** | ✅ |
| 5. Compositing (*satsu*) | Compositors | This pipeline: grade, impact frames, shake, speed lines, blackout. **Blender compositor** (scriptable) and particles for auras, glows, dust. Not DaVinci Resolve: its node graphs are built by hand in a GUI | ✅ partly |

This is the **3DCG anime** approach (e.g. *Land of the Lustrous*, *Beastars*, Genshin/Honkai-style cutscenes):
3D characters rendered to look 2D. It is the only free, local way found so far to get **real movement, consistent
characters and accurate lip-sync** without human animators.

**Honest limits:**
- It will look like **3D cel-shaded anime**, not hand-drawn Pierrot/Bones. Hand-drawn exaggeration and top-tier
  fight animation (*sakuga*) won't be matched.
- **Acting needs motion data:** free mocap libraries cover walking, fighting and idles; specific acting needs a
  matching clip or a phone recording of someone acting the motion (no voice needed).
- **Setup is the real cost** (characters, motion library, scene-building scripts), not render time: Blender's
  real-time renderer (Eevee) takes seconds per frame on this GPU, versus ~26 s/frame for Wan.

---

## 3. The four audio pillars, done locally

| Studio pillar | Studio tools | Local, free substitute | Status |
|---|---|---|---|
| 1. Voice acting (*afreco*) | Seiyū, Neumann U87, Pro Tools | edge-tts (EN) + VOICEVOX (JA), silence trimming | ⚠️ works, **flat delivery** |
| 2. Music (BGM) | Composer, orchestra, Kontakt/Spitfire | CC0 tracks via Freesound (wired in). Free composing tools exist (LMMS + Vital synth) but need a composer | ⚠️ generic |
| 3. Sound effects (SE) | Designers in Phase Plant/Serum + Foley | Freesound CC0 (wired in) + scripted layering, pitching, reversing and distortion with ffmpeg + **procedural synthesis in Python** (oscillators, LFOs, filters, drive; replaces hand-tweaked Vital patches) | ✅ base / 🧪 layering |
| 4. Final mix | Sound director, Avid S6, FabFilter, iZotope | Ducking, per-cut music/SFX, drops, −14 LUFS loudness (built). Per-stem EQ/compression and sidechain ducking via ffmpeg filters. Not Reaper (paid after its 60-day evaluation, and a GUI) | ✅ basic / 🧪 upgrade |

**Buildable now, free:**
1. **Signature power sounds:** layered fire roar + low boom + reversed whoosh, pitched and saturated per character.
2. **Mix stage:** per-stem EQ and compression; sidechain so explosions dip the music while dialogue stays clear;
   music hits timed to impact frames.

**The hard part, voices:** afreco relies on actors who *perform* (grunts, battle cries, a voice breaking). Free,
local, expressive TTS models exist but are **untested on this laptop**, and several **forbid commercial use**.
Cloning real seiyuu voices is ruled out (legal and ethical). Expect voice to remain the weakest element without
paid voice actors.

---

## 4. Reference: what paid cloud tools produce (Higgsfield research, 2026-11-06)

Four public Higgsfield community projects were downloaded and watched (frame sheets, cut detection, one lip-sync
measurement). Costs are estimates from public plan prices (about $0.033–0.049 per credit); the sources conflict.

| Project | Length | Generations / credits | What it shows | Est. cost |
|---|---|---|---|---|
| renderedstories "anime style" | 0:20 | 2 / 144 | Painterly samurai standoff; one Seedance 2.5 generation made 7 edited shots. No dialogue | $5–7 |
| jamil_safari "Epic Anime" | 0:28 vertical | 48 / 986 | Dialogue with **lip-sync** (mouth openness vs voice loudness r = 0.68), shouting, 33-shot fight | $33–48 |
| tambv "Iman anime OP" | 2:00 4K | 176 / 2,354 | 90s retro-cel biography montage, consistent character over ~96 shots, captions | $78–116 |
| Higgsfield staff "launch video" | 0:46 | 440 / 42,087 | Mostly a screen-recorded tutorial; ~12–14 s of actual anime | not representative |

Takeaways: paid models (Seedream 5.0 Pro, Seedance 2.0/2.5) **can** now produce lip-synced, emotional anime
dialogue and multi-shot sequences. Realistic cost: roughly **$300–2,100 per 20-minute episode**. None of these
models can be downloaded or run locally. `muapi.ai` and "Open Higgsfield AI" are front ends to the same kind of
paid service (the owner's muapi balance was $0.00), not free alternatives.

---

## 5. Working model
The owner is showrunner only (see "Working with the owner" in CLAUDE.md): every crew task is delivered as
automation. Biggest enabler: running Claude Code on the laptop itself, so installs, Blender renders and checks run
directly instead of through copy-pasted commands.

## 6. Decision and next step

Chosen direction: **local and free**, accepting long render times. Next step is a **10-second test** before any new
story is written:

1. The owner designs one character in **VRoid Studio** and exports it (about 30–60 min, no drawing skill needed).
2. Claude writes a Blender script: load the character, place it over an Animagine background, lip-sync one voice
   line with Rhubarb, change the expression mid-line, add a gesture, render with toon shading.
3. The render goes through this pipeline (sound, subtitles, final encode).
4. The owner judges it. If it still feels like a "school project", stop after hours, not weeks.

In parallel: research free, commercially usable, expressive TTS and test the best candidate by ear.

**To install on the laptop (free):** Blender, VRoid Studio, the VRM add-on for Blender, Rhubarb Lip Sync.
Exact versions and steps get recorded here once they have been run on the laptop.
