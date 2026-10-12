"""Config-free, mouse/keyboard listening display placement choice."""
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QPushButton

from samsara.ui import theme
from samsara.ui.widgets.indicator_demo import (CAPTIONS, DesktopSketch, IndicatorDemo, POSITIONS,
                                               log_paint_failure_once)

DRAG_CAPTION = "You can also drag the real display anywhere."


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
        try:
            self._paint(painter)
        except Exception:
            log_paint_failure_once(self, "placement target")
        finally:
            painter.end()

    def _paint(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QRectF(3, 6, self.width()-6, self.height()-12)
        selected = self.isChecked()
        ink = theme.qcolor(theme.ACCENT)
        radius = theme.CARD_RADIUS if self.owner.display == "full" else shape.height()/2
        if selected:
            desktop = self.owner.desktop
            width, height = desktop.indicator_size()
            scale = desktop.preview_scale
            painter.save()
            try:
                painter.translate((self.width()-width*scale)/2, (self.height()-height*scale)/2)
                painter.scale(scale, scale)
                desktop.paint_indicator(painter, QRectF(0, 0, width, height))
            finally:
                painter.restore()
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor(theme.BORDER), 1))
            painter.drawRoundedRect(shape, radius, radius)
            from samsara.ui.tray_qt import paint_mark
            paint_mark(painter, QRectF(9, 13, 18, 18), "idle", "asleep", 0, 1)
            painter.setPen(QPen(ink, 2))
            painter.drawLine(34, 17, self.width()-16, 17)
            if self.owner.display == "full":
                painter.setPen(QPen(ink, 1))
                painter.drawLine(34, 27, self.width()-25, 27)
        # 515's property is optional until its focus policy lands on master.
        keyboard_focus = self.property('keyboardFocus')
        if self.hasFocus() and (keyboard_focus is None or keyboard_focus):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.qcolor(theme.ACCENT), 2))
            painter.drawRoundedRect(QRectF(1, 1, self.width()-2, self.height()-2), radius, radius)

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
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
        else:
            super().keyPressEvent(event)


class _PlacementDesktop(DesktopSketch):
    preview_scale = .65

    def target_size(self):
        return 146, 68 if self.display == 'full' else 48

    def indicator_rect(self):
        value = self.parentWidget().value()
        if value not in POSITIONS:
            return QRectF()
        target = self.target_rect(value, *self.target_size())
        width, height = self.indicator_size()
        width, height = width*self.preview_scale, height*self.preview_scale
        return QRectF(target.center().x()-width/2, target.center().y()-height/2, width, height)

    def target_rect(self, position, width=44, height=44):
        # Keep all six 44px controls disjoint in the shorter onboarding sketch.
        area = self.work_rect().adjusted(2, 2, -2, -2)
        vertical, horizontal = position.split('-')
        x = {'left': area.left(), 'center': area.center().x()-width/2,
             'right': area.right()-width}[horizontal]
        return QRectF(x, area.top() if vertical == 'top' else area.bottom()-height, width, height)

    def place_targets(self):
        if hasattr(self, "targets"):
            for value, button in self.targets.items():
                button.setGeometry(self.target_rect(value, *self.target_size()).toRect())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.place_targets()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self.paint_desktop(painter)
        except Exception:
            log_paint_failure_once(self, "placement desktop")
        finally:
            painter.end()


class IndicatorPlacePicker(IndicatorDemo):
    """Six native button roles, one persisted-value-shaped output signal."""
    placement_chosen = Signal(str)

    def __init__(self, value="bottom-left", parent=None, *, display="full", reduced_motion=None):
        super().__init__(parent, display=display, reduced_motion=reduced_motion,
                         desktop_type=_PlacementDesktop, manual_steps=False)
        self.display = display
        # PLACEHOLDER COPY (490)
        self.setAccessibleName("Listening display placement")
        self.targets = {position: _Target(position, self, self.desktop) for position in POSITIONS}
        self.desktop.targets = self.targets
        self.hint = self.caption
        self.set_value(value)

    def caption_for_beat(self, beat):
        return CAPTIONS[1] if self.reduced_motion else CAPTIONS[beat] if beat in (1, 2, 3) else DRAG_CAPTION

    def set_frame(self, beat, phase=1.0):
        super().set_frame(1 if self.reduced_motion else beat, phase)
        for button in getattr(self, 'targets', {}).values():
            button.update()

    def set_display(self, display):
        if display not in ('full', 'compact'):
            raise ValueError(display)
        self.display = self.desktop.display = display
        self.desktop.setAccessibleName(f'{display.title()} listening display illustration')
        self.desktop.place_targets()
        self.set_frame(0)

    def value(self):
        return self._value

    def set_value(self, value):
        # A custom saved position is not silently relabelled as a preset.
        value = value if value in (*POSITIONS, "custom") else None
        self._value = value
        self.setFocusProxy(None)
        for position, button in self.targets.items():
            button.setAutoExclusive(False)
            button.setChecked(position == value)
            button.setAutoExclusive(True)
        if value in self.targets:
            self.setFocusProxy(self.targets[value])
            self.targets[value].setFocus(Qt.FocusReason.OtherFocusReason)
        self.desktop.update()
        for button in self.targets.values():
            button.update()

    def showEvent(self, event):
        super().showEvent(event)
        if self._value in self.targets:
            self.targets[self._value].setFocus(Qt.FocusReason.OtherFocusReason)

    def choose(self, value):
        self.set_value(value)
        self.placement_chosen.emit(value)
