"""AI Command Mode HUD -- small always-on-top overlay showing the plan as it executes.

All widget operations are marshalled to the Qt thread via qt_runtime.post().
Close = hide pattern: the window is created once on first show and hidden
between uses; it is never destroyed.

Status indicators use ASCII prefixes only (unicode avoidance):
  pending  "[ ] "
  running  "[>] "
  done     "[+] "
  failed   "[!] "
"""
from __future__ import annotations
from typing import Optional

# Module-level widget singleton -- created on first show, reused thereafter.
_window: Optional["_HudWindow"] = None


# ---------------------------------------------------------------------------
# Public API -- safe to call from any thread
# ---------------------------------------------------------------------------

def show_hud(app, actions: list[str]) -> None:
    from samsara.ui import qt_runtime  # noqa: PLC0415
    qt_runtime.ensure_started()
    qt_runtime.post(lambda: _qt_show(actions))


def update_step(step_index: int, status: str) -> None:
    from samsara.ui import qt_runtime  # noqa: PLC0415
    qt_runtime.post(lambda: _qt_update(step_index, status))


def hide_hud() -> None:
    from samsara.ui import qt_runtime  # noqa: PLC0415
    qt_runtime.post(_qt_hide)


# ---------------------------------------------------------------------------
# Qt-thread implementations (only called from the Qt thread via qt_runtime.post)
# ---------------------------------------------------------------------------

def _qt_show(actions: list[str]) -> None:
    global _window
    if _window is None:
        _window = _HudWindow()
    _window.set_plan(actions)
    _window.show()
    _window.raise_()


def _qt_update(step_index: int, status: str) -> None:
    if _window is not None:
        _window.update_step(step_index, status)


def _qt_hide() -> None:
    if _window is not None:
        _window.hide()


# ---------------------------------------------------------------------------
# Widget wrapper (Qt imports deferred to creation time)
# ---------------------------------------------------------------------------

class _HudWindow:
    """Frameless, always-on-top overlay.  Wraps QWidget to defer Qt import."""

    _PREFIX = {
        "pending": "[ ] ",
        "running": "[>] ",
        "done":    "[+] ",
        "failed":  "[!] ",
    }
    _STYLE = {
        "pending": "color:#aaa3a0;",
        "running": "color:#89ddff; font-weight:bold;",
        "done":    "color:#c3e88d;",
        "failed":  "color:#f78c6c;",
    }

    def __init__(self) -> None:
        from PySide6.QtWidgets import (  # noqa: PLC0415
            QWidget, QVBoxLayout, QLabel, QFrame,
        )
        from PySide6.QtCore import Qt  # noqa: PLC0415

        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        self._win = QWidget(None, flags)
        self._win.setStyleSheet(
            "QWidget {"
            "  background: #171720;"
            "  border: 1px solid #454150;"
            "  border-radius: 10px;"
            "}"
        )

        layout = QVBoxLayout(self._win)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(4)

        title = QLabel("AI Command Mode")
        title.setStyleSheet(
            "color: #f4d06f; font-size: 9pt; font-weight: bold; border: none;"
        )
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("border: none; border-top: 1px solid #30303a;")
        layout.addWidget(sep)

        self._steps_layout = QVBoxLayout()
        self._steps_layout.setSpacing(2)
        layout.addLayout(self._steps_layout)

        self._win.setMinimumWidth(270)
        self._step_labels: list = []
        self._step_cmds:   list[str] = []

    def _reposition(self) -> None:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
        screen = QApplication.primaryScreen()
        if screen is not None:
            geom = screen.availableGeometry()
            self._win.adjustSize()
            self._win.move(
                geom.right() - self._win.width() - 20,
                geom.top() + 60,
            )

    def set_plan(self, actions: list[str]) -> None:
        from PySide6.QtWidgets import QLabel  # noqa: PLC0415

        while self._steps_layout.count():
            item = self._steps_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._step_labels = []
        self._step_cmds = []

        for cmd in actions:
            lbl = QLabel(f"[ ] {cmd}")
            lbl.setStyleSheet("color: #aaa3a0; font-size: 9pt; border: none;")
            self._steps_layout.addWidget(lbl)
            self._step_labels.append(lbl)
            self._step_cmds.append(cmd)

        self._win.adjustSize()
        self._reposition()

    def update_step(self, step_index: int, status: str) -> None:
        if step_index >= len(self._step_labels):
            return
        cmd = self._step_cmds[step_index]
        prefix = self._PREFIX.get(status, "[ ] ")
        style  = self._STYLE.get(status,  "color: #ece9df;")
        self._step_labels[step_index].setText(f"{prefix}{cmd}")
        self._step_labels[step_index].setStyleSheet(
            f"{style} font-size: 9pt; border: none;"
        )

    def show(self)   -> None: self._win.show()
    def hide(self)   -> None: self._win.hide()
    def raise_(self) -> None: self._win.raise_()
