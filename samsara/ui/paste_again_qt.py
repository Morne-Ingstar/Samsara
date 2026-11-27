"""The passive saved-dictation action shared by both listening displays."""
from time import monotonic

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from samsara import paste_again
from samsara.ui import theme
from samsara.ui.indicator_drag import capsule_region


class PasteAgainControl(QWidget):
    def __init__(self, owner, app):
        pill = getattr(owner, '_pill_window', None)
        super().__init__(pill if pill is not None else owner)
        self.owner = owner
        self._app = app
        self._compact = pill is not None
        self._deadline = 0.0
        self._shown_for_retry = None
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.button = QPushButton('Paste again', self)  # PLACEHOLDER COPY (1163)
        self.button.setAccessibleName('Paste again')
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.setToolTip('Say “paste that again” to insert your last dictation into the current window.')
        self.button.clicked.connect(self._request)
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
        size = self.button.fontMetrics().horizontalAdvance(self.button.text()) + 24
        self.button.setFixedSize(size, 28)
        self.setFixedSize(self.button.size())
        if self.active:
            self._reserve_space()

    @property
    def active(self):
        return bool(self._deadline)

    def _request(self):
        if self.isVisible() and self._deadline > monotonic() and not _explicitly_hidden(self._app):
            paste_again.request(self._app)

    def present(self):
        self._deadline = monotonic() + paste_again.CONTROL_MS / 1000
        self.apply_theme()
        self._timer.start()
        self.sync()

    def _reserve_space(self):
        if self._compact:
            self.owner._paste_again_width = self.width() + 18
            self.owner._reposition()
            self.owner.update()
        else:
            relayout = getattr(self.owner, '_relayout_paste_again', None)
            if callable(relayout):
                relayout()

    def dismiss(self):
        if not self.active:
            return
        self._deadline = 0.0
        self._timer.stop()
        self.hide()
        if self._compact:
            self.owner._paste_again_width = 0
            self.owner._reposition()
            self.owner.update()
        else:
            self._reserve_space()
            if (self._shown_for_retry is not None and self._shown_for_retry is self.owner.view
                    and getattr(self._app, '_listening_display_visible', None) is not True):
                self.owner.hide()
            self._shown_for_retry = None

    def sync(self):
        if not self.active:
            return
        if monotonic() >= self._deadline or _explicitly_hidden(self._app):
            self.dismiss()
            return
        if self._compact:
            # Keep the containing indicator until this action expires, even
            # when its shorter delivery receipt has already disappeared.
            body = self.owner._layout()[2].toRect().translated(self.owner.pos())
            pill = self.parentWidget()
            pill.setGeometry(body)
            pill.setMask(capsule_region(QRectF(pill.rect())))
            pill.show()
            pill.update()
            self.move(body.width() - self.width() - 12, (body.height() - self.height()) // 2)
            self.show()
        elif not self.owner.isVisible():
            # Like Compact, keep the short-lived action when its receipt ends.
            if not hasattr(self.owner, 'view'):
                return
            self._shown_for_retry = self.owner.view
            self.owner.show()


def _explicitly_hidden(app):
    return getattr(app, '_listening_display_visible', None) is False


def show_control(owner, app, available=True):
    if not isinstance(owner, QWidget):
        return
    control = getattr(owner, '_paste_again_control', None)
    if not available or _explicitly_hidden(app):
        if control is not None:
            control.dismiss()
        return
    if control is None:
        control = owner._paste_again_control = PasteAgainControl(owner, app)
    control.present()
