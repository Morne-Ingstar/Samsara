"""Reminder toast -- reusable, batching, bottom-right notification surface.

Public API (thread-safe, callable from any thread):
    get_toast() -> ReminderToast
    ReminderToast.show(title, message, on_shown=None)  -- on_shown fires once
        the row has actually been drawn on the Qt thread (not merely posted)
    ReminderToast.stop()  -- call during app shutdown, before Show Numbers
        (the overlay this gates against) is torn down. Optional Dismiss and
        Complete callbacks add click targets without taking keyboard focus.

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
  - Generic reminders remain click-through. Rows with an explicit action
    callback become interactive and show Dismiss/Complete buttons; the
    buttons never take keyboard focus, so existing app-level shortcuts remain
    available.
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

import threading
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
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


@dataclass(eq=False)
class _ToastRow:
    title: str
    message: str
    on_dismiss: object = None
    on_complete: object = None


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

        self._rows: list[_ToastRow] = []
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

    def add_row(
        self, title: str, message: str, on_dismiss=None, on_complete=None
    ) -> None:
        self._rows.append(_ToastRow(title, message, on_dismiss, on_complete))
        self._sync_input_mode()
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
                w.hide()
                w.setParent(None)
                w.deleteLater()

        for i, row in enumerate(self._rows):
            if i > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.HLine)
                sep.setStyleSheet(
                    f"border: none; border-top: 1px solid {theme.BORDER_FAINT};"
                    f" background: transparent;"
                )
                self._rows_layout.addWidget(sep)

            title_lbl = QLabel(row.title)
            title_lbl.setStyleSheet(
                f"color: {theme.ACCENT}; font-weight: 600;"
                f" font-size: {theme.FONT_SIZE_CAPTION}px; background: transparent;"
            )
            self._rows_layout.addWidget(title_lbl)

            msg_lbl = QLabel(row.message)
            msg_lbl.setWordWrap(True)
            msg_lbl.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY};"
                f" font-size: {theme.FONT_SIZE_BODY}px; background: transparent;"
            )
            self._rows_layout.addWidget(msg_lbl)


            if row.on_dismiss is not None or row.on_complete is not None:
                actions_widget = QWidget()
                actions_widget.setStyleSheet("background: transparent;")
                actions = QHBoxLayout(actions_widget)
                actions.setContentsMargins(0, 2, 0, 0)
                actions.setSpacing(8)
                actions.addStretch()
                if row.on_dismiss is not None:
                    dismiss_btn = QPushButton("Dismiss")
                    dismiss_btn.setAccessibleName(f"Dismiss {row.title}")
                    dismiss_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                    dismiss_btn.setStyleSheet(
                        f"QPushButton{{background:transparent;color:{theme.TEXT_PRIMARY};"
                        f"border:1px solid {theme.BORDER};border-radius:6px;"
                        f"font-size:{theme.FONT_SIZE_CAPTION}px;padding:6px 12px;}}"
                        f"QPushButton:hover{{background:rgba(255,255,255,0.06);}}"
                    )
                    dismiss_btn.clicked.connect(
                        lambda _checked=False, r=row: self._activate_row(r, r.on_dismiss)
                    )
                    actions.addWidget(dismiss_btn)
                if row.on_complete is not None:
                    complete_btn = QPushButton("Complete")
                    complete_btn.setAccessibleName(f"Complete {row.title}")
                    complete_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                    complete_btn.setStyleSheet(
                        f"QPushButton{{background:{theme.ACCENT};color:{theme.TEXT_ON_ACCENT};"
                        f"border:none;border-radius:6px;font-weight:600;"
                        f"font-size:{theme.FONT_SIZE_CAPTION}px;padding:6px 12px;}}"
                        f"QPushButton:hover{{background:{theme.ACCENT_HOVER};}}"
                    )
                    complete_btn.clicked.connect(
                        lambda _checked=False, r=row: self._activate_row(r, r.on_complete)
                    )
                    actions.addWidget(complete_btn)
                self._rows_layout.addWidget(actions_widget)

    def _sync_input_mode(self) -> None:
        interactive = any(
            row.on_dismiss is not None or row.on_complete is not None
            for row in self._rows
        )
        transparent = bool(self.windowFlags() & Qt.WindowTransparentForInput)
        if transparent == (not interactive):
            return
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowTransparentForInput, not interactive)
        if was_visible:
            self.show()

    def _activate_row(self, row: _ToastRow, callback) -> None:
        if row not in self._rows:
            return
        self._rows.remove(row)
        self._hold_timer.stop()
        self._cancel_fade()
        if self._rows:
            self._sync_input_mode()
            self._rebuild_rows()
            self.adjustSize()
            self._reposition()
            self._hold_timer.start(_HOLD_MS)
        else:
            self.hide()
            self._sync_input_mode()
        try:
            callback()
        except Exception as e:
            logger.exception(f"Reminder toast action failed for {row.title!r}: {e}")
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
        self._sync_input_mode()

    def _cancel_fade(self) -> None:
        if self._fade_anim is not None:
            self._fade_anim.stop()
            # QPropertyAnimation(self, ..., self) parents the animation to
            # the window, which (hide-not-destroy) is never destroyed --
            # without deleteLater() every fade in/out leaks one QObject
            # child for the life of the process.
            self._fade_anim.deleteLater()
            self._fade_anim = None

    def stop(self) -> None:
        """Terminally hide this window and release its transient UI state."""
        self._hold_timer.stop()
        self._cancel_fade()
        self.hide()
        self._rows.clear()
        self._sync_input_mode()
        self._rebuild_rows()

    def closeEvent(self, e) -> None:
        # Hide instead of destroy -- keeps the Python reference valid, same
        # reuse pattern as status_overlay.py / task_overlay.py.
        e.ignore()
        self.hide()


class ReminderToast:
    """Thread-safe wrapper around _ToastWindow using the shared qt_runtime."""

    def __init__(self):
        self._window: "_ToastWindow | None" = None
        self._pending: list[tuple[str, str, object, object, object]] = []
        self._gate_timer: "QTimer | None" = None
        self._state_lock = threading.Lock()
        self._accepting = True

    def show(
        self, title: str, message: str, on_shown=None, *, on_dismiss=None, on_complete=None
    ) -> bool:
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
        with self._state_lock:
            if not self._accepting:
                return False
        qt_runtime.post(lambda: self._show_on_qt_thread(
            title, message, on_shown, on_dismiss, on_complete
        ))
        return True

    def _show_on_qt_thread(
        self, title: str, message: str, on_shown=None,
        on_dismiss=None, on_complete=None,
    ) -> None:
        with self._state_lock:
            if not self._accepting:
                return
        if _is_show_numbers_active():
            self._pending.append(
                (title, message, on_shown, on_dismiss, on_complete)
            )
            self._arm_gate()
            return
        self._ensure_window()
        self._window.add_row(title, message, on_dismiss, on_complete)
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
        with self._state_lock:
            if not self._accepting:
                return
        if _is_show_numbers_active():
            return
        self._gate_timer.stop()
        self._gate_timer.deleteLater()
        self._gate_timer = None

        pending, self._pending = self._pending, []
        self._ensure_window()
        for title, message, on_shown, on_dismiss, on_complete in pending:
            try:
                self._window.add_row(title, message, on_dismiss, on_complete)
            except Exception as e:
                # Don't let one bad row abort the rest of the flush queue,
                # and don't confirm-as-shown the row that actually failed.
                logger.exception(f"_check_gate: add_row failed for {title!r}: {e}")
                continue
            if on_shown is not None:
                on_shown()

    def stop(self) -> None:
        """Terminally stop this toast for application shutdown.

        Safe to call from any thread. Once called, future and already-posted
        show requests are discarded and the window cannot be recreated.
        """
        with self._state_lock:
            if not self._accepting:
                return
            self._accepting = False
        qt_runtime.post(self._stop_on_qt_thread)

    def _stop_on_qt_thread(self) -> None:
        if self._gate_timer is not None:
            self._gate_timer.stop()
            self._gate_timer.deleteLater()
            self._gate_timer = None
        self._pending = []
        if self._window is not None:
            self._window.stop()


# Module-level singleton — mirrors status_overlay.get_overlay() / the
# TaskOverlay instance pattern.
_toast: "ReminderToast | None" = None
_toast_lock = threading.Lock()


def get_toast() -> ReminderToast:
    global _toast
    if _toast is None:
        with _toast_lock:
            if _toast is None:
                _toast = ReminderToast()
    return _toast
