"""Runs INSIDE the rembg env ($REMBG_PYTHON): character alpha mattes for cel_shot.py.

Job JSON: {"model": "isnet-anime", "items": [{"image": path, "out": path, "gamma": null | 0.45}]}.
'gamma' brightens the image first (dark close-ups: the matte model barely sees a character in low light).
Imports only stdlib + rembg + its deps (PIL, numpy), so the env needs nothing from this repo.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(job_path: str) -> int:
    import numpy as np
    from PIL import Image
    from rembg import new_session, remove

    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    session = new_session(job.get("model") or "isnet-anime")
    for item in job["items"]:
        im = Image.open(item["image"]).convert("RGB")
        if item.get("gamma"):
            a = np.asarray(im, np.float32) / 255
            im = Image.fromarray((np.clip(a ** float(item["gamma"]) * 1.1, 0, 1) * 255).astype(np.uint8))
        mask = remove(im, session=session, only_mask=True)
        Path(item["out"]).parent.mkdir(parents=True, exist_ok=True)
        mask.save(item["out"])
        print("matte", item["out"], flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: cel_matte_worker.py job.json", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
