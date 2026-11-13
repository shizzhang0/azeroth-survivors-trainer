"""Draw the trainer's icon and write it for the window and the in-game button.

  uv run --no-project --with pillow python src/make_icon.py

A round medallion in the game's own style (gold rim, deep blue disc) with a gold
lightning bolt: our own drawing, so it sits well next to the game's icon without
being mistaken for it. Writes:
  src/icon.ico                     window / taskbar icon of gui.py (16-256 px)
  addon/AZSRemote/icon.tga         the in-game AZS button (64 x 64, 32-bit, uncompressed)
  addon/AZSRemote/dot_*.tga        its state dot: red / yellow / green (16 x 16)
Pillow is only needed to re-make them, not to run the trainer.
"""
import math
import os
import random
import struct

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ICO = os.path.join(ROOT, "src", "icon.ico")
TGA = os.path.join(ROOT, "addon", "AZSRemote", "icon.tga")
DOTS = {"red": (255, 85, 85), "yellow": (255, 209, 0), "green": (85, 255, 85)}   # the panel's colours

GOLD_HI, GOLD_MID, GOLD_LO = (255, 244, 170), (250, 192, 48), (176, 104, 8)
ART_HI, ART_LO = (255, 236, 120), (220, 122, 10)        # the bolt: yellow down to orange
OUTLINE = (58, 34, 4)
BLUE_C, BLUE_E, BLUE_DARK = (62, 112, 190), (14, 34, 78), (8, 22, 60)


