"""Focused regressions from the clean-laptop install report (prompt 223)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QPushButton, QTableWidget

from samsara import config_defaults
from samsara.ui import command_marquee, theme
from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
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


def test_hold_toggle_dispatcher_notifies_tutorial_before_returning():
    source = Path("dictation.py").read_text(encoding="utf-8")
    # Continuous already had both notifications. The hotkey final path must
    # now contain one command and one normal-dictation notification as well.
    assert source.count("_tutorial_hooks.pop('command', None)") >= 2
    assert source.count("_tutorial_hooks.pop('dictation', None)") >= 2


def test_fresh_install_command_strip_has_a_visible_real_command(qapp, monkeypatch):
    monkeypatch.setattr(command_marquee.command_catalog, "guidance_catalog", lambda _rows: None)
    strip = command_marquee.build_example_strip(SimpleNamespace(command_registry=None))
    strip.resize(420, 44)
    qapp.processEvents()

    assert strip.phrase == command_marquee.FRESH_INSTALL_FALLBACK
    assert strip.text == 'Try saying "what can I say"'
    assert strip.label.palette().color(strip.label.foregroundRole()).isValid()
    strip.close()


def test_dictionary_items_use_readable_theme_tokens(qapp):
    panel = DictionaryPanelQt.__new__(DictionaryPanelQt)
    table = QTableWidget(0, 3)
    panel._kv_insert_row(table, "heard", "wake word", "user")
    assert table.item(0, 0).foreground().color() == QColor(theme.TEXT_PRIMARY)
    panel._kv_insert_row(table, "seed", "wake word", "default")
    assert table.item(1, 0).foreground().color() == QColor(theme.TEXT_SECONDARY)


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
