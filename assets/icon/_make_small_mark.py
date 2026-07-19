"""One-off script: rasterize samsara_mark.svg (the purpose-drawn small-size
mark -- see that file's header comment for why) to samsara_{16,32,48}.png.

Renders via ImageMagick (system install, not a repo dependency): each size
is rasterized from the vector at a high internal density, then resized down
to the exact target pixel size, so antialiasing is computed from clean high-
resolution vector data rather than from a low-density direct render.

Not part of the app; run once, then assets/icon/_pack_ico.py repacks
samsara.ico from the resulting PNGs.
"""
import subprocess
import sys
from pathlib import Path

MAGICK = r"C:\Program Files\ImageMagick-7.1.2-Q16-HDRI\magick.exe"
ICON_DIR = Path(__file__).resolve().parent
SVG = ICON_DIR / "samsara_mark.svg"
SIZES = [16, 32, 48]


def main():
    if not SVG.exists():
        print(f"missing {SVG}", file=sys.stderr)
        return 1
    for size in SIZES:
        out = ICON_DIR / f"samsara_{size}.png"
        cmd = [
            MAGICK,
            "-background", "none",
            "-density", str(max(384, size * 8)),
            str(SVG),
            "-resize", f"{size}x{size}",
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"FAILED size={size}: {result.stderr}", file=sys.stderr)
            return 1
        print(f"saved {out} ({size}x{size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
