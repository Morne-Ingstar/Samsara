"""Compact dictation-result painting, including native History links."""
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainterPath

from samsara.consent_feedback import compact_max_lines
from samsara.ui import theme
from samsara.ui.indicator_message_layout import message_lines, message_line_spacing
from samsara.ui.result_message_rendering import is_ava_chain_result, paint_result


def paint_chip(self, painter, rect, label, kind, chip_fill, padding):
    radius = min(13, rect.height() / 2.0)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    running = getattr(getattr(self._chip_feedback, 'kind', None), 'value', '') == 'running'
    fill = theme.AVA if is_ava_chain_result(label) else theme.WARNING if running else theme.TEXT_SECONDARY if kind == 'accent' else chip_fill().get(kind, theme.WARNING)
    history = getattr(self._chip_feedback, 'history', None)
    if history:
        fill = theme.BG1
    if kind in ('error', 'warning'):
        fill = theme._mix(chip_fill()[kind], theme.BG0, .80)
    painter.fillPath(path, theme.qcolor(fill))
    if history:
        return  # The message hit window paints the native keyboard/mouse link.
    font = self._chip_font()
    lines, _ = message_lines(label, font, rect.width()-2*padding-self._report_width(), max_lines=compact_max_lines(self))
    height = message_line_spacing(label, font)
    for index, line in enumerate(lines):
        line_rect = QRectF(rect.x()+padding, rect.center().y()-len(lines)*height/2+index*height,
                           rect.width()-2*padding-self._report_width(), height)
        ink = QColor(theme.ink_on(fill) if kind in ('error', 'warning') or is_ava_chain_result(label) else theme.TEXT_ON_ACCENT)
        if index and getattr(self._chip_feedback, 'owner', '') == 'capture_clipping':
            ink.setAlphaF(.72)
        paint_result(painter, line_rect, line, font, ink)
