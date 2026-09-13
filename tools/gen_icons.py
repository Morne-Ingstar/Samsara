"""Render every Samsara icon size and state from assets/icon/samsara.svg.

The SVG is the single source (see its header comment for the structure);
colours come from samsara/ui/theme.py tokens, so there is no palette in this
file. Replaces the old one-off assets/icon/_make_*.py / _pack_ico.py scripts
and keeps their output filenames.

Outputs (all under assets/icon/):
  samsara_{16,24,32,48,64,128,256}.png   app icon (APP_STATE), one per size
  samsara.ico                            multi-size container of those PNGs
  states/samsara_{state}_{size}.png      every tray state at TRAY_SIZES

16 px uses the simplified drawing (#small); 24 px and up use #regular.

Usage:
  F:\\envs\\sami\\python.exe tools\\gen_icons.py              regenerate assets
  F:\\envs\\sami\\python.exe tools\\gen_icons.py --check      exit 1 if assets are stale
  F:\\envs\\sami\\python.exe tools\\gen_icons.py --montage PATH
      16 px state sheet (1x next to 4x, dark and light taskbar) for sign-off
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform ships no fonts; point it at the system fonts so the
# montage labels render (icons themselves use no text).
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

from samsara.ui import theme  # noqa: E402

ICON_DIR = REPO / "assets" / "icon"
SVG_PATH = ICON_DIR / "samsara.svg"
STATES_DIR = ICON_DIR / "states"

APP_SIZES = (16, 24, 32, 48, 64, 128, 256)
TRAY_SIZES = (16, 24, 32, 48, 64)
SMALL_MAX = 16          # sizes at or below this use the simplified drawing

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# Capture state -> (segment colour, ring drawing).
CAPTURE = {
    "idle":      (theme.ICON_IDLE, "ring-hollow"),
    "listening": (theme.ACCENT, "ring-hollow"),
    "recording": (theme.RECORDING, "ring-filled"),
    "ava":       (theme.AVA, "ring-hollow"),
}
# Hands-free state -> (eye drawing, eye colour).
EYE = {
    "asleep": ("eye-closed", theme.ICON_IDLE),
    "armed":  ("eye-open", theme.ACCENT),
    "heard":  ("eye-heard", theme.ACCENT),
}
# Named states the app shows: (capture, hands-free).
STATES = {
    "idle":      ("idle", "asleep"),
    "listening": ("listening", "asleep"),
    "recording": ("recording", "asleep"),
    "ava":       ("ava", "asleep"),
    "asleep":    ("idle", "asleep"),
    "armed":     ("listening", "armed"),
    "heard":     ("listening", "heard"),
}
# The app/taskbar/exe icon is not a live state: brand cyan, eye closed.
APP_STATE = "listening"

MONTAGE_STATES = ("idle", "listening", "recording", "asleep", "armed", "heard", "ava")
_TASKBAR_DARK = "#202020"
_TASKBAR_LIGHT = "#f3f3f3"
_RING_IDS = ("ring-hollow", "ring-filled")
_EYE_IDS = ("eye-closed", "eye-open", "eye-heard")


def _ensure_gui_app():
    return QGuiApplication.instance() or QGuiApplication(sys.argv[:1])


def state_svg(state: str, small: bool, source: bytes | None = None) -> bytes:
    """The SVG with one variant, one ring and one eye visible, recoloured."""
    capture, hands_free = STATES[state]
    seg_colour, ring_id = CAPTURE[capture]
    eye_id, eye_colour = EYE[hands_free]
    suffix = "-small" if small else ""

    root = ET.fromstring(source if source is not None else SVG_PATH.read_bytes())
    by_id = {el.get("id"): el for el in root.iter() if el.get("id")}

    by_id["regular"].set("display", "none" if small else "inline")
    by_id["small"].set("display", "inline" if small else "none")
    for base in _RING_IDS + _EYE_IDS:
        wanted = base in (ring_id, eye_id)
        by_id[base + suffix].set("display", "inline" if wanted else "none")

    ring = by_id[ring_id + suffix]
    for attr in ("fill", "stroke"):
        if ring.get(attr, "none") != "none":
            ring.set(attr, seg_colour)
    by_id["hub" + suffix].set("fill", theme.ICON_HUB)
    for el in by_id[eye_id + suffix].iter():
        role = el.get("data-role")
        if role == "eye":
            el.set("stroke", eye_colour)
        elif role == "pupil":
            el.set("fill", eye_colour)
    return ET.tostring(root, encoding="utf-8")


def render(state: str, size: int, source: bytes | None = None) -> QImage:
    """Rasterise one state at one pixel size (ARGB32, transparent background)."""
    _ensure_gui_app()
    renderer = QSvgRenderer(QByteArray(state_svg(state, size <= SMALL_MAX, source)))
    if not renderer.isValid():
        raise ValueError(f"samsara.svg did not parse for state {state!r}")
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def build_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    """Multi-size ICO holding each PNG verbatim (same layout as the old
    _pack_ico.py: 6-byte header, 16-byte entry per size, PNG blobs)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    directory, blobs = b"", b""
    offset = 6 + 16 * len(pngs)
    for size, data in pngs:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + directory + blobs


