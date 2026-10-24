"""783: synthetic editors only; no desktop, clipboard, model or app bootstrap."""
import ctypes
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard, delivery_verify as dv, paste
from samsara.session_modes import InjectionDelivery, SessionMode, outcome_chip
from tests.test_scratch_that_unit_606 import Editor, GOOD, manager, push_unit


class RichEditor(Editor):
    def __init__(self):
        super().__init__()
        self.label = 'RichEditD2DPT'
        self.password = False
        self.GetWindowLongW = Mock(side_effect=lambda *a: 0x20 if self.password else 0)
        self.on_replace = None

    def GetForegroundWindow(self):
        return self.window

    def GetWindowThreadProcessId(self, *args):
        return 7

    def GetGUIThreadInfo(self, thread, info):
        info._obj.hwndFocus = self.control
        return True

    def GetLastInputInfo(self, tick):
        tick._obj.tick = self.tick
        return True

    def GetClassNameW(self, control, name, length):
        name.value = self.label
        return len(self.label)

    def SendMessageTimeoutW(self, control, message, wp, lp, flags, timeout, result):
        assert control == self.control
        if message == 0xE:
            result._obj.value = len(self.document) // 2
        elif message == 0xD:
            value = ctypes.create_unicode_buffer(self.document.decode('utf-16-le'))
            ctypes.memmove(lp, value, ctypes.sizeof(value))
            result._obj.value = len(self.document) // 2
        elif message == 0xB0:
            ctypes.cast(wp, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.start
            ctypes.cast(lp, ctypes.POINTER(ctypes.c_ulong)).contents.value = self.end
        else:
            if message == 0xC2 and self.on_replace:
                return self.on_replace()
            return super().SendMessageTimeoutW(control, message, wp, lp, flags, timeout, result)
        return 1


@pytest.fixture
def rig(monkeypatch):
    editor = RichEditor()
    monkeypatch.setattr(clipboard, '_user32', editor)
    monkeypatch.setattr(paste, '_editor_snapshot', lambda: None)  # not classic Edit
    monkeypatch.setattr(dv, '_uia_snapshot', lambda: None)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda *a: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'notepad.exe')
    m = manager()
    m._foreground_exe_resolver = lambda: 'notepad.exe'
    m._foreground_hwnd_resolver = lambda: editor.window
    m._require_unit_undo = True
    app = SimpleNamespace(config={}, _undo_timer=None, _arm_undo_timer=Mock(),
        _foreground_wants_typed_injection=lambda: False,
        _flight_foreground_process_name=lambda: 'notepad.exe',
        adaptive_learner=Mock(), _show_outcome_chip=Mock(), play_sound=Mock(),
        _session_mode_manager=m)
    app._record_undoable_paste = lambda text, target_hwnd: paste.record_undoable_paste(
        app, text, target_hwnd=target_hwnd)
    app._clear_undo = lambda: paste.clear_undo(app)
    return editor, app, m


def deliver(rig, text='Synthetic sentence.', *, confirmation=False, effect=None, ok=True):
    editor, app, m = rig
    def central(text, **kwargs):
        assert kwargs['before_paste']()
        if effect:
            effect(editor, text)
        elif ok:
            editor._replace(text)
            editor.tick += 1
        kwargs['delivery_observer'](clipboard.DeliveryOutcome.SENT)
        return ok
    return paste.paste_preserving_clipboard(app, text,
        return_delivery_confirmation=confirmation,
        paste_with_preservation_fn=central, get_foreground_hwnd_fn=lambda: editor.window)


@pytest.mark.parametrize('mode', list(SessionMode))
def test_unacknowledged_hold_can_be_scratched_in_every_mode(rig, mode):
    editor, app, m = rig
    editor._replace('Existing owner content. ')
    original = editor.document
    m.force_mode(mode)
    assert deliver(rig)
    assert app._undo_unit is not None
    result = m.dispatch_utterance('scratch that', GOOD)
    assert result.kind == 'scratch_success'
    assert editor.document == original
    assert editor.messages == [0xB1, 0xC2]
    m._remove_chars_fn.assert_not_called()


def test_session_commit_gets_the_same_verified_receipt_without_duplicate_stack_item(rig):
    editor, app, m = rig
    def inject(text, *args):
        sent, confirmed = deliver(rig, text, confirmation=True)
        assert sent and confirmed
        return InjectionDelivery(text, confirmed, app._undo_unit)
    m._inject_fn = inject
    m._dictate_pending_buffer = 'Synthetic sentence.'
    result = m.commit_pending_dictation()
    assert result.kind == 'dictate_committed'
    assert 'delivery_unverified' not in result.detail
    assert m.stack_depth == 1
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert editor.document == b''


