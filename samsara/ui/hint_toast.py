"""Hint toast -- small bottom-right notification widget.

Features:
  - Fixed 350px width, auto-height based on content
  - Dark semi-transparent pill matching Samsara's UI palette
  - "i" icon, hint text, close button, "Don't show hints" checkbox
  - Fade in on show (300ms), fade out on dismiss (300ms)
  - Auto-dismisses after 8 seconds
  - Always-on-top, no focus steal, not click-through (user can interact)
  - Only one instance visible at a time (enforced by HintManager)
"""

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

_BG     = "rgba(18, 18, 22, 235)"
_BORDER = "rgba(94, 234, 212, 0.22)"


class HintToast(QWidget):
    """Non-blocking hint notification rendered in the bottom-right corner."""

    _WIDTH   = 350
    _AUTO_MS = 8000
    _FADE_MS = 300

    def __init__(
        self,
        message: str,
        *,
        on_dismiss=None,
        on_disable=None,
    ) -> None:
        super().__init__(None)
        self._on_dismiss = on_dismiss
        self._on_disable = on_disable
        self._dismissed  = False
        self._auto_timer = None
        self._fade_in    = None
        self._fade_out   = None

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedWidth(self._WIDTH)

        self._build(message)

    def _build(self, message: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        box = QWidget()
        box.setObjectName("box")
        box.setStyleSheet(
            f"#box {{"
            f" background: {_BG};"
            f" border-radius: 10px;"
            f" border: 1px solid {_BORDER};"
            f"}}"
        )
        outer.addWidget(box)

        vl = QVBoxLayout(box)
        vl.setContentsMargins(14, 10, 10, 12)
        vl.setSpacing(6)

        # header: icon + "Hint" label + close button
        hdr = QHBoxLayout()
        hdr.setSpacing(6)

        icon_lbl = QLabel("i")
        icon_lbl.setFixedSize(18, 18)
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet(
            "background: rgba(94,234,212,0.15);"
            " color: #5EEAD4;"
            " border-radius: 9px;"
            " font-weight: bold;"
            " font-size: 11px;"
        )
        hdr.addWidget(icon_lbl)

        title_lbl = QLabel("Hint")
        title_lbl.setStyleSheet(
            "color: #5EEAD4; font-size: 11px; font-weight: 600;"
        )
        hdr.addWidget(title_lbl)
        hdr.addStretch()

        close_btn = QPushButton("x")
        close_btn.setFixedSize(18, 18)
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton {"
            " background: transparent; border: none;"
            " color: #55555C; font-size: 13px; font-weight: bold;"
            " border-radius: 3px;"
            "}"
            "QPushButton:hover {"
            " background: rgba(255,255,255,0.08); color: #E8E8EA;"
            "}"
        )
        close_btn.clicked.connect(self._dismiss)
        hdr.addWidget(close_btn)
        vl.addLayout(hdr)

        # message
        msg = QLabel(message)
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #E8E8EA; font-size: 12px;")
        vl.addWidget(msg)

        # "don't show hints" opt-out checkbox
        no_hints = QCheckBox("Don't show hints")
        no_hints.setStyleSheet("color: #55555C; font-size: 11px;")
        no_hints.setFocusPolicy(Qt.NoFocus)
        no_hints.toggled.connect(self._on_no_hints_toggled)
        vl.addWidget(no_hints)

    # ── Lifecycle ───────────────────────────────────────────────────────────────

    def show(self) -> None:
        self.adjustSize()
        geom = QApplication.primaryScreen().availableGeometry()
        self.move(
            geom.right() - self.width() - 16,
            geom.bottom() - self.height() - 48,
        )
        self.setWindowOpacity(0.0)
        super().show()

        self._fade_in = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_in.setDuration(self._FADE_MS)
        self._fade_in.setStartValue(0.0)
        self._fade_in.setEndValue(1.0)
        self._fade_in.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_in.start()

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._dismiss)
        self._auto_timer.start(self._AUTO_MS)

    def _dismiss(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True

        if self._auto_timer:
            self._auto_timer.stop()

        if self._on_dismiss:
            self._on_dismiss()
            self._on_dismiss = None

        self._fade_out = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_out.setDuration(self._FADE_MS)
        self._fade_out.setStartValue(self.windowOpacity())
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.InCubic)
        self._fade_out.finished.connect(self.close)
        self._fade_out.start()

    def _on_no_hints_toggled(self, checked: bool) -> None:
        if not checked:
            return
        if self._auto_timer:
            self._auto_timer.stop()
        self._dismissed = True
        self._on_dismiss = None   # _on_disable owns the toast reference cleanup
        self.close()
        if self._on_disable:
            self._on_disable()

    def closeEvent(self, event) -> None:
        if not self._dismissed:
            self._dismissed = True
            if self._on_dismiss:
                self._on_dismiss()
                self._on_dismiss = None
        event.accept()
