"""606: owned ranges, intervening input, and grouped scratch restoration.

All editor I/O is synthetic; these tests never read a desktop or bootstrap
dictation. The same tests run in a plain source export.
"""
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard, paste
from samsara.session_modes import (
    CommandDispatchResult, InjectionDelivery, SessionMode, SessionModeManager,
    StackItem, UtteranceSignals, outcome_chip,
)

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))


class Editor:
    def __init__(self):
        self.window, self.control, self.tick = 101, 102, 1
        self.document = b''
        self.start = self.end = 0
        self.messages = []
        self.on_select = None

    def snapshot(self):
        return (self.window, self.control, self.document, self.start, self.end, self.tick)

    def insert(self, text):
        before = self.snapshot()
        self._replace(text)
        self.tick += 1
        return paste.DictationUnit.capture(before, self.snapshot(), text)

    def _replace(self, text):
        payload = text.encode('utf-16-le')
        self.document = self.document[:self.start * 2] + payload + self.document[self.end * 2:]
        self.start += len(payload) // 2
        self.end = self.start

    def SendMessageTimeoutW(self, control, message, wp, lp, flags, timeout, result):
        assert control == self.control
        self.messages.append(message)
        if message == 0xB1:
            self.start, self.end = wp, lp
            if self.on_select:
                self.on_select()
        elif message == 0xC2:
            self._replace(ctypes.wstring_at(lp))
        else:
            raise AssertionError(message)
        return 1


@pytest.fixture
def editor(monkeypatch):
    editor = Editor()
    monkeypatch.setattr(paste, '_editor_snapshot', editor.snapshot)
    monkeypatch.setattr(clipboard, '_user32', editor)
    return editor


def manager(*, buffered=True):
    result = SessionModeManager(
        abort_phrases=['abort'], foreground_exe_resolver=lambda: 'editor.exe',
        foreground_hwnd_resolver=lambda: 101, inject_fn=lambda text, *args: text,
        remove_chars_fn=Mock(return_value=True),
        command_dispatch_fn=lambda text: CommandDispatchResult(matched=False, phrase=None),
        agent_dispatch_fn=Mock(), buffer_dictate_until_commit=buffered,
        on_scratch_result=Mock(),
    )
    result.reset(initial_mode=SessionMode.DICTATE)
    return result


def push_unit(manager, unit):
    manager._stack.push(StackItem(kind='dictation_chunk', payload=unit.text,
        mode=SessionMode.DICTATE, timestamp=0,
        extra={'target_process': 'editor.exe', 'hwnd': 101, 'undo_unit': unit}))
    manager._require_unit_undo = True


def test_one_native_replacement_for_long_unicode_utterance(editor):
    unit = editor.insert('Synthetic \U0001f642 sentence. ' * 150)
    assert unit.change()
    assert editor.document == b''
    assert editor.messages == [0xB1, 0xC2]
    assert unit.change(redo=True)
    assert editor.document.decode('utf-16-le') == unit.text
    assert unit.change()  # restored output remains undoable


@pytest.mark.parametrize('change', ['typing', 'caret', 'away_and_back', 'field', 'window', 'document'])
def test_intervening_change_refuses_without_selection_or_deletion(editor, change):
    unit = editor.insert('synthetic sentence')
    if change == 'typing':
        editor.insert('manual')
    elif change == 'caret':
        editor.start = editor.end = 0
    elif change == 'away_and_back':
        editor.tick += 1
    elif change == 'field':
        editor.control += 1
    elif change == 'window':
        editor.window += 1
    else:
        editor.document = b'X\0' + editor.document[2:]
    unchanged = editor.snapshot()
    assert unit.change() is False
    assert editor.snapshot() == unchanged
    assert editor.messages == []


def test_input_during_selection_refuses_before_replacement(editor):
    unit = editor.insert('synthetic sentence')
    editor.on_select = lambda: setattr(editor, 'tick', editor.tick + 1)
    assert not unit.change()
    assert editor.messages == [0xB1]
    assert editor.document.decode('utf-16-le') == 'synthetic sentence'
    assert unit.expected is None


def test_unknown_or_replaced_selection_cannot_arm(editor):
    assert paste.DictationUnit.capture(None, editor.snapshot(), 'x') is None
    before = editor.snapshot()
    editor.insert('abc')
    assert paste.DictationUnit.capture(before, editor.snapshot(), 'different') is None
    editor.start, editor.end = 0, 2
    assert editor.insert('replacement') is None


def test_unsupported_control_has_no_snapshot(monkeypatch):
    monkeypatch.setattr(clipboard, '_user32', None)
    assert paste._editor_snapshot() is None


