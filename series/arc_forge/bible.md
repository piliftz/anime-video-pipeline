# Arc-Forge: Ignition — Series Bible (v0.1)

*Battle shonen / urban sci-fi fantasy / academy action. 12 episodes (1 cour). Pilot: Ep 1 (~9 min).*
Names, places and lore below are first-draft proposals: change anything, then keep this file the
single source of truth (character tags here are what the image prompts use).

## 1. Core concept
In the Forge District, every citizen is measured by their **Catalyst**: an elemental-scientific
output that powers both the city and its ranking system. Spark-Forge Academy turns high-output
students into licensed Forgers. **Kaito Kurogane** measures exactly zero. He doesn't produce energy:
he **cancels** it. When an unstable **Wildfire Vein** ruptures during his ranking exam, his touch kills
it, and the academy arrests him instead of thanking him.

**Theme:** a system that ranks people by output fears the one person who can switch it off.
**Series question (answered in Ep 10):** was Kaito's "zero" an accident, or a design?

## 2. World rules (keep consistent)
| Rule | Detail |
|---|---|
| Catalyst | Innate energy output, ranked S/A/B/C/D, plus "Zero" (unranked, social bottom). Visual: colored energy flares (cyan = high control, ember-orange = raw output). |
| Wildfire Vein | Underground leaks of unstable catalyst energy: glowing orange crystalline fissures. Officially "natural". Actually tied to the academy's founding experiments (Ep 7 / Ep 10). |
| Kaito's power | Touch-range nullification. Costs him: his hands go numb and cold, and frost-like gray cracks crawl up his arm the longer he holds it. Gives fights limits and stakes. |
| Tech level | Near-future industrial: cables, pipes, holo-HUDs, rusted slums under chrome towers. |

## 3. Cast — design lock
Tags are Animagine / Danbooru style and go straight into `characters` in the storyboard.
Hex colors are for humans (editing, promo, merch); image models ignore them.
**Production rule:** one named character per shot. Two named characters only from behind,
in silhouette, or far away (this avoids traits swapping, as seen in the test run).

### Kaito Kurogane (黒鉄カイト) — protagonist, 16, Rank Zero
- **Silhouette:** spiky hair with one ember streak, oversized academy blazer with sleeves pushed up, bandaged right hand.
- **Personality:** stubborn, funny under pressure, refuses to let anyone get hurt; hides how scared he is.
- **Tags:** `spiky hair, black hair, streaked hair, orange hair streak, amber eyes, gray blazer, school uniform, rolled up sleeves, bandaged hand, fingerless gloves`
- **Palette:** hair `#14151C` · streak `#FF7A1A` · eyes `#FFB347` · blazer `#3A4152` · piping `#FF7A1A`

### Rei Shinonome (東雲レイ) — top-ranked prodigy, rival (Ep 1 cameo, Ep 5 match)
- **Silhouette:** very long white high ponytail, white officer-style uniform, cyan energy sigils.
- **Personality:** precise, cold in public, privately curious about Kaito.
- **Tags:** `very long hair, white hair, high ponytail, cyan eyes, white uniform, military uniform, white gloves, serious`
- **Palette:** hair `#F2F5FA` · eyes `#19E6FF` · uniform `#E8ECF2` · trim `#19E6FF`

### Instructor Tetsu Gōda (剛田テツ) — exam proctor, interrogator
- **Tags:** `mature male, dark skin, dark-skinned male, muscular, short gray hair, scar on face, gray trench coat, black turtleneck, stern`
- **Palette:** coat `#4A505E` · hair `#9AA1AC`

### Locked references
Picked from `character_refs.json`, recorded in `locked_refs.json` (seed and exact prompt), copied to
`refs/` by `scripts/lock_refs.py`. These images are the identity anchors for every later shot
(IP-Adapter input). A seed alone does not keep a character consistent in new poses.
| Character | Portrait | Full body |
|---|---|---|
| Kaito | take #2 (seed 7002) | take #2 (seed 8002) |
| Rei | take #2 (seed 7002) | take #2 (seed 8002) |
| Gōda | take #3 (seed 7003) | fix take #2 (seed 8102): dark skin, black turtleneck, plain background |

### Introduced in Ep 2 (remedial squad)
- **Mina Hoshizaki (星崎ミナ)**: gadget inventor. `short orange hair, goggles on head, oversized jacket, tool belt`
- **Daigo Iwase (岩瀬ダイゴ)**: deadpan muscle-head. `buzz cut, brown hair, very muscular, tank top, expressionless`

## 4. Episode 1 (pilot): "The Zero-Catalyst Drop" — YouTube structure
TV's 90 s OP, eyecatch and 90 s ED are replaced with YouTube-friendly beats (no ad breaks, and
viewers decide in the first 30–60 s). Script: `ep01_pilot.script.json`.

| Time | Segment | Beat |
|---|---|---|
| 0:00–1:20 | Cold open | Kaito in handcuffs in an interrogation room. Gōda: a Class-A Vein died when he touched it. "Start from the beginning." |
| 1:20–1:25 | Title card | Remotion `TitleCard` (5 s) |
| 1:25–2:00 | Act A, seq 1 (written) | Hazard Trial arena: ignitions, Rei's S-rank, Kaito's zero reading (comedy drop), the fire wall, the real Vein breaks through. Shonen pacing, ~1.8 s/cut. |
| 2:00–2:45 | Act A, seq 2 (written) | Kaito charges in and grabs the Vein (Zero-Catalyst reveal), every catalyst in the arena blacks out (music-free silence), security drones take him down. |
| 2:45–~3:45 | Rei scene (planned) | Rei replays the arena footage frame by frame and stops on Kaito's grin (sets up the Ep 5 match). |
| ~3:45–~5:30 | Resolution (written) | Back in the room. Gōda tests Kaito with catalyst cells (they die in his hand), turns the recorder off: "You saved forty-one students." Accident or weapon? Gōda pockets the evidence. The Director, a faceless silhouette on a monitor: "Find out what he is, quietly." / "And if he's dangerous?" / "Then he's ours." Kaito alone in a holding cell: "Rank Zero, huh." A crack on his wrist glows. |
| ~5:30–~6:00 | Stinger (outline) | Slums. A hooded figure holds a vial of glowing Vein crystal: "So the zero finally woke up." (Sets up Ep 4.) |

## 5. Voice casting (to audition; none are final)
No lip-sync is possible, so direction favors off-screen lines, backs, eyes and hands, and wide shots.
| Character | English | Japanese |
|---|---|---|
| Kaito | Kokoro `am_puck` / `am_fenrir` | VOICEVOX young male (check that character's terms) |
| Rei | Kokoro `af_bella` / `bf_emma` | VOICEVOX calm female |
| Gōda | Kokoro `am_michael` / `bm_george` | VOICEVOX low male |
| Narrator / system voice | Kokoro `bm_george` | Kokoro `jm_kumo` or VOICEVOX |
Japanese dub: VOICEVOX is preferred because Kokoro has few Japanese voices. **Every VOICEVOX
character has its own terms of use** (most allow commercial use with a credit such as
"VOICEVOX:Name"); record them before release. A native Japanese speaker should review the
Japanese script before the JA dub ships.
