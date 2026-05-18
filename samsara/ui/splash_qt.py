"""PySide6 splash screen for Samsara.

Drop-in replacement for SplashScreen with the same public API:
    set_status(text) / close() / get_root()

Architecture note
-----------------
The splash starts a permanent Qt event loop on a daemon thread with
QApplication.setQuitOnLastWindowClosed(False).  This thread becomes the
single Qt event-loop thread for the entire process lifetime.  Every
subsequent Qt window (Settings, History, Main, etc.) will find
QApplication.instance() already set, take the owns_app=False path, and
post its window creation to this thread — eliminating the race condition
where whichever window opened first would claim the Qt thread.

The hidden tk.Tk() root is still created here so dictation.py can reuse
it for after() scheduling, the listening indicator, and the tray icon,
exactly as before.
"""

import threading
import time
import tkinter as tk

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel,
    QProgressBar, QVBoxLayout, QWidget,
)

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

        # Centre on primary screen
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
        self._bar.setRange(0, 0)   # indeterminate
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
# Public class — same API as SplashScreen
# ---------------------------------------------------------------------------

class SplashScreenQt:
    """Qt splash screen that also seeds the permanent Qt event-loop thread."""

    def __init__(self):
        self._start_time = time.time()
        self._widget: "_SplashWidget | None" = None
        self._qt_ready   = threading.Event()

        # Start the permanent Qt thread before creating the Tkinter root so
        # QApplication is established first (avoids DPI-awareness fight with CTk).
        self._thread = threading.Thread(
            target=self._run_qt, daemon=True, name="samsara-qt",
        )
        self._thread.start()
        self._qt_ready.wait(timeout=5.0)

        # Hidden Tkinter root reused by the app for after(), tray icon, etc.
        self.root = tk.Tk()
        self.root.withdraw()

    def _run_qt(self):
        qt_app = QApplication([])
        # Keep the event loop alive after the splash closes so all
        # subsequent Qt windows can use the same thread.
        qt_app.setQuitOnLastWindowClosed(False)

        self._widget = _SplashWidget()
        self._widget.show()
        qt_app.processEvents()
        self._qt_ready.set()

        qt_app.exec()

    # ---- Public API ---------------------------------------------------------

    def set_status(self, text: str):
        """Update the status line. Thread-safe."""
        if self._widget is not None:
            self._widget._status_sig.emit(text)

    def close(self):
        """Enforce minimum display time then dismiss the splash."""
        elapsed = time.time() - self._start_time
        remaining = _MIN_DISPLAY_S - elapsed
        if remaining > 0:
            time.sleep(remaining)

        if self._widget is not None:
            self._widget._close_sig.emit()
            self._widget = None

    def get_root(self):
        """Return the hidden tk.Tk() root for the app to reuse."""
        return self.root