def test_grouped_staged_scratch_redo_restores_exact_draft_audio_and_stack():
    m = manager()
    for text in ['first.', 'second.', 'third.', 'fourth.']:
        m.dispatch_utterance(text, GOOD)
    before = m._scratch_state()
    result = m.dispatch_utterance('scratch that 3 times', GOOD)
    assert result.detail['scratched'] == 3
    m._on_scratch_result.reset_mock()
    result = m.dispatch_utterance('redo that', GOOD)
    assert result.kind == 'scratch_success'
    assert outcome_chip(result.kind, result.detail) == ('Redone', 'success')
    restored = m._scratch_state()
    assert restored['draft']['_segments'] == before['draft']['_segments']
    assert restored['audio'] == before['audio']
    assert restored['items'] == before['items']
    assert restored['draft']['revision'] > before['draft']['revision']
    assert m.recoverable_draft == ''
    m._on_scratch_result.assert_called_once_with(True)
    assert m.dispatch_utterance('redo that', GOOD).kind == 'scratch_refuse'


def test_grouped_external_scratch_redo_and_second_scratch(editor):
    m = manager()
    for text in ['first.', 'second.', 'third.']:
        push_unit(m, editor.insert(text))
    original = editor.document
    assert m.dispatch_utterance('scratch that 3 times', GOOD).detail['scratched'] == 3
    assert editor.document == b''
    assert m.dispatch_utterance('redo that', GOOD).kind == 'scratch_success'
    assert editor.document == original
    assert m.dispatch_utterance('scratch that 3 times', GOOD).detail['scratched'] == 3
    assert editor.document == b''
    m._remove_chars_fn.assert_not_called()


def test_user_typing_between_original_units_cannot_be_scratched_through(editor):
    m = manager()
    push_unit(m, editor.insert('first.'))
    editor.insert('manual')
    push_unit(m, editor.insert('second.'))
    result = m.dispatch_utterance('scratch that twice', GOOD)
    assert result.detail == {'requested': 2, 'scratched': 1, 'reason': 'failed'}
    assert editor.document.decode('utf-16-le') == 'first.manual'
    assert m.dispatch_utterance('redo that', GOOD).kind == 'scratch_success'
    assert editor.document.decode('utf-16-le') == 'first.manualsecond.'


@pytest.mark.parametrize('change', ['utterance', 'revision', 'reset'])
def test_redo_invalidates_when_staged_context_changes(change):
    m = manager()
    m.dispatch_utterance('first.', GOOD)
    m.dispatch_utterance('scratch that', GOOD)
    if change == 'utterance':
        m.dispatch_utterance('new text', GOOD)
    elif change == 'revision':
        m._draft.replace_text(m._draft.text)
    else:
        m.reset(initial_mode=SessionMode.DICTATE)
    assert m.dispatch_utterance('redo that', GOOD).kind == 'scratch_refuse'


def test_intervening_input_also_blocks_external_redo(editor):
    m = manager()
    push_unit(m, editor.insert('first.'))
    m.dispatch_utterance('scratch that', GOOD)
    editor.insert('manual')
    assert m.dispatch_utterance('redo that', GOOD).kind == 'scratch_refuse'
    assert editor.document.decode('utf-16-le') == 'manual'


def test_interrupted_group_redo_reports_and_tracks_only_restored_units(editor):
    m = manager()
    units = [editor.insert('first.'), editor.insert('second.')]
    for unit in units:
        push_unit(m, unit)
    m.dispatch_utterance('scratch that twice', GOOD)
    units[1].change = Mock(return_value=False)
    result = m.dispatch_utterance('redo that', GOOD)
    assert result.kind == 'scratch_refuse'
    assert result.detail['message'] == 'Redo stopped after 1'
    assert editor.document.decode('utf-16-le') == 'first.'
    assert [item.payload for item in m._stack._items] == ['first.']
    assert m.dispatch_utterance('redo that', GOOD).kind == 'scratch_refuse'


def test_undo_that_reuses_the_staged_scratch_path():
    m = manager()
    m.dispatch_utterance('first.', GOOD)
    assert m.dispatch_utterance('undo that', GOOD).kind == 'scratch_success'
    assert m.dictate_pending_buffer == ''
    assert m.dispatch_utterance('redo that', GOOD).kind == 'scratch_success'
    assert m.dictate_pending_buffer


def test_missing_receipt_in_production_never_calls_character_deleter():
    m = manager()
    m._require_unit_undo = True
    m._stack.push(StackItem(kind='dictation_chunk', payload='synthetic',
        mode=SessionMode.DICTATE, timestamp=0,
        extra={'target_process': 'editor.exe', 'hwnd': 101}))
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'
    m._remove_chars_fn.assert_not_called()


def test_exact_redo_registration_preserves_generic_redo():
    commands = json.loads((Path(__file__).resolve().parents[1] / 'commands.json').read_text('utf-8'))
    # The catalogue uses normal built-in registration, no competing plugin.
    commands = commands['commands']
    assert commands['redo that']['method'] == 'redo_last_dictation'
    assert commands['redo']['keys'] == ['ctrl', 'y']


