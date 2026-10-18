"""Real-font Full display controls retain their complete labels and padding."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QStyle, QStyleOptionButton

from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import LiveSurfaceModel, Lane
from samsara.ui import theme
from samsara.ui.live_surface_qt import LiveSurfaceWidget


@pytest.mark.parametrize('palette', ['dark', 'light'])
def test_clear_label_and_padding_with_runtime_font(qapp, monkeypatch, qt_widget_scope, palette):
    old_theme, old_sheet = theme.active_theme(), qapp.styleSheet()
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    theme.set_theme(palette, refresh=False)
    # qt_runtime creates QApplication without a global stylesheet. Preserve
    # that native Windows font, then let Full apply its own local QSS.
    qapp.setStyleSheet('')
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}})
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(),
        widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True))
    controller.start()
    try:
        with qt_widget_scope(controller.widget, close=controller.close):
            controller.begin_capture(Lane.HOLD)
            LiveSurfaceIndicatorAdapter(controller).show_outcome('Synthetic result', 'success', None)
            controller.drain()
            widget = controller.widget
            widget.show(); widget.ensurePolished(); qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
            button = widget._clear
            option = QStyleOptionButton(); button.initStyleOption(option)
            label_rect = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
            text_width = button.fontMetrics().horizontalAdvance(button.text())
            required = max(44, text_width + 2 * theme.SPACE_2)
            metrics = dict(palette=palette, dpr=widget.devicePixelRatioF(), font=button.font().family(),
                resolved_font=QFontInfo(button.font()).family(), pixel_size=button.font().pixelSize(),
                text=button.text(), text_width=text_width, box_width=button.width(),
                label_width=label_rect.width(), padding=theme.SPACE_2, required=required)
            # Optional evidence outputs are provided by the external proof harness.
            if os.environ.get('PROOF_466_OUT'):
                Path(os.environ['PROOF_466_OUT'], f'{palette}.json').write_text(json.dumps(metrics, indent=2))
                images = Path(os.environ['PROOF_466_IMAGES']); images.mkdir(parents=True, exist_ok=True)
                widget.grab().save(str(images / f"Full_{os.environ['PROOF_466_MODE']}_{palette}_{round(widget.devicePixelRatioF()*100)}.png"))
            print(metrics)
            assert button.isVisible() and button.text() == 'Clear draft'
            assert QFontInfo(button.font()).family() == QFontInfo(theme.qfont(theme.TYPE_MIN)).family(), metrics
            assert button.width() >= required, metrics
            assert label_rect.width() >= text_width, metrics
            assert button.fontMetrics().elidedText(button.text(), Qt.TextElideMode.ElideRight, label_rect.width()) == button.text()
            # 658's approved action row uses the compact 26px button family.
            assert button.height() >= 26
            assert button.height() >= button.fontMetrics().height() + 6
    finally:
        qapp.setStyleSheet(old_sheet); theme.set_theme(old_theme, refresh=False)


def test_latest_counter_uses_the_same_text_padding_rule(qapp, qt_widget_scope):
    from samsara.live_surface.model import (CaptureState, DocumentState, Notice,
        NoticeKind, PresentationState, SurfaceView, VisibleForm)
    view = SurfaceView(VisibleForm.LIVE, CaptureState.RECORDING, None,
        DocumentState.EDITABLE_DRAFT, PresentationState.COMPACT,
        Notice(NoticeKind.NONE, '', None), 'synthetic', 1, 'Synthetic draft', '', True)
    with qt_widget_scope(LiveSurfaceWidget(view, reduced_motion=True)) as widget:
        widget._follow_latest = False
        widget._unread_segments = 12
        widget.show(); widget._layout_children(); qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
        button = widget._latest
        assert button.isVisible() and button.text() == 'Latest (+12)'
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 2 * theme.SPACE_2
        assert not button.geometry().intersects(widget._clear.geometry())
        assert widget.rect().contains(button.geometry())


def test_parked_draft_text_controls_fit_the_effective_font(qapp, qt_widget_scope):
    from samsara.live_surface.model import (CaptureState, DocumentState, Notice,
        NoticeKind, PresentationState, SurfaceView, VisibleForm)
    old_sheet = qapp.styleSheet()
    qapp.setStyleSheet(theme.build_stylesheet())
    view = SurfaceView(VisibleForm.DRAFT_BADGE, CaptureState.OFF, None,
        DocumentState.PARKED_DRAFT, PresentationState.COMPACT,
        Notice(NoticeKind.NONE, '', None), 'synthetic', 1, 'Synthetic draft', '', True)
    try:
        with qt_widget_scope(LiveSurfaceWidget(view, reduced_motion=True)) as widget:
            widget.show(); qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 5)
            for button in (widget._clear, widget._insert):
                option = QStyleOptionButton(); button.initStyleOption(option)
                contents = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
                assert button.isVisible()
                assert button.width() >= max(44, button.fontMetrics().horizontalAdvance(button.text()) + 2 * theme.SPACE_2)
                assert contents.width() >= button.fontMetrics().horizontalAdvance(button.text())
                assert widget.rect().contains(button.geometry())
            assert not widget._clear.geometry().intersects(widget._state.geometry())
            assert not widget._insert.geometry().intersects(widget._badge.geometry())
    finally:
        qapp.setStyleSheet(old_sheet)
