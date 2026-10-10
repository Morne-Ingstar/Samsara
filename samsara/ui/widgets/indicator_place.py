"""Config-free, mouse/keyboard listening display placement choice."""
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from samsara.ui import theme
from samsara.ui.widgets.indicator_demo import CAPTIONS, DesktopSketch, POSITIONS


class _Target(QPushButton):
    def __init__(self, position, owner, parent):
        super().__init__(parent)
        self.position, self.owner = position, owner
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setProperty('walkthrough_detail', True)  # Keep Enter on this control in setup.
        self.setAccessibleName(position.replace("-", " "))
        self.setToolTip(self.accessibleName())
        self.clicked.connect(lambda: owner.choose(position))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QRectF(3, 6, self.width()-6, self.height()-12)
        selected = self.isChecked()
        ink = theme.qcolor(theme.ink_on(theme.ACCENT) if selected else theme.ACCENT)
        painter.setBrush(theme.qcolor(theme.ACCENT) if selected else Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(theme.qcolor(theme.ACCENT if selected else theme.BORDER), 1))
        radius = theme.CARD_RADIUS if self.owner.display == "full" else shape.height()/2
        painter.drawRoundedRect(shape, radius, radius)
        from samsara.ui.tray_qt import paint_mark
        paint_mark(painter, QRectF(9, 13, 18, 18), "idle", "asleep", 0, 1)
        painter.setPen(QPen(ink, 2))
        painter.drawLine(34, 17, self.width()-16, 17)
        if self.owner.display == "full":
            painter.setPen(QPen(ink, 1))
            painter.drawLine(34, 27, self.width()-25, 27)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor(theme.ACCENT), 2))
            painter.drawRoundedRect(QRectF(1, 1, self.width()-2, self.height()-2), radius, radius)
        painter.end()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            index = POSITIONS.index(self.position)
            row, column = divmod(index, 3)
            if event.key() == Qt.Key.Key_Left:
                column = (column - 1) % 3
            elif event.key() == Qt.Key.Key_Right:
                column = (column + 1) % 3
            else:
                row = 1 - row
            position = POSITIONS[row * 3 + column]
            self.owner.choose(position)
            self.owner.targets[position].setFocus()
            event.accept()
        else:
            super().keyPressEvent(event)


class _PlacementDesktop(DesktopSketch):
    def target_rect(self, position, width=44, height=44):
        # Keep all six 44px controls disjoint in the shorter onboarding sketch.
        area = self.work_rect().adjusted(2, 2, -2, -2)
        vertical, horizontal = position.split('-')
        x = {'left': area.left(), 'center': area.center().x()-width/2,
             'right': area.right()-width}[horizontal]
        return QRectF(x, area.top() if vertical == 'top' else area.bottom()-height, width, height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "targets"):
            for value, button in self.targets.items():
                button.setGeometry(self.target_rect(value, 112 if self.display == "full" else 92, 44).toRect())

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter
        painter = QPainter(self)
        self.paint_desktop(painter)
        painter.end()


class IndicatorPlacePicker(QWidget):
    """Six native button roles, one persisted-value-shaped output signal."""
    placement_chosen = Signal(str)

    def __init__(self, value="bottom-left", parent=None, *, display="full"):
        super().__init__(parent)
        self.display = display
        self.setMinimumWidth(540)
        self.setFont(theme.qfont(theme.TYPE_BODY))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"background: {theme.BG0}; color: {theme.TEXT_PRIMARY}; font-family: {theme.FONT_FAMILY};")
        # PLACEHOLDER COPY (490)
        self.setAccessibleName("Listening display placement")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 16, 28, 16)
        layout.setSpacing(12)
        self.desktop = _PlacementDesktop(self, display=display)
        layout.addWidget(self.desktop, 1)
        self.targets = {position: _Target(position, self, self.desktop) for position in POSITIONS}
        self.desktop.targets = self.targets
        # PLACEHOLDER COPY (490)
        self.hint = QLabel("You can also drag the real display anywhere.")
        self.hint.setWordWrap(True)
        self.hint.setMinimumHeight(44)
        layout.addWidget(self.hint)
        self.set_value(value)

    def value(self):
        return self._value

    def set_value(self, value):
        # A custom saved position is not silently relabelled as a preset.
        if value not in (*POSITIONS, "custom"):
            raise ValueError(value)
        self._value = value
        for position, button in self.targets.items():
            button.setAutoExclusive(False)
            button.setChecked(position == value)
            button.setAutoExclusive(True)
        if value in self.targets:
            self.setFocusProxy(self.targets[value])
            self.targets[value].setFocus(Qt.FocusReason.OtherFocusReason)

    def showEvent(self, event):
        super().showEvent(event)
        if self._value in self.targets:
            self.targets[self._value].setFocus(Qt.FocusReason.OtherFocusReason)

    def choose(self, value):
        self.set_value(value)
        self.placement_chosen.emit(value)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
