"""828d: measure the real hub's Vocabulary budget without live app state."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QHBoxLayout, QPushButton, QSizePolicy, QToolButton, QWidget

from samsara.ui import dictionary_panel_qt as dictionary, home_qt, main_window_qt

VOICE_HELP = (
    'Say: “add the word Siobhan to my vocabulary spelled S I O B H A N”. '
    'After the readback, say yes or no without a key. '
    'You can also say “learn the word Siobhan spelled S-I-O-B-H-A-N”, '
    '“correct that to Siobhan spelled S I O B H A N”, or '
    '“add the selected word to my vocabulary”. '
    'Works with Ava off. Say “add that to the dictionary” to edit your last dictation here.'
)


@pytest.fixture
def hub(qapp, monkeypatch, qt_widget_scope):
    values = {}

    class Settings:
        def __init__(self, *args):
            pass

        def value(self, key, default=None, type=None):
            return values.get(key, default)

        def setValue(self, key, value):
            values[key] = value

    store = SimpleNamespace(custom_vocab=['word ' + str(i) for i in range(40)],
                            get_builtin_words=lambda: [])
    monkeypatch.setattr(dictionary, 'QSettings', Settings)
    monkeypatch.setattr(dictionary, 'get_dictionary_store', lambda app: store)
    monkeypatch.setattr(dictionary.DictionaryPanelQt, '_kv_load', lambda *args: None)
    monkeypatch.setattr(home_qt, 'HomePage', lambda *args, **kwargs: QWidget())
    monkeypatch.setattr(main_window_qt._MainWindow, '_geometry_screens',
                        lambda self: ([(0, 0, 1920, 1040)], (0, 0, 1920, 1040)))
    app = SimpleNamespace(config={}, available_mics=[], update_config_and_save=lambda changes: True)
    window = main_window_qt._MainWindow(app)
    window._poll_timer.stop()
    window._activate('Dictionary')
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    with qt_widget_scope(window):
        window.show()
        qapp.processEvents()
        yield window, window._panel_cache['Dictionary'], values


@pytest.mark.parametrize('width,height,rows', [(900, 650, 8), (700, 500, 4)])
def test_vocabulary_height_budget(hub, qapp, width, height, rows):
    window, panel, _ = hub
    window.resize(width, height)
    for _ in range(10):
        qapp.processEvents()
    assert (window.width(), window.height()) == (width, height)
    view = panel._vocab_list
    assert view.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Expanding
    assert view.viewport().height() >= rows * view.sizeHintForRow(0)
    assert view.minimumHeight() >= 4 * view.sizeHintForRow(0)
    page = panel._tabs.widget(0)
    assert page.rect().contains(view.geometry())
    for button in page.findChildren(QPushButton):
        assert button.width() <= button.sizeHint().width() + 1


def test_builtin_controls_share_row_and_expander_stays_available(hub):
    _, panel, _ = hub
    layout = panel._tabs.widget(0).layout()
    rows = [layout.itemAt(i).layout() for i in range(layout.count())]
    assert any(isinstance(row, QHBoxLayout) and
               row.indexOf(panel._builtin_expander) >= 0 and
               row.indexOf(panel._builtin_enabled) >= 0 for row in rows)
    panel._builtin_enabled.blockSignals(True)
    panel._builtin_enabled.setChecked(False)
    panel._builtin_expander.click()
    assert panel._builtin_scroll.isVisibleTo(panel)
    assert panel._builtin_expander.isEnabled()


def test_voice_help_starts_collapsed_preserves_818_and_remembers(hub, qapp):
    _, panel, values = hub
    help_text = panel.findChild(QLabel, 'dictionaryVoiceHelp')
    assert help_text.text() == VOICE_HELP
    assert not panel._voice_expander.isChecked()
    assert not help_text.isVisibleTo(panel)
    assert panel._voice_expander.accessibleName() == 'How to add words by voice'
    panel._voice_expander.click()
    qapp.processEvents()
    assert help_text.isVisibleTo(panel)
    assert values['dictionary/voice_help_expanded'] is True
    second = dictionary.DictionaryPanelQt(panel._app)
    try:
        assert second._voice_expander.isChecked()
        assert not second.findChild(QLabel, 'dictionaryVoiceHelp').isHidden()
    finally:
        second.close()
        second.deleteLater()


@pytest.mark.parametrize('width,height', [(900, 650), (700, 500)])
def test_transfer_actions_fit_one_row_and_preserve_distinct_bundle_actions(hub, qapp, width, height):
    window, panel, _ = hub
    window.resize(width, height)
    for _ in range(10):
        qapp.processEvents()
    page = panel._tabs.widget(0)
    buttons = [b for b in page.findChildren(QPushButton) if b.text() in ('Remove Selected', 'Import', 'Export')]
    # 838b: identify the action independently of its visible dropdown glyph.
    more = next(b for b in page.findChildren(QToolButton)
                if b.accessibleName() == 'More dictionary actions')
    assert len(buttons) == 3
    row = next(page.layout().itemAt(i).layout() for i in range(page.layout().count())
               if page.layout().itemAt(i).layout() and page.layout().itemAt(i).layout().indexOf(more) >= 0)
    assert all(row.indexOf(button) >= 0 for button in buttons)
    controls = [*buttons, more]
    assert (window.width(), window.height()) == (width, height)
    assert all(button.geometry().top() <= more.geometry().center().y() <= button.geometry().bottom()
               for button in controls)
    assert all(page.rect().contains(button.geometry()) for button in controls)
    assert all(button.width() == button.sizeHint().width() for button in controls)
    assert all(button.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Fixed
               for button in controls)
    assert [action.text() for action in more.menu().actions()] == ['Export dictionary…', 'Import dictionary…']
