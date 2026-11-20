"""Explicit keyboard review of passive feedback; normal display never activates.

Review operates on the actual controls and their existing callbacks, including
draft links and correction choices. No transcript or notification is persisted.
"""
from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractScrollArea, QApplication, QDialog, QLabel,
    QLineEdit, QListWidget, QPushButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from samsara.ui import theme


def _is_retry_button(button):
    parent = button.parentWidget()
    notice = getattr(parent, 'notice', None)
    return (getattr(notice, 'owner', None) == 'dictation_retry'
            and getattr(parent, 'report', None) is button
            and button.isVisible() and button.isEnabled())


def _is_reviewable(window):
    if window.windowFlags() & Qt.WindowDoesNotAcceptFocus:
        return True
    # Full and Compact drop the focus flag to expose their History links.
    return window.testAttribute(Qt.WA_ShowWithoutActivating) and any(
        button.window() is window and _is_retry_button(button)
        for button in window.findChildren(QPushButton)
    )


def _remember_foreground():
    if QApplication.platformName() == 'windows':
        import win32gui
        from samsara.handlers import _get_foreground_hwnd

        hwnd = _get_foreground_hwnd()

        def restore():
            if not hwnd:
                return False
            if _get_foreground_hwnd() != hwnd:
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except win32gui.error:
                    return False
            return _get_foreground_hwnd() == hwnd
    else:
        window = QApplication.activeWindow()

        def restore():
            if window is None or not isValid(window) or not window.isVisible():
                return False
            QApplication.setActiveWindow(window)
            return window.isActiveWindow()
    return restore


class ReviewSession(QObject):
    def __init__(self, window, on_finished=None, *, on_retry=None):
        super().__init__(window)
        self.window = window
        self.on_finished = on_finished
        self.on_retry = on_retry
        self.active = False
        self._policies = []
        self._links = []
        self._timers = []
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._enable_if_active)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._finish_if_hidden)

    def start(self):
        if self.active or not self.window.isVisible():
            return
        self.active = True
        self._flags = self.window.windowFlags()
        self._without_activation = self.window.testAttribute(Qt.WA_ShowWithoutActivating)
        self._geometry = self.window.geometry()
        # Expiry/idle timers must not remove feedback while it is being read.
        for name in ('_auto_timer', '_hold_timer', '_fade_timer', '_life_timer'):
            timer = getattr(self.window, name, None)
            if isinstance(timer, QTimer) and timer.isActive():
                self._timers.append((timer, timer.remainingTime(), timer.interval()))
                timer.stop()
        for name in ('_fade_in', '_fade_out', '_fade_anim'):
            animation = getattr(self.window, name, None)
            if isinstance(animation, QAbstractAnimation):
                animation.stop()
        self.window.setWindowOpacity(1.0)
        flags = self._flags & ~(Qt.WindowType_Mask | Qt.WindowDoesNotAcceptFocus
                                | Qt.WindowTransparentForInput)
        self.window.setWindowFlags(flags | Qt.Tool)
        self.window.setAttribute(Qt.WA_ShowWithoutActivating, False)
        app = QApplication.instance()
        app.installEventFilter(self)
        self._enable_controls()
        self.window.setGeometry(self._geometry)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        targets = [w for w, _ in self._policies if w.isVisible() and w.isEnabled()]
        (targets[0] if targets else self.window).setFocus(Qt.TabFocusReason)

    def _enable_controls(self):
        known = {id(widget) for widget, _ in self._policies}
        for child in (self.window, *self.window.findChildren(QWidget)):
            if id(child) in known:
                continue
            if isinstance(child, QLabel):
                self._links.append((child, child.textInteractionFlags()))
                child.setTextInteractionFlags(child.textInteractionFlags()
                                             | Qt.TextSelectableByKeyboard | Qt.LinksAccessibleByKeyboard)
            if isinstance(child, (QLabel, QAbstractButton, QAbstractScrollArea, QLineEdit)):
                self._policies.append((child, child.focusPolicy()))
                child.setFocusPolicy(Qt.StrongFocus)

    def eventFilter(self, watched, event):
        if not self.active:
            return False
        belongs = watched is self.window or (isinstance(watched, QWidget)
                                              and self.window.isAncestorOf(watched))
        if belongs and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.finish()
            return True
        if (belongs and self.on_retry and isinstance(watched, QPushButton)
                and event.type() in (QEvent.KeyPress, QEvent.KeyRelease)
                and event.key() == Qt.Key_Space and _is_retry_button(watched)):
            if event.type() == QEvent.KeyRelease and not event.isAutoRepeat():
                self.on_retry(watched)
            return True
        if watched is self.window and event.type() == QEvent.Hide:
            # The hiding callback may still update flags (e.g. a completed
            # reminder). Restore only after that callback has finished.
            self._hide_timer.start(0)
        elif belongs and event.type() == QEvent.ChildAdded:
            self._refresh_timer.start(0)
        return False

    def _enable_if_active(self):
        if self.active:
            self._enable_controls()

    def _finish_if_hidden(self):
        if not self.window.isVisible():
            self.finish()

    def finish(self):
        if not self.active:
            return
        self.active = False
        self._refresh_timer.stop()
        self._hide_timer.stop()
        QApplication.instance().removeEventFilter(self)
        visible = self.window.isVisible()
        for child, policy in self._policies:
            if isValid(child):
                child.setFocusPolicy(policy)
        for child, flags in self._links:
            if isValid(child):
                child.setTextInteractionFlags(flags)
        self.window.setWindowFlags(self._flags)
        self.window.setAttribute(Qt.WA_ShowWithoutActivating, self._without_activation)
        self.window.setGeometry(self._geometry)
        if visible:
            self.window.show()
            for timer, remaining, interval in self._timers:
                if isValid(timer):
                    timer.start(max(1, remaining) if timer.isSingleShot() else interval)
        if self.on_finished:
            self.on_finished()
        self.deleteLater()


class ReviewWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        # PLACEHOLDER COPY (1001)
        self.setWindowTitle('Review on-screen messages')
        self.setAccessibleName(self.windowTitle())
        self.setStyleSheet(theme.build_stylesheet())
        self.resize(440, 340)
        layout = QVBoxLayout(self)
        note = QLabel('Choose a visible surface, then Review. Use Tab to reach its controls. '
                      'Press Escape to return here.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.surfaces = QListWidget()
        self.surfaces.setAccessibleName('Visible messages and drafts')
        layout.addWidget(self.surfaces)
        self.review = QPushButton('Review')
        layout.addWidget(self.review, alignment=Qt.AlignLeft)
        close = QPushButton('Close')
        layout.addWidget(close, alignment=Qt.AlignLeft)
        close.clicked.connect(self.close)
        self.review.clicked.connect(self._review)
        self.surfaces.itemActivated.connect(lambda _item: self._review())
        self._session = None
        self._restore_foreground = None
        self.refresh()

    def refresh(self):
        self.surfaces.clear()
        self._windows = sorted(
            (w for w in QApplication.topLevelWidgets() if w is not self and w.isVisible()
             and _is_reviewable(w)),
            key=lambda w: w.accessibleName() or w.windowTitle() or type(w).__name__)
        for window in self._windows:
            self.surfaces.addItem(window.accessibleName() or window.windowTitle() or 'Dictation preview')
        self.review.setEnabled(bool(self._windows))
        if self._windows:
            self.surfaces.setCurrentRow(0)
        else:
            self.surfaces.addItem('No on-screen messages or drafts are visible.')

    def _review(self):
        index = self.surfaces.currentRow()
        if not 0 <= index < len(self._windows):
            return
        window = self._windows[index]
        if not isValid(window) or not window.isVisible():
            self.refresh()
            return
        self.hide()
        on_retry = self._retry if self._restore_foreground is not None else None
        self._session = ReviewSession(window, self._returned, on_retry=on_retry)
        self._session.start()

    def _retry(self, button):
        # The existing retry delivers to the foreground. Restore the editor
        # while review still owns activation, then release the review session.
        restore = self._restore_foreground
        if not restore():
            QApplication.beep()
            return
        session, self._session = self._session, None
        session.on_finished = None
        session.finish()
        if restore() and isValid(button) and _is_retry_button(button):
            button.click()

    def _returned(self):
        self._session = None
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()
        self.surfaces.setFocus()


def open_review(app):
    window = getattr(app, '_passive_review_window', None)
    if window is None or not isValid(window):
        window = ReviewWindow()
        app._passive_review_window = window
    if not window.isVisible() and window._session is None:
        window._restore_foreground = _remember_foreground()
    window.refresh()
    window.show()
    window.raise_()
    window.activateWindow()
    window.surfaces.setFocus()
