"""Draw the integration's brand images: a stylized video door station.

Original artwork, not the manufacturer's logo. Usage, with Pillow installed:

    python scripts/make_brand.py custom_components/aiphone_jo/brand
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(sys.argv[1])
SS = 4  # supersampling for smooth edges


def draw(size: int) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 512  # design grid: 512 x 512

    def box(x0, y0, x1, y1):
        return [x0 * u, y0 * u, x1 * u, y1 * u]

    # Body of the door station.
    d.rounded_rectangle(box(126, 24, 386, 488), radius=64 * u, fill=(25, 118, 210, 255))
    # Lighter front panel.
    d.rounded_rectangle(box(150, 48, 362, 464), radius=48 * u, fill=(33, 150, 243, 255))
    # Camera: dark housing, lens, highlight.
    d.ellipse(box(196, 82, 316, 202), fill=(13, 27, 42, 255))
    d.ellipse(box(226, 112, 286, 172), fill=(100, 181, 246, 255))
    d.ellipse(box(242, 126, 262, 146), fill=(227, 242, 253, 255))
    # Speaker grille.
    for y in (236, 268, 300):
        grille = (13, 71, 161, 255)
        d.rounded_rectangle(box(196, y, 316, y + 14), radius=7 * u, fill=grille)
    # Call button: white ring with a bell-like dot.
    d.ellipse(box(196, 344, 316, 464), fill=(255, 255, 255, 255))
    d.ellipse(box(214, 362, 298, 446), fill=(25, 118, 210, 255))
    d.ellipse(box(238, 386, 274, 422), fill=(255, 255, 255, 255))
    img = img.resize((size, size), Image.LANCZOS)
    return img


def trimmed(img: Image.Image) -> Image.Image:
    return img.crop(img.getbbox())


for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
    draw(size).save(OUT / name, optimize=True)
# Logo: same artwork, trimmed to its content, shortest side 128 px (256 at @2x).
for name, width in (("logo.png", 128), ("logo@2x.png", 256)):
    art = trimmed(draw(2048))
    height = round(art.height * width / art.width)
    art.resize((width, height), Image.LANCZOS).save(OUT / name, optimize=True)
