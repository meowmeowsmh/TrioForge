"""Generate TrioForge's PWA / home-screen icons — pure stdlib (no Pillow needed).

Draws a blue→purple gradient tile with three white dots (a "trio") and writes
real PNGs. Run once; the results are committed under static/pwa/.

    python py/make_pwa_icons.py

NOTE: this is the *fallback* generator, used when there is no artwork to derive
icons from (it needs nothing but the standard library). If `logo/` contains the
illustration, `py/make_brand_assets.py` is the one to run: it cuts the wordmark
and the app icons out of that artwork and writes the same static/pwa/*.png files,
so it always wins if both are run.
"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "static", "pwa")

# Brand gradient (matches the app's accent colours)
C_TOP = (88, 166, 255)     # #58a6ff
C_BOT = (163, 113, 247)    # #a371f7


def _png(width, height, rows):
    """Encode RGBA byte rows as a PNG (8-bit, colour type 6)."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _cover(dist, radius, feather=1.0):
    """Smooth 0..1 coverage for a circle edge (analytic anti-aliasing)."""
    if dist <= radius - feather:
        return 1.0
    if dist >= radius + feather:
        return 0.0
    return (radius + feather - dist) / (2.0 * feather)


def make_icon(size, path, pad_ratio=0.0, rounded=True):
    """Render one icon. pad_ratio shrinks the mark for maskable icons."""
    radius_corner = size * 0.22 if rounded else 0.0
    # triad geometry (relative to a padded inner box)
    inner = size * (1.0 - 2 * pad_ratio)
    cx = cy = size / 2.0
    dot_r = inner * 0.115
    ring = inner * 0.205
    dots = [
        (cx, cy - ring * 0.92),               # top
        (cx - ring, cy + ring * 0.62),        # bottom-left
        (cx + ring, cy + ring * 0.62),        # bottom-right
    ]
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            # rounded-rect coverage (only needed near the border)
            a_rect = 1.0
            if radius_corner > 0:
                dx = max(radius_corner - px, px - (size - radius_corner), 0)
                dy = max(radius_corner - py, py - (size - radius_corner), 0)
                if dx or dy:
                    d = (dx * dx + dy * dy) ** 0.5
                    a_rect = _cover(d, radius_corner)
                    if a_rect <= 0:
                        row += bytes((0, 0, 0, 0))
                        continue
            # vertical gradient
            t = py / size
            r = int(C_TOP[0] + (C_BOT[0] - C_TOP[0]) * t)
            g = int(C_TOP[1] + (C_BOT[1] - C_TOP[1]) * t)
            b = int(C_TOP[2] + (C_BOT[2] - C_TOP[2]) * t)
            # white dots on top
            a_dot = 0.0
            for (dx0, dy0) in dots:
                d = ((px - dx0) ** 2 + (py - dy0) ** 2) ** 0.5
                a_dot = max(a_dot, _cover(d, dot_r))
            if a_dot > 0:
                r = int(r + (255 - r) * a_dot)
                g = int(g + (255 - g) * a_dot)
                b = int(b + (255 - b) * a_dot)
            row += bytes((r, g, b, int(255 * a_rect)))
        rows.append(row)
    with open(path, "wb") as f:
        f.write(_png(size, size, rows))
    return path


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    made = [
        make_icon(192, os.path.join(OUT_DIR, "icon-192.png")),
        make_icon(512, os.path.join(OUT_DIR, "icon-512.png")),
        # maskable: extra padding so Android's circle/squircle mask never clips the mark
        make_icon(512, os.path.join(OUT_DIR, "icon-maskable-512.png"), pad_ratio=0.14, rounded=False),
        make_icon(180, os.path.join(OUT_DIR, "apple-touch-icon.png"), rounded=False),
    ]
    for p in made:
        print("wrote %s (%d bytes)" % (p, os.path.getsize(p)))


if __name__ == "__main__":
    main()
