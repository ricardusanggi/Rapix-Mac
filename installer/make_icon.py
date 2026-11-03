"""Membuat logo Rapix: ikon aplikasi (icon.png, icon.icns, icon.ico) dan ikon menu bar (menubar.png)
di installer/build/.

Gaya teknis/piksel: tulisan RAPix dari huruf piksel buatan sendiri (5x7), seperti titik-titik LED di layar
digital, di atas latar gelap dengan grid titik halus. Tempat kecil (menu bar, kotak logo panel) memakai
huruf R piksel saja. Huruf yang sama dipakai panel (static/panel.html) lewat pixel_svg().
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).parent / "build"
OUT.mkdir(parents=True, exist_ok=True)

S = 1024
TOP, BOTTOM = (10, 15, 31), (17, 30, 66)   # latar: hampir hitam ke navy
WHITE, CYAN = (255, 255, 255), (34, 211, 238)

# huruf piksel 5x7 (lebar boleh berbeda); "1" = titik menyala
GLYPHS = {
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "i": ["010", "000", "110", "010", "010", "010", "111"],
    "x": ["00000", "00000", "10001", "01010", "00100", "01010", "10001"],
}
WORD = [("R", WHITE), ("A", WHITE), ("P", WHITE), ("i", CYAN), ("x", CYAN)]


def layout(word):
    """Posisi (kolom, baris, warna) setiap titik menyala, dan lebar total dalam kolom (jarak 1 kolom antar huruf)."""
    dots, col = [], 0
    for ch, color in word:
        g = GLYPHS[ch]
        for row, line in enumerate(g):
            dots += [(col + c, row, color) for c, v in enumerate(line) if v == "1"]
        col += len(g[0]) + 1
    return dots, col - 1


def draw_dots(d, dots, ox, oy, cell, fill=None):
    pad = cell * 0.09
    for c, r, color in dots:
        x, y = ox + c * cell, oy + r * cell
        d.rounded_rectangle([x + pad, y + pad, x + cell - pad, y + cell - pad], radius=cell * 0.2, fill=fill or color)


def background():
    grad = Image.new("RGB", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)))
    # grid titik halus: kesan layar piksel
    for y in range(100 + 13, S - 100, 26):
        for x in range(100 + 13, S - 100, 26):
            gd.ellipse([x - 1.6, y - 1.6, x + 1.6, y + 1.6], fill=(40, 58, 104))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([100, 100, S - 100, S - 100], radius=185, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    return img, mask


def icon():
    img, mask = background()
    dots, cols = layout(WORD)
    cell = 26                                   # sama dengan jarak grid, jadi tulisan "duduk" di grid
    ox = round((S - cols * cell) / 2 / 26) * 26 - 13 + 13
    oy = round((S - 7 * cell) / 2 / 26) * 26
    # cahaya lembut di belakang titik cyan, seperti LED yang menyala
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw_dots(ImageDraw.Draw(glow), [p for p in dots if p[2] == CYAN], ox, oy, cell)
    glow = glow.filter(ImageFilter.GaussianBlur(14))
    img.alpha_composite(glow)
    draw_dots(ImageDraw.Draw(img), dots, ox, oy, cell)
    # tiga titik "memuat" di bawah tulisan: piksel yang sedang dikirim ke layar lain
    d = ImageDraw.Draw(img)
    for i, a in enumerate((255, 170, 90)):
        x, y = ox + (cols - 3 + i) * cell, oy + 9 * cell
        d.rounded_rectangle([x + 3, y + 3, x + cell - 3, y + cell - 3], radius=5, fill=CYAN + (a,))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def monogram(size, color=(0, 0, 0, 255)):
    """Huruf R piksel saja, untuk tempat kecil. Hitam di atas transparan = gambar template menu bar macOS."""
    big = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dots, cols = layout([("R", color)])
    cell = S / 7 * 0.98
    draw_dots(ImageDraw.Draw(big), dots, (S - cols * cell) / 2, (S - 7 * cell) / 2, cell)
    return big.resize((size, size), Image.LANCZOS)


def pixel_svg(word=WORD, colors=None):
    """SVG tulisan piksel untuk panel; colors mengganti warna per huruf (mis. 'currentColor')."""
    dots, cols = layout(word)
    rects = "".join(f'<rect x="{c + 0.09:.2f}" y="{r + 0.09:.2f}" width="0.82" height="0.82" rx="0.2" '
                    f'fill="{"#%02x%02x%02x" % color if not colors else colors[color]}"/>' for c, r, color in dots)
    return f'<svg viewBox="0 0 {cols} 7" xmlns="http://www.w3.org/2000/svg">{rects}</svg>'


if __name__ == "__main__":
    img = icon()
    img.save(OUT / "icon.png")
    img.save(OUT / "icon.icns")
    img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    monogram(36).save(OUT / "menubar.png")
    print("Ikon dibuat di", OUT)
