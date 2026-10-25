"""844: command activation follows live settings, never sample/default bindings."""
from types import SimpleNamespace

import pytest

from samsara.ui import command_cheatsheet_qt as cs
from tests.test_home_qt import _app, _hub, _isolated, _native_home_font  # noqa: F401


def config(**updates):
    return dict(hotkey='right_ctrl', command_hotkey='ctrl+alt+k',
                wake_word_enabled=True, wake_word_config={'phrase': 'jarvis'}, **updates)


@pytest.mark.parametrize('binding,label', [
    ('ctrl+alt+k', 'Ctrl+Alt+K'), ('right_alt', 'Right Alt'), ('mouse4', 'Mouse 4'),
])
def test_hold_names_command_key_instead_of_dictation_key(binding, label):
    cfg = config()
    cfg.update(command_hotkey=binding, wake_word_enabled=False)
    assert cs.command_instruction(cfg) == f'Hold {label} to speak commands.'
    assert 'Right Ctrl' not in cs.command_instruction(cfg)


def test_wake_names_configured_phrase_and_command_key():
    assert cs.command_instruction(config()) == (
        'Say "jarvis" then speak a command, or hold Ctrl+Alt+K to speak commands.')


@pytest.mark.parametrize('wake,key,expected', [
    (False, '', 'Set up a command key or wake word in Settings to speak commands.'),
    (False, None, 'Set up a command key or wake word in Settings to speak commands.'),
    (True, '', 'Say "jarvis" then speak a command.'),
    (True, None, 'Say "jarvis" then speak a command.'),
])
def test_disabled_activation_is_omitted_and_hint_never_empty(wake, key, expected):
    cfg = config()
    cfg.update(wake_word_enabled=wake, command_hotkey=key)
    assert cs.command_instruction(cfg) == expected


@pytest.mark.parametrize('phrase', [None, '', '   '])
def test_missing_phrase_never_falls_back_to_default(phrase):
    cfg = config()
    cfg['wake_word_config'] = {'phrase': phrase}
    assert cs.command_instruction(cfg) == 'Hold Ctrl+Alt+K to speak commands.'


def test_hands_free_needs_no_key():
    assert cs.command_instruction(config(), SimpleNamespace(command_mode_active=True)) == (
        'Hands-free is on. Speak commands without a key.')


def test_standalone_timer_follows_replaced_live_config(qapp, tmp_path, monkeypatch, qt_widget_scope):
    current = config()
    monkeypatch.setattr(cs, '_disabled_packs', lambda *args: set())
    with qt_widget_scope(cs._CheatSheetWindow(
            lambda _: None, lambda: [], tmp_path / 'palette.json',
            config_cb=lambda: current)) as panel:
        assert '"jarvis"' in panel._instruction.text()
        current = dict(command_hotkey='shift+f8', wake_word_enabled=True,
                       wake_word_config={'phrase': 'hello atlas'})
        panel._instruction_timer.timeout.emit()
        assert panel._instruction.text() == (
            'Say "hello atlas" then speak a command, or hold Shift+F8 to speak commands.')
        current.update(wake_word_enabled=False, command_hotkey='')
        panel._instruction_timer.timeout.emit()
        assert panel._instruction.text()
        assert 'hello atlas' not in panel._instruction.text()
        assert 'Shift+F8' not in panel._instruction.text()


def test_hub_mode_poll_refreshes_same_source_as_home(qapp, tmp_path, monkeypatch, qt_widget_scope):
    app = _app()
    app.config.update(config())
    app.cheat_sheet = SimpleNamespace(_palette_path=tmp_path / 'palette.json')
    monkeypatch.setattr(cs, '_disabled_packs', lambda *args: set())
    with qt_widget_scope(_hub(qapp, monkeypatch, app=app)) as host:
        host._nav_btns['Commands'].click()
        panel = host._stack.currentWidget()
        assert not panel._instruction_timer.isActive()
        assert '"jarvis"' in panel._instruction.text()
        app.config.update(command_hotkey='alt+f9', wake_word_config={'phrase': 'hey nova'})
        host._poll_timer.timeout.emit()
        assert panel._instruction.text() == (
            'Say "hey nova" then speak a command, or hold Alt+F9 to speak commands.')
        app.command_mode_active = True
        host._poll_timer.timeout.emit()
        assert panel._instruction.text() == 'Hands-free is on. Speak commands without a key.'
        app.command_mode_active = False
        app.wake_word_active = False
        host._poll_timer.timeout.emit()
        assert panel._instruction.text() == 'Hold Alt+F9 to speak commands.'
