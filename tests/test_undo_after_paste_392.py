"""392 paste undo: fake app/window inputs, no dictation bootstrap or clipboard."""
import ast
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import paste


@pytest.fixture(scope='module')
def head_delivery():
    """Compile the exact HEAD delivery function without app bootstrap."""
    source = subprocess.run(
        ['git', 'show', 'HEAD:samsara/paste.py'], cwd=Path(__file__).resolve().parents[1],
        check=True, capture_output=True, text=True, encoding='utf-8',
    ).stdout
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                and n.name == 'paste_preserving_clipboard')
    namespace = dict(vars(paste))
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<HEAD paste delivery>', 'exec'), namespace)
    return namespace['paste_preserving_clipboard']


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'notepad.exe')
    return SimpleNamespace(
        config={'clipboard_delay': 0.23},
        _foreground_wants_typed_injection=Mock(return_value=False),
        _flight_foreground_process_name=Mock(return_value=None),
        _record_undoable_paste=Mock(),
        adaptive_learner=SimpleNamespace(record_transcription=Mock()),
        play_sound=Mock(), _show_outcome_chip=Mock(),
        _last_dictation_text='synthetic test sentence', _last_dictation_hwnd=101,
        _last_dictation_length=23, _undo_timer=None,
    )


def deliver(app, *, before=101, after=101, ok=True, capture=True, function=None):
    foreground = Mock(side_effect=[before, after])
    def central(text, **kwargs):
        if capture:
            assert kwargs['before_paste']() is True
        return ok
    return (function or paste.paste_preserving_clipboard)(
        app, 'synthetic test sentence', return_delivery_confirmation=True,
        paste_with_preservation_fn=central, type_text_unicode_fn=Mock(),
        get_foreground_hwnd_fn=foreground,
    )


def test_same_window_arms_without_confirming_or_learning(app, head_delivery):
    baseline = deliver(app, function=head_delivery)
    app._record_undoable_paste.reset_mock()
    assert deliver(app) == baseline == (True, False)
    app._record_undoable_paste.assert_called_once_with('synthetic test sentence', target_hwnd=101)
    app.adaptive_learner.record_transcription.assert_not_called()


@pytest.mark.parametrize('before,after,capture,reason', [
    (101, 202, True, 'foreground changed'),
    (None, None, True, 'no target window'),
    (0, 0, True, 'no target window'),
    (101, 101, False, 'no target window'),
])
def test_focus_conditions_refuse_once_at_info(app, caplog, before, after, capture, reason):
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert deliver(app, before=before, after=after, capture=capture) == (True, False)
    app._record_undoable_paste.assert_not_called()
    records = [r for r in caplog.records if '[PASTE] undo not armed:' in r.message]
    assert len(records) == 1 and records[0].levelno == logging.INFO
    assert reason in records[0].message


@pytest.mark.parametrize('process', [
    'WindowsTerminal.exe', 'cmd.exe', 'powershell.exe', 'pwsh.exe', 'conhost.exe',
    'warp.exe', 'wezterm-gui.exe', 'alacritty.exe', 'mintty.exe', 'CMD.EXE',
])
def test_terminals_do_not_arm(app, monkeypatch, caplog, process):
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: process)
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert deliver(app) == (True, False)
    app._record_undoable_paste.assert_not_called()
    assert 'undo not armed: terminal' in caplog.text


@pytest.mark.parametrize('process', [None, '', 'notepad.exe'])
def test_unknown_and_editor_processes_arm(app, monkeypatch, process):
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: process)
    assert deliver(app) == (True, False)
    app._record_undoable_paste.assert_called_once_with('synthetic test sentence', target_hwnd=101)


def test_failed_paste_keeps_error_path(app, caplog, head_delivery):
    baseline = deliver(app, ok=False, function=head_delivery)
    with caplog.at_level(logging.ERROR, logger='Samsara'):
        assert deliver(app, ok=False) == baseline == (False, False)
    app._record_undoable_paste.assert_not_called()
    app.adaptive_learner.record_transcription.assert_not_called()
    assert '[PASTE] Ctrl+V delivery failed; text retained by caller when possible' in caplog.text


@pytest.mark.parametrize('path', ['typed', 'snapshot_fallback', 'typed_failed_snapshot_fallback'])
def test_typed_delivery_calls_match_head(app, path, head_delivery):
    app._foreground_wants_typed_injection.return_value = True
    text = 't' * (300 if path == 'snapshot_fallback' else 20)
    def run(function):
        type_text = Mock(side_effect=[False, True] if path == 'typed_failed_snapshot_fallback' else [True])
        central_calls = []
        def central(received, **kwargs):
            central_calls.append((received, kwargs['paste_delay'], kwargs['restore_delay']))
            return kwargs['incomplete_snapshot_fallback']()
        app._record_undoable_paste.reset_mock()
        app.adaptive_learner.record_transcription.reset_mock()
        result = function(
            app, text, return_delivery_confirmation=True,
            paste_with_preservation_fn=central, type_text_unicode_fn=type_text,
            get_foreground_hwnd_fn=Mock(return_value=101),
        )
        return (result, type_text.call_args_list, central_calls,
                app._record_undoable_paste.call_args_list,
                app.adaptive_learner.record_transcription.call_args_list)
    assert run(paste.paste_preserving_clipboard) == run(head_delivery)


def test_undo_sends_one_ctrl_z_clears_and_shows_40_character_preview(app, monkeypatch):
    import pyautogui
    hotkey = Mock()
    monkeypatch.setattr(pyautogui, 'hotkey', hotkey)
    app._last_dictation_text = 'synthetic preview ' * 5
    expected = app._last_dictation_text[:40]
    app._clear_undo = lambda: paste.clear_undo(app)
    assert paste.undo_last_dictation(app, get_foreground_hwnd_fn=lambda: 101) is True
    hotkey.assert_called_once_with('ctrl', 'z')
    assert app._last_dictation_text is None and app._last_dictation_hwnd is None
    app.play_sound.assert_called_once_with('success')
    app._show_outcome_chip.assert_called_once_with('Undid: ' + expected, 'success')
    assert paste.undo_last_dictation(app, get_foreground_hwnd_fn=lambda: 101) is False
    assert hotkey.call_count == 1


@pytest.mark.parametrize('text,current,chip', [
    (None, 101, 'Nothing to undo'),
    ('synthetic', 202, 'Undo: go back to where it was typed'),
])
def test_undo_refusals_send_nothing_and_show_chip(app, monkeypatch, text, current, chip):
    import pyautogui
    hotkey = Mock()
    monkeypatch.setattr(pyautogui, 'hotkey', hotkey)
    app._last_dictation_text = text
    app._clear_undo = Mock()
    assert paste.undo_last_dictation(app, get_foreground_hwnd_fn=lambda: current) is False
    hotkey.assert_not_called()
    app._clear_undo.assert_not_called()
    app.play_sound.assert_called_once_with('error')
    app._show_outcome_chip.assert_called_once_with(chip, 'error')
