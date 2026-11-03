"""Readable Home status messages, with the same policy as the displays."""
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QStatusBar, QWidget

from samsara.message_lifetime import MessageSlot
from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer


class MessageStatusBar(QStatusBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.messages = MessageSlot()
        self._hidden = []
        self._label = QLabel(self)
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._label.setMaximumHeight(160)
        self._label.hide()
        self.addWidget(self._label, 1)
        self._announcer = LiveSurfaceAnnouncer(self._label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render)
        QApplication.instance().installEventFilter(self)

    def showMessage(self, text, timeout=0):
        self.show_feedback(text, 'accent', timeout)

    def show_feedback(self, text, kind='accent', timeout=1800):
        if not text:
            self.clearMessage()
            return
        self.messages.offer(text, kind, timeout)
        self._render()

    def currentMessage(self):
        current = self.messages.current
        return current.text if current else ''

    def clearMessage(self):
        self.messages.clear()
        self._render()

    def dismiss(self, *, next_action=False):
        self.messages.dismiss(next_action=next_action)
        self._render()

    def _render(self):
        current = self.messages.advance()
        self._timer.stop()
        if current:
            if not self._hidden:
                self._hidden = [w for w in self.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
                                if w is not self._label and w.isVisible()]
                for widget in self._hidden:
                    widget.hide()
            self._label.setText(current.text)
            self._label.setAccessibleName(current.text)
            self._label.setToolTip(current.text)
            self._label.show()
            self._announcer.announce_transition(current, current.text)
            if current.deadline is not None:
                self._timer.start(max(1, round(1000 * (current.deadline - self.messages.clock()))))
        else:
            self._announcer.reset()
            self._label.clear()
            self._label.hide()
            for widget in self._hidden:
                widget.show()
            self._hidden = []

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            if hasattr(watched, 'window') and watched.window() is self.window():
                card = self.window().findChild(QWidget, 'homeNotice')
                if card is not None and card.isVisible() and watched is not card and not card.isAncestorOf(watched):
                    from samsara.ui.home_notice_qt import dismiss_problem_notice
                    dismiss_problem_notice(card)
        if self.messages.current and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress):
            if watched is self._label or watched is self:
                self.dismiss()
            elif hasattr(watched, 'window') and watched.window() is self.window():
                self.dismiss(next_action=True)
        return super().eventFilter(watched, event)
