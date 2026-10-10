"""Nine-step setup treatment built on the Ava guide's list-semantic strip."""
from PySide6.QtCore import QLineF, QRectF, QSize, Qt
from PySide6.QtGui import QFontMetrics, QPainter, QPen
from samsara.ui import theme
from samsara.ui.widgets.step_indicator import StepIndicator, _StepDelegate, _upcoming_ink


class _StripDelegate(_StepDelegate):
    def _circle(self, painter, box, step, state, ink, accent, scale):
        strip = self.indicator
        circle, label = strip.geometry_for(step)
        center, radius = circle.center(), circle.width() / 2
        if step:
            painter.setPen(QPen(accent if strip.state(step - 1) == 'done' else _upcoming_ink(), 2 * scale))
            painter.drawLine(QLineF(box.left(), center.y(), circle.left() - 4 * scale, center.y()))
        if step < strip.count() - 1:
            painter.setPen(QPen(accent if state == 'done' else _upcoming_ink(), 2 * scale))
            painter.drawLine(QLineF(circle.right() + 4 * scale, center.y(), box.right(), center.y()))
        self._marker(painter, center, radius, step, state, accent, _upcoming_ink(), scale)
        if strip.all_labels:
            painter.setFont(theme.qfont(round(theme.TYPE_MIN * scale)))
            painter.setPen(ink)
            painter.drawText(label, Qt.AlignmentFlag.AlignCenter, strip.steps[step])


class StepStrip(StepIndicator):
    """Keep every circle; show just the current label when nine labels cannot fit."""
    def __init__(self, steps, parent=None):
        super().__init__(steps, parent)
        self.setItemDelegate(_StripDelegate(self))

    def geometry_for(self, index):
        box = QRectF(self.visualItemRect(self.item(index)))
        scale = self.ui_scale
        diameter = round(theme.TYPE_BODY * 1.52 * scale)
        circle = QRectF(box.center().x() - diameter / 2, 4 * scale, diameter, diameter)
        metrics = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * scale)))
        label = QRectF(box.left(), circle.bottom() + theme.SPACE_2 * scale,
                       box.width(), metrics.height())
        if not self.all_labels:
            width = metrics.horizontalAdvance(self.steps[index]) + theme.SPACE_2 * scale
            left = max(0, min(box.center().x() - width / 2, self.viewport().width() - width))
            label.setRect(left, label.top(), width, label.height())
        return circle, label

    def _size_items(self):
        scale = self.ui_scale
        width = max(1, self.viewport().width())
        metrics = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * scale)))
        item_width = width // self.count()
        self.all_labels = all(metrics.horizontalAdvance(s) + theme.SPACE_1 * scale <= item_width
                              for s in self.steps)
        height = round(4 * scale + theme.TYPE_BODY * 1.52 * scale
                       + theme.SPACE_2 * scale + metrics.height() + theme.SPACE_2 * scale)
        self.setFixedHeight(height)
        for index in range(self.count()):
            self.item(index).setSizeHint(QSize(item_width, height))
            self.item(index).setToolTip(self.steps[index])

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.all_labels:
            # Paint the wider current label outside the individual delegate cell.
            painter = QPainter(self.viewport())
            painter.setFont(theme.qfont(round(theme.TYPE_MIN * self.ui_scale)))
            painter.setPen(theme.qcolor(theme.ACCENT))
            _, label = self.geometry_for(self._current)
            painter.drawText(label, Qt.AlignmentFlag.AlignCenter, self.steps[self._current])
            painter.end()
