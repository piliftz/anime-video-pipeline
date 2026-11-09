"""Prototype v4 (rigid layers) of the 2D cel shot: human acting, lip sync on twos, retimed cuts.

Acting is a puppet rig on each drawing (weighted rotate / scale / shift / wave fields), driven by the voice:
an inhale before speaking, head nods on stressed syllables (spring impulse per onset), the jaw following loudness,
hair that lags behind the head (overlapping action), and smooth noise instead of perfect sine idles. The rig and the
camera are ONE bilinear resample per frame. Acting on twos, the strike on ones, water on threes (twos after the hit)."""
import math
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, r"C:\Users\adu94\anime-video-pipeline\scripts")
import character_shot as cs  # noqa: E402
from pipeline_utils import save_json  # noqa: E402

ROOT = Path(r"C:\Users\adu94\anime-video-pipeline")
G = ROOT / "assets/generated/cel_test"
FRAMES = ROOT / "temp/cel_test/frames"
OUT = ROOT / "output/cel_test/cel_rigid.mp4"
W, H, FPS = 1920, 1080, 24
ACT_ON = 2                                       # acting + lip flaps on twos
ONLY = [float(a) for a in sys.argv[1:]]

shot, base = cs.load_shot(ROOT / "character_shot_style_template.json")
shot["duration"] = 6.60                          # was 7.15: final stare 1.2 s instead of 1.8 s of silence
probe = cs.build_storyboard(shot, base, FRAMES, OUT, [0.0] * len(shot["sfx"]))
voice = cs.synthesize(probe, "none")
sents = voice.get("boundaries")
offset = float(shot["line"]["offset"])
s2 = offset + sents[1]["start"]
work = ROOT / "temp/cel_test"
rh = cs.rhubarb_cues(voice, shot["line"]["recognizer"], work)
dur = float(shot["duration"])
flaps = cs.drawn_mouth_cues(rh, Path(voice["audio"]), offset, dur, FPS, ACT_ON, "flaps", 0.04)

# ---------------------------------------------------------------- voice-driven acting signals
with wave.open(str(voice["audio"])) as wf:
    sr, nch = wf.getframerate(), wf.getnchannels()
    pcm = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float32) / 32768
if nch > 1:
    pcm = pcm.reshape(-1, nch).mean(1)
HOP = sr // 100                                   # 10 ms loudness grid (line time)
nh = len(pcm) // HOP
DB = 20 * np.log10(np.sqrt((pcm[:nh * HOP].reshape(nh, HOP) ** 2).mean(1)) + 1e-6)
SPEECH_END = offset + sents[-1]["end"]


def loud(t):
    """0..1 loudness at shot time t (smoothed over 30 ms)."""
    i = int((t - offset) * 100)
    if i < 0 or i >= nh:
        return 0.0
    v = DB[max(0, i - 1):i + 2].mean()
    return float(np.clip((v + 40) / 25, 0, 1))


ONSETS = []                                       # (time, strength): stressed syllable starts
for i in range(4, nh):
    if DB[i] > -27 and DB[i] - DB[i - 4:i].min() > 10:
        t0 = offset + i / 100
        if not ONSETS or t0 - ONSETS[-1][0] > 0.16:
            ONSETS.append((t0, float(np.clip((DB[i:i + 8].max() + 30) / 14, 0.35, 1.0))))


def nod(t, tau=0.10):
    """Head nod signal: a damped impulse after every stressed syllable (peaks ~tau s after the onset)."""
    v = 0.0
    for t0, a in ONSETS:
        d = t - t0 + 0.03                         # the head leads the sound a hair, like real speech
        if 0 < d < 8 * tau:
            v += a * (d / tau) * math.exp(1 - d / tau)
    return v


def noise(t, seed):
    """Smooth, non-repeating idle drift in -1..1 (sum of incommensurate sines)."""
    r = np.random.default_rng(seed)
    f, p = r.uniform(0.25, 0.9, 3), r.uniform(0, 6.28, 3)
    return float(sum(math.sin(2 * math.pi * fi * t + pi) for fi, pi in zip(f, p)) / 3)


