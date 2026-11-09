import numpy as np
from pathlib import Path
from PIL import Image
from rembg import new_session, remove
G = Path(r"C:\Users\adu94\anime-video-pipeline\assets\generated\cel_test")
S = Path(r"C:\Users\adu94\AppData\Local\Temp\claude\3fe6f5f7f2994ca4\bd27a8e9-dfbc-4bf3-b206-effe687ae62f\scratchpad")
im = Image.open(G / "cel_c1_eyes.png").convert("RGB")
a = np.asarray(im, np.float32) / 255
bright = Image.fromarray((np.clip(a ** 0.45 * 1.1, 0, 1) * 255).astype(np.uint8))
outs = []
for model in ("isnet-anime", "isnet-general-use"):
    s = new_session(model)
    for tag, src in (("orig", im), ("bright", bright)):
        m = remove(src, session=s, only_mask=True)
        m.save(S / f"c1_{model}_{tag}.png")
        outs.append(m.resize((480, 274)))
        print(model, tag, np.asarray(m).mean().round(1), flush=True)
sheet = Image.new("L", (960, 548))
for i, o in enumerate(outs):
    sheet.paste(o, ((i % 2) * 480, (i // 2) * 274))
sheet.save(S / "c1_mattes.png")
