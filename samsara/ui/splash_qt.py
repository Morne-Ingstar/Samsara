"""PySide6 splash screen for Samsara.

Public API: set_status(text) / close()

Architecture note
-----------------
Thread/QApplication ownership belongs entirely to qt_runtime.  This module
creates the splash widget on the Qt thread via qt_runtime.post() and
exposes a thread-safe set_status / close API.  It no longer manages its
own thread or QApplication instance.
"""

import logging
import threading
import time

from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QLabel,
    QVBoxLayout, QWidget,
)

from samsara.ui import qt_runtime, theme
from samsara.runtime import thread_registry

log = logging.getLogger(__name__)

_MIN_DISPLAY_S = 3.0


# ---------------------------------------------------------------------------
# Spinner widget -- indeterminate, segmented "wheel" progress indicator
# ---------------------------------------------------------------------------

class _SpinnerWidget(QWidget):
    """8-segment ring spinner, brand-red, with a comet-tail opacity falloff.

    Same "dharma wheel" arc-segment language as dictation.py's tray icon
    (create_icon_image / _arc_polygon) -- one solid color here instead of
    the tray's 3-color chase, since this is a single indeterminate spinner
    rather than an activity-state indicator.

    Rotation is driven by elapsed wall-clock time (QElapsedTimer), not a
    per-tick angle increment, so the ~1.2s rotation period stays exact even
    if the QTimer's ~60fps ticks land late or get coalesced.
    """

    _SEGMENTS = 8
    _ARC_SPAN_DEG = 25.0     # each segment's sweep
    _PERIOD_MS = 1200        # one full rotation
    _TICK_MS = 16            # ~60fps

    def __init__(self, parent=None, diameter: int = 56):
        super().__init__(parent)
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)

        self._gap_deg = 360.0 / self._SEGMENTS - self._ARC_SPAN_DEG
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    def start(self):
        self._elapsed.start()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _on_tick(self):
        self.update()

    def paintEvent(self, event):
        angle = 360.0 * (self._elapsed.elapsed() % self._PERIOD_MS) / self._PERIOD_MS

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(self._diameter / 2, self._diameter / 2)
        painter.rotate(angle)

        pen_width = max(3, self._diameter // 10)
        radius = self._diameter / 2 - pen_width
        rect = QRectF(-radius, -radius, radius * 2, radius * 2)

        base = QColor(theme.BRAND_RED)
        for i in range(self._SEGMENTS):
            # Segment 0 leads (drawn first, brightest); opacity falls off
            # to ~0.2 by the trailing segment so the spin direction reads.
            frac = i / (self._SEGMENTS - 1)
            color = QColor(base)
            color.setAlphaF(1.0 - frac * 0.8)
            pen = QPen(color, pen_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            start_angle = i * (self._ARC_SPAN_DEG + self._gap_deg)
            painter.drawArc(rect, int(start_angle * 16), int(self._ARC_SPAN_DEG * 16))

        painter.end()


# ---------------------------------------------------------------------------
# Qt splash widget
# ---------------------------------------------------------------------------

class _SplashWidget(QWidget):
    _status_sig = Signal(str)
    _close_sig  = Signal()

    def __init__(self):
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        # Height grew from 165 to fit the spinner (56px) plus its spacing --
        # the old height was sized for a 6px progress bar.
        self.setFixedSize(350, 190)

        screen = QApplication.primaryScreen().geometry()
        self.move(
            screen.center().x() - 175,
            screen.center().y() - 95,
        )

        self._dot_count = 0
        self._dot_timer  = QTimer(self)
        self._dot_timer.setInterval(500)
        self._dot_timer.timeout.connect(self._animate_dots)

        self._build_ui()

        self._status_sig.connect(self._set_status)
        self._close_sig.connect(self.close)

    def _build_ui(self):
        self.setStyleSheet("background: #2d2d2d;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(4)

        title = QLabel("Samsara")
        title.setFont(QFont("Segoe UI", 20, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #00CED1; background: transparent;")
        outer.addWidget(title)

        self._subtitle = QLabel("De-articulating Splines.")
        self._subtitle.setAlignment(Qt.AlignCenter)
        self._subtitle.setStyleSheet(
            "color: #666666; font-size: 9px; font-style: italic;"
            " background: transparent;"
        )
        outer.addWidget(self._subtitle)
        outer.addSpacing(6)

        self._spinner = _SpinnerWidget(self)
        outer.addWidget(self._spinner, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addSpacing(4)

        self._status_lbl = QLabel("Starting...")
        self._status_lbl.setAlignment(Qt.AlignCenter)
        self._status_lbl.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FONT_SIZE_CAPTION}px;"
            " background: transparent;"
        )
        outer.addWidget(self._status_lbl)

    def showEvent(self, e):
        super().showEvent(e)
        self._dot_timer.start()
        self._spinner.start()

    def closeEvent(self, e):
        self._dot_timer.stop()
        self._spinner.stop()
        e.accept()

    def _animate_dots(self):
        self._dot_count = (self._dot_count % 3) + 1
        self._subtitle.setText("De-articulating Splines" + "." * self._dot_count)

    @Slot(str)
    def _set_status(self, text: str):
        self._status_lbl.setText(text)


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class SplashScreenQt:
    """Qt splash screen backed by the shared qt_runtime event loop."""

    def __init__(self):
        self._start_time   = time.time()
        self._widget: "_SplashWidget | None" = None
        self._widget_ready = threading.Event()

        # Start the shared Qt runtime (idempotent) then create the widget on
        # its thread.  We must wait for widget creation before returning so
        # that set_status() calls made immediately after __init__ are safe.
        qt_runtime.ensure_started()
        qt_runtime.post(self._create_widget)
        if not self._widget_ready.wait(timeout=5.0):
            log.warning("SplashScreenQt: widget not created within 5 s")

    def _create_widget(self):
        """Runs on the Qt thread via qt_runtime.post()."""
        self._widget = _SplashWidget()
        self._widget.destroyed.connect(self._on_widget_destroyed)
        self._widget.show()
        self._widget_ready.set()

    def _on_widget_destroyed(self):
        """Runs on the Qt thread when the C++ object is finalised."""
        self._widget = None

    # ---- Public API ---------------------------------------------------------

    def set_status(self, text: str):
        """Update the status line. Thread-safe."""
        if self._widget is not None:
            self._widget._status_sig.emit(text)

    def close(self):
        """Dismiss the splash, honouring the minimum display time.

        The sleep runs on a throw-away thread so the caller is never blocked.
        The Python reference to _widget is cleared only via the destroyed
        signal on the Qt thread.
        """
        def _do_close():
            elapsed   = time.time() - self._start_time
            remaining = _MIN_DISPLAY_S - elapsed
            if remaining > 0:
                time.sleep(remaining)
            w = self._widget
            if w is not None:
                w._close_sig.emit()

        thread_registry.spawn("splash-close", _do_close, daemon=True)
