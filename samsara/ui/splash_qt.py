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

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QLabel,
    QProgressBar, QVBoxLayout, QWidget,
)

from samsara.ui import qt_runtime

log = logging.getLogger(__name__)

_MIN_DISPLAY_S = 3.0


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
        self.setFixedSize(350, 165)

        screen = QApplication.primaryScreen().geometry()
        self.move(
            screen.center().x() - 175,
            screen.center().y() - 82,
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

        self._status_lbl = QLabel("Starting...")
        self._status_lbl.setAlignment(Qt.AlignCenter)
        self._status_lbl.setStyleSheet(
            "color: #aaaaaa; font-size: 10px; background: transparent;"
        )
        outer.addWidget(self._status_lbl)
        outer.addSpacing(4)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        self._bar.setFixedHeight(6)
        self._bar.setTextVisible(False)
        self._bar.setStyleSheet("""
            QProgressBar {
                background: #1a1a1a;
                border: none;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                background: #00CED1;
                border-radius: 3px;
            }
        """)
        outer.addWidget(self._bar)

    def showEvent(self, e):
        super().showEvent(e)
        self._dot_timer.start()

    def closeEvent(self, e):
        self._dot_timer.stop()
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

        threading.Thread(target=_do_close, daemon=True, name="splash-close").start()
