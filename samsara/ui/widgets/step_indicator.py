"""Accessible, non-interactive progress steps with three review treatments."""

from __future__ import annotations

from math import ceil, hypot

from PySide6.QtCore import QLineF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QListView, QListWidget, QListWidgetItem, QStyledItemDelegate

from samsara.ui import theme


def _upcoming_ink() -> QColor:
    background = theme.BG0
    disabled = QColor(*theme.composite(theme.TEXT_DISABLED, background))
    primary = QColor(*theme.composite(theme.TEXT_PRIMARY, background))
    lower, upper = 0.0, 1.0
    for _ in range(theme.TYPE_BODY):
        fraction = (lower + upper) / 2
        candidate = QColor(
            round(disabled.red() + (primary.red() - disabled.red()) * fraction),
            round(disabled.green() + (primary.green() - disabled.green()) * fraction),
            round(disabled.blue() + (primary.blue() - disabled.blue()) * fraction),
        )
        if min(theme.contrast_ratio(candidate.name(), surface) for surface in (theme.BG0, theme.BG1, theme.BG2)) >= 4.5:
            upper = fraction
        else:
            lower = fraction
    return QColor(
        round(disabled.red() + (primary.red() - disabled.red()) * upper),
        round(disabled.green() + (primary.green() - disabled.green()) * upper),
        round(disabled.blue() + (primary.blue() - disabled.blue()) * upper),
    )


class _StepDelegate(QStyledItemDelegate):
    def __init__(self, indicator: "StepIndicator") -> None:
        super().__init__(indicator)
        self.indicator = indicator

    def paint(self, painter: QPainter, option, index) -> None:
        indicator = self.indicator
        step = index.row()
        state = indicator.state(step)
        scale = indicator.ui_scale
        box = QRectF(option.rect)
        accent = theme.qcolor(theme.ACCENT)
        primary = theme.qcolor(theme.TEXT_PRIMARY)
        upcoming = _upcoming_ink()
        ink = accent if state == "current" else primary if state == "done" else upcoming
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if indicator.variant == "B":
            self._pill(painter, box, step, state, ink, accent, scale)
        elif indicator.variant == "C":
            self._track(painter, box, step, state, ink, accent, scale)
        else:
            self._circle(painter, box, step, state, ink, accent, scale)
        painter.restore()

    def _label(self, painter, box, step, state, color, scale, *, top) -> None:
        if not self.indicator.all_labels:
            return
        font = theme.qfont(round(theme.TYPE_MIN * scale))
        font.setWeight(QFont.Weight.DemiBold if state == "current" else QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(
            self.indicator.geometry_for(step)[1],
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            self.indicator.steps[step],
        )

    def _marker(self, painter, center, radius, step, state, accent, upcoming, scale) -> None:
        outline = accent if state in ("done", "current") else upcoming
        fill = accent if state == "done" else theme.qcolor(theme.BG0)
        painter.setBrush(fill)
        painter.setPen(QPen(outline, theme.TYPE_MIN * scale / 7))
        painter.drawEllipse(center, radius, radius)
        painter.setPen(theme.qcolor(theme.TEXT_ON_ACCENT) if state == "done" else outline)
        painter.setFont(self.indicator.marker_font())
        glyph = "✓" if state == "done" else "–" if state == "skipped" else str(step + 1)
        painter.drawText(
            QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2),
            Qt.AlignmentFlag.AlignCenter,
            glyph,
        )

    def _circle(self, painter, box, step, state, ink, accent, scale) -> None:
        circle, label = self.indicator.geometry_for(step)
        radius, center = circle.width() / 2, circle.center()
        gap = theme.TYPE_MIN * scale / 2
        pen_width = theme.TYPE_MIN * scale / 7
        if step:
            painter.setPen(QPen(accent if self.indicator.state(step - 1) == "done" else _upcoming_ink(), pen_width))
            painter.drawLine(QLineF(box.left(), center.y(), center.x() - radius - gap, center.y()))
        if step < len(self.indicator.steps) - 1:
            painter.setPen(QPen(accent if state == "done" else _upcoming_ink(), pen_width))
            painter.drawLine(QLineF(center.x() + radius + gap, center.y(), box.right(), center.y()))
        self._marker(painter, center, radius, step, state, accent, _upcoming_ink(), scale)
        self._label(painter, box, step, state, ink, scale, top=box.top() + theme.TYPE_TITLE * scale * 1.6)

    def _pill(self, painter, box, step, state, ink, accent, scale) -> None:
        inset = theme.TYPE_MIN * scale / 3
        pill = box.adjusted(inset, inset, -inset, -inset)
        painter.setPen(QPen(accent if state in ("done", "current") else _upcoming_ink(), theme.TYPE_MIN * scale / 7))
        painter.setBrush(accent if state == "current" else theme.qcolor(theme.BG2))
        painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.setPen(theme.qcolor(theme.TEXT_ON_ACCENT) if state == "current" else ink)
        painter.setFont(theme.qfont(round(theme.TYPE_MIN * scale), weight=QFont.Weight.DemiBold))
        prefix = "✓" if state == "done" else "–" if state == "skipped" else str(step + 1)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, f"{prefix}  {self.indicator.steps[step]}")

    def _track(self, painter, box, step, state, ink, accent, scale) -> None:
        circle, label = self.indicator.geometry_for(step)
        center, radius = circle.center(), circle.width() / 2
        painter.setPen(QPen(accent if state == "done" else _upcoming_ink(), theme.TYPE_MIN * scale / 7))
        if step:
            painter.drawLine(QLineF(box.left(), center.y(), center.x() - radius, center.y()))
        if step < len(self.indicator.steps) - 1:
            painter.drawLine(QLineF(center.x() + radius, center.y(), box.right(), center.y()))
        self._marker(painter, center, radius, step, state, accent, _upcoming_ink(), scale)
        self._label(painter, box, step, state, ink, scale, top=box.top() + theme.TYPE_TITLE * scale)
        if state == "current":
            pen_width = theme.TYPE_MIN * scale / 4
            painter.setPen(QPen(accent, pen_width))
            margin = theme.TYPE_TITLE * scale
            bottom = box.bottom() - pen_width / 2
            painter.drawLine(QLineF(box.left() + margin, bottom, box.right() - margin, bottom))