STRIKE0, STRIKE1 = s2 - 0.12, s2
C2_AT, C3_AT, C4_AT = 2.95, s2 + 0.55, SPEECH_END + 0.06   # C3 cuts in the silence after "Watch me"
CUTS = [
    (0.0, "c1", (1.00, 1.06, 0.47, 0.50)),
    (C2_AT, "c2", (1.00, 1.04, 0.50, 0.55)),
    (C3_AT, "c3", (1.04, 1.09, 0.48, 0.64)),
    (C4_AT, "c4", (1.02, 1.08, 0.52, 0.50)),
]


def shape_at(t):
    lt = t - offset
    for c in flaps:
        if c["start"] <= lt < c["end"]:
            return c["value"]
    return "X"


def load(name):
    return np.asarray(Image.open(G / f"{name}.png").convert("RGB"), np.float32) / 255


IMG = {n: load(n) for n in ("cel_c1_eyes", "cel_c1_m_half", "cel_c1_m_open", "cel_c2_fist", "cel_c2_w2",
                            "cel_c2_w3", "cel_c2_f_closed", "cel_c2_f_open", "cel_c3_m_closed3", "cel_c3_m_closed2",
                            "cel_c3_m_half3", "cel_c4_eyes")}
FACE2 = {k: IMG[f"cel_c2_f_{k}"] - IMG["cel_c2_fist"] for k in ("closed", "open")}   # face-only deltas
SH, SW = IMG["cel_c1_eyes"].shape[:2]


