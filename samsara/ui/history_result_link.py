"""Qt's native mouse/keyboard links, styled as accent text without underline."""
from html import escape
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel
from samsara.ui import theme


class HistoryResultLabel(QLabel):
    def keyPressEvent(self, event):
        callback = getattr(self, '_history_callback', None)
        if callback and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            callback()
            event.accept()
            return
        super().keyPressEvent(event)


def set_history_link(label, message, callback):
    label._history_callback = callback
    if not getattr(label, '_history_connected', False):
        label._history_connected = True
        label.setOpenExternalLinks(False)
        label.linkActivated.connect(lambda _: label._history_callback()
                                    if label._history_callback else None)
        label.linkHovered.connect(lambda url: label.setCursor(
            Qt.CursorShape.PointingHandCursor if url else Qt.CursorShape.ArrowCursor))
    if not callback or 'History' not in message:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        label.setCursor(Qt.CursorShape.ArrowCursor)
        return False
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse |
                                  Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
    label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    anchor = f'<a href="history" style="color:{theme.ACCENT};text-decoration:none">History</a>'
    label.setText(escape(message).replace('History', anchor).replace('\n', '<br>'))
    return True
