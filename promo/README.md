# Arc-Forge: Ignition — Promo Toolkit (Remotion + Three.js + GSAP)

Programmatic, frame-exact promo assets for the series. Everything is code, so changing an episode
title or the countdown day is a one-line edit followed by a re-render.

| Composition | Size | Length | Use |
|---|---|---|---|
| `TitleCard` | 1920×1080 | 5 s | Episode title card (feeds into the episode pipeline) |
| `Teaser` | 1920×1080 | 30 s | YouTube/X promo teaser |
| `CountdownReel` | 1080×1920 | 15 s | Shorts / Reels / TikTok countdown |

## Setup (Windows PowerShell, from the repo root)
Needs Node.js 20 or newer (`winget install OpenJS.NodeJS.LTS`, then open a new terminal).
```powershell
cd promo
npm ci
npm run studio            # live editor at http://localhost:3000: scrub, tweak props, preview
```

## Render
```powershell
npm run render:title      # out/title_card.mp4
npm run render:teaser     # out/teaser.mp4
npm run render:countdown  # out/countdown_reel.mp4
npm run still:title       # out/title_card.png (frame 80), a quick check of the look
npm run render:title:frames   # lossless PNG frames -> ../assets/generated/clips/title_card (episode use)
```
Change props without touching code:
```powershell
npx remotion render src/index.ts TitleCard out/ep02_title.mp4 --props='{\"episodeNumber\":2,\"episodeTitle\":\"The Gear and the Muscle\"}'
npx remotion render src/index.ts CountdownReel out/countdown_3.mp4 --props='{\"daysLeft\":3,\"premiereLabel\":\"EPISODE 01  ·  FRIDAY\",\"callToAction\":\"SUBSCRIBE\"}'
```
**Using the title card inside an episode:** render `render:title:frames`, then in the episode storyboard
add a scene with `"clip": "assets/generated/clips/title_card", "sequence_fps": 24`. PNG frames keep
the pipeline's single-lossy-encode rule (a rendered MP4 would add a second lossy generation).

**Teaser keyframes and music:** drop `shot_01.png`…`shot_05.png` into `public/keyframes/` and
`teaser_music.wav` into `public/audio/`. Missing files are skipped automatically (energy plates are
shown instead), so the teaser renders before any art exists. The music cuts out for the final tagline
and slams back in on the title impact.

## Layout
```
promo/
  remotion.config.ts        PNG frames -> H.264 CRF 16, yuv420p, BT.709 tags, GPU WebGL (angle)
  public/fonts/             bundled fonts (SIL OFL licenses alongside); renders need no internet
  public/keyframes/, audio/ your teaser assets
  src/brand.ts              colors, fonts, series title (single source of truth)
  src/Root.tsx              compositions + default props
  src/compositions/         TitleCard, Teaser, CountdownReel
  src/components/           EnergyFlare (Three.js), Fx (background, impact frames, slash), TitleReveal
  src/lib/useGsapTimeline.ts  GSAP driven by Remotion's frame clock
```

## The one rule: every pixel is a function of the frame number
Remotion renders frames out of order, in several browser tabs at once. Anything driven by real time
or unseeded randomness flickers or jumps in the final video. So:

- Animate with `useCurrentFrame()`, `interpolate()` and `spring()`; never `setTimeout`, CSS
  transitions or CSS animations.
- Randomness: `random('seed-string')` from `remotion`, never `Math.random()`.
- **Three.js:** compute positions from `useCurrentFrame()` (see `EnergyFlare.tsx`). Never use
  react-three-fiber's `useFrame()` or `THREE.Clock`, which run on the browser's real-time loop.
- **GSAP:** only through `useGsapTimeline()`, which builds a paused timeline and `seek()`s it to
  `frame / fps` on every frame. A playing GSAP timeline is wrong in Remotion.
- Fonts and images are loaded via `loadFont` / `<Img>` / `staticFile()`, which hold the render
  until they're ready, so no frame can show a fallback font.

Verified while building this: identical props render the same frame within 1 level in 1 pixel
(floating-point noise of software WebGL).

## Three.js tips
- Particles: 1–3k points with additive blending and a soft sprite texture look like energy without
  post-processing. Heavy bloom passes multiply render time.
- Render with `--gl=angle` on Windows/macOS (uses the GPU; set in `remotion.config.ts`).
  On a Linux server without a GPU use `--gl=swangle` (software, slow).
- Keep geometry and textures in `useMemo`. Only per-frame data (positions, opacity) should change.
- Japanese text uses a **kana-only subset** of Noto Sans JP (`noto-sans-jp-kana-900.woff2`, 89 KB).
  Kanji are not included. To add them, subset the full font again with fontTools
  (`pyftsubset NotoSansJP[wght].ttf --text="…" --flavor=woff2`) and replace the file.

## GSAP for a future landing page (best practices)
- Animate only `transform` and `opacity` (`x`, `y`, `scale`, `rotation`, `autoAlpha`). Animating
  `top`, `left`, `width` or `height` forces layout every frame and stutters on phones.
- Build sequences with `gsap.timeline({defaults: {...}})`, labels and the position parameter
  (`'<'`, `'-=0.2'`) instead of chained delays. That's easier to retime.
- In React use `useGSAP()` from `@gsap/react` (or `gsap.context()`), which reverts every tween and
  ScrollTrigger on unmount, preventing leaks.
- Use `gsap.matchMedia()` for breakpoints **and** `prefers-reduced-motion`: give reduced-motion
  users fades instead of motion.
- ScrollTrigger: prefer `scrub` and a few pinned sections over dozens of triggers. Use
  `ScrollTrigger.batch()` for card grids, and call `ScrollTrigger.refresh()` after images and fonts load.
- Three.js on the web (unlike in Remotion, real time is fine there): cap
  `renderer.setPixelRatio(Math.min(devicePixelRatio, 2))`, pause rendering when the canvas is
  off-screen (IntersectionObserver), and lazy-load the 3D bundle after first paint.

## Licenses (check before monetizing)
- **Remotion** is free for individuals and companies of up to 3 people; larger companies need a
  paid company license (remotion.pro). An indie solo studio qualifies for free use.
- **GSAP** has been free (including its formerly paid plugins) under its own "no charge" license
  since 2025, which forbids using it to build a competing visual website builder. Fine here.
- **Three.js**: MIT. **Fonts**: SIL OFL (licenses in `public/fonts/`).
