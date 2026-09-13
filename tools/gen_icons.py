"""Render every Samsara icon size and state from assets/icon/samsara.svg.

The SVG is the single source (see its header comment for the structure). The
drawing itself is samsara.ui.tray_qt.render_mark -- the same routine the
tray, the listening indicator and the splash use -- so this tool has no
palette and no renderer of its own. Replaces the old one-off
assets/icon/_make_*.py / _pack_ico.py scripts and keeps their output
filenames.

Outputs (all under assets/icon/):
  samsara_{16,24,32,48,64,128,256}.png   app icon (tray_qt.APP_MARK), one per size
  samsara.ico                            multi-size container of those PNGs
  states/samsara_{state}_{size}.png      every tray_qt.MARK_STATES entry at TRAY_SIZES
                                         ("heard" is the animation's static fallback frame)

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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform ships no fonts; point it at the system fonts so the
# montage labels render (icons themselves use no text).
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter  # noqa: E402

from samsara.ui import theme  # noqa: E402
from samsara.ui.tray_qt import APP_MARK, MARK_STATES, render_mark  # noqa: E402

ICON_DIR = REPO / "assets" / "icon"
STATES_DIR = ICON_DIR / "states"

APP_SIZES = (16, 24, 32, 48, 64, 128, 256)
TRAY_SIZES = (16, 24, 32, 48, 64)

MONTAGE_STATES = ("off", "asleep", "idle", "listening", "recording", "ava", "armed", "heard")
_MONTAGE_LABELS = {"heard": "heard-flash"}
# Montage swatches imitate the Windows taskbar, not the app palette.
_TASKBAR_DARK = "#202020"
_TASKBAR_LIGHT = "#f3f3f3"


def _ensure_gui_app():
    return QGuiApplication.instance() or QGuiApplication(sys.argv[:1])


def render(state: str, size: int) -> QImage:
    """One named state (tray_qt.MARK_STATES) at one pixel size."""
    _ensure_gui_app()
    capture, eye = MARK_STATES[state]
    return render_mark(capture, eye, size)


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
    _ensure_gui_app()
    out: dict[Path, QImage | bytes] = {}
    app_pngs = []
    for size in APP_SIZES:
        image = render_mark(*APP_MARK, size)
        out[ICON_DIR / f"samsara_{size}.png"] = image
        app_pngs.append((size, png_bytes(image)))
    out[ICON_DIR / "samsara.ico"] = build_ico(app_pngs)
    for state in MARK_STATES:
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
    """Generated files that are missing, whose pixels differ from the SVG,
    or that no state produces any more."""
    expected = expected_outputs()
    stale = []
    for path, value in expected.items():
        if not path.exists():
            stale.append(path)
        elif isinstance(value, bytes):
            if path.suffix == ".ico" and _ico_sizes(path.read_bytes()) != list(APP_SIZES):
                stale.append(path)
        elif QImage(str(path)).convertToFormat(QImage.Format.Format_ARGB32) != value:
            stale.append(path)
    if STATES_DIR.exists():
        stale.extend(p for p in STATES_DIR.glob("*.png") if p not in expected)
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
    and a light taskbar, plus the 32 px regular drawing on both."""
    _ensure_gui_app()
    cell = 16 * magnify
    label_w, pad, header_h = 170, 16, 44
    columns = [
        ("1x dark", _TASKBAR_DARK, 1), (f"{magnify}x dark", _TASKBAR_DARK, magnify),
        ("1x light", _TASKBAR_LIGHT, 1), (f"{magnify}x light", _TASKBAR_LIGHT, magnify),
        ("32 px dark", _TASKBAR_DARK, None), ("32 px light", _TASKBAR_LIGHT, None),
    ]
    col_w = cell + 2 * pad
    row_h = cell + 2 * pad
    width = label_w + col_w * len(columns)
    height = header_h + row_h * len(MONTAGE_STATES)

    sheet = QImage(width, height, QImage.Format.Format_ARGB32)
    sheet.fill(QColor(theme.BG0))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setFont(QFont("Segoe UI", 10))
    painter.setPen(QColor(theme.TEXT_PRIMARY))
    for i, (title, _bg, _scale) in enumerate(columns):
        painter.drawText(QRectF(label_w + i * col_w, 0, col_w, header_h),
                         Qt.AlignmentFlag.AlignCenter, title)

    for r, state in enumerate(MONTAGE_STATES):
        y = header_h + r * row_h
        capture, eye = MARK_STATES[state]
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.drawText(QRectF(pad, y, label_w - pad, row_h / 2),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                         _MONTAGE_LABELS.get(state, state))
        painter.setPen(QColor(theme.ICON_IDLE))
        painter.drawText(QRectF(pad, y + row_h / 2, label_w - pad, row_h / 2),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                         f"{capture} / {eye}")
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
