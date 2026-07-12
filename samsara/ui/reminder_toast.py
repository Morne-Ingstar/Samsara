"""Reminder toast -- reusable, batching, bottom-right notification surface.

Public API (thread-safe, callable from any thread):
    get_toast() -> ReminderToast
    ReminderToast.show(title, message, on_shown=None)  -- on_shown fires once
        the row has actually been drawn on the Qt thread (not merely posted)
    ReminderToast.stop()  -- call during app shutdown, before Show Numbers
        (the overlay this gates against) is torn down

Architecture
------------
One reusable QWidget, created lazily on first show() and hidden (never
destroyed) afterward -- the same singleton-reuse pattern as
status_overlay.py / task_overlay.py. All Qt construction and mutation is
marshaled onto the Qt thread via qt_runtime.post(); show() itself is safe
to call from the 30s reminder check loop or any other thread.

Behavior (locked decisions):
  - Fade in, hold ~10s, fade out -- quiet auto-dismiss, no acknowledgment
    required (RSI-style reminders get reflex-dismissed, not read carefully).
  - Click-through (Qt.WindowTransparentForInput, matching numbers_overlay_qt's
    proven approach): the toast has no interactive affordance, so clicks
    always pass through to whatever is underneath instead of being eaten by
    a 10-second-visible, unclickable box.
  - Same-tick batching: reminders that fire in the same check cycle are
    added as additional ROWS in the one visible box, not separate boxes.
  - Late append: a reminder arriving while the box is already showing adds
    a row AND restarts the ~10s hold timer.
  - No foreground-window polling of any kind, unlike numbers_overlay_qt's
    Show Numbers overlay -- this toast never watches or reacts to which
    window has focus, so it cannot reintroduce that overlay's self-dismiss
    bug (a naive "foreground changed -> dismiss" check that fired on the
    overlay's own HWND immediately after being shown).
  - Suppressed while the Show Numbers overlay is up (checked via
    plugins.commands.show_numbers.is_overlay_active()) -- reminders queue
    and flush as soon as it closes, instead of the two overlays colliding
    on screen.
"""

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QVBoxLayout, QWidget,
)

from samsara.log import get_logger
from samsara.ui import qt_runtime, theme

logger = get_logger(__name__)

_WIDTH       = 340
_HOLD_MS     = 10000
_FADE_MS     = 220
_GATE_MS     = 500   # poll interval while waiting for Show Numbers to close

# Bottom-right, but well above hint_toast.py's -48px offset (and the
# listening indicator pill it already reserves room for) so a reminder
# toast and a hint toast/pill showing at the same time never overlap.
_MARGIN_RIGHT  = 16
_MARGIN_BOTTOM = 120


def _is_show_numbers_active() -> bool:
    """True while the Show Numbers overlay is up. Safe from any thread."""
    try:
        from plugins.commands.show_numbers import is_overlay_active
        return is_overlay_active()
    except Exception as e:
        logger.debug(f"_is_show_numbers_active: {e}")
        return False


class _ToastWindow(QWidget):
    """Single reusable toast box. All methods run on the Qt thread."""

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedWidth(_WIDTH)

        self._rows: list[tuple[str, str]] = []
        self._fade_anim: "QPropertyAnimation | None" = None

        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._start_fade_out)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._box = QWidget()
        self._box.setObjectName("reminderToastBox")
        self._box.setStyleSheet(
            f"#reminderToastBox {{"
            f" background: {theme.BG1};"
            f" border-radius: 10px;"
            f" border: 1px solid {theme.BORDER};"
            f"}}"
        )
        outer.addWidget(self._box)

        self._rows_layout = QVBoxLayout(self._box)
        self._rows_layout.setContentsMargins(14, 12, 12, 12)
        self._rows_layout.setSpacing(8)

    def add_row(self, title: str, message: str) -> None:
        self._rows.append((title, message))
        self._rebuild_rows()
        self.adjustSize()
        self._reposition()

        if self.isHidden():
            self._fade_in()
        else:
            self._cancel_fade()
            self.setWindowOpacity(1.0)

        self._hold_timer.start(_HOLD_MS)

    def _rebuild_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        for i, (title, message) in enumerate(self._rows):
            if i > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.HLine)
                sep.setStyleSheet(
                    f"border: none; border-top: 1px solid {theme.BORDER_FAINT};"
                    f" background: transparent;"
                )
                self._rows_layout.addWidget(sep)

            title_lbl = QLabel(title)
            title_lbl.setStyleSheet(
                f"color: {theme.ACCENT}; font-weight: 600;"
                f" font-size: {theme.FONT_SIZE_CAPTION}px; background: transparent;"
            )
            self._rows_layout.addWidget(title_lbl)

            msg_lbl = QLabel(message)
            msg_lbl.setWordWrap(True)
            msg_lbl.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY};"
                f" font-size: {theme.FONT_SIZE_BODY}px; background: transparent;"
            )
            self._rows_layout.addWidget(msg_lbl)

    def _reposition(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        self.move(
            geom.right() - self.width() - _MARGIN_RIGHT,
            geom.bottom() - self.height() - _MARGIN_BOTTOM,
        )

    def _fade_in(self) -> None:
        self.setWindowOpacity(0.0)
        self.show()
        self._cancel_fade()
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setDuration(_FADE_MS)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.start()

    def _start_fade_out(self) -> None:
        self._cancel_fade()
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setDuration(_FADE_MS)
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.InCubic)
        self._fade_anim.finished.connect(self._on_faded_out)
        self._fade_anim.start()

    def _on_faded_out(self) -> None:
        self.hide()
        self._rows.clear()

    def _cancel_fade(self) -> None:
        if self._fade_anim is not None:
            self._fade_anim.stop()
            # QPropertyAnimation(self, ..., self) parents the animation to
            # the window, which (hide-not-destroy) is never destroyed --
            # without deleteLater() every fade in/out leaks one QObject
            # child for the life of the process.
            self._fade_anim.deleteLater()
            self._fade_anim = None

    def closeEvent(self, e) -> None:
        # Hide instead of destroy -- keeps the Python reference valid, same
        # reuse pattern as status_overlay.py / task_overlay.py.
        e.ignore()
        self.hide()