def expected_outputs() -> dict[Path, QImage | bytes]:
    """Every generated file -> its image (PNG) or bytes (ICO)."""
    out: dict[Path, QImage | bytes] = {}
    app_pngs = []
    for size in APP_SIZES:
        image = render(APP_STATE, size)
        out[ICON_DIR / f"samsara_{size}.png"] = image
        app_pngs.append((size, png_bytes(image)))
    out[ICON_DIR / "samsara.ico"] = build_ico(app_pngs)
    for state in STATES:
        for size in TRAY_SIZES:
            out[STATES_DIR / f"samsara_{state}_{size}.png"] = render(state, size)
    return out


def write_assets() -> list[Path]:
    STATES_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for path, value in expected_outputs().items():
        data = value if isinstance(value, bytes) else png_bytes(value)
        path.write_bytes(data)
        written.append(path)
    return written


def stale_assets() -> list[Path]:
    """Generated files that are missing or whose pixels differ from the SVG."""
    stale = []
    for path, value in expected_outputs().items():
        if not path.exists():
            stale.append(path)
        elif isinstance(value, bytes):
            if path.suffix == ".ico" and _ico_sizes(path.read_bytes()) != list(APP_SIZES):
                stale.append(path)
        elif QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32) != value:
            stale.append(path)
    return stale


def _ico_sizes(data: bytes) -> list[int]:
    _, _, count = struct.unpack_from("<HHH", data, 0)
    sizes = []
    for i in range(count):
        w = struct.unpack_from("<B", data, 6 + 16 * i)[0]
        sizes.append(256 if w == 0 else w)
    return sizes


def write_montage(path: Path, magnify: int = 4) -> Path:
    """16 px state sheet: each state at 1x and nearest-neighbour 4x, on a dark
    and a light taskbar, plus the 32 px regular drawing for reference."""
    _ensure_gui_app()
    cell = 16 * magnify
    label_w, pad, header_h = 150, 16, 44
    columns = [
        ("1x dark", _TASKBAR_DARK, 1), (f"{magnify}x dark", _TASKBAR_DARK, magnify),
        ("1x light", _TASKBAR_LIGHT, 1), (f"{magnify}x light", _TASKBAR_LIGHT, magnify),
        ("32 px dark", _TASKBAR_DARK, None),
    ]
    col_w = cell + 2 * pad
    row_h = cell + 2 * pad
    width = label_w + col_w * len(columns)
    height = header_h + row_h * len(MONTAGE_STATES)

    sheet = QImage(width, height, QImage.Format.Format_ARGB32)
    sheet.fill(QColor(theme.BG0))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    font = QFont("Segoe UI", 10)
    painter.setFont(font)
    painter.setPen(QColor(theme.TEXT_PRIMARY))
    for i, (title, _bg, _scale) in enumerate(columns):
        painter.drawText(QRectF(label_w + i * col_w, 0, col_w, header_h),
                         Qt.AlignmentFlag.AlignCenter, title)

    for r, state in enumerate(MONTAGE_STATES):
        y = header_h + r * row_h
        capture, hands_free = STATES[state]
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.drawText(QRectF(pad, y, label_w - pad, row_h / 2),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, state)
        painter.setPen(QColor(theme.ICON_IDLE))
        painter.drawText(QRectF(pad, y + row_h / 2, label_w - pad, row_h / 2),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                         f"{capture} / {hands_free}")
        small = render(state, 16)
        regular = render(state, 32)
        for i, (_title, bg, scale) in enumerate(columns):
            x = label_w + i * col_w
            painter.fillRect(x + 4, y + 4, col_w - 8, row_h - 8, QColor(bg))
            if scale is None:
                image = regular
            elif scale == 1:
                image = small
            else:
                image = small.scaled(cell, cell, Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.FastTransformation)
            painter.drawImage(x + (col_w - image.width()) // 2,
                              y + (row_h - image.height()) // 2, image)
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not sheet.save(str(path)):
        raise OSError(f"could not write {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if generated assets are stale")
    parser.add_argument("--montage", type=Path, help="write the 16 px state sheet to this path")
    args = parser.parse_args(argv)
    _ensure_gui_app()

    if args.check:
        stale = stale_assets()
        for path in stale:
            print(f"stale: {path.relative_to(REPO)}")
        return 1 if stale else 0
    if args.montage is not None:
        print(f"saved {write_montage(args.montage)}")
        return 0
    for path in write_assets():
        print(f"saved {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