def ramp(S, angle=0.0, curve=None):
    """An L-mode gradient over S x S along `angle` (radians, 0 = top to bottom): 0 at one
    edge, 255 at the other (clamped past them, at the corners of a turned one). curve
    maps 0..1 to 0..255."""
    g = Image.linear_gradient("L").resize((S * 2, S * 2))
    g = g.rotate(math.degrees(angle), resample=Image.BICUBIC)
    g = g.crop((S // 2, S // 2, S // 2 + S, S // 2 + S))
    curve = curve or (lambda t: int(255 * t))
    return g.point(lambda v: curve(min(1.0, max(0.0, (v - 64) / 127.0))))


def circle(S, r0, r1=None):
    """Disc of radius r0 (ring if r1) centred in S x S."""
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    c = S / 2
    d.ellipse([c - r0, c - r0, c + r0, c + r0], fill=255)
    if r1:
        d.ellipse([c - r1, c - r1, c + r1, c + r1], fill=0)
    return m


def metal(S, angle):
    """Polished gold: light and dark bands sweeping across."""
    bands = ramp(S, angle, lambda t: int(127.5 + 127.5 * math.cos(t * math.pi * 2.2)))
    return ImageOps.colorize(bands, GOLD_LO, GOLD_HI).convert("RGBA")


def blue_disc(S):
    """Deep blue, lighter a little above centre, faintly mottled."""
    g = Image.radial_gradient("L").resize((S * 2, S * 2))
    g = g.crop((S // 2, int(S * 0.58), S // 2 + S, int(S * 0.58) + S))      # light spot above centre
    blue = ImageOps.colorize(g.point(lambda v: min(255, int(v * 1.6))), BLUE_C, BLUE_E).convert("RGBA")
    rnd = random.Random(7)
    n = max(4, S // 24)
    tex = Image.new("L", (n, n))
    tex.putdata([rnd.randint(0, 255) for _ in range(n * n)])
    tex = tex.resize((S, S), Image.BICUBIC).filter(ImageFilter.GaussianBlur(S / 60))
    darker = Image.blend(blue, Image.new("RGBA", (S, S), BLUE_DARK + (255,)), 0.45)
    return Image.composite(blue, darker, tex.point(lambda v: 255 if v > 150 else int(v * 1.4)))


def bolt(S, u, cx, cy):
    m = Image.new("L", (S, S), 0)
    pts = [(6, -33), (-19, 5), (-2, 5), (-9, 33), (19, -8), (2, -8), (11, -33)]
    ImageDraw.Draw(m).polygon([(cx + x * u, cy + y * u) for x, y in pts], fill=255)
    return m


def draw(size=512):
    """The icon at size x size, drawn at 3x and scaled down for smooth edges."""
    S = size * 3
    u = S / 100.0                                   # design units: 100 x 100
    c = S / 2
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)

    # rim: outline, gold band, groove, inner gold ring, rivets
    outer, band_in, disc = 49 * u, 43 * u, 41 * u
    out.paste(OUTLINE + (255,), (0, 0), circle(S, outer))
    out.paste(metal(S, -0.6), (0, 0), circle(S, outer - 1.6 * u))
    d.ellipse([c - band_in, c - band_in, c + band_in, c + band_in], fill=OUTLINE + (255,))
    out.paste(metal(S, 2.4), (0, 0), circle(S, band_in - 1.2 * u, disc + 0.8 * u))
    out.paste(OUTLINE + (255,), (0, 0), circle(S, disc + 0.8 * u, disc))
    for k in range(24):
        a = k * math.pi / 12 + math.pi / 24
        rr = (outer + band_in) / 2 - 0.4 * u
        x, y, r = c + rr * math.cos(a), c + rr * math.sin(a), 1.3 * u
        d.ellipse([x - r - 0.6 * u, y - r - 0.6 * u, x + r + 0.6 * u, y + r + 0.6 * u], fill=OUTLINE + (255,))
        d.ellipse([x - r, y - r, x + r, y + r], fill=GOLD_MID + (255,))
        d.ellipse([x - r * 0.45 - 0.5 * u, y - r * 0.45 - 0.5 * u, x - 0.5 * u, y - 0.5 * u],
                  fill=GOLD_HI + (255,))

    out.paste(blue_disc(S), (0, 0), circle(S, disc))

    # the bolt: dark outline, soft shadow on the disc, gold fill, light from the left
    art = bolt(S, u, c, c + 1 * u)
    outline = art.filter(ImageFilter.MaxFilter(int(2.4 * u) | 1))
    shadow = outline.filter(ImageFilter.GaussianBlur(2.2 * u))
    out.paste((0, 0, 20, 150), (int(1.2 * u), int(2 * u)), ImageChops.multiply(shadow, circle(S, disc)))
    out.paste(OUTLINE + (255,), (0, 0), outline)
    def smooth(lo, hi):                              # 0 above lo, 1 below hi, eased
        def f(t):
            t = min(1.0, max(0.0, (t - lo) / (hi - lo)))
            return int(255 * t * t * (3 - 2 * t))
        return f
    top_down = ramp(S, 0.0, smooth(0.15, 0.85))
    out.paste(ImageOps.colorize(top_down, ART_HI, ART_LO).convert("RGBA"), (0, 0), art)
    across = ramp(S, math.pi / 2, smooth(0.25, 0.75))   # 0 on the left .. 255 on the right
    out.paste((255, 250, 220, 255), (0, 0), ImageChops.multiply(art, across.point(lambda v: int((255 - v) * 0.30))))
    out.paste((110, 50, 0, 255), (0, 0), ImageChops.multiply(art, across.point(lambda v: int(v * 0.30))))
    return out.resize((size, size), Image.LANCZOS)


def draw_small(size):
    """The same medallion simplified for 32 px and below: no rivets, texture or shadow
    (they turn to mush that small), a solid gold ring, a bigger bolt, then sharpened."""
    S = size * 8
    u = S / 100.0
    c = S / 2
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(OUTLINE + (255,), (0, 0), circle(S, 49.5 * u))
    out.paste(metal(S, -0.6), (0, 0), circle(S, 45 * u))
    out.paste(OUTLINE + (255,), (0, 0), circle(S, 37 * u))
    g = Image.radial_gradient("L").resize((S * 2, S * 2)).crop((S // 2, int(S * 0.58), S // 2 + S, int(S * 0.58) + S))
    blue = ImageOps.colorize(g.point(lambda v: min(255, int(v * 1.6))), (52, 100, 182), (18, 42, 92)).convert("RGBA")
    out.paste(blue, (0, 0), circle(S, 34.5 * u))
    art = Image.new("L", (S, S), 0)
    pts = [(7, -31), (-20, 6), (-1, 6), (-8, 31), (21, -8), (2, -8), (12, -31)]
    ImageDraw.Draw(art).polygon([(c + x * u, c + y * u) for x, y in pts], fill=255)
    out.paste(OUTLINE + (255,), (0, 0), art.filter(ImageFilter.MaxFilter(int(5 * u) | 1)))
    out.paste((255, 214, 70, 255), (0, 0), art)
    small = out.resize((size, size), Image.LANCZOS)
    return small.filter(ImageFilter.UnsharpMask(radius=1, percent=60, threshold=0))


def dot(rgb, size=16):
    """A glossy round dot with a dark rim, for the button's state."""
    S = size * 8
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([S * 0.06, S * 0.06, S * 0.94, S * 0.94], fill=(20, 14, 6, 255))
    d.ellipse([S * 0.16, S * 0.16, S * 0.84, S * 0.84], fill=rgb + (255,))
    shine = tuple(min(255, int(v + (255 - v) * 0.65)) for v in rgb)
    d.ellipse([S * 0.30, S * 0.24, S * 0.56, S * 0.46], fill=shine + (255,))
    return img.resize((size, size), Image.LANCZOS)


def write_tga(img, path):
    """32-bit uncompressed TGA, rows bottom to top (what the WoW 1.12 client reads)."""
    w, h = img.size
    px = img.convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM).tobytes()
    bgra = bytearray(len(px))
    bgra[0::4], bgra[1::4], bgra[2::4], bgra[3::4] = px[2::4], px[1::4], px[0::4], px[3::4]
    header = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, w, h, 32, 8)
    with open(path, "wb") as f:
        f.write(header + bytes(bgra))


def main():
    # every size Windows asks for at 100-250% scaling, so none has to be stretched;
    # 32 px and below are drawn simplified
    sizes = [16, 20, 24, 30, 32, 40, 48, 64, 96, 128, 256]
    frames = [draw_small(s) if s <= 32 else draw(s) for s in sizes]
    frames[-1].save(ICO, format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[:-1])
    write_tga(draw(64), TGA)
    for name, rgb in DOTS.items():
        path = os.path.join(os.path.dirname(TGA), f"dot_{name}.tga")
        write_tga(dot(rgb), path)
        print(f"wrote {os.path.normpath(path)}")
    print(f"wrote {os.path.normpath(ICO)}\nwrote {os.path.normpath(TGA)}")


if __name__ == "__main__":
    main()