def test_receipt_flows_from_injection_to_session_stack(editor):
    m = manager()
    m._inject_fn = lambda text, *args: InjectionDelivery(text, True, editor.insert(text))
    m.dispatch_utterance('first.', GOOD)
    m.dispatch_utterance('end', GOOD)
    assert m._stack.peek().extra['undo_unit'] is not None
    m._require_unit_undo = True
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert editor.document == b''


def test_real_paste_undo_and_redo_uses_owned_receipt(editor):
    unit = editor.insert('synthetic sentence')
    app = SimpleNamespace(_undo_unit=unit, _last_dictation_text=unit.text,
        _last_dictation_hwnd=101, _undo_timer=None,
        play_sound=Mock(), _show_outcome_chip=Mock(), _arm_undo_timer=Mock())
    app._clear_undo = lambda: paste.clear_undo(app)
    assert paste.undo_last_dictation(app, get_foreground_hwnd_fn=lambda: 101)
    assert paste.redo_last_dictation(app)
    assert editor.document.decode('utf-16-le') == unit.text


def test_failed_receipt_proof_never_falls_back_to_native_undo(monkeypatch):
    hotkey = Mock()
    import pyautogui
    monkeypatch.setattr(pyautogui, 'hotkey', hotkey)
    app = SimpleNamespace(_undo_unit=None, _last_dictation_text='synthetic',
        _last_dictation_hwnd=101, play_sound=Mock(), _show_outcome_chip=Mock())
    assert not paste.undo_last_dictation(app, get_foreground_hwnd_fn=lambda: 101)
    hotkey.assert_not_called()


def test_native_failure_disarms_without_retry(editor):
    unit = editor.insert('synthetic')
    editor.SendMessageTimeoutW = Mock(side_effect=OSError('synthetic failure'))
    assert not unit.change()
    assert unit.expected is None
    assert not unit.change()
    editor.SendMessageTimeoutW.assert_called_once()


@pytest.mark.parametrize('typed', [False, True])
def test_real_delivery_seam_records_a_verified_unit(editor, monkeypatch, typed):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda *a: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'notepad.exe')
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    app = SimpleNamespace(config={'clipboard_delay': 0}, _arm_undo_timer=Mock(), _undo_timer=None,
        adaptive_learner=Mock(), play_sound=Mock(), _show_outcome_chip=Mock(),
        _foreground_wants_typed_injection=lambda: typed,
        _flight_foreground_process_name=lambda: 'notepad.exe')
    app._record_undoable_paste = lambda text, target_hwnd: paste.record_undoable_paste(
        app, text, target_hwnd=target_hwnd)
    app._clear_undo = lambda: paste.clear_undo(app)
    def deliver(text, **kwargs):
        assert kwargs['before_paste']()
        editor.insert(text)
        return True
    assert paste.paste_preserving_clipboard(app, 'synthetic',
        paste_with_preservation_fn=deliver,
        type_text_unicode_fn=lambda text: bool(editor.insert(text)),
        get_foreground_hwnd_fn=lambda: 101)
    assert app._undo_unit is not None
    assert paste.undo_last_dictation(app, get_foreground_hwnd_fn=lambda: 101)
    assert editor.document == b''


@pytest.mark.parametrize('label,password,expected', [
    ('Edit', False, True), ('Edit', True, False), ('BrowserControl', False, False),
])
def test_native_snapshot_limits_to_readable_nonpassword_edit(monkeypatch, label, password, expected):
    def info(thread, pointer):
        pointer._obj.hwndFocus = 102
        return 1
    def last(pointer):
        pointer._obj.tick = 5
        return 1
    def name(control, buffer, length):
        buffer.value = label
        return len(label)
    def send(control, message, wp, lp, flags, timeout, result):
        if message == 0xE:
            result._obj.value = 3
        elif message == 0xD:
            value = ctypes.create_unicode_buffer('abc')
            ctypes.memmove(lp, value, ctypes.sizeof(value))
        elif message == 0xB0:
            ctypes.cast(wp, ctypes.POINTER(ctypes.c_ulong)).contents.value = 3
            ctypes.cast(lp, ctypes.POINTER(ctypes.c_ulong)).contents.value = 3
        return 1
    api = SimpleNamespace(GetLastInputInfo=last, GetForegroundWindow=lambda: 101,
        GetWindowThreadProcessId=lambda *a: 1, GetGUIThreadInfo=info,
        GetClassNameW=name, GetWindowLongW=Mock(return_value=0x20 if password else 0),
        SendMessageTimeoutW=Mock(side_effect=send))
    monkeypatch.setattr(clipboard, '_user32', api)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda *a: SimpleNamespace(confirmed_ok=True))
    snapshot = paste._editor_snapshot()
    assert (snapshot is not None) == expected
    if expected:
        assert snapshot == (101, 102, b'a\0b\0c\0', 3, 3, 5)
    else:
        api.SendMessageTimeoutW.assert_not_called()
