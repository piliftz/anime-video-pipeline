"""Headless Blender renderer for one VRM character shot. Runs INSIDE Blender's Python.

Launched by character_shot.py, which prepares the job JSON (voice, Rhubarb mouth cues, resolved timings):

    blender --background --python-exit-code 1 --python scripts/blender_vrm_shot.py -- temp/character_shots/<id>/job.json

What it does:
- imports a VRM 1.0 model with the VRM add-on (keeps its MToon cel materials and outline modifiers);
- puts it in front of a background image plane, with a warm key light and a cool rim light;
- keys lip-sync (Rhubarb mouth shapes -> VRM aa/ih/ou/ee/oh), emotion expressions with VRM override rules,
  blinks, breathing/idle motion, gestures and a camera push/punch;
- poses are evaluated only on "drawings" (every `on` frames: twos/threes like limited TV animation), so the
  motion is held like anime and only the drawings are rendered; held frames are hard links to the drawing;
- renders PNG frames with Eevee and writes a per-frame timing log.
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

# Rhubarb mouth shapes -> VRM 1.0 mouth expression weights.
# A = closed (M/B/P), B = slightly open (most consonants), C = open (EH), D = wide (AA), E = rounded (AO/ER),
# F = puckered (UW/OW/W), G = F/V (teeth on lip), H = L (tongue), X = rest.
VISEMES: dict[str, dict[str, float]] = {
    "A": {},
    "B": {"ih": 0.35},
    "C": {"ee": 0.7},
    "D": {"aa": 1.0},
    "E": {"oh": 0.8},
    "F": {"ou": 0.9},
    "G": {"ih": 0.25, "ou": 0.2},
    "H": {"aa": 0.45},
    "X": {},
}
MOUTH = ("aa", "ih", "ou", "ee", "oh")
BLINKS = ("blink", "blinkLeft", "blinkRight")
LOOK = ("lookUp", "lookDown", "lookLeft", "lookRight")
GESTURES = ("fist_to_chest", "nod")
FINGERS = ("index", "middle", "ring", "little")
SEGMENTS = ("proximal", "intermediate", "distal")
UP = Vector((0.0, 0.0, 1.0))


def rot(axis: Vector, degrees: float) -> Quaternion:
    return Quaternion(axis, math.radians(degrees))


def toward(direction: Vector, target: Vector, degrees: float) -> Quaternion:
    """Rotate *direction* toward *target* by *degrees* (armature frame)."""
    axis = direction.cross(target)
    if axis.length < 1e-6:
        return Quaternion()
    return Quaternion(axis.normalized(), math.radians(degrees))


def window(t: float, start: float, rise: float, hold: float, fall: float) -> float:
    """0 -> 1 (ease_out over *rise*) -> hold -> 0 (ease_in_out over *fall*)."""
    if t < start:
        return 0.0
    if t < start + rise:
        return ease("ease_out", (t - start) / rise)
    if t < start + rise + hold:
        return 1.0
    return 1.0 - ease("ease_in_out", (t - start - rise - hold) / fall)


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
    """Humanoid bone access and pose application in armature-rest-frame rotations."""

    def __init__(self, arm) -> None:
        self.arm = arm
        self.ext = arm.data.vrm_addon_extension.vrm1
        hb = self.ext.humanoid.human_bones
        self.bone: dict[str, str] = {}
        for side in ("left", "right"):
            names = [f"{side}_{n}" for n in ("shoulder", "upper_arm", "lower_arm", "hand")]
            names += [f"{side}_{f}_{s}" for f in FINGERS for s in SEGMENTS]
            names += [f"{side}_thumb_{s}" for s in ("metacarpal", "proximal", "distal")]
            for n in names:
                if hasattr(hb, n) and getattr(hb, n).node.bone_name:
                    self.bone[n] = getattr(hb, n).node.bone_name
        for n in ("hips", "spine", "chest", "upper_chest", "neck", "head"):
            if getattr(hb, n).node.bone_name:
                self.bone[n] = getattr(hb, n).node.bone_name
        for need in ("spine", "head", "left_upper_arm", "right_upper_arm", "left_lower_arm", "right_lower_arm"):
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
        for pb in arm.pose.bones:
            pb.rotation_mode = "QUATERNION"

    def head_world(self, name: str) -> Vector:
        return self.arm.matrix_world @ self.arm.data.bones[self.bone[name]].head_local

    def apply(self, pose: dict[str, Quaternion], frame: int) -> None:
        for key, bone_name in self.bone.items():
            pb = self.arm.pose.bones[bone_name]
            R = pose.get(key, Quaternion())
            M = self.rest[key]
            pb.rotation_quaternion = M.inverted() @ R @ M
            pb.keyframe_insert("rotation_quaternion", frame=frame)


# --------------------------------------------------------------------------- #
# Body animation
# --------------------------------------------------------------------------- #
def hand_curl(rig: Rig, side: str, finger_deg: tuple[float, float, float], thumb_deg: float) -> dict:
    pose: dict[str, Quaternion] = {}
    palm = -UP  # VRM T-pose: palms face down
    for f in FINGERS:
        for seg, deg in zip(SEGMENTS, finger_deg):
            key = f"{side}_{f}_{seg}"
            if key in rig.bone:
                pose[key] = toward(rig.dir[key], palm, deg)
    for seg in ("proximal", "distal"):
        key = f"{side}_thumb_{seg}"
        if key in rig.bone:
            pose[key] = toward(rig.dir[key], palm - rig.outward[side] * 0.5, thumb_deg)
    return pose


def idle_pose(rig: Rig, t: float, talk: float) -> dict[str, Quaternion]:
    breath = math.sin(2 * math.pi * t / 3.6)
    sway = math.sin(2 * math.pi * t / 5.3 + 0.7)
    pose: dict[str, Quaternion] = {
        "spine": rot(rig.roll, 0.8 * sway),
        "chest": rot(rig.pitch, -0.7 * breath),
        "upper_chest": rot(rig.pitch, -0.5 * breath),
        "neck": rot(rig.yaw, 1.5 * math.sin(2 * math.pi * t / 7.7)),
        "head": (rot(rig.roll, 1.6 * math.sin(2 * math.pi * t / 6.1 + 1.3))
                 @ rot(rig.pitch, 1.2 * talk * math.sin(2 * math.pi * t / 0.95))),
    }
    for side in ("left", "right"):
        sh = f"{side}_shoulder"
        if sh in rig.bone:
            pose[sh] = toward(rig.dir[sh], UP, 1.0 * (breath + 1.0) * 0.5)
        pose[f"{side}_upper_arm"] = toward(rig.dir[f"{side}_upper_arm"], -UP, 72.0)
        pose[f"{side}_lower_arm"] = toward(rig.dir[f"{side}_lower_arm"], rig.forward, 14.0)
        if f"{side}_hand" in rig.bone:
            pose[f"{side}_hand"] = toward(rig.dir[f"{side}_hand"], -UP, 6.0)
        pose.update(hand_curl(rig, side, (12.0, 18.0, 12.0), 10.0))
    return pose


def gesture_pose(rig: Rig, name: str, side: str) -> tuple[dict[str, Quaternion], dict[str, Quaternion]]:
    """(blended pose, additive pose) at full strength for gesture *name*."""
    if name == "fist_to_chest":
        out = rig.outward[side]
        upper_target = (out * 0.25 + rig.forward * 0.45 - UP * 0.86).normalized()
        R_u = rig.dir[f"{side}_upper_arm"].rotation_difference(upper_target)
        fore_target = (-out * 0.55 + rig.forward * 0.45 + UP * 0.7).normalized()
        R_l = rig.dir[f"{side}_lower_arm"].rotation_difference(R_u.inverted() @ fore_target)
        pose = {f"{side}_upper_arm": R_u, f"{side}_lower_arm": R_l}
        pose.update(hand_curl(rig, side, (80.0, 100.0, 70.0), 40.0))
        sh = f"{side}_shoulder"
        if sh in rig.bone:
            pose[sh] = toward(rig.dir[sh], UP, 5.0)
        additive = {"upper_chest": rot(rig.pitch, 4.0), "head": rot(rig.pitch, 4.0)}
        return pose, additive
    if name == "nod":
        return {}, {"head": rot(rig.pitch, 10.0), "neck": rot(rig.pitch, 3.0)}
    raise PipelineError(f"Unknown gesture '{name}'. Options: {', '.join(GESTURES)}")


def body_pose(rig: Rig, t: float, job: dict, gestures: list) -> dict[str, Quaternion]:
    talk = 1.0 if any(a <= t < b for a, b in job["speech"]) else 0.0
    pose = idle_pose(rig, t, talk)
    for g, (gpose, additive) in gestures:
        rise = 0.12 if g["name"] == "nod" else 0.25
        fall = 0.25 if g["name"] == "nod" else 0.5
        w = window(t, float(g["at"]), rise, float(g.get("hold", 0.0)), fall)
        if w <= 0:
            continue
        for k, R in gpose.items():
            pose[k] = pose.get(k, Quaternion()).slerp(R, w)
        for k, R in additive.items():
            pose[k] = Quaternion().slerp(R, w) @ pose.get(k, Quaternion())
    return pose


# --------------------------------------------------------------------------- #
# Face
# --------------------------------------------------------------------------- #
class Face:
    """VRM 1.0 expressions resolved to shape keys, with the spec's mouth/blink override rules."""

    def __init__(self, rig: Rig) -> None:
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
        self.keys = sorted({(o.name, k) for binds in self.binds.values() for o, k, _ in binds})

    def check(self, name: str) -> None:
        if name not in self.binds:
            raise PipelineError(f"Expression '{name}' is not on this model. Available: {', '.join(sorted(self.binds))}")

    def apply(self, weights: dict[str, float], frame: int) -> None:
        mouth_scale = blink_scale = 1.0
        for name, w in weights.items():
            if w <= 0 or name in MOUTH or name in BLINKS or name in LOOK or name not in self.override:
                continue
            om, ob = self.override[name]
            mouth_scale = 0.0 if om == "block" else mouth_scale * (1 - w) if om == "blend" else mouth_scale
            blink_scale = 0.0 if ob == "block" else blink_scale * (1 - w) if ob == "blend" else blink_scale
        values = {k: 0.0 for k in self.keys}
        for name, w in weights.items():
            if name in MOUTH:
                w *= mouth_scale
            elif name in BLINKS:
                w *= blink_scale
            for obj, key, bw in self.binds.get(name, ()):
                values[(obj.name, key)] += w * bw
        for (obj_name, key), v in values.items():
            kb = bpy.data.objects[obj_name].data.shape_keys.key_blocks[key]
            kb.value = min(1.0, max(0.0, v))
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
    for i, c in enumerate(cues):
        ramp = max(1e-3, float(c.get("ramp", 0.2)))
        u = ease("ease_in_out", (t - float(c["at"])) / ramp) if t >= float(c["at"]) else 0.0
        for nxt in cues[i + 1:]:
            r2 = max(1e-3, float(nxt.get("ramp", 0.2)))
            u *= 1.0 - (ease("ease_in_out", (t - float(nxt["at"])) / r2) if t >= float(nxt["at"]) else 0.0)
        if u > 0:
            w[c["name"]] = min(1.0, w.get(c["name"], 0.0) + float(c.get("weight", 1.0)) * u)
    shape = "X"
    for cue in job["mouth_cues"]:
        if cue["start"] <= t < cue["end"]:
            shape = cue["value"]
            break
    for k, v in VISEMES[shape].items():
        w[k] = v * float(job.get("mouth_gain", 1.0))
    for b in blinks:
        if b <= t < b + 0.25:
            x = t - b
            w["blink"] = ease("ease_out", x / 0.06) if x < 0.06 else 1.0 if x < 0.14 else 1.0 - ease("ease_in", (x - 0.14) / 0.11)
    return w