@pytest.mark.parametrize('change', ['typing', 'caret', 'away_and_back', 'field', 'window', 'document'])
def test_readback_scratch_rechecks_and_never_deletes_after_change(rig, change):
    editor, app, m = rig
    deliver(rig)
    if change == 'typing':
        editor._replace('Manual words')
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
    before = editor.snapshot()
    result = m.dispatch_utterance('scratch that', GOOD)
    assert result.kind == 'scratch_refuse'
    assert outcome_chip(result.kind, result.detail) == (dv.CHANGED, 'warning')
    assert editor.snapshot() == before
    assert editor.messages == []
    m._remove_chars_fn.assert_not_called()


def test_input_during_selection_refuses_before_replacement(rig):
    editor, app, m = rig
    deliver(rig)
    editor.on_select = lambda: setattr(editor, 'tick', editor.tick + 1)
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'
    assert editor.document.decode('utf-16-le') == 'Synthetic sentence.'
    assert editor.messages == [0xB1]


@pytest.mark.parametrize('effect', ['none', 'duplicate', 'wrong', 'focus', 'selection'])
def test_false_proof_never_arms_or_deletes_existing_text(rig, effect):
    editor, app, m = rig
    editor._replace('Owner content. Synthetic sentence.')
    def change(e, text):
        if effect == 'duplicate':
            e._replace(text * 2)
        elif effect == 'wrong':
            e._replace('Something else')
        elif effect == 'focus':
            e.control += 1
            e._replace(text)
        elif effect == 'selection':
            e._replace(text)
            e.start -= 1
    assert deliver(rig, effect=change)
    before = editor.snapshot()
    result = m.dispatch_utterance('scratch that', GOOD)
    assert result.kind == 'scratch_refuse'
    assert result.detail['message'] == dv.REFUSAL
    assert editor.snapshot() == before
    assert editor.messages == []


def test_replacing_an_owner_selection_does_not_authorize_scratch(rig):
    editor, app, m = rig
    editor._replace('Owner content')
    editor.start, editor.end = 0, editor.end
    assert deliver(rig)
    before = editor.snapshot()
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'
    assert editor.snapshot() == before


@pytest.mark.parametrize('unreadable', ['password', 'unknown', 'oversized', 'exception', 'integrity'])
def test_unreadable_controls_remain_unarmed(rig, monkeypatch, unreadable):
    editor, app, m = rig
    if unreadable == 'password':
        editor.password = True
    elif unreadable == 'unknown':
        editor.label = 'Chrome_RenderWidgetHostHWND'
    elif unreadable == 'oversized':
        editor._replace('x' * (dv.MAX_UNITS + 1))
    elif unreadable == 'exception':
        editor.GetClassNameW = Mock(side_effect=RuntimeError('unavailable'))
    else:
        monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                            lambda *a: SimpleNamespace(blocked=False, confirmed_ok=False))
    assert deliver(rig)
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'
    assert editor.messages == []


def test_unverified_newest_delivery_fences_an_older_receipt(rig):
    editor, app, m = rig
    deliver(rig, 'First.')
    deliver(rig, effect=lambda *a: None)
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'
    assert editor.document.decode('utf-16-le') == 'First.'
    assert editor.messages == []


def test_failed_delivery_does_not_expose_older_receipt(rig):
    editor, app, m = rig
    deliver(rig, 'First.')
    assert not deliver(rig, ok=False)
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'
    assert editor.messages == []


def test_utf16_emoji_multiline_middle_insertion_and_redo(rig):
    editor, app, m = rig
    editor._replace('Before. After.')
    editor.start = editor.end = 8
    original = editor.document
    text = '\U0001f642\r\nSynthetic sentence. '
    deliver(rig, text)
    sent = editor.document
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert editor.document == original
    assert m.dispatch_utterance('redo that', GOOD).kind == 'scratch_success'
    assert editor.document == sent
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert editor.document == original


def test_uncertain_replacement_is_never_retried(rig):
    editor, app, m = rig
    deliver(rig)
    unit = app._undo_unit
    editor.on_replace = Mock(return_value=0)
    assert not unit.change()
    assert not unit.change()
    editor.on_replace.assert_called_once()


