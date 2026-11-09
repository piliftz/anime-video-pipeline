"""Character mattes for the cel test (runs in the rembg env): isnet-anime alpha -> _masks/<name>_alpha.png."""
import sys
from pathlib import Path

from PIL import Image
from rembg import new_session, remove

G = Path(r"C:\Users\adu94\anime-video-pipeline\assets\generated\cel_test")
session = new_session("isnet-anime")
for name in sys.argv[1:]:
    im = Image.open(G / f"{name}.png").convert("RGB")
    a = remove(im, session=session, only_mask=True)
    a.save(G / "_masks" / f"{name}_alpha.png")
    print("matte", name, flush=True)