def drawing(cut, t):
    """Returns (image, mouth_open) for the cut at acting time t."""
    sh = shape_at(t)
    if cut == "c1":
        name = "cel_c1_eyes" if sh in "XA" else "cel_c1_m_half" if sh in "BGF" else "cel_c1_m_open"
        return IMG[name], name.endswith("open")
    if cut == "c2":
        step = 2 if t >= s2 else 3
        water = IMG[("cel_c2_fist", "cel_c2_w2", "cel_c2_w3")[int((t * FPS) // step) % 3]]
        face = "open" if t >= s2 and sh not in "XA" else "closed"                      # gritted teeth on the strike
        return water + FACE2[face], False
    if cut == "c3":
        name = "cel_c3_m_closed3" if sh in "XA" else "cel_c3_m_closed2" if sh in "BGF" else "cel_c3_m_half3"
        return IMG[name], name.endswith("half3")
    return IMG["cel_c4_eyes"], False


# ---------------------------------------------------------------- rig primitives (inverse maps, source pixels)
def weight(x, y, cx, cy, rx, ry):
    d = np.sqrt(((x - cx * SW) / (rx * SW)) ** 2 + ((y - cy * SH) / (ry * SH)) ** 2)
    u = np.clip((1.0 - d) / 0.45, 0.0, 1.0)
    return u * u * (3 - 2 * u)


def rotate(x, y, w, px, py, deg):
    a = -math.radians(deg) * w
    dx, dy = x - px * SW, y - py * SH
    c, s = np.cos(a), np.sin(a)
    return px * SW + c * dx - s * dy, py * SH + s * dx + c * dy


def scale(x, y, w, cx, cy, k):
    f = 1.0 + (k - 1.0) * w
    return cx * SW + (x - cx * SW) / f, cy * SH + (y - cy * SH) / f


def shift(x, y, w, dx, dy):
    return x - dx * w, y - dy * w


def wave(x, y, w, amp, wl, ph, ramp=True):
    """Hair sway: horizontal sine travelling down the strand; tips (lower) move more."""
    g = np.clip(y / SH, 0, 1) if ramp else 1.0
    return x - amp * w * g * np.sin(2 * math.pi * y / (wl * SH) + ph), y


def ease_out(u):
    u = min(max(u, 0.0), 1.0)
    return 1 - (1 - u) ** 3


def ease_in(u):
    u = min(max(u, 0.0), 1.0)
    return u ** 3


def back_out(u, s=2.2):
    u = min(max(u, 0.0), 1.0) - 1
    return 1 + (s + 1) * u ** 3 + s * u ** 2


def jitter(t, seed, step=2):
    """Stepped tremble: a new random offset every `step` frames (reads as drawn shake, not video shake)."""
    k = int(t * FPS) // step
    r = np.random.default_rng(seed * 1000 + k)
    return r.uniform(-1, 1), r.uniform(-1, 1)


def rig_k(t):
    """Lunge scale during the strike (also used for the smear taps)."""
    if t < STRIKE0:
        return 1.0
    return 1.0 + 0.14 * ease_in((t - STRIKE0) / (STRIKE1 - STRIKE0))


def ease_io(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


# ---------------------------------------------------------------- layers: matte + clean plate (rigid moves only)
def load_alpha(name, bright_fix=False):
    a = np.asarray(Image.open(G / "_masks" / f"{name}_alpha.png").convert("L"), np.float32) / 255
    if bright_fix:
        a[:, : int(0.15 * a.shape[1])] = 0.0           # drop the lantern blob the matte picked up
    a = np.clip((a - 0.35) / 0.3, 0, 1)                # crisp cel edge: no half-transparent character areas
    im = Image.fromarray((a * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
    return np.asarray(im, np.float32) / 255


ALPHA = {"c1": load_alpha("cel_c1_eyes_bright", True), "c2": load_alpha("cel_c2_fist")}


def push_pull(img, known):
    """Fill unknown pixels from their surroundings (pyramid average), for the plate behind the character."""
    if min(img.shape[:2]) < 4:
        k = known.sum()
        return np.broadcast_to((img * known[..., None]).sum((0, 1)) / max(k, 1e-6), img.shape).copy()
    h2, w2 = img.shape[0] // 2 * 2, img.shape[1] // 2 * 2
    pi = (img * known[..., None])[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2, 3).sum((1, 3))
    pk = known[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2).sum((1, 3))
    coarse = push_pull(pi / np.maximum(pk, 1e-6)[..., None], (pk > 0).astype(np.float32))
    up = np.stack([np.asarray(Image.fromarray(coarse[..., c]).resize((img.shape[1], img.shape[0]), Image.BILINEAR))
                   for c in range(3)], axis=-1)                                     # smooth, no blocks
    return img * known[..., None] + up * (1 - known[..., None])


_PLATES = {}


def plate(cut, key, img):
    """Background with the character removed (cached per drawing); the matte is grown so no outline is left."""
    if (cut, key) not in _PLATES:
        a = Image.fromarray((ALPHA[cut] * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(25))
        known = (np.asarray(a, np.float32) / 255 < 0.03).astype(np.float32)
        _PLATES[(cut, key)] = push_pull(img, known)
    return _PLATES[(cut, key)]


def layer_pose(cut, t):
    """Rigid pose of the character layer: (pivot_x, pivot_y, degrees, scale, dx, dy) in source pixels."""
    if cut == "c1":
        inhale = ease_io((t - 0.40) / 0.30) * (1 - ease_io((t - 0.75) / 0.6))         # breath in before speaking
        lift = ease_io((t - 2.62) / 0.30)                                             # beat: she looks up
        dip = math.exp(-((t - 2.60) / 0.06) ** 2)                                     # tiny antic before the lift
        n = nod(t)
        return (0.58, 1.25, 3.0 * (1 - lift) + 0.5 * n + 0.4 * noise(t, 1) + 0.6 * dip, 1.0,
                3 * noise(t, 2), 26 * (1 - lift) + 6 * n + 5 * dip - 7 * inhale)
    if cut == "c2":                                                                   # whole body drives the punch
        a = ease_io((t - C2_AT) / 0.18)
        if t < STRIKE0:
            deg, k, dx, dy = -2.0 * a, 1.0, 14 * a, -10 * a                              # wind back: lean away
        elif t < STRIKE1:
            u = ease_in((t - STRIKE0) / (STRIKE1 - STRIKE0))
            deg, k, dx, dy = -2.0 + 4.0 * u, rig_k(t), 14 - 24 * u, -10 + 16 * u          # lunge into the camera
        else:
            v = (t - s2) / 0.40
            deg = 2.0 - 1.5 * ease_out(v)
            k = 1.14 - 0.08 * back_out(v)                                               # overshoot, settle
            dx, dy = -10 + 6 * ease_out(v), 6 - 3 * ease_out(v)
        return (0.41, 0.66, deg + 0.3 * nod(t), k, dx, dy + 3 * nod(t))
    if cut == "c3":                                                                   # whole cel moves (ECU)
        u = ease_out((t - C3_AT) / 0.18)
        n = nod(t)
        jx, jy = jitter(t, 3)
        rage = 1.5 if t < SPEECH_END else 0.7
        return (0.50, 0.95, 0.8 * noise(t, 6) - 0.6 * n, 1.03 + 0.03 * back_out(u) + 0.01 * n,
                rage * jx + 3 * noise(t, 5), 12 * n + rage * jy)
    pant = math.sin(2 * math.pi * (t - C4_AT) / 1.1)                                  # c4: catching her breath
    return (0.5, 0.9, 0.3 * noise(t, 7), 1.02, 2 * noise(t, 8), 4 * pant)


def pose_map(pose, x, y, k_override=None):
    """Inverse rigid map: output coords -> layer source coords (rotation + uniform scale + shift, no bending)."""
    px, py, deg, k, dx, dy = pose
    if k_override is not None:
        k = k_override
    px, py = px * SW, py * SH
    a = -math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    ux, uy = x - px - dx, y - py - dy
    return px + (c * ux - s * uy) / k, py + (s * ux + c * uy) / k


# ---------------------------------------------------------------- one resample: camera + rig, bilinear
UU, VV = np.meshgrid(np.arange(W, dtype=np.float32) + 0.5, np.arange(H, dtype=np.float32) + 0.5)


def cam_coords(z, cx, cy):
    cover = max(W / SW, H / SH)
    bw, bh = W / (cover * z), H / (cover * z)
    x0 = min(max(cx * SW - bw / 2, 0), SW - bw)
    y0 = min(max(cy * SH - bh / 2, 0), SH - bh)
    return x0 + UU * (bw / W) - 0.5, y0 + VV * (bh / H) - 0.5


def sample(img, x, y):
    x = np.clip(x, 0, SW - 1.001)
    y = np.clip(y, 0, SH - 1.001)
    x0, y0 = x.astype(np.int32), y.astype(np.int32)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    a, b = img[y0, x0], img[y0, x0 + 1]
    c, d = img[y0 + 1, x0], img[y0 + 1, x0 + 1]
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


rng = np.random.default_rng(7)
N = 70
EMB = dict(x=rng.random(N), y=rng.random(N), v=0.04 + 0.08 * rng.random(N), r=1.5 + 3.5 * rng.random(N),
           ph=rng.random(N) * 6.28, a=0.5 + 0.5 * rng.random(N))


def embers(t):
    layer = np.zeros((H, W, 3), np.float32)
    ys = (EMB["y"] - EMB["v"] * t) % 1.0
    xs = (EMB["x"] + 0.015 * np.sin(EMB["ph"] + t * 1.7)) % 1.0
    for x, y, r, a, ph in zip(xs * W, ys * H, EMB["r"], EMB["a"], EMB["ph"]):
        flick = a * (0.7 + 0.3 * math.sin(ph + t * 9))
        x0, x1, y0, y1 = int(max(0, x - 3 * r)), int(min(W, x + 3 * r)), int(max(0, y - 3 * r)), int(min(H, y + 3 * r))
        if x1 <= x0 or y1 <= y0:
            continue
        gy, gx = np.mgrid[y0:y1, x0:x1]
        g = np.exp(-((gx - x) ** 2 + (gy - y) ** 2) / (2 * (r * 0.6) ** 2)) * flick
        layer[y0:y1, x0:x1] += g[..., None] * np.array([1.0, 0.62, 0.25], np.float32)
    return layer


def bloom(frame):
    lum = frame.max(axis=2)
    bright = np.clip((lum - 0.93) / 0.07, 0, 1)[..., None] * frame
    small = Image.fromarray((bright * 255).astype(np.uint8)).resize((W // 4, H // 4), Image.BILINEAR)
    glow = np.asarray(small.filter(ImageFilter.GaussianBlur(10)).resize((W, H), Image.BILINEAR), np.float32) / 255
    return frame + glow * 0.35


def acting_time(cut, t):
    if cut == "c2" and STRIKE0 - 1e-6 <= t < s2 + 0.10:
        return t
    return math.floor(t * FPS / ACT_ON + 1e-6) * ACT_ON / FPS


def render(t):
    i = max(k for k, c in enumerate(CUTS) if c[0] <= t + 1e-6)
    start, cut, (z0, z1, cx, cy) = CUTS[i]
    end = CUTS[i + 1][0] if i + 1 < len(CUTS) else dur
    u = (t - start) / max(1e-6, end - start)
    z = z0 + (z1 - z0) * (1 - (1 - u) ** 2)
    if cut == "c2" and t >= s2:
        k = min(1.0, (t - s2) / 0.18)
        z *= 1.0 + 0.10 * (1 - k) ** 2
    ta = acting_time(cut, t)
    img, _ = drawing(cut, ta)
    x, y = cam_coords(z, cx, cy)
    pose = layer_pose(cut, ta)
    if cut in ALPHA:                                                  # character layer over its clean plate
        key = int((ta * FPS) // (2 if ta >= s2 else 3)) % 3 if cut == "c2" else 0
        bg = sample(plate(cut, key, img), x, y)
        taps = [ta - (1 / FPS) * j / 5 for j in range(6)] if cut == "c2" and STRIKE0 <= t < s2 else [None]
        fg = np.zeros((H, W, 3), np.float32)
        al = np.zeros((H, W, 1), np.float32)
        for tt in taps:                                               # smear = rigid layer averaged along travel
            sx, sy = pose_map(pose, x, y, None if tt is None else rig_k(tt))
            a = sample(ALPHA[cut][..., None], sx, sy)
            fg += sample(img, sx, sy) * a
            al += a
        fg /= len(taps)
        al /= len(taps)
        fr = fg + bg * (1 - al)
    else:
        sx, sy = pose_map(pose, x, y)
        fr = sample(img, sx, sy)
    return np.clip(bloom(fr) + embers(t) * (0.9 if cut != "c4" else 0.5), 0, 1)


def save(fr, path):
    Image.fromarray((fr * 255 + 0.5).astype(np.uint8)).save(path, compress_level=1)


if ONLY:
    for t in ONLY:
        save(render(t), work / f"still_{t:05.2f}.png")
        print("still", t, flush=True)
    sys.exit(0)

FRAMES.mkdir(parents=True, exist_ok=True)
for old in FRAMES.glob("frame_*.png"):
    old.unlink()
total = int(round(dur * FPS))
for f in range(1, total + 1):
    save(render((f - 1) / FPS), FRAMES / f"frame_{f:05d}.png")
    if f % 24 == 0:
        print(f"frame {f}/{total}", flush=True)

sfx_times = [cs.resolve_time(c, sents, offset) for c in shot["sfx"]]
shot["fx"] = cs.resolve_fx(shot["fx"], sents, offset)
sb = cs.build_storyboard(shot, base, FRAMES, OUT, sfx_times)
sb["project"]["title"] = "cel_rigid"
sb_path = save_json(work / "cel_rigid.storyboard.json", sb)
from video_orchestrator import main as orchestrate  # noqa: E402

OUT.parent.mkdir(parents=True, exist_ok=True)
sys.exit(orchestrate([str(sb_path), "-o", str(OUT)]))
