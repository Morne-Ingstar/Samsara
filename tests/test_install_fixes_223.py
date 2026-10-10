"""Focused regressions from the clean-laptop install report (prompt 223)."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QTableWidget

from samsara import config_defaults
from samsara.ui import command_marquee, theme
from samsara.ui.dictionary_panel_qt import DictionaryPanelQt, _ss
from samsara.ui.tutorial_qt import TutorialWindow


def _tutorial_app(mode):
    return SimpleNamespace(
        config={"mode": mode, "hotkey": "ctrl+shift", "wake_word_enabled": False},
        _tutorial_hooks={},
    )


@pytest.mark.parametrize("mode", ["hold", "toggle", "continuous"])
def test_practice_dictation_callback_enables_next_for_each_mode(qapp, mode):
    app = _tutorial_app(mode)
    window = TutorialWindow(app)
    window._step = 1
    window._show_step()

    app._tutorial_hooks["dictation"]("hello world")
    qapp.processEvents()

    assert window._next_btn.isEnabled()
    window.close()


@pytest.mark.parametrize('action', ['close', 'skip_tutorial', 'finish', 'skip_step', 'back'])
@pytest.mark.parametrize('step', [1, 2])
def test_tutorial_releases_hooks_when_step_or_window_ends(qapp, action, step):
    app = _tutorial_app('hold')
    window = TutorialWindow(app)
    window._step = step
    window._show_step()
    key = 'dictation' if step == 1 else 'command'
    callback = app._tutorial_hooks[key]
    getattr(window, {'close': 'close', 'skip_tutorial': '_skip_tutorial',
        'finish': '_finish', 'skip_step': '_skip_step', 'back': '_go_back'}[action])()
    assert app._tutorial_hooks.get(key) is not callback
    window.close()
    assert app._tutorial_hooks == {}
    window.deleteLater()
    qapp.processEvents()


def test_fresh_install_command_strip_has_a_visible_real_command(qapp, monkeypatch):
    monkeypatch.setattr(command_marquee.command_catalog, "guidance_catalog", lambda _rows: None)
    strip = command_marquee.build_example_strip(SimpleNamespace(command_registry=None))
    strip.resize(420, 44)
    qapp.processEvents()

    assert strip.phrase == command_marquee.FRESH_INSTALL_FALLBACK
    assert strip.text == 'Try saying "what can I say"'
    assert strip.label.palette().color(strip.label.foregroundRole()).isValid()
    strip.close()


@pytest.mark.parametrize("palette", ["dark", "light"])
def test_dictionary_items_use_readable_theme_tokens(qapp, palette):
    previous = theme.active_theme()
    theme.set_theme(palette, refresh=False)
    panel = DictionaryPanelQt.__new__(DictionaryPanelQt)
    table = QTableWidget(0, 3)
    try:
        panel._kv_insert_row(table, "heard", "wake word", "user")
        panel._kv_insert_row(table, "seed", "wake word", "default")
        for row in range(table.rowCount()):
            for col in range(table.columnCount()):
                assert table.item(row, col).data(Qt.ItemDataRole.ForegroundRole) is None
        assert f"color:{theme.TEXT_PRIMARY}" in _ss()
    finally:
        theme.set_theme(previous, refresh=False)


def test_new_install_defaults_keep_tts_opt_in_and_use_edge_ava():
    assert config_defaults.DEFAULTS["tts.enabled"] is False
    assert config_defaults.DEFAULTS["tts.engine"] == "edge"
    assert config_defaults.DEFAULTS["tts.voice_id"] == "en-US-AvaNeural"
    assert config_defaults.DEFAULTS["alarms.enabled"] is False


def test_settings_footer_buttons_size_to_their_text(qapp):
    from tests._theme_stub_app import build_settings_window

    window = build_settings_window()
    window.resize(1100, 760)
    window.layout().activate()
    for label in ("Close", "Apply && Close"):
        button = next(btn for btn in window.findChildren(QPushButton) if btn.text() == label)
        assert button.minimumWidth() >= button.sizeHint().width()
        assert button.contentsRect().width() >= button.fontMetrics().horizontalAdvance(label)
    window.close()
