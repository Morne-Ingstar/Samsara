"""
PySide6 dictation history window for Samsara.

Thin qt_runtime shell around samsara.ui.history_view.HistoryView -- all
list/toolbar/detail-pane logic lives there, shared with main_window_qt.py's
embedded History tab. No list logic is duplicated in this file.
"""

from PySide6.QtWidgets import QMainWindow

from samsara.ui import qt_runtime
from samsara.ui.history_view import HistoryView

from samsara.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class HistoryQt:
    def __init__(self, app):
        self.app = app
        self._window = None
        self._init_posted = False

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._show_and_refresh)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def refresh(self):
        """Refresh an existing history view on its owning Qt thread."""
        if self._window is not None:
            qt_runtime.post(self._window._view.refresh)

    def _show_and_refresh(self):
        """Runs on the Qt thread; prevents a reopened window looking stale."""
        self._window._view.refresh()
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _HistoryWindow(self.app)
        self._window.show()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class _HistoryWindow(QMainWindow):

    def __init__(self, app):
        super().__init__()
        self.app = app

        self.setWindowTitle("Dictation History")
        self.resize(860, 620)
        self.setMinimumSize(520, 400)

        store = getattr(app, 'history_store', None)
        self._view = HistoryView(
            store,
            legacy_history_fn=lambda: getattr(app, 'history', []),
            legacy_clear_fn=self._clear_legacy,
        )
        self.setCentralWidget(self._view)

    def _clear_legacy(self):
        legacy = getattr(self.app, 'history', None)
        if legacy is not None:
            legacy.clear()
        if hasattr(self.app, 'save_history'):
            try:
                self.app.save_history()
            except Exception as e:
                logger.debug(f"_clear_legacy: {e}")

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        e.ignore()
        self.hide()
