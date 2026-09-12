"""Visual-proof capture for the Window Cube panel (owner sign-off).

Not collected by pytest (the name does not match python_files = test_*.py),
and it lives here rather than in tools/ only because this task's allowed
paths cover tests/ but not tools/. It follows tools/wizard_screenshots.py's
approach: construct the widget directly, bypassing qt_runtime's thread
marshalling (this runs standalone, in its own process, so it owns its own
QApplication and never touches the running app's).

Renders the panel composited over a DARK and a LIGHT desktop region at the
configured opacity, so the owner can judge legibility on both.

Usage:
    F:\\envs\\sami\\python.exe tests\\_window_cube_screenshots.py

Output: C:\\Users\\Morne\\Documents\\Claude\\ui_proof\\window_cube\\*.png
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from samsara.ui.window_cube_qt import CubeRow, _CubeWindow

OUT_DIR = Path(r"C:\Users\Morne\Documents\Claude\ui_proof\window_cube")

OPACITY = 0.6

# Deliberately covers all four row states the panel can render: plain rows,
# two rows sharing an app name (second line appears), and a closed/greyed slot.
ROWS = [
    CubeRow(number=1, app="Warp"),
    CubeRow(number=2, app="Obsidian"),
    CubeRow(number=3, app="Claude"),
    CubeRow(number=4, app="Chrome", detail="Inbox (12) - Gmail"),
    CubeRow(number=5, app="Chrome", detail="Samsara docs - GitHub"),
    CubeRow(number=6, app="Spotify", closed=True),
]

BACKDROPS = {
    "over_dark": QColor("#0c0c10"),
    "over_light": QColor("#e9eaee"),
}


def _settle(app: QApplication, widget, ms: int = 600) -> None:
    widget.show()
    widget.raise_()
    end = time.monotonic() + (ms / 1000.0)
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])

    window = _CubeWindow(on_click=None, opacity=1.0, position=(80, 80))
    window._on_render(list(ROWS))
    _settle(app, window)

    panel = window.grab()
    written = []

    for name, colour in BACKDROPS.items():
        canvas = QPixmap(panel.width() + 160, panel.height() + 160)
        canvas.fill(colour)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Composite at the configured opacity so the proof shows what the
        # user actually sees, not an opaque render.
        painter.setOpacity(OPACITY)
        painter.drawPixmap(80, 80, panel)
        painter.end()

        out = OUT_DIR / f"window_cube_{name}.png"
        canvas.save(str(out), "PNG")
        written.append(out)

    window.hide()
    window.deleteLater()
    app.processEvents()

    for path in written:
        print(f"[CUBE-SHOT] {path}  ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