# --------------------------------------------------------------------------- #
# Camera, lights, background
# --------------------------------------------------------------------------- #
def character_bounds() -> tuple[float, float]:
    zs = [(o.matrix_world @ Vector(c)).z for o in bpy.data.objects if o.type == "MESH" for c in o.bound_box]
    return min(zs), max(zs)


def setup_camera(rig: Rig, job: dict):
    cam_cfg = job["camera"]
    scene = bpy.context.scene
    w, h = job["resolution"]
    bottom, top = character_bounds()
    height = top - bottom
    span = height * {"medium": 0.42, "closeup": 0.2}[cam_cfg.get("framing", "medium")]
    frame_top = top + 0.04 * height
    target = Vector((0.0, 0.0, frame_top - span / 2)) + rig.head_world("head") * Vector((1, 1, 0))
    data = bpy.data.cameras.new("ShotCam")
    data.lens = float(cam_cfg.get("focal_mm", 50.0))
    data.sensor_fit = "HORIZONTAL"
    data.sensor_width = 36.0
    data.shift_x = float(cam_cfg.get("offset_x", 0.0))
    vfov = 2 * math.atan((36.0 * h / w) / 2 / data.lens)
    dist = (span / 2) / math.tan(vfov / 2)
    cam = bpy.data.objects.new("ShotCam", data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    low = math.radians(float(cam_cfg.get("low_angle_deg", 0.0)))
    return cam, target, dist, vfov, low


def camera_at(cam, rig: Rig, target: Vector, dist: float, low: float, t: float, job: dict, frame: int) -> None:
    cfg = job["camera"]
    m = 1.0 - float(cfg.get("push_in", 0.0)) * t / job["duration"]
    for p in cfg.get("punches", []):
        if t >= float(p["at"]):
            m *= 1.0 - float(p["amount"]) * ease("ease_out", (t - float(p["at"])) / float(p.get("duration", 0.12)))
    d = dist * m
    cam.location = target + rig.forward * (d * math.cos(low)) - UP * (d * math.sin(low))
    cam.rotation_mode = "QUATERNION"
    cam.rotation_quaternion = (target - cam.location).to_track_quat("-Z", "Y")
    cam.keyframe_insert("location", frame=frame)
    cam.keyframe_insert("rotation_quaternion", frame=frame)


def setup_background(job: dict, rig: Rig, target: Vector, dist: float, vfov: float, aspect: float) -> None:
    world = bpy.data.worlds.new("ShotWorld")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg_node = world.node_tree.nodes["Background"]
    lighting = job["lighting"]
    bg_node.inputs["Color"].default_value = (*lighting["ambient_color"], 1.0)
    bg_node.inputs["Strength"].default_value = float(lighting["ambient_strength"])
    path = job.get("background")
    if not path:
        return
    img = bpy.data.images.load(str(path))
    img.colorspace_settings.name = "sRGB"
    behind = float(job["camera"].get("background_distance", 6.0))
    total = dist + behind
    need_h = 2 * total * math.tan(vfov / 2) * 1.3
    need_w = need_h * aspect * (1 + 2 * abs(float(job["camera"].get("offset_x", 0.0))))
    img_aspect = img.size[0] / img.size[1]
    plane_h = max(need_h, need_w / img_aspect)
    plane_w = plane_h * img_aspect
    bpy.ops.mesh.primitive_plane_add(size=1.0)
    plane = bpy.context.active_object
    plane.name = "Background"
    plane.scale = (plane_w, plane_h, 1.0)
    plane.location = target - rig.forward * behind
    plane.rotation_mode = "QUATERNION"
    plane.rotation_quaternion = rig.forward.to_track_quat("Z", "Y")
    plane.visible_shadow = False
    mat = bpy.data.materials.new("BackgroundMat")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.extension = "EXTEND"
    emit = nt.nodes.new("ShaderNodeEmission")
    outn = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(tex.outputs["Color"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], outn.inputs["Surface"])
    plane.data.materials.append(mat)


def setup_lights(rig: Rig, job: dict) -> None:
    lighting = job["lighting"]
    right = -rig.outward["left"]  # character's right
    for name, pos, color, strength in (
        ("Key", rig.forward * 1.0 + right * 0.75 + UP * 0.9, lighting["key_color"], lighting["key_strength"]),
        ("Rim", -rig.forward * 1.0 - right * 0.8 + UP * 0.6, lighting["rim_color"], lighting["rim_strength"]),
    ):
        data = bpy.data.lights.new(name, "SUN")
        data.color = color
        data.energy = float(strength)
        data.angle = math.radians(5)
        obj = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(obj)
        obj.rotation_mode = "QUATERNION"
        obj.rotation_quaternion = (-pos).to_track_quat("-Z", "Y")


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
    face = Face(rig)
    for c in job["expressions"]:
        face.check(c["name"])
    gestures = []
    for g in job["gestures"]:
        side = g.get("side", "right")
        if side not in ("left", "right"):
            raise PipelineError(f"gesture side must be left or right, got {side!r}")
        gestures.append((g, gesture_pose(rig, g["name"], side)))
    if job.get("spring_bones", True):
        arm.data.vrm_addon_extension.spring_bone1.enable_animation = True
    setup_render(job)
    w, h = job["resolution"]
    cam, target, dist, vfov, low = setup_camera(rig, job)
    setup_background(job, rig, target, dist, vfov, w / h)
    setup_lights(rig, job)

    scene = bpy.context.scene
    total = int(round(job["duration"] * job["fps"]))
    scene.frame_start, scene.frame_end = 1, total
    on = int(job["on"])
    drawings = [f for f in range(1, total + 1) if (f - 1) % on == 0]
    blinks = blink_times(job)
    prefs = bpy.context.preferences.edit
    old_interp = prefs.keyframe_new_interpolation_type
    prefs.keyframe_new_interpolation_type = "CONSTANT"  # held drawings, like limited animation
    try:
        for f in drawings:
            t = (f - 1) / job["fps"]
            rig.apply(body_pose(rig, t, job, gestures), f)
            face.apply(face_weights(t, job, blinks), f)
            camera_at(cam, rig, target, dist, low, t, job, f)
    finally:
        prefs.keyframe_new_interpolation_type = old_interp
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
        log.info("scene built in %.1fs: %d frames, %d drawings (on %ss)", time.perf_counter() - t0, total,
                 len(drawings), job["on"])
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
