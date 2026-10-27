"""Membuat ikon Rapix (icon.png, icon.icns, icon.ico) di installer/build/."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "build"
OUT.mkdir(parents=True, exist_ok=True)

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# latar: kotak membulat biru
d.rounded_rectangle([64, 64, S - 64, S - 64], radius=200, fill=(47, 111, 222))

# layar laptop (belakang) dan tablet (depan)
d.rounded_rectangle([180, 250, 700, 600], radius=36, fill=(255, 255, 255))
d.rounded_rectangle([210, 280, 670, 570], radius=18, fill=(196, 214, 246))
d.rounded_rectangle([140, 610, 740, 650], radius=20, fill=(255, 255, 255))

d.rounded_rectangle([540, 420, 860, 820], radius=44, fill=(29, 36, 51))
d.rounded_rectangle([566, 446, 834, 794], radius=22, fill=(255, 255, 255))
d.ellipse([686, 800, 714, 814], fill=(255, 255, 255))

# piksel yang berpindah dari layar laptop ke tablet (Rapix = RAP + pixel)
BLUE, WHITE = (47, 111, 222), (255, 255, 255)
for x, y, size, color in [(470, 350, 30, BLUE), (540, 310, 44, BLUE), (610, 370, 36, BLUE),
                          (730, 300, 46, WHITE), (810, 360, 32, WHITE)]:
    d.rounded_rectangle([x, y, x + size, y + size], radius=size // 6, fill=color)

# inisial pembuat di layar tablet
font = None
for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
             "C:/Windows/Fonts/arialbd.ttf"):
    try:
        font = ImageFont.truetype(path, 104)
        break
    except OSError:
        pass
font = font or ImageFont.load_default(size=104)
d.text((700, 620), "RAP", font=font, fill=(47, 111, 222), anchor="mm")

img.save(OUT / "icon.png")
img.save(OUT / "icon.icns")
img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("Ikon dibuat di", OUT)
