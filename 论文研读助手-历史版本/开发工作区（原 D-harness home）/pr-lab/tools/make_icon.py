"""Generate the application icon (multi-size .ico) for the installer and shortcuts.

The mark is the same one the app shows in its sidebar: a rounded dark-green square with 研.
Needs Pillow, so it runs with the *harness* Python, not the app's bundled runtime.

Usage:  <harness python> tools/make_icon.py
"""
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:                                     # pragma: no cover
    sys.exit("Pillow is required for icon generation (run me with the harness Python)")

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "installer" / "icon.ico"
GREEN = (40, 91, 80, 255)
WHITE = (255, 255, 255, 255)
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
)
SIZES = (256, 128, 64, 48, 40, 32, 24, 20, 16)


def glyph(size):
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = max(3, int(size * 0.22))
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=GREEN)
    font = None
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                font = ImageFont.truetype(candidate, int(size * 0.62))
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()
    text = "研"
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((size - (box[2] - box[0])) / 2 - box[0],
               (size - (box[3] - box[1])) / 2 - box[1]), text, font=font, fill=WHITE)
    return image


def main():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    frames = [glyph(size) for size in SIZES]
    frames[0].save(TARGET, format="ICO",
                   sizes=[(size, size) for size in SIZES], append_images=frames[1:])
    print(f"wrote {TARGET} ({TARGET.stat().st_size} bytes, sizes={list(SIZES)})")


if __name__ == "__main__":
    main()
