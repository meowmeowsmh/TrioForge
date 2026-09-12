"""Derive every web-sized brand asset from logo/logo_TrioForge.png.

The artwork is a full 1315x1196 illustration: perfect for the README, unreadable
at the ~26px a top bar can spare. So this script keeps the original untouched and
produces the pieces the UI actually needs:

  static/logo/wordmark.png   the glowing "TrioForge" sign text, background made
                             transparent so it sits on the dark top bar
  static/logo/mark.png       square crop of the character - app/PWA icon
  static/logo/favicon.png    small square for the browser tab
  static/logo/logo-512.png   the whole illustration, for docs/README
  static/pwa/*.png           install icons (192 / 512 / maskable / apple-touch)

Re-run after replacing the source artwork:

    python py/make_brand_assets.py
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "logo" / "logo_TrioForge.png"
LOGO_DIR = ROOT / "static" / "logo"
PWA_DIR = ROOT / "static" / "pwa"

# Where the wordmark and the character sit inside the artwork (fractions).
WORDMARK_BOX = (0.045, 0.715, 0.985, 0.995)
MARK_BOX = (0.295, 0.045, 0.735, 0.485)

WORDMARK_HEIGHT = 132          # 2x a ~66px header logo; CSS scales it down
MARK_SIZE = 512


def build_wordmark(art: Image.Image) -> None:
    w, h = art.size
    box = (int(w * WORDMARK_BOX[0]), int(h * WORDMARK_BOX[1]),
           int(w * WORDMARK_BOX[2]), int(h * WORDMARK_BOX[3]))
    crop = art.crop(box).convert("RGBA")

    # Chroma -> alpha: the glowing letters are strongly orange while the metal
    # sign behind them is neutral grey, so "max(RGB) - min(RGB)" separates them.
    # A brightness threshold does NOT work here - the plaque is the brighter of
    # the two and would come along as a white box on the dark top bar.
    rgb = crop.convert("RGB")
    r, g, b = rgb.split()
    high = ImageChops.lighter(ImageChops.lighter(r, g), b)
    low = ImageChops.darker(ImageChops.darker(r, g), b)
    chroma = ImageChops.subtract(high, low)
    alpha = chroma.point(lambda v: 0 if v < 38 else min(255, (v - 38) * 4))
    crop.putalpha(alpha)
    content = alpha.getbbox()               # trim to what is actually visible
    if content:
        crop = crop.crop(content)

    height = WORDMARK_HEIGHT
    crop = crop.resize((max(1, round(crop.width * height / crop.height)), height), Image.LANCZOS)
    out = LOGO_DIR / "wordmark.png"
    crop.save(out, optimize=True)
    print("wordmark  {}x{}  {:.1f} KB".format(crop.width, crop.height, out.stat().st_size / 1024))


def _save_icon(img: Image.Image, path: Path, colors: int = 256) -> None:
    """Save an icon, palette-quantized.

    The artwork is a detailed illustration, so a truecolor 512px PNG lands around
    600 KB; 256 colours is indistinguishable at icon sizes and roughly 4x smaller.
    """
    img.convert("RGB").quantize(colors=colors, method=Image.MEDIANCUT,
                                dither=Image.FLOYDSTEINBERG).save(path, optimize=True)
    print("{:<22} {}x{}  {:.1f} KB".format(path.name, img.width, img.height,
                                           path.stat().st_size / 1024))


def build_mark(art: Image.Image) -> Image.Image:
    w, h = art.size
    box = (int(w * MARK_BOX[0]), int(h * MARK_BOX[1]),
           int(w * MARK_BOX[2]), int(h * MARK_BOX[3]))
    mark = art.crop(box)
    edge = min(mark.size)
    mark = mark.crop((0, 0, edge, edge)).resize((MARK_SIZE, MARK_SIZE), Image.LANCZOS)
    _save_icon(mark, LOGO_DIR / "mark.png")
    _save_icon(mark.resize((64, 64), Image.LANCZOS), LOGO_DIR / "favicon.png")
    return mark


def build_docs_logo(art: Image.Image) -> None:
    full = art.copy()
    full.thumbnail((512, 512), Image.LANCZOS)
    _save_icon(full, LOGO_DIR / "logo-512.png")


def build_pwa_icons(art: Image.Image, mark: Image.Image) -> None:
    """Install icons from the logo (the same files the manifest already points at)."""
    PWA_DIR.mkdir(parents=True, exist_ok=True)
    bg = art.getpixel((8, 8))[:3]            # the artwork's own dark backdrop

    _save_icon(mark.resize((192, 192), Image.LANCZOS), PWA_DIR / "icon-192.png")
    _save_icon(mark.resize((512, 512), Image.LANCZOS), PWA_DIR / "icon-512.png")

    # Maskable: keep the subject inside the ~80% safe circle.
    canvas = Image.new("RGB", (512, 512), bg)
    inner = mark.convert("RGB").resize((int(512 * 0.72), int(512 * 0.72)), Image.LANCZOS)
    canvas.paste(inner, ((512 - inner.width) // 2, (512 - inner.height) // 2))
    canvas.save(PWA_DIR / "icon-maskable-512.png", optimize=True)
    print("{:<22} 512x512 (72% safe zone)".format("icon-maskable-512.png"))

    _save_icon(mark.resize((180, 180), Image.LANCZOS), PWA_DIR / "apple-touch-icon.png")


def main() -> None:
    if not SRC.is_file():
        raise SystemExit("source artwork not found: {}\n"
                         "(expected the logo at logo/logo_TrioForge.png)".format(SRC))
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    art = Image.open(SRC).convert("RGBA")
    # The illustration is RGB with no alpha; keep it opaque and drop any EXIF.
    art = ImageOps.exif_transpose(art)
    print("source    {}x{}  {:.0f} KB".format(art.width, art.height, SRC.stat().st_size / 1024))

    build_wordmark(art)
    mark = build_mark(art)
    build_docs_logo(art)
    build_pwa_icons(art, mark)
    print("\nDone. Reload the app (no restart needed) to see the new wordmark.")


if __name__ == "__main__":
    main()
