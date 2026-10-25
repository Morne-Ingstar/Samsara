"""870: measured rings and unclipped learning/setup strips at accessible scales."""
from math import hypot

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFontMetrics, QImage, QPainter

from samsara.ui import theme
from samsara.ui.widgets.step_indicator import StepIndicator
from samsara.ui.widgets.step_strip import StepStrip


@pytest.mark.parametrize('display_scale', [1.0, 1.25, 1.5])
@pytest.mark.parametrize('text_scale', [1.0, 1.5])
@pytest.mark.parametrize('kind', ['learning', 'setup', 'microphone'])
@pytest.mark.parametrize('current', [0, 3])
def test_marker_clearance_and_strip_fit(qapp, monkeypatch, display_scale, text_scale, kind, current):
    original = theme.qfont
    monkeypatch.setattr(theme, 'qfont', lambda px, *a, **kw: original(round(px * text_scale), *a, **kw))
    if kind == 'setup':
        from samsara.ui.first_run_wizard_qt import _STEPS
        indicator = StepStrip([name for name, _ in _STEPS])
        # Setup's real host caps the strip at 680 and reserves 28 on each side.
        indicator.setViewportMargins(28, 0, 28, 0)
        width = 680
    else:
        steps = (['Welcome', 'Dictation', 'Command', 'Ways', 'Show', 'Try', 'Done']
                 if kind == 'learning' else ['Device', 'Level', 'Wake word', 'Done'])
        indicator = StepIndicator(steps, variant='C' if kind == 'learning' else 'A')
        width = 900
    try:
        indicator.set_ui_scale(display_scale)
        indicator.resize(width, indicator.height())
        indicator.set_progress(current, skipped={1})
        indicator.show()
        qapp.processEvents()
        indicator.doItemsLayout()
        metrics = QFontMetrics(indicator.marker_font())
        viewport = QRectF(indicator.viewport().rect())
        circles = []
        for index in range(indicator.count()):
            circle, label = indicator.geometry_for(index)
            circles.append(circle)
            padding = indicator.marker_padding()
            assert circle.width() >= metrics.height() + 2 * padding
            for glyph in (str(index + 1), '✓', '–'):
                width = max(metrics.horizontalAdvance(glyph), metrics.boundingRect(glyph).width())
                height = max(metrics.height(), metrics.boundingRect(glyph).height())
                inner_radius = circle.width() / 2 - theme.TYPE_MIN * display_scale / 14
                assert inner_radius >= hypot(width / 2, height / 2) + padding
            assert viewport.contains(circle)
            assert viewport.contains(QRectF(indicator.visualItemRect(indicator.item(index))))
            if indicator.all_labels or index == current:
                assert viewport.contains(label)
                label_metrics = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * display_scale)))
                assert label.width() >= label_metrics.horizontalAdvance(indicator.steps[index])
                assert label.height() >= label_metrics.height()
        assert all(not a.intersects(b) for i, a in enumerate(circles) for b in circles[i + 1:])
        assert indicator.horizontalScrollBar().maximum() == 0
        assert indicator.verticalScrollBar().maximum() == 0
        # Exercise the actual delegate paint path with the tested geometry.
        image = QImage(indicator.size(), QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        indicator.render(painter, QPointF(0, 0).toPoint())
        painter.end()
        assert not image.isNull()
    finally:
        indicator.close()
        indicator.deleteLater()
