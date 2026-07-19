"""One-off script: repack samsara.ico from the six samsara_{N}.png files.

samsara.ico is a standard multi-resolution ICO container that embeds each
size as a raw PNG blob (verified against the existing file: each directory
entry's byte size and offset line up exactly with the corresponding loose
samsara_{N}.png's file size). This script rebuilds the container the same
way -- concatenating each source PNG's bytes verbatim behind a 6-byte ICO
header and one 16-byte directory entry per size -- rather than routing
through a re-encoder that could recompress (and so silently change) the
untouched larger sizes.

16/32/48 come from the new purpose-drawn samsara_mark.svg (via
_make_small_mark.py). 64/128/256 are the existing painting-derived
layers and must be byte-identical to what shipped in bd93504; this script
does not regenerate them.

Not part of the app; run once after regenerating any of the six PNGs.
"""
import struct
import sys
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent
SIZES = [16, 32, 48, 64, 128, 256]
OUT = ICON_DIR / "samsara.ico"


def build_ico(png_paths):
    count = len(png_paths)
    header = struct.pack("<HHH", 0, 1, count)

    directory = b""
    image_data = b""
    offset = 6 + 16 * count

    for path in png_paths:
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"{path} is not a PNG file")
        # Pillow-derived PNG dimensions aren't re-parsed here; the ICO
        # directory's own w/h/size/offset fields are what Windows and PIL
        # actually use to locate each frame, and the byte-for-byte source
        # size is taken straight from the filename convention (samsara_N.png).
        size = int(path.stem.rsplit("_", 1)[-1])
        w = 0 if size >= 256 else size
        h = 0 if size >= 256 else size
        entry = struct.pack(
            "<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset
        )
        directory += entry
        image_data += data
        offset += len(data)

    return header + directory + image_data


def main():
    png_paths = [ICON_DIR / f"samsara_{s}.png" for s in SIZES]
    for p in png_paths:
        if not p.exists():
            print(f"missing {p}", file=sys.stderr)
            return 1

    ico_bytes = build_ico(png_paths)
    OUT.write_bytes(ico_bytes)
    print(f"saved {OUT} ({len(ico_bytes)} bytes, {len(png_paths)} sizes: {SIZES})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
