"""Nine-step setup treatment built on the Ava guide's list-semantic strip."""
from PySide6.QtCore import QLineF, Qt
from PySide6.QtGui import QPen
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

    def _layout_spacing(self):
        return 4 * self.ui_scale, theme.SPACE_2 * self.ui_scale, theme.SPACE_2 * self.ui_scale