class StepIndicator(QListWidget):
    """A list-semantic progress indicator; steps are not keyboard actions."""

    def __init__(self, steps: list[str], parent=None, *, variant: str = "A") -> None:
        if not steps:
            raise ValueError("StepIndicator needs at least one step")
        if variant not in {"A", "B", "C"}:
            raise ValueError("Unknown step indicator variant")
        super().__init__(parent)
        self.steps = list(steps)
        self.variant = variant
        self.ui_scale = 1.0
        self._current = 0
        self._done: set[int] = set()
        self._skipped: set[int] = set()
        self.setAccessibleName("Progress steps")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(_StepDelegate(self))
        for step in self.steps:
            item = QListWidgetItem(step)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.addItem(item)
        self._refresh()

    def state(self, index: int) -> str:
        if index in self._done:
            return "done"
        if index in self._skipped:
            return "skipped"
        if index == self._current:
            return "current"
        return "upcoming"

    def set_current(self, index: int) -> None:
        self._check_index(index)
        self._current = index
        self._done.discard(index)
        self._skipped.discard(index)
        self._refresh()

    def set_done(self, index: int) -> None:
        self._check_index(index)
        self._done.add(index)
        self._skipped.discard(index)
        self._refresh()

    def set_skipped(self, index: int) -> None:
        self._check_index(index)
        self._skipped.add(index)
        self._done.discard(index)
        self._refresh()

    def set_progress(self, index: int, skipped=()) -> None:
        self._check_index(index)
        self._current = index
        self._skipped = {step for step in skipped if 0 <= step < index}
        self._done = set(range(index)) - self._skipped
        self._refresh()

    def set_ui_scale(self, scale: float) -> None:
        if scale <= 0:
            raise ValueError("Scale must be positive")
        self.ui_scale = scale
        self._refresh()

    def _check_index(self, index: int) -> None:
        if not 0 <= index < len(self.steps):
            raise IndexError(index)

    def _refresh(self) -> None:
        label_height = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * self.ui_scale))).height()
        height = max(round(theme.HIT_TARGET_MIN * self.ui_scale),
                     round(theme.TYPE_TITLE * self.ui_scale * 1.6) + label_height + theme.SPACE_1)
        self.setMinimumHeight(height)
        self.setFixedHeight(height)
        self.setStyleSheet(f"QListWidget{{background:{theme.BG0};border:none;}}")
        for index, step in enumerate(self.steps):
            item = self.item(index)
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"Step {index + 1} of {len(self.steps)}, {step}, {self.state(index)}",
            )
        self._size_items()
        self.viewport().update()

    def marker_font(self):
        return theme.qfont(round(theme.TYPE_MIN * self.ui_scale), weight=QFont.Weight.DemiBold)

    def marker_padding(self) -> float:
        return theme.SPACE_1 * self.ui_scale

    def marker_diameter(self) -> int:
        metrics = QFontMetrics(self.marker_font())
        glyphs = [str(index + 1) for index in range(self.count())] + ["✓", "–"]
        width = max(max(metrics.horizontalAdvance(glyph), metrics.boundingRect(glyph).width())
                    for glyph in glyphs)
        height = max(metrics.height(), *(metrics.boundingRect(glyph).height() for glyph in glyphs))
        # A square text box needs its diagonal inside the ring, not just its height.
        return ceil(hypot(width, height) + 2 * self.marker_padding()
                    + theme.TYPE_MIN * self.ui_scale / 7)

    def geometry_for(self, index):
        box = QRectF(self.visualItemRect(self.item(index)))
        diameter = self.marker_diameter()
        padding = self.marker_padding()
        top, gap, _ = self._layout_spacing()
        circle = QRectF(box.center().x() - diameter / 2, box.top() + top,
                        diameter, diameter)
        metrics = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * self.ui_scale)))
        label = QRectF(box.left(), circle.bottom() + gap, box.width(), metrics.height())
        if not self.all_labels:
            width = metrics.horizontalAdvance(self.steps[index]) + 2 * padding
            left = max(0, min(box.center().x() - width / 2, self.viewport().width() - width))
            label.setRect(left, label.top(), width, label.height())
        return circle, label

    def _layout_spacing(self):
        # Keep the measured ring roomy without consuming the compact page's height.
        margin = self.marker_padding() / 2
        return margin, margin, margin

    def _size_items(self) -> None:
        width = max(1, self.viewport().width())
        padding = self.marker_padding()
        columns = max(1, min(self.count(), int(width // (self.marker_diameter() + 2 * padding))))
        item_width = width // columns
        metrics = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * self.ui_scale)))
        self.all_labels = all(metrics.horizontalAdvance(step) + padding <= width // self.count()
                              for step in self.steps)
        self._cell_height = ceil(self.marker_diameter() + metrics.height() + sum(self._layout_spacing()))
        height = max(round(theme.HIT_TARGET_MIN * self.ui_scale),
                     self._cell_height * ceil(self.count() / columns))
        self.setWrapping(columns < self.count())
        self.setFixedHeight(height)
        for index in range(len(self.steps)):
            self.item(index).setSizeHint(QSize(item_width, self._cell_height))
            self.item(index).setToolTip(self.steps[index])

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self.all_labels and self.variant != "B":
            painter = QPainter(self.viewport())
            painter.setFont(theme.qfont(round(theme.TYPE_MIN * self.ui_scale), weight=QFont.Weight.DemiBold))
            painter.setPen(theme.qcolor(theme.ACCENT))
            painter.drawText(self.geometry_for(self._current)[1], Qt.AlignmentFlag.AlignCenter,
                             self.steps[self._current])
            painter.end()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._size_items()