def test_masked_delivery_log_has_counts_and_receipt_only(rig, caplog):
    with caplog.at_level(logging.INFO, logger='Samsara'):
        deliver(rig, 'Synthetic private example.')
    assert 'Synthetic private example.' not in caplog.text
    assert '[DELIVERY] Standalone sent=True confirmed=True chars=26 scratch_armed=True' in caplog.text


@pytest.mark.parametrize('sent', [True, False])
def test_standalone_without_initialized_session_slot_does_not_bootstrap(sent, caplog):
    app = SimpleNamespace(_ensure_session_mode_manager=Mock(
        side_effect=AssertionError('delivery must not initialize an incomplete app')))
    with caplog.at_level(logging.INFO, logger='Samsara'):
        dv.remember_standalone(app, 'Synthetic.', sent, False)
    app._ensure_session_mode_manager.assert_not_called()
    assert f'Standalone sent={sent} confirmed=False chars=10 scratch_armed=False' in caplog.text


def test_initialized_app_lazily_installs_verified_standalone_receipt(rig):
    editor, app, m = rig
    app._session_mode_manager = None
    app._ensure_session_mode_manager = Mock(return_value=m)
    assert deliver(rig)
    app._ensure_session_mode_manager.assert_called_once_with()
    assert m.stack_depth == 1
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert editor.document == b''


def test_uia_readback_confirms_but_never_guesses_a_native_writer(monkeypatch):
    before = ((101, (4, 9), b'', 0, 0, 1), False)
    after = ((101, (4, 9), 'Synthetic'.encode('utf-16-le'), 9, 9, 2), False)
    monkeypatch.setattr(dv, 'snapshot', lambda: after)
    confirmed, unit = dv.verify(before, 'Synthetic', 101)
    assert confirmed and unit is None


def test_clipboard_only_delivery_never_waits_for_accessibility(rig, monkeypatch):
    editor, app, m = rig
    readback = Mock(side_effect=AssertionError('agent delivery must not wait for UIA'))
    monkeypatch.setattr(dv, 'snapshot', readback)
    guard = Mock(return_value=True)
    def central(text, **kwargs):
        assert kwargs['incomplete_snapshot_fallback'] is None
        assert kwargs['before_paste']()
        kwargs['delivery_observer'](clipboard.DeliveryOutcome.SENT)
        return True
    assert paste.paste_preserving_clipboard(app, 'Synthetic.', clipboard_only=True,
        feedback=False, before_paste=guard, paste_with_preservation_fn=central,
        get_foreground_hwnd_fn=lambda: editor.window)
    readback.assert_not_called()
    guard.assert_called_once_with()
    app._show_outcome_chip.assert_not_called()
    app.play_sound.assert_not_called()
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'


def test_uia_uses_focused_textpattern_selection_and_stays_on_service_worker(monkeypatch):
    from samsara import uia
    class Range:
        def __init__(self, text, start=0, end=None):
            self.text, self.start, self.end = text, start, len(text) if end is None else end
        def Clone(self):
            return Range(self.text, self.start, self.end)
        def GetText(self, limit):
            return self.text[self.start:self.end][:limit]
        def MoveEndpointByRange(self, endpoint, other, target):
            self.end = other.start if target == 0 else other.end
    selected = Range('A\U0001f642B', 2, 2)
    pattern = SimpleNamespace(DocumentRange=Range(selected.text),
        GetSelection=lambda: SimpleNamespace(Length=1, GetElement=lambda i: selected))
    ctrl = SimpleNamespace(IsPassword=False, pattern=lambda kind: pattern,
        element=SimpleNamespace(CurrentHasKeyboardFocus=True, GetRuntimeId=lambda: (4, 9)))
    calls = []
    service = SimpleNamespace(_get_backend=lambda: SimpleNamespace(focused=lambda: ctrl),
        _call=lambda fn: (calls.append('worker'), fn())[1])
    monkeypatch.setattr(uia, 'get_service', lambda: service)
    monkeypatch.setattr(dv, '_focus', lambda: (101, 102, 7))
    monkeypatch.setattr(dv.injection_safety, 'window_integrity',
                        lambda *a: SimpleNamespace(confirmed_ok=True))
    assert dv._uia_snapshot() == (101, (4, 9), selected.text.encode('utf-16-le'), 3, 3, 7)
    assert calls == ['worker']
    ctrl.IsPassword = True
    assert dv._uia_snapshot() is None
    ctrl.IsPassword = False
    ctrl.pattern = lambda kind: None
    assert dv._uia_snapshot() is None  # Value without a caret cannot prove ownership.
