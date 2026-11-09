"""Prototype: redraw a masked region of a keyframe N ways (mouth flaps, effect boil) with ComfyUI core nodes only.
The mask is feathered; ImageCompositeMasked pastes the redraw back so every pixel outside the mask is untouched."""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, r"C:\Users\adu94\anime-video-pipeline\scripts")
from comfy_client import ComfyServer  # noqa: E402

GEN = Path(r"C:\Users\adu94\anime-video-pipeline\assets\generated\cel_test")
WORK = GEN / "_masks"
WORK.mkdir(exist_ok=True)
CHAR = "1girl, solo, long brown hair, sidelocks, brown eyes, white shirt, short sleeves"
Q = "masterpiece, high score, great score, absurdres"
NEG = ("lowres, bad anatomy, text, error, worst quality, low quality, low score, bad score, signature, watermark, "
       "blurry, deformed mouth, extra teeth")


def mask_png(name, shapes, size, feather):
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    W, H = size
    for kind, (cx, cy, rx, ry) in shapes:
        box = [(cx - rx) * W, (cy - ry) * H, (cx + rx) * W, (cy + ry) * H]
        (d.ellipse if kind == "ellipse" else d.rectangle)(box, fill=255)
    m = m.filter(ImageFilter.GaussianBlur(feather))
    p = WORK / f"{name}.png"
    Image.merge("RGB", (m, m, m)).save(p)
    return p


def graph(ckpt, image, mask, prompt, seed, denoise, fresh=False):
    g = {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["ckpt", 1], "text": prompt}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["ckpt", 1], "text": NEG}},
        "img": {"class_type": "LoadImage", "inputs": {"image": image}},
        "msk": {"class_type": "LoadImageMask", "inputs": {"image": mask, "channel": "red"}},
        "enc": {"class_type": "VAEEncode", "inputs": {"pixels": ["img", 0], "vae": ["ckpt", 2]}},
        "noise": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["enc", 0], "mask": ["msk", 0]}},
        "ks": {"class_type": "KSampler", "inputs": {"model": ["ckpt", 0], "positive": ["pos", 0],
                                                     "negative": ["neg", 0], "latent_image": ["noise", 0],
                                                     "seed": seed, "steps": 28, "cfg": 5.0,
                                                     "sampler_name": "euler_ancestral", "scheduler": "normal",
                                                     "denoise": denoise}},
        "dec": {"class_type": "VAEDecode", "inputs": {"samples": ["ks", 0], "vae": ["ckpt", 2]}},
        "comp": {"class_type": "ImageCompositeMasked", "inputs": {"destination": ["img", 0], "source": ["dec", 0],
                                                                  "x": 0, "y": 0, "resize_source": False,
                                                                  "mask": ["msk", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["comp", 0], "filename_prefix": "celvar"}},
    }
    if fresh:  # true inpainting: the masked area is blanked, so the model draws a new shape (open -> closed mouth)
        g["enc"] = {"class_type": "VAEEncodeForInpaint", "inputs": {"pixels": ["img", 0], "vae": ["ckpt", 2],
                                                                     "mask": ["msk", 0], "grow_mask_by": 8}}
        del g["noise"]
        g["ks"]["inputs"]["latent_image"] = ["enc", 0]
    return g


JOBS = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
with ComfyServer() as c:
    ckpt = next(x for x in c.checkpoints() if "animagine" in x.lower())
    for job in JOBS:
        base = GEN / f"{job['base']}.png"
        size = Image.open(base).size
        mask = mask_png(job["out"], job["shapes"], size, job.get("feather", 18))
        img_ref, mask_ref = c.upload_image(base, f"{job['base']}.png"), c.upload_image(mask, f"{job['out']}_mask.png")
        prompt = ", ".join([Q, CHAR, job["prompt"]])
        outs = c.wait(c.submit(graph(ckpt, img_ref, mask_ref, prompt, job["seed"], job["denoise"], job.get("fresh", False))))
        ref = next(iter(outs.values()))["images"][0]
        (GEN / f"{job['out']}.png").write_bytes(c.download(ref))
        print("wrote", job["out"], flush=True)
