"""Visual-proof capture for the listening-indicator outcome chip (queue 41).

Not collected by pytest (name does not match test_*.py). Lives in tests/
rather than tools/ because this task's allowed paths cover tests/ only.
Follows tools/wizard_screenshots.py: construct the widget directly, bypassing
qt_runtime's thread marshalling -- this runs standalone in its own process
with its own QApplication, never touching the running app's.

Every chip label comes from samsara.session_modes.outcome_chip -- the same
mapping the app uses -- so a screenshot can never drift from real behaviour.
The indicator paints with a translucent background, so each capture is
composited onto a dark desktop-like region to be legible.

Usage:
    F:\\envs\\sami\\python.exe tests\\_outcome_chip_screenshots.py

Output: C:\\Users\\Morne\\Documents\\Claude\\ui_proof\\outcome_chip\\*.png
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

OUT_DIR = Path(r"C:\Users\Morne\Documents\Claude\ui_proof\outcome_chip")
BACKDROP = QColor("#1c1f26")
PAD = 40


def _cases():
    from samsara.session_modes import SessionMode, chip_ttl_ms, outcome_chip

    # (file stem, pill session badge, outcome kind, detail)
    spec = [
        ("01_miss", "COMMAND", "command_miss", {}),
        ("02_mode_switch_ava", "AVA", "mode_switch", {"mode": SessionMode.AVA}),
        ("03_command_executed_switch_window", "COMMAND", "command_executed",
         {"phrase": "switch window"}),
        ("04_refused_focus_lock", "DICTATE", "dictate_commit_blocked_focus_lock", {}),
        ("05_staged_pending", "DICTATE", "dictate_staged", {}),
    ]
    for stem, badge, kind, detail in spec:
        label, chip_kind = outcome_chip(kind, detail)
        yield stem, badge, kind, label, chip_kind, chip_ttl_ms(kind, chip_kind)


def _pump(app, ms=400):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])

    from samsara.ui import theme
    from samsara.ui.listening_indicator import ListeningIndicator

    badge_colour = {"COMMAND": theme.WARNING, "DICTATE": theme.SUCCESS, "AVA": theme.ACCENT}
    written = []

    for stem, badge, kind, label, chip_kind, ttl in _cases():
        widget = ListeningIndicator()
        widget.set_position("top-center")
        widget.set_session_mode(badge, badge_colour[badge])
        widget.show()
        # ttl None keeps the chip up for the capture regardless of kind.
        widget.show_outcome(label, chip_kind, ttl_ms=None)
        _pump(app)

        grabbed = widget.grab()
        canvas = QPixmap(grabbed.width() + 2 * PAD, grabbed.height() + 2 * PAD)
        canvas.fill(BACKDROP)
        painter = QPainter(canvas)
        painter.drawPixmap(PAD, PAD, grabbed)
        painter.end()

        out = OUT_DIR / f"{stem}.png"
        canvas.save(str(out), "PNG")
        written.append((out, kind, chip_kind, ttl))

        widget.destroy()
        widget.deleteLater()
        app.processEvents()

    for out, kind, chip_kind, ttl in written:
        print(f"[CHIP-SHOT] {out.name:<40} kind={kind:<34} chip={chip_kind:<8} "
              f"ttl={ttl}  ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
