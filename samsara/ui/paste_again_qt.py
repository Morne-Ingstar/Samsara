"""Passive saved-dictation actions, inside Compact and beside Full."""
from ctypes import wintypes
from time import monotonic

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from samsara import paste_again
from samsara.ui import theme


class PasteAgainControl(QWidget):
    def __init__(self, owner, app):
        super().__init__(owner, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.owner = owner
        self._compact = hasattr(owner, '_pill_window')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName('Paste again')
        layout = QHBoxLayout(self)
        margin = 0 if self._compact else 6
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(6)
        self.button = QPushButton('Paste again', self)  # PLACEHOLDER COPY (1163)
        self.button.setAccessibleName('Paste again')
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.setToolTip('Say “paste that again” to insert your last dictation into the current window.')
        self.button.clicked.connect(lambda: paste_again.request(app))
        layout.addWidget(self.button)
        self.apply_theme()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.sync)

    def apply_theme(self):
        # Same font, face, height and hover as Full's Report/Dismiss buttons;
        # Compact's Report uses the same face, font and 28-DIP height.
        self.button.setFont(theme.qfont(theme.TYPE_MIN))
        self.button.setStyleSheet(f"QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: 5px; padding: 0; font-size: {theme.TYPE_MIN}px; }} QPushButton:hover {{ border-color: {theme.ACCENT}; }}")
        self.button.ensurePolished()
        self.button.setFixedSize(self.button.fontMetrics().horizontalAdvance(self.button.text()) + 24, 28)
        self.adjustSize()
        if self._compact and getattr(self.owner, '_paste_again_width', 0):
            self.owner._paste_again_width = self.width() + 18
            self.owner._reposition()
        self.update()

    def paintEvent(self, event):
        if self._compact:
            return  # The indicator paints the enclosing pill and its outline.
        # An opaque display-coloured backing keeps it legible over any app.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(theme.BORDER), 1))
        painter.setBrush(theme.qcolor(theme.BG1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)
        painter.end()

    def nativeEvent(self, event_type, message):
        # WM_MOUSEACTIVATE: keep the editor active AND deliver the click.
        # MA_NOACTIVATE (3), unlike MA_NOACTIVATEANDEAT, retains the click.
        if event_type == b'windows_generic_MSG':
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0021:
                return True, 3
        return super().nativeEvent(event_type, message)

    def present(self):
        self._deadline = monotonic() + paste_again.CONTROL_MS / 1000
        self.apply_theme()
        self._timer.start()
        if self._compact:
            self.owner._paste_again_width = self.width() + 18
            self.owner._reposition()
            self.owner.update()
        self.sync()

    def dismiss(self):
        self._timer.stop()
        self.hide()
        if self._compact:
            self.owner._paste_again_width = 0
            self.owner._reposition()
            self.owner.update()

    def sync(self):
        if monotonic() >= self._deadline:
            self.dismiss()
            return
        if self._compact:
            # Keep the containing indicator until this action expires, even
            # when its shorter delivery receipt has already disappeared.
            body = self.owner._layout()[2].toRect().translated(self.owner.pos())
            pill = self.owner._pill_window
            pill.setGeometry(body)
            from samsara.ui.indicator_drag import capsule_region
            from PySide6.QtCore import QRectF
            pill.setMask(capsule_region(QRectF(pill.rect())))
            pill.show()
            pill.update()
            self.move(body.right() - self.width() - 11,
                      body.top() + (body.height() - self.height()) // 2)
            self.show()
            return
        bounds = self.owner.frameGeometry()
        screen = self.owner.screen().availableGeometry()
        x = bounds.right()+7
        y = bounds.bottom()-self.height()+1
        if x+self.width() > screen.right()+1:
            x = bounds.left()-self.width()-6
        if x < screen.left():
            x, y = bounds.left(), bounds.top()-self.height()-6
            if y < screen.top():
                y = bounds.bottom()+7
        x = min(max(x, screen.left()), max(screen.left(), screen.right()-self.width()+1))
        y = min(max(y, screen.top()), max(screen.top(), screen.bottom()-self.height()+1))
        self.move(x, y)
        self.show()


def show_control(owner, app, available=True):
    if not isinstance(owner, QWidget):
        return
    control = getattr(owner, '_paste_again_control', None)
    if not available:
        if control is not None:
            control.dismiss()
        return
    if control is None:
        control = owner._paste_again_control = PasteAgainControl(owner, app)
    control.present()