class ReminderToast:
    """Thread-safe wrapper around _ToastWindow using the shared qt_runtime."""

    def __init__(self):
        self._window: "_ToastWindow | None" = None
        self._pending: list[tuple[str, str, object]] = []
        self._gate_timer: "QTimer | None" = None

    def show(self, title: str, message: str, on_shown=None) -> None:
        """Queue title/message for display. Safe to call from any thread.

        on_shown, if given, is called with no arguments on the Qt thread
        once add_row() has actually run (i.e. .show()/.raise_() were really
        invoked, not just posted) -- NOT merely once this method returns.
        qt_runtime.post() can silently drop the callback during shutdown,
        and widget construction can raise; in either case on_shown never
        fires. Callers (NotificationManager) use this to distinguish
        "posted" from "actually displayed" before recording a reminder as
        delivered.
        """
        qt_runtime.post(lambda: self._show_on_qt_thread(title, message, on_shown))

    def _show_on_qt_thread(self, title: str, message: str, on_shown=None) -> None:
        if _is_show_numbers_active():
            self._pending.append((title, message, on_shown))
            self._arm_gate()
            return
        self._ensure_window()
        self._window.add_row(title, message)
        # Reached only if the above didn't raise -- add_row() really ran.
        if on_shown is not None:
            on_shown()

    def _ensure_window(self) -> None:
        if self._window is None:
            self._window = _ToastWindow()

    def _arm_gate(self) -> None:
        if self._gate_timer is not None:
            return
        self._gate_timer = QTimer()
        self._gate_timer.setInterval(_GATE_MS)
        self._gate_timer.timeout.connect(self._check_gate)
        self._gate_timer.start()

    def _check_gate(self) -> None:
        if _is_show_numbers_active():
            return
        self._gate_timer.stop()
        self._gate_timer.deleteLater()
        self._gate_timer = None

        pending, self._pending = self._pending, []
        self._ensure_window()
        for title, message, on_shown in pending:
            try:
                self._window.add_row(title, message)
            except Exception as e:
                # Don't let one bad row abort the rest of the flush queue,
                # and don't confirm-as-shown the row that actually failed.
                logger.exception(f"_check_gate: add_row failed for {title!r}: {e}")
                continue
            if on_shown is not None:
                on_shown()

    def stop(self) -> None:
        """Stop the gate-poll timer and drop any pending queued reminders.

        Safe to call from any thread. Call during app shutdown BEFORE the
        overlay this gates against (Show Numbers) is torn down -- otherwise
        a gate tick landing mid-teardown could flush queued reminders into
        a freshly (re)constructed toast window while the rest of the app is
        shutting down.
        """
        qt_runtime.post(self._stop_on_qt_thread)

    def _stop_on_qt_thread(self) -> None:
        if self._gate_timer is not None:
            self._gate_timer.stop()
            self._gate_timer.deleteLater()
            self._gate_timer = None
        self._pending = []


# Module-level singleton — mirrors status_overlay.get_overlay() / the
# TaskOverlay instance pattern.
_toast: "ReminderToast | None" = None


def get_toast() -> ReminderToast:
    global _toast
    if _toast is None:
        _toast = ReminderToast()
    return _toast
