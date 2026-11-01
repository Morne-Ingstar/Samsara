"""Small-window safeguards shared by the hub's list pages."""
from PySide6.QtCore import QEvent, QPointF, QSize, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QFrame, QLayout, QScrollArea, QSizeGrip, QVBoxLayout, QWidget

from samsara.ui import theme


class FitContent(QWidget):
    """Report the height of wrapping labels at the actual viewport width."""
    def minimumSizeHint(self):
        base = super().minimumSizeHint()
        if self.layout() is None:
            return base
        height = self.layout().totalHeightForWidth(max(base.width(), self.width()))
        return QSize(base.width(), max(base.height(), height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._fit_minimum()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self._fit_minimum()
        return result

    def _fit_minimum(self):
        if self.layout() is not None:
            # QTabWidget uses explicit page minimums when calculating its
            # scrollable height; a height-for-width hint alone is ignored.
            minimum = self.minimumSizeHint()
            if self.minimumSize() != minimum:
                self.setMinimumSize(minimum)


def scroll_page(page):
    """Keep layout minimums inside a viewport instead of overlapping controls.

    The public page remains the stack item, preserving its methods and focus
    shortcuts. Qt scrolls focused controls into view, including keyboard use.
    """
    layout = page.layout()
    if layout is None or hasattr(page, '_fit_scroll'):
        return
    margins = layout.contentsMargins()
    content = FitContent()
    # Preserve page-specific theme selectors after reparenting the controls.
    content.setObjectName(page.objectName())
    content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    content.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    content.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
    scroll = QScrollArea(page)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.viewport().setAutoFillBackground(False)
    scroll.setWidget(content)
    outer = QVBoxLayout(page)
    outer.setContentsMargins(margins)
    outer.setSpacing(0)
    outer.addWidget(scroll)
    page._fit_scroll = scroll


class HubSizeGrip(QSizeGrip):
    """Native drag resizing with a visible themed handle and 28-DIP target."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setAccessibleName('Resize window')  # PLACEHOLDER COPY (997c)
        self.setToolTip('Drag to resize window')  # PLACEHOLDER COPY (997c)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(theme.ACCENT if self.underMouse()
                                        else theme.TEXT_SECONDARY), 2))
        for offset in (5, 11, 17):
            painter.drawLine(QPointF(self.width() - offset, self.height() - 4),
                             QPointF(self.width() - 4, self.height() - offset))
