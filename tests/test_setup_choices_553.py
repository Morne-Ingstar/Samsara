"""Setup choice rows: real widgets, synthetic config, no app bootstrap."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QButtonGroup, QLabel, QPushButton, QVBoxLayout, QWidget

from samsara import config_defaults
from samsara.ui import first_run_pages_qt as pages, theme


class RowHost(QWidget):
    """Only the page owner's layout/config contract; no audio or workers."""
    def __init__(self, display):
        super().__init__()
        self._config = {}
        for key, value in config_defaults.DEFAULTS.items():
            pages.put(self._config, key, value)
        pages.put(self._config, 'ui.listening_display', display)
        pages.put(self._config, 'ui.live_surface.enabled', display == 'full')
        pages.put(self._config, 'tts.network_voice_accepted', True)
        pages.put(self._config, 'wake_word_enabled', True)
        pages.put(self._config, 'tts.feedback_level', 'brief')
        pages.put(self._config, 'ducking.recording_mode', 'pause')
        self._existing_config = {}
        self._stack = self
        self._hotkey_btns = {}
        self._step_indicator = QWidget(self)
        self._step_indicator.hide()
        self._model_group = QButtonGroup(self)
        self._listening_display_page = SimpleNamespace(group=QButtonGroup(self))
        self._next_btn = QPushButton(self)
        self._next_btn.hide()
        self.result = None
        self._decisions = pages.Decisions(self)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.setStyleSheet(theme.build_stylesheet())
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(28, 20, 28, 20)

    def _padded(self):
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_3)
        return body, layout

    def _go_next(self):
        pass

    def build(self, name):
        self._layout.addWidget(pages.label(name.title()))
        widget = getattr(self._decisions, name)()
        self._layout.addWidget(widget)
        return widget


@pytest.mark.parametrize('width', [1000, 800, 667], ids=['100pct', '125pct', '150pct'])
@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('display', ['compact', 'full'])
def test_selected_explanation_and_tick_stay_in_their_row(qapp, width, palette, display):
    previous = theme.active_theme()
    theme.set_theme(palette, refresh=False)
    try:
        for page_name, names in [('talking', ['mode', 'wake']), ('music', ['music_group']),
                                 ('voice', ['level', 'engine'])]:
            host = RowHost(display)
            host.build(page_name)
            host.resize(width, 640)
            host.show()
            qapp.processEvents()
            try:
                for name in names:
                    group = getattr(host._decisions, name)
                    # Voice engine is visible when feedback is not Silent.
                    if name == 'engine':
                        host._decisions.level.buttons()[1].click()
                    help_button, expanded = next((b, label) for b, label in host._decisions.details
                        if b.accessibleDescription() == group._segments.accessibleName())
                    assert help_button.focusPolicy() != Qt.FocusPolicy.NoFocus
                    assert help_button.accessibleName() != '?'
                    for button in group.buttons():
                        button.click()
                        for opened in (False, True, False):
                            if help_button.isChecked() != opened:
                                QTest.keyClick(help_button, Qt.Key.Key_Return)
                            qapp.processEvents()
                            explanations = [w for w in (group._description, expanded) if w.isVisibleTo(host)]
                            expected = expanded.text() if opened else group._description.text()
                            assert len(explanations) == bool(expected)
                            assert group._segments.combo.currentData() == button.property('_value')
                            assert sum(b.isChecked() for b in group.buttons()) == 1
                            # These are the rectangles used by the actual paint filter.
                            box, tick, text = group._segments.paint_rects(button)
                            assert box.contains(tick.adjusted(-1, -1, 1, 1))
                            assert QRectF(button.rect()).contains(box)
                            assert text.right() < tick.left()
                            assert text.width() >= button.fontMetrics().horizontalAdvance(button.text())
                            assert group._row.rect().contains(group._segments.geometry())
                            for other in group.buttons():
                                assert other.accessibleDescription()
                    help_button.setChecked(False)
                    question = next(w for w in group._row.findChildren(QLabel)
                                    if w.text() == group._segments.accessibleName())
                    if width == 1000:
                        assert question.width() >= question.fontMetrics().horizontalAdvance(question.text())
            finally:
                host.hide()
                host.deleteLater()
                qapp.processEvents()
    finally:
        theme.set_theme(previous, refresh=False)
