"""Decorative status glyphs using the existing Settings icon vocabulary."""
from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QWidget, QBoxLayout

from samsara.ui import theme
from samsara.ui.settings.sidebar import PAGE_GLYPHS, icon_family


class StatusRow(QWidget):
    """Keep full status text inside the screen bound at larger text sizes."""

    def __init__(self, window, bar, minimum):
        super().__init__()
        self._window, self._bar, self._minimum = window, bar, minimum
        self._fitting = False

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Resize) and self.layout() is not None and not self._fitting:
            self._fitting = True
            try:
                layout = self.layout()
                segments = [layout.itemAt(i).widget() for i in (0, 2, 4)] if layout.count() >= 6 else []
                need = layout.contentsMargins().left() + layout.spacing() * 5 + 2 + 44
                for segment in segments:
                    inner = segment.layout()
                    need += sum(inner.itemAt(i).sizeHint().width() for i in (0, 1)) + inner.spacing()
                stacked = need + 2 > self._window.width()
                for segment in segments:
                    inner = segment.layout()
                    direction = QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
                    if inner.direction() != direction:
                        inner.setDirection(direction)
                    for i in (0, 1):
                        inner.setAlignment(inner.itemAt(i).widget(), Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag(0))
                self._bar.setMinimumWidth(self.minimumSizeHint().width() + 2)
                self._window.setMinimumWidth(max(self._minimum, self.minimumSizeHint().width() + 2))
            finally:
                self._fitting = False
        return result


class StatusLabel(QLabel):
    """An ordinary accessible label; its leading icon carries no state."""

    _pages = {"MODE": "Shortcuts", "WAKE": "Audio", "MIC": "Dictation"}

    def __init__(self, text):
        super().__init__(text)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.Polish):
            size = self.font().pixelSize()
            if size > 0:
                inset = round(size * (theme.TYPE_MIN + theme.SPACE_1) / theme.TYPE_MIN) if icon_family() else 0
                if self.contentsMargins().left() != inset:
                    self.setContentsMargins(inset, 0, 0, 0)
        return result

    def paintEvent(self, event):
        super().paintEvent(event)
        family = icon_family()
        if family is None:
            return
        size = self.font().pixelSize()
        if size <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(theme.qcolor(theme.TEXT_SECONDARY))
        painter.setFont(theme.qfont(size, family))
        painter.drawText(QRect(0, (self.height()-size)//2, size, size), Qt.AlignmentFlag.AlignCenter,
                         PAGE_GLYPHS[self._pages[self.text()]])
