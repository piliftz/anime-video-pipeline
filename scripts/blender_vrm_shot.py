"""Headless Blender renderer for one VRM character shot. Runs INSIDE Blender's Python.

Launched by character_shot.py, which prepares the job JSON (voice, Rhubarb mouth cues, resolved timings):

    blender --background --python-exit-code 1 --python scripts/blender_vrm_shot.py -- temp/character_shots/<id>/job.json

What it does:
- imports a VRM 1.0 model with the VRM add-on (keeps its MToon cel materials and outline modifiers) and lights it
  into the scene through MToon itself: lit/shade colours tinted toward the background's mean colour and darkened,
  a coloured fresnel rim (neon), and face materials shifted so the cel shadow edge never crosses the face;
- background image on a plane sized to cover every cut's frustum, blurred by per-cut Eevee depth of field;
- keys lip-sync (Rhubarb shapes -> anime closed/half/open flaps, or plain visemes), VRM expressions (spec
  override rules) and raw shape-key expressions, blinks, idle breathing, gestures built from key poses
  (anticipation -> overshoot strike -> settle -> hold -> release), and a camera that hard-cuts between setups
  (framing, target, low/dutch angle, lens, push-in, punch-ins);
- poses are evaluated only on "drawings" (every `on` frames: twos/threes like limited TV animation); only the
  drawings are rendered, held frames are hard links. Renders PNG frames with Eevee and writes a timing log.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import time
from pathlib import Path

import bpy
from mathutils import Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_utils import PipelineError, ease, get_logger, load_json, save_json, setup_logging  # noqa: E402

log = get_logger("blender_vrm_shot")

# Rhubarb mouth shapes: A = closed (M/B/P), B = slightly open (most consonants), C = open (EH), D = wide (AA),
# E = rounded (AO/ER), F = puckered (UW/OW/W), G = F/V, H = L, X = rest.
VISEMES: dict[str, dict[str, float]] = {     # "visemes": realistic VRM vowel shapes
    "A": {}, "B": {"ih": 0.35}, "C": {"ee": 0.7}, "D": {"aa": 1.0}, "E": {"oh": 0.8},
    "F": {"ou": 0.9}, "G": {"ih": 0.25, "ou": 0.2}, "H": {"aa": 0.45}, "X": {},
}
ANIME_FLAPS: dict[str, dict[str, float]] = {  # "anime": three readable drawings: closed / half / open
    "A": {}, "X": {},
    "B": {"aa": 0.5}, "G": {"aa": 0.4, "ih": 0.3}, "F": {"aa": 0.3, "ou": 0.7},
    "C": {"aa": 0.85, "ee": 0.35}, "D": {"aa": 1.0}, "E": {"aa": 0.75, "oh": 0.6}, "H": {"aa": 0.8},
}
MOUTH_STYLES = {"anime": ANIME_FLAPS, "visemes": VISEMES}
MOUTH = ("aa", "ih", "ou", "ee", "oh")
BLINKS = ("blink", "blinkLeft", "blinkRight")
LOOK = ("lookUp", "lookDown", "lookLeft", "lookRight")
FINGERS = ("index", "middle", "ring", "little")
SEGMENTS = ("proximal", "intermediate", "distal")
SPANS = {"extreme_closeup": 0.06, "closeup": 0.14, "medium_closeup": 0.26, "medium": 0.42}  # x character height
TARGETS = ("eyes", "face", "chest", "right_hand", "left_hand")
UP = Vector((0.0, 0.0, 1.0))


def rot(axis: Vector, degrees: float) -> Quaternion:
    return Quaternion(axis, math.radians(degrees))


def toward(direction: Vector, target: Vector, degrees: float) -> Quaternion:
    """Rotate *direction* toward *target* by *degrees* (armature frame)."""
    axis = direction.cross(target)
    if axis.length < 1e-6:
        return Quaternion()
    return Quaternion(axis.normalized(), math.radians(degrees))


def extrapolate(base: Quaternion, pose: Quaternion, factor: float) -> Quaternion:
    """base -> pose by *factor*; factor > 1 overshoots past *pose* (slerp would clamp)."""
    if factor <= 1.0:
        return base.slerp(pose, factor)
    delta = pose @ base.inverted()
    angle = delta.angle
    if angle < 1e-6:
        return pose
    return Quaternion(delta.axis, angle * factor) @ base


# --------------------------------------------------------------------------- #
# Scene + model
# --------------------------------------------------------------------------- #
def import_vrm(path: Path):
    if "vrm" not in dir(bpy.ops.import_scene):
        import addon_utils

        addon_utils.enable("bl_ext.blender_org.vrm", default_set=True)
    if "vrm" not in dir(bpy.ops.import_scene):
        raise PipelineError("The VRM add-on is not installed in Blender. Install it with: "
                            "blender --command extension install -s --enable vrm")
    if not path.is_file():
        raise PipelineError(f"VRM file not found: {path}")
    bpy.ops.wm.read_homefile(use_empty=True)
    if "FINISHED" not in bpy.ops.import_scene.vrm(filepath=str(path)):
        raise PipelineError(f"Blender's VRM importer failed on {path}")
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    if arm is None:
        raise PipelineError(f"{path} has no armature after import.")
    if arm.data.vrm_addon_extension.spec_version != "1.0":
        raise PipelineError(f"{path.name} is VRM 0.x. Export it again from VRoid Studio as VRM 1.0 "
                            "(File > Export As > VRM, choose VRM 1.0).")
    return arm


class Rig:
    """Humanoid bone access and pose application in armature-rest-frame rotations (basis = M^-1 R M)."""

    def __init__(self, arm) -> None:
        self.arm = arm
        self.ext = arm.data.vrm_addon_extension.vrm1
        hb = self.ext.humanoid.human_bones
        self.bone: dict[str, str] = {}
        names = ["hips", "spine", "chest", "upper_chest", "neck", "head"]
        for side in ("left", "right"):
            names += [f"{side}_{n}" for n in ("shoulder", "upper_arm", "lower_arm", "hand")]
            names += [f"{side}_{f}_{s}" for f in FINGERS for s in SEGMENTS]
            names += [f"{side}_thumb_{s}" for s in ("metacarpal", "proximal", "distal")]
        for n in names:
            if hasattr(hb, n) and getattr(hb, n).node.bone_name:
                self.bone[n] = getattr(hb, n).node.bone_name
        for need in ("spine", "upper_chest", "neck", "head", "left_upper_arm", "right_upper_arm",
                     "left_lower_arm", "right_lower_arm", "left_hand", "right_hand"):
            if need not in self.bone:
                raise PipelineError(f"VRM humanoid bone '{need}' is not mapped in this model.")
        bones = arm.data.bones
        self.rest = {k: bones[b].matrix_local.to_quaternion() for k, b in self.bone.items()}
        self.dir = {k: (bones[b].tail_local - bones[b].head_local).normalized() for k, b in self.bone.items()}
        left = bones[self.bone["left_upper_arm"]].head_local
        right = bones[self.bone["right_upper_arm"]].head_local
        f = (left - right).cross(UP)
        f.z = 0.0
        self.forward = f.normalized()            # direction the character faces
        self.pitch = UP.cross(self.forward)      # +deg: lean / look down
        self.roll = self.forward.copy()          # +deg: tilt toward the character's right
        self.yaw = UP.copy()                     # +deg: turn toward the character's left
        self.outward = {"left": (left - right).normalized(), "right": (right - left).normalized()}
        # lookAt offsetFromHeadBone is stored in glTF axes (y = up); only its height is reliable across
        # exporters, so place the eye point at that height, slightly in front of the head bone.
        eye_h = float(self.ext.look_at.offset_from_head_bone[1])
        self.eye_offset = UP * (eye_h if abs(eye_h) > 1e-4 else 0.06) + self.forward * 0.07
        for pb in arm.pose.bones:
            pb.rotation_mode = "QUATERNION"

    def apply(self, pose: dict[str, Quaternion], frame: int | None) -> None:
        for key, bone_name in self.bone.items():
            pb = self.arm.pose.bones[bone_name]
            M = self.rest[key]
            pb.rotation_quaternion = M.inverted() @ pose.get(key, Quaternion()) @ M
            if frame is not None:
                pb.keyframe_insert("rotation_quaternion", frame=frame)

    def point(self, target: str) -> Vector:
        """World position of a camera target in the CURRENT (evaluated) pose."""
        bpy.context.view_layer.update()
        mw = self.arm.matrix_world

        def bone_head(key: str) -> Vector:
            return mw @ self.arm.pose.bones[self.bone[key]].head

        if target in ("eyes", "face"):
            pb = self.arm.pose.bones[self.bone["head"]]
            rest = self.arm.data.bones[self.bone["head"]].matrix_local.to_3x3()
            eyes = mw @ (pb.head + pb.matrix.to_3x3() @ rest.inverted() @ self.eye_offset)
            return eyes if target == "eyes" else eyes - UP * 0.035
        if target == "chest":
            return bone_head("upper_chest")
        if target in ("right_hand", "left_hand"):
            return bone_head(target)
        raise PipelineError(f"Unknown camera target '{target}'. Options: {', '.join(TARGETS)}")


# --------------------------------------------------------------------------- #
# Body animation: idle + gestures from key poses
# --------------------------------------------------------------------------- #
def hand_curl(rig: Rig, side: str, finger_deg: tuple[float, float, float], thumb_deg: float) -> dict:
    pose: dict[str, Quaternion] = {}
    palm = -UP  # VRM T-pose: palms face down
    for f in FINGERS:
        for seg, deg in zip(SEGMENTS, finger_deg):
            key = f"{side}_{f}_{seg}"
            if key in rig.bone:
                pose[key] = toward(rig.dir[key], palm, deg)
    # Thumb: swing it inward across the curled fingers (toward the palm and the body's midline), by a
    # fraction of the full aim set by thumb_deg (90 = all the way).
    across = (palm * 0.5 - rig.outward[side]).normalized()
    for seg, k in (("metacarpal", 0.35), ("proximal", 0.8), ("distal", 1.0)):
        key = f"{side}_thumb_{seg}"
        if key in rig.bone:
            full = rig.dir[key].rotation_difference(across)
            pose[key] = Quaternion().slerp(full, min(1.0, thumb_deg / 90.0) * k)
    return pose


FIST = (85.0, 100.0, 70.0)
FIST_THUMB = 70.0


def idle_pose(rig: Rig, t: float, talk: float) -> dict[str, Quaternion]:
    breath = math.sin(2 * math.pi * t / 3.6)
    sway = math.sin(2 * math.pi * t / 5.3 + 0.7)
    pose: dict[str, Quaternion] = {
        "spine": rot(rig.roll, 0.4 * sway),
        "chest": rot(rig.pitch, -0.7 * breath),
        "upper_chest": rot(rig.pitch, -0.5 * breath),
        "neck": rot(rig.yaw, 0.8 * math.sin(2 * math.pi * t / 7.7)),
        "head": (rot(rig.roll, 1.0 * math.sin(2 * math.pi * t / 6.1 + 1.3))
                 @ rot(rig.pitch, 1.0 * talk * math.sin(2 * math.pi * t / 0.95))),
    }
    for side in ("left", "right"):
        sh = f"{side}_shoulder"
        if sh in rig.bone:
            pose[sh] = toward(rig.dir[sh], UP, 1.0 * (breath + 1.0) * 0.5)
        pose[f"{side}_upper_arm"] = toward(rig.dir[f"{side}_upper_arm"], -UP, 72.0)
        pose[f"{side}_lower_arm"] = toward(rig.dir[f"{side}_lower_arm"], rig.forward, 14.0)
        pose[f"{side}_hand"] = toward(rig.dir[f"{side}_hand"], -UP, 6.0)
        pose.update(hand_curl(rig, side, (12.0, 18.0, 12.0), 10.0))
    return pose


def arm_to(rig: Rig, side: str, upper: Vector, fore: Vector) -> dict[str, Quaternion]:
    """Upper arm and forearm pointing along world directions *upper* / *fore* (forearm relative to the upper)."""
    R_u = rig.dir[f"{side}_upper_arm"].rotation_difference(upper.normalized())
    R_l = rig.dir[f"{side}_lower_arm"].rotation_difference(R_u.inverted() @ fore.normalized())
    return {f"{side}_upper_arm": R_u, f"{side}_lower_arm": R_l}


# A gesture is key poses (blend: replaces idle rotations, add: applied on top) + phase timings.
def gesture_def(rig: Rig, name: str, side: str) -> dict:
    out = rig.outward[side]
    other = "left" if side == "right" else "right"
    f, up = rig.forward, UP
    turn = 1.0 if side == "right" else -1.0  # +: torso turns toward the gesture arm's opposite side
    if name == "fist_raise":
        antic = arm_to(rig, side, out * 0.25 - f * 0.35 - up * 0.9, f * 0.6 - up * 0.2 + out * 0.1)
        antic.update(hand_curl(rig, side, FIST, FIST_THUMB))
        # held: elbow down at the side, forearm thrust forward, fist at chest height pointing at the camera
        hold = arm_to(rig, side, out * 0.35 + f * 0.55 - up * 0.75, -out * 0.1 + f * 0.85 + up * 0.4)
        hold.update(hand_curl(rig, side, FIST, FIST_THUMB))
        hold.update(hand_curl(rig, other, FIST, FIST_THUMB))
        hold[f"{other}_upper_arm"] = toward(rig.dir[f"{other}_upper_arm"], -up, 64.0)
        hold[f"{other}_lower_arm"] = toward(rig.dir[f"{other}_lower_arm"], f, 30.0)
        return {
            "antic": (antic, {"upper_chest": rot(rig.yaw, -10.0 * turn) @ rot(rig.pitch, -3.0),
                              "head": rot(rig.pitch, 4.0)}),
            "hold": (hold, {"upper_chest": rot(rig.yaw, 12.0 * turn) @ rot(rig.pitch, 9.0),
                            "chest": rot(rig.pitch, 4.0), "neck": rot(rig.pitch, 2.0),
                            "head": rot(rig.pitch, 7.0) @ rot(rig.yaw, -8.0 * turn)}),
            "antic_s": 0.2, "strike_s": 0.1, "settle_s": 0.12, "release_s": 0.45, "overshoot": 1.18,
        }
    if name == "fist_to_chest":
        hold = arm_to(rig, side, out * 0.25 + f * 0.45 - up * 0.86, -out * 0.55 + f * 0.45 + up * 0.7)
        hold.update(hand_curl(rig, side, FIST, FIST_THUMB))
        return {
            "antic": ({}, {"upper_chest": rot(rig.pitch, -2.0)}),
            "hold": (hold, {"upper_chest": rot(rig.pitch, 5.0), "head": rot(rig.pitch, 5.0)}),
            "antic_s": 0.12, "strike_s": 0.12, "settle_s": 0.1, "release_s": 0.5, "overshoot": 1.12,
        }
    if name == "head_down":
        return {
            "antic": ({}, {}),
            "hold": ({}, {"head": rot(rig.pitch, 14.0), "neck": rot(rig.pitch, 6.0),
                          "upper_chest": rot(rig.pitch, 3.0)}),
            "antic_s": 0.0, "strike_s": 0.3, "settle_s": 0.0, "release_s": 0.12, "overshoot": 1.0,
        }
    if name == "nod":
        return {
            "antic": ({}, {"head": rot(rig.pitch, -4.0)}),
            "hold": ({}, {"head": rot(rig.pitch, 12.0), "neck": rot(rig.pitch, 3.0)}),
            "antic_s": 0.08, "strike_s": 0.1, "settle_s": 0.0, "release_s": 0.25, "overshoot": 1.0,
        }
    raise PipelineError(f"Unknown gesture '{name}'. Options: fist_raise, fist_to_chest, head_down, nod")


def gesture_keys(g: dict, d: dict) -> list[tuple[float, str, float, str]]:
    """(time, key pose, factor, easing INTO this key) for one gesture instance."""
    at = float(g["at"])
    t1 = at + d["antic_s"]
    t2 = t1 + d["strike_s"]
    t3 = t2 + d["settle_s"]
    t4 = t3 + float(g.get("hold", 0.0))
    keys = [(at, "idle", 1.0, "linear")]
    if d["antic_s"] > 0:
        keys.append((t1, "antic", 1.0, "ease_in_out"))
    if d["settle_s"] > 0:
        keys += [(t2, "hold", d["overshoot"], "ease_out"), (t3, "hold", 1.0, "ease_in_out")]
    else:
        keys.append((t2, "hold", 1.0, "ease_out"))
    release = float(g.get("release", d["release_s"]))  # 0 = drop to idle instantly (hide it behind a cut)
    keys += [(t4, "hold", 1.0, "linear"), (t4 + release, "idle", 1.0, "ease_in_out")]
    return keys


def key_rot(d: dict, key: str, factor: float, part: int, bone: str, base: Quaternion) -> Quaternion:
    if key == "idle":
        return base
    pose = d[key][part]
    if bone not in pose:
        return base
    return extrapolate(base, pose[bone] if part == 0 else pose[bone] @ base, factor)


def body_pose(rig: Rig, t: float, job: dict, gestures: list) -> dict[str, Quaternion]:
    talk = 1.0 if any(a <= t < b for a, b in job["speech"]) else 0.0
    pose = idle_pose(rig, t, talk)
    for g, d, keys in gestures:
        if t < keys[0][0] or t >= keys[-1][0]:
            continue
        i = max(j for j, k in enumerate(keys) if k[0] <= t)
        (ta, ka, fa, _), (tb, kb, fb, easing) = keys[i], keys[i + 1]
        u = ease(easing, (t - ta) / max(1e-6, tb - ta))
        bones = set(d["antic"][0]) | set(d["hold"][0]) | set(d["antic"][1]) | set(d["hold"][1])
        for bone in bones:
            base = pose.get(bone, Quaternion())
            pose[bone] = key_rot(d, ka, fa, 0, bone, base).slerp(key_rot(d, kb, fb, 0, bone, base), u)
            base = pose[bone]
            pose[bone] = key_rot(d, ka, fa, 1, bone, base).slerp(key_rot(d, kb, fb, 1, bone, base), u)
    return pose


# --------------------------------------------------------------------------- #
# Face
# --------------------------------------------------------------------------- #
class Face:
    """VRM 1.0 expressions resolved to shape keys (spec mouth/blink override rules) + raw shape-key cues."""

    def __init__(self, rig: Rig, mouth_gain: float) -> None:
        self.binds: dict[str, list[tuple[object, str, float]]] = {}
        self.override: dict[str, tuple[str, str]] = {}
        for name, e in rig.ext.expressions.all_name_to_expression_dict().items():
            binds = []
            for b in e.morph_target_binds:
                obj = bpy.data.objects.get(b.node.mesh_object_name)
                if obj is None or obj.data.shape_keys is None or b.index not in obj.data.shape_keys.key_blocks:
                    continue
                binds.append((obj, b.index, float(b.weight)))
            if binds:
                self.binds[name] = binds
                self.override[name] = (e.override_mouth, e.override_blink)
        missing = [m for m in MOUTH if m not in self.binds]
        if missing:
            raise PipelineError(f"Model has no shape keys for mouth expressions {missing}; lip-sync needs aa/ih/ou/ee/oh.")
        self.raw: dict[str, list[tuple[object, str]]] = {}  # short shape-key name -> (object, full key name)
        for o in bpy.data.objects:
            if o.type == "MESH" and o.data.shape_keys:
                for kb in o.data.shape_keys.key_blocks[1:]:
                    self.raw.setdefault(kb.name.rsplit(".", 1)[-1], []).append((o, kb.name))
                    self.raw.setdefault(kb.name, []).append((o, kb.name))
        self.keys = sorted({(o.name, k) for binds in self.binds.values() for o, k, _ in binds})
        self.keys += sorted({(o.name, k) for lst in self.raw.values() for o, k in lst} - set(self.keys))
        # Bigger-than-modelled mouth shapes: let mouth keys go past 1.0 when mouth_gain asks for it.
        self.limit = {k: 1.0 for k in self.keys}
        for m in MOUTH:
            for obj, key, _ in self.binds[m]:
                obj.data.shape_keys.key_blocks[key].slider_max = max(1.0, mouth_gain)
                self.limit[(obj.name, key)] = max(1.0, mouth_gain)

    @property
    def face_objects(self) -> set:
        return {obj for m in MOUTH for obj, _, _ in self.binds[m]}

    def check(self, cue: dict) -> None:
        if "name" in cue and cue["name"] not in self.binds:
            raise PipelineError(f"Expression '{cue['name']}' is not on this model. "
                                f"Available: {', '.join(sorted(self.binds))}")
        for key in cue.get("shape_keys", {}):
            if key not in self.raw:
                raise PipelineError(f"Shape key '{key}' is not on this model.")

    def apply(self, weights: dict[str, float], frame: int) -> None:
        mouth_scale = blink_scale = 1.0
        for name, w in weights.items():
            if w <= 0 or name not in self.override or name in MOUTH or name in BLINKS or name in LOOK:
                continue
            om, ob = self.override[name]
            mouth_scale = 0.0 if om == "block" else mouth_scale * (1 - w) if om == "blend" else mouth_scale
            blink_scale = 0.0 if ob == "block" else blink_scale * (1 - w) if ob == "blend" else blink_scale
        values = {k: 0.0 for k in self.keys}
        for name, w in weights.items():
            if name.startswith("shape:"):
                for obj, key in self.raw[name[6:]]:
                    values[(obj.name, key)] += w
                continue
            if name in MOUTH:
                w *= mouth_scale
            elif name in BLINKS:
                w *= blink_scale
            for obj, key, bw in self.binds.get(name, ()):
                values[(obj.name, key)] += w * bw
        for (obj_name, key), v in values.items():
            kb = bpy.data.objects[obj_name].data.shape_keys.key_blocks[key]
            kb.value = min(self.limit[(obj_name, key)], max(0.0, v))
            kb.keyframe_insert("value", frame=frame)


def blink_times(job: dict) -> list[float]:
    cfg = job["blink"]
    if not cfg.get("enabled", True):
        return []
    out, t, k = [], float(cfg.get("first", 1.2)), 1
    while t < job["duration"] - 0.3:
        out.append(t)
        jitter = (math.sin(k * 12.9898) * 43758.5453) % 1.0  # deterministic, so reruns match
        t += float(cfg.get("interval", 3.4)) * (0.7 + 0.6 * jitter)
        k += 1
    return out


def face_weights(t: float, job: dict, blinks: list[float]) -> dict[str, float]:
    w: dict[str, float] = {}
    cues = job["expressions"]

    def u_at(c: dict) -> float:
        ramp = max(1e-3, float(c.get("ramp", 0.2)))
        return ease("ease_in_out", (t - float(c["at"])) / ramp) if t >= float(c["at"]) else 0.0

    for i, c in enumerate(cues):
        u = u_at(c)
        for nxt in cues[i + 1:]:
            u *= 1.0 - u_at(nxt)
        if u <= 0:
            continue
        parts = {c["name"]: 1.0} if "name" in c else {f"shape:{k}": float(v) for k, v in c["shape_keys"].items()}
        for k, v in parts.items():
            w[k] = min(1.0, w.get(k, 0.0) + float(c.get("weight", 1.0)) * v * u)
    shape = "X"
    for cue in job["mouth_cues"]:
        if cue["start"] <= t < cue["end"]:
            shape = cue["value"]
            break
    for k, v in MOUTH_STYLES[job["mouth_style"]][shape].items():
        w[k] = v * float(job.get("mouth_gain", 1.0))
    for b in blinks:
        if b <= t < b + 0.25:
            x = t - b
            w["blink"] = ease("ease_out", x / 0.06) if x < 0.06 else 1.0 if x < 0.14 else 1.0 - ease("ease_in", (x - 0.14) / 0.11)
    return w


# --------------------------------------------------------------------------- #
# Look: MToon tint/rim/face shading, lights, background
# --------------------------------------------------------------------------- #
def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def style_materials(job: dict, face_objects: set) -> None:
    """Light the character into the scene through MToon parameters (MToon caps light intensity)."""
    look = job["lighting"]
    mean = [srgb_to_linear(c) for c in (job.get("background_mean") or [1.0, 1.0, 1.0])]
    peak = max(mean) or 1.0
    match = float(look["match_background"])
    tint = [float(look["exposure"]) * ((1 - match) + match * c / peak) for c in mean]
    shade_mul = [c * float(look["shade_darken"]) for c in tint]
    rim = [c * float(look["rim_strength"]) for c in look["rim_color"]]
    face_mats = {m.name for o in face_objects for m in o.data.materials if m}
    for mat in bpy.data.materials:
        ext = getattr(mat, "vrm_addon_extension", None)
        if ext is None or not ext.mtoon1.enabled:
            continue
        pbr = ext.mtoon1.pbr_metallic_roughness
        mt = ext.mtoon1.extensions.vrmc_materials_mtoon
        base = list(pbr.base_color_factor)
        pbr.base_color_factor = (*[b * k for b, k in zip(base[:3], tint)], base[3])
        mt.shade_color_factor = tuple(s * k for s, k in zip(mt.shade_color_factor, shade_mul))
        mt.parametric_rim_color_factor = rim
        mt.parametric_rim_fresnel_power_factor = float(look["rim_power"])
        mt.parametric_rim_lift_factor = float(look["rim_lift"])
        mt.rim_lighting_mix_factor = 0.0
        if mat.name in face_mats:
            mt.shading_shift_factor = float(look["face_shading_shift"])


def setup_lights(rig: Rig, job: dict) -> None:
    lighting = job["lighting"]
    right = -rig.outward["left"]  # character's right
    for name, pos, color, strength in (
        ("Key", rig.forward * 1.0 + right * 0.75 + UP * 0.9, lighting["key_color"], lighting["key_strength"]),
        ("Rim", -rig.forward * 1.0 - right * 0.8 + UP * 0.6, lighting["rim_color"], lighting["back_strength"]),
    ):
        data = bpy.data.lights.new(name, "SUN")
        data.color = color
        data.energy = float(strength)
        data.angle = math.radians(5)
        obj = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(obj)
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = (-pos).to_track_quat("-Z", "Y")
    world = bpy.data.worlds.new("ShotWorld")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs["Color"].default_value = (*lighting["ambient_color"], 1.0)
    bg.inputs["Strength"].default_value = float(lighting["ambient_strength"])


def setup_background(job: dict, rig: Rig, corners: list[Vector], plane_point: Vector) -> None:
    path = job.get("background")
    if not path:
        return
    img = bpy.data.images.load(str(path))
    img.colorspace_settings.name = "sRGB"
    rq = rig.forward.to_track_quat("Z", "Y")
    ax, ay = rq @ Vector((1, 0, 0)), rq @ Vector((0, 1, 0))
    xs = [(p - plane_point).dot(ax) for p in corners]
    ys = [(p - plane_point).dot(ay) for p in corners]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    need_w, need_h = (max(xs) - min(xs)) * 1.08, (max(ys) - min(ys)) * 1.08
    img_aspect = img.size[0] / img.size[1]
    plane_h = max(need_h, need_w / img_aspect)
    bpy.ops.mesh.primitive_plane_add(size=1.0)
    plane = bpy.context.active_object
    plane.name = "Background"
    plane.scale = (plane_h * img_aspect, plane_h, 1.0)
    plane.location = plane_point + ax * cx + ay * cy
    plane.rotation_mode = "QUATERNION"
    plane.rotation_quaternion = rq
    plane.visible_shadow = False
    mat = bpy.data.materials.new("BackgroundMat")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.extension = "EXTEND"
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.inputs["Strength"].default_value = float(job["lighting"]["background_brightness"])
    outn = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tex.outputs["Color"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], outn.inputs["Surface"])
    plane.data.materials.append(mat)


# --------------------------------------------------------------------------- #
# Camera: hard cuts between setups
# --------------------------------------------------------------------------- #
class Cut:
    def __init__(self, cfg: dict, end: float, rig: Rig, height: float, aspect: tuple[int, int]) -> None:
        self.cfg, self.at, self.end = cfg, float(cfg["at"]), end
        if cfg["framing"] not in SPANS:
            raise PipelineError(f"cut framing '{cfg['framing']}' unknown. Options: {', '.join(SPANS)}")
        self.span = height * SPANS[cfg["framing"]]
        self.lens = float(cfg["focal_mm"])
        w, h = aspect
        self.sensor_h = 36.0 * h / w
        vfov = 2 * math.atan(self.sensor_h / 2 / self.lens)
        self.dist = (self.span / 2) / math.tan(vfov / 2)
        yaw = rot(UP, float(cfg["yaw_deg"]))
        self.back = yaw @ rig.forward           # from target toward the camera (horizontal)
        self.low = math.radians(float(cfg["low_angle_deg"]))
        self.target = Vector()                  # filled by lock_targets()

    def placement(self, t: float, punches: list[dict]) -> tuple[Vector, Quaternion, float]:
        cfg = self.cfg
        u = (t - self.at) / max(1e-6, self.end - self.at)
        m = 1.0 - float(cfg["push_in"]) * min(1.0, max(0.0, u))
        for p in punches:
            if self.at <= float(p["at"]) <= t:
                m *= 1.0 - float(p["amount"]) * ease("ease_out", (t - float(p["at"])) / float(p.get("duration", 0.12)))
        d = self.dist * m
        target = self.target + UP * float(cfg["offset_y"]) * self.span
        pos = target + self.back * (d * math.cos(self.low)) - UP * (d * math.sin(self.low))
        q = (target - pos).to_track_quat("-Z", "Y") @ Quaternion((0, 0, 1), math.radians(float(cfg["dutch_deg"])))
        return pos, q, d

    def corners(self, pos: Vector, q: Quaternion, plane_point: Vector, normal: Vector) -> list[Vector]:
        half_w, half_h = 18.0 / self.lens, self.sensor_h / 2 / self.lens
        sx = float(self.cfg["offset_x"]) * 36.0 / self.lens
        out = []
        for x in (-half_w, half_w):
            for y in (-half_h, half_h):
                ray = q @ Vector((x + sx, y, -1.0))
                denom = ray.dot(normal)
                if abs(denom) < 1e-6:
                    raise PipelineError("A cut looks parallel to the background plane; lower its yaw/low angle.")
                s = (plane_point - pos).dot(normal) / denom
                if s <= 0:
                    raise PipelineError("A cut looks away from the background; check yaw_deg / low_angle_deg.")
                out.append(pos + ray * s)
        return out


def build_cuts(job: dict, rig: Rig, gestures: list) -> list[Cut]:
    zs = [(o.matrix_world @ Vector(c)).z for o in bpy.data.objects if o.type == "MESH" for c in o.bound_box]
    height = max(zs) - min(zs)
    cfgs = job["cuts"]
    cuts = [Cut(c, cfgs[i + 1]["at"] if i + 1 < len(cfgs) else job["duration"], rig, height, job["resolution"])
            for i, c in enumerate(cfgs)]
    for cut in cuts:  # aim at the target where it is when the cut's action has settled
        t_lock = min(cut.end - 1e-3, cut.at + float(cut.cfg["lock"]))
        rig.apply(body_pose(rig, t_lock, job, gestures), None)
        cut.target = rig.point(cut.cfg["target"])
    return cuts


def setup_camera(job: dict):
    scene = bpy.context.scene
    data = bpy.data.cameras.new("ShotCam")
    data.sensor_fit = "HORIZONTAL"
    data.sensor_width = 36.0
    data.clip_start = 0.02
    data.dof.use_dof = True
    cam = bpy.data.objects.new("ShotCam", data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.rotation_mode = "QUATERNION"
    return cam


def key_camera(cam, cut: Cut, t: float, job: dict, frame: int) -> None:
    pos, q, d = cut.placement(t, job["camera"]["punches"])
    cam.location, cam.rotation_quaternion = pos, q
    data = cam.data
    data.lens = cut.lens
    data.shift_x = float(cut.cfg["offset_x"])
    data.dof.focus_distance = d
    data.dof.aperture_fstop = float(cut.cfg["fstop"])
    for obj, path in ((cam, "location"), (cam, "rotation_quaternion"), (data, "lens"), (data, "shift_x"),
                      (data.dof, "focus_distance"), (data.dof, "aperture_fstop")):
        obj.keyframe_insert(path, frame=frame)


def setup_render(job: dict) -> None:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = job["resolution"]
    scene.render.resolution_percentage = 100
    scene.render.fps = int(job["fps"])
    scene.render.use_motion_blur = False
    scene.eevee.taa_render_samples = int(job["samples"])
    scene.view_settings.view_transform = "Standard"  # MToon colors as authored; AgX would wash them out
    scene.view_settings.look = "None"
    scene.render.film_transparent = False
    img = scene.render.image_settings
    img.file_format = "PNG"
    img.color_mode = "RGB"
    img.color_depth = "8"
    img.compression = 15


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build(job: dict):
    arm = import_vrm(Path(job["vrm"]))
    rig = Rig(arm)
    face = Face(rig, float(job.get("mouth_gain", 1.0)))
    if job["mouth_style"] not in MOUTH_STYLES:
        raise PipelineError(f"mouth_style must be one of {', '.join(MOUTH_STYLES)}")
    for c in job["expressions"]:
        face.check(c)
    gestures = []
    for g in job["gestures"]:
        side = g.get("side", "right")
        if side not in ("left", "right"):
            raise PipelineError(f"gesture side must be left or right, got {side!r}")
        d = gesture_def(rig, g["name"], side)
        gestures.append((g, d, gesture_keys(g, d)))
    if job.get("spring_bones", True):
        arm.data.vrm_addon_extension.spring_bone1.enable_animation = True
    setup_render(job)
    style_materials(job, face.face_objects)
    setup_lights(rig, job)
    cuts = build_cuts(job, rig, gestures)
    cam = setup_camera(job)

    scene = bpy.context.scene
    total = int(round(job["duration"] * job["fps"]))
    scene.frame_start, scene.frame_end = 1, total
    on = int(job["on"])
    drawings = [f for f in range(1, total + 1) if (f - 1) % on == 0]
    blinks = blink_times(job)
    plane_point = cuts[0].target * Vector((1, 1, 0)) - rig.forward * float(job["camera"]["background_distance"])
    corners: list[Vector] = []
    prefs = bpy.context.preferences.edit
    old_interp = prefs.keyframe_new_interpolation_type
    prefs.keyframe_new_interpolation_type = "CONSTANT"  # held drawings and hard cuts, like limited animation
    try:
        for f in drawings:
            t = (f - 1) / job["fps"]
            cut = next(c for c in reversed(cuts) if c.at <= t + 1e-6)
            rig.apply(body_pose(rig, t, job, gestures), f)
            face.apply(face_weights(t, job, blinks), f)
            key_camera(cam, cut, t, job, f)
            corners += cut.corners(cam.location.copy(), cam.rotation_quaternion.copy(), plane_point, rig.forward)
    finally:
        prefs.keyframe_new_interpolation_type = old_interp
    setup_background(job, rig, corners, plane_point)
    return drawings, total


def render(job: dict, drawings: list[int], total: int) -> dict:
    scene = bpy.context.scene
    frames_dir = Path(job["frames_dir"])
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("frame_*.png"):
        old.unlink()
    limit = job.get("frame_limit")
    if limit:
        total = min(total, int(limit))
        drawings = [f for f in drawings if f <= total]
    on = int(job["on"])
    if job.get("spring_bones", True):
        # Spring bones start from the T-pose; step the simulation through a pre-roll (held at the frame-1
        # pose by constant extrapolation) so hair and sleeves have settled when frame 1 renders.
        for f in range(1 - int(job["fps"]) * 2, 1):
            scene.frame_set(f)
    times: list[float] = []
    t_all = time.perf_counter()
    for i, f in enumerate(drawings):
        scene.frame_set(f)
        out = frames_dir / f"frame_{f:05d}.png"
        scene.render.filepath = str(out)
        t0 = time.perf_counter()
        bpy.ops.render.render(write_still=True)
        times.append(time.perf_counter() - t0)
        if not out.is_file():
            raise PipelineError(f"Eevee did not write {out}")
        for hold in range(f + 1, min(f + on, total + 1)):
            dst = frames_dir / f"frame_{hold:05d}.png"
            try:
                os.link(out, dst)
            except OSError:
                shutil.copyfile(out, dst)
        if i == 0 or (i + 1) % 10 == 0 or i == len(drawings) - 1:
            avg = sum(times[1:]) / max(1, len(times) - 1) if len(times) > 1 else times[0]
            log.info("drawing %d/%d (frame %d) %.2fs, ETA %.0fs", i + 1, len(drawings), f, times[-1],
                     avg * (len(drawings) - i - 1))
    steady = times[1:]
    return {
        "frames": total, "drawings": len(drawings), "on": on, "resolution": job["resolution"],
        "samples": job["samples"], "first_frame_s": round(times[0], 3),
        "avg_s_per_drawing_after_first": round(sum(steady) / len(steady), 3) if steady else None,
        "min_s": round(min(steady), 3) if steady else None, "max_s": round(max(steady), 3) if steady else None,
        "wall_s": round(time.perf_counter() - t_all, 2), "per_drawing_s": [round(x, 3) for x in times],
    }


def main() -> int:
    setup_logging()
    if "--" not in sys.argv or len(sys.argv) <= sys.argv.index("--") + 1:
        log.error("usage: blender --background --python blender_vrm_shot.py -- <job.json>")
        return 1
    try:
        job = load_json(Path(sys.argv[sys.argv.index("--") + 1]))
        t0 = time.perf_counter()
        drawings, total = build(job)
        log.info("scene built in %.1fs: %d frames, %d drawings (on %ss), %d cuts", time.perf_counter() - t0,
                 total, len(drawings), job["on"], len(job["cuts"]))
        if job.get("save_blend"):
            bpy.ops.wm.save_as_mainfile(filepath=str(job["save_blend"]), copy=True)
        stats = render(job, drawings, total)
        save_json(Path(job["log"]), stats)
        log.info("rendered %d drawings, avg %.2fs/drawing after the first (first %.2fs)", stats["drawings"],
                 stats["avg_s_per_drawing_after_first"] or 0.0, stats["first_frame_s"])
        return 0
    except PipelineError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
