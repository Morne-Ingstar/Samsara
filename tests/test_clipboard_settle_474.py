"""Readiness, ownership and delivery tests with no native clipboard or hooks."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard as cb, paste


_REAL_MESSAGE_PASTE = cb.message_paste_to_focused_control


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    perf_counter = monotonic

    def sleep(self, seconds):
        self.now += seconds


class Clipboard:
    def __init__(self, clock):
        self.clock = clock
        self.seq = 11
        self.opened = False
        self.busy_until = 0.0
        self.other_opener_until = 0.0
        self.unicode = True
        self.change_at = None
        self.opens = self.closes = 0

    def GetClipboardSequenceNumber(self):
        return self.seq + int(self.change_at is not None and self.clock.now >= self.change_at)

    def OpenClipboard(self, _hwnd):
        if self.clock.now < self.busy_until:
            return False
        assert not self.opened
        self.opened = True
        self.opens += 1
        return True

    def CloseClipboard(self):
        assert self.opened
        self.opened = False
        self.closes += 1
        return True

    def GetOpenClipboardWindow(self):
        assert not self.opened
        return 123 if self.clock.now < self.other_opener_until else 0

    def IsClipboardFormatAvailable(self, fmt):
        assert self.opened and fmt == cb.CF_UNICODETEXT
        return self.unicode


@pytest.fixture
def transport(monkeypatch):
    clock = Clock()
    native = Clipboard(clock)
    monkeypatch.setattr(cb, 'time', clock)
    monkeypatch.setattr(cb, '_user32', native)
    monkeypatch.setattr(cb.sys, 'platform', 'win32')
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', lambda: cb.DeliveryOutcome.SENT)
    saved = cb.ClipboardSnapshot({cb.CF_UNICODETEXT: b'synthetic prior text'})
    monkeypatch.setattr(cb, 'save_clipboard', lambda: saved)
    restore = Mock(return_value=True)
    monkeypatch.setattr(cb, 'restore_clipboard', restore)
    copy = Mock()
    monkeypatch.setattr(cb, 'pyperclip', SimpleNamespace(copy=copy))
    monkeypatch.setattr(cb, 'HAS_PYPERCLIP', True)
    hotkey = Mock()
    monkeypatch.setitem(__import__('sys').modules, 'pyautogui', SimpleNamespace(hotkey=hotkey))
    return SimpleNamespace(clock=clock, native=native, saved=saved, restore=restore, copy=copy, hotkey=hotkey)


def test_common_text_wait_matches_previous_50ms(transport):
    t = transport
    pasted_at = []
    t.hotkey.side_effect = lambda *_keys: pasted_at.append(t.clock.now)
    assert cb.paste_with_preservation('synthetic sentence')
    assert .05 <= pasted_at[0] <= .055
    assert t.native.opens == t.native.closes
    t.restore.assert_called_once_with(t.saved)


@pytest.mark.parametrize('condition', ['busy_until', 'other_opener_until'])
def test_busy_clipboard_restarts_continuous_settle_window(transport, condition):
    t = transport
    setattr(t.native, condition, .12)
    assert cb.wait_for_clipboard_ready(11, .05) == 'ready'
    assert .17 <= t.clock.now <= .18
    assert not t.native.opened


@pytest.mark.parametrize('fmt', [cb.CF_DIB, cb.CF_DIBV5, 50000])
def test_picture_uses_ordinary_50ms_and_preserves_snapshot(transport, fmt):
    t = transport
    t.saved.clear()
    t.saved.update({fmt: b'synthetic picture or OLE payload'})
    times = []
    t.hotkey.side_effect = lambda *_: times.append(t.clock.now)
    assert cb.paste_with_preservation('synthetic sentence')
    assert times == [pytest.approx(.05)]
    t.restore.assert_called_once_with(t.saved)


def test_sequence_change_cancels_without_sending_or_fallback(transport):
    t = transport
    t.native.change_at = .02
    fallback = Mock()
    assert not cb.paste_with_preservation('synthetic sentence', incomplete_snapshot_fallback=fallback)
    t.hotkey.assert_not_called()
    fallback.assert_not_called()
    assert not t.native.opened
    t.restore.assert_called_once()


@pytest.mark.parametrize('condition', ['busy', 'missing_unicode', 'opener'])
def test_readiness_timeout_is_bounded_and_uses_existing_fallback(transport, condition, caplog):
    t = transport
    if condition == 'busy':
        t.native.busy_until = 10
    elif condition == 'missing_unicode':
        t.native.unicode = False
    else:
        t.native.other_opener_until = 10
    fallback = Mock(return_value=True)
    assert cb.paste_with_preservation('synthetic sentence', incomplete_snapshot_fallback=fallback)
    assert t.clock.now == pytest.approx(1.0)
    assert 'readiness timed out' in caplog.text
    fallback.assert_called_once()
    t.hotkey.assert_not_called()
    t.restore.assert_called_once()


def test_timeout_without_safe_typed_fallback_returns_failure(transport):
    transport.native.busy_until = 10
    assert not cb.paste_with_preservation('synthetic sentence')
    transport.hotkey.assert_not_called()


def test_focus_is_checked_after_ordinary_settle_even_for_images(transport):
    t = transport
    t.saved[cb.CF_DIB] = b'synthetic image'
    guard = Mock(return_value=False)
    assert not cb.paste_with_preservation('synthetic sentence', before_paste=guard)
    guard.assert_called_once()
    assert t.clock.now == pytest.approx(.05)
    t.hotkey.assert_not_called()
    t.restore.assert_called_once()


def test_hotkey_exception_still_restores(transport):
    transport.hotkey.side_effect = RuntimeError('synthetic injection failure')
    assert not cb.paste_with_preservation('synthetic sentence')
    transport.restore.assert_called_once()


def test_restore_failure_is_reported_honestly(transport, caplog):
    transport.restore.return_value = False
    assert cb.paste_with_preservation('synthetic sentence')  # sent, not acknowledged
    assert 'exact preservation not confirmed' in caplog.text


def test_ordinary_paste_keeps_legacy_tuple_and_undo(monkeypatch, caplog):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'notepad++.exe')
    recorder = Mock()
    monkeypatch.setattr(paste.flight_recorder, 'record', recorder)
    app = SimpleNamespace(config={}, _TYPED_INJECTION_PROCESSES=(),
        _foreground_wants_typed_injection=lambda: False, _flight_foreground_process_name=lambda: 'notepad++.exe',
        _record_undoable_paste=Mock(), adaptive_learner=SimpleNamespace(record_transcription=Mock()))
    def central(_text, **kwargs):
        return kwargs['before_paste']()
    assert paste.paste_preserving_clipboard(app, 'synthetic sentence', return_delivery_confirmation=True,
        paste_with_preservation_fn=central, get_foreground_hwnd_fn=lambda: 123) == (True, False)
    assert 'without target acknowledgement' not in caplog.text
    assert recorder.call_args.kwargs['result'] == 'ok'
    app.adaptive_learner.record_transcription.assert_not_called()
    app._record_undoable_paste.assert_called_once_with('synthetic sentence', target_hwnd=123)


def test_text_command_uses_central_preservation(monkeypatch):
    from samsara.handlers import TextHandler
    deliver = Mock(return_value=False)
    monkeypatch.setattr(cb, 'paste_with_preservation', deliver)
    assert TextHandler().execute({'text': 'synthetic sentence'}, SimpleNamespace()) is False
    deliver.assert_called_once_with('synthetic sentence', restore_delay=.4)




class Editor(Clipboard):
    def __init__(self, clock, *, label='Scintilla', grow=True, fail=None):
        super().__init__(clock)
        self.label, self.grow, self.fail = label, grow, fail
        self.length = 8
        self.caret, self.start, self.end = 8, 8, 8
        self.change = None
        self.messages = []
        self.focus = 456

    def GetForegroundWindow(self):
        return 123

    def GetWindowThreadProcessId(self, hwnd, pid):
        assert hwnd == 123 and pid is None
        return 7

    def GetGUIThreadInfo(self, thread, info):
        assert thread == 7
        info._obj.hwndFocus = self.focus
        return True

    def GetClassNameW(self, hwnd, buffer, _capacity):
        assert hwnd == self.focus
        buffer.value = self.label
        return len(self.label)

    def SendMessageTimeoutW(self, hwnd, message, wp, lp, flags, timeout, result):
        assert hwnd == self.focus and wp == lp == 0
        assert flags == 0x23 and timeout == (1000 if message == 2179 else 200)
        self.messages.append(message)
        if self.fail == message or (self.fail == 'after' and 2179 in self.messages and message != 2179):
            return 0
        if message == 2179 and self.grow:
            self.length += 17
        if message == 2179 and self.change:
            self.length, self.caret, self.start, self.end = self.change
        result._obj.value = {2006: self.length, 2008: self.caret, 2143: self.start, 2145: self.end}.get(message, 0)
        return 1


@pytest.mark.parametrize('label,grow,fail,confirmed,keys,messages', [
    ('Scintilla', True, None, cb.DeliveryOutcome.CONFIRMED, 0, [2006, 2008, 2143, 2145, 2179, 2006, 2008, 2143, 2145]),
    ('Scintilla', False, None, cb.DeliveryOutcome.UNVERIFIED, 1, [2006, 2008, 2143, 2145, 2179, 2006, 2008, 2143, 2145]),
    ('Edit', True, None, cb.DeliveryOutcome.SENT, 1, []),
    ('Chrome_RenderWidgetHostHWND', True, None, cb.DeliveryOutcome.SENT, 1, []),
    ('Scintilla', True, 2179, cb.DeliveryOutcome.UNVERIFIED, 0, [2006, 2008, 2143, 2145, 2179]),
    ('Scintilla', True, 'after', cb.DeliveryOutcome.UNVERIFIED, 0, [2006, 2008, 2143, 2145, 2179, 2006]),
    ('Scintilla', True, 2006, cb.DeliveryOutcome.UNVERIFIED, 1, [2006]),
])
def test_message_receipt_and_keystroke_fallback(transport, monkeypatch, label, grow, fail,
                                               confirmed, keys, messages):
    t = transport
    editor = Editor(t.clock, label=label, grow=grow, fail=fail)
    monkeypatch.setattr(cb, '_user32', editor)
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', _REAL_MESSAGE_PASTE)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda hwnd=None: SimpleNamespace(confirmed_ok=True))
    receipt = []
    assert cb.paste_with_preservation('synthetic sentence', delivery_observer=receipt.append)
    assert receipt == [confirmed]
    assert t.hotkey.call_count == keys
    assert editor.messages == messages
    t.restore.assert_called_once_with(t.saved)


@pytest.mark.parametrize('confirmed_ok', [False, None])
def test_no_messages_to_higher_or_unknown_integrity(transport, monkeypatch, confirmed_ok):
    editor = Editor(transport.clock)
    monkeypatch.setattr(cb, '_user32', editor)
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', _REAL_MESSAGE_PASTE)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda hwnd=None: SimpleNamespace(confirmed_ok=confirmed_ok))
    assert cb.paste_with_preservation('synthetic sentence')
    assert editor.messages == []
    transport.hotkey.assert_called_once_with('ctrl', 'v')


def test_confirmed_receipt_reaches_application_seam(monkeypatch):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    app = SimpleNamespace(config={}, _foreground_wants_typed_injection=lambda: False,
        _flight_foreground_process_name=lambda: 'notepad++.exe',
        _record_undoable_paste=Mock(), adaptive_learner=SimpleNamespace(record_transcription=Mock()))
    typed = Mock()
    def central(_text, **kwargs):
        assert kwargs['before_paste']()
        kwargs['delivery_observer'](True)
        return True
    assert paste.paste_preserving_clipboard(app, 'synthetic sentence', return_delivery_confirmation=True,
        paste_with_preservation_fn=central, type_text_unicode_fn=typed,
        get_foreground_hwnd_fn=lambda: 123) == (True, True)
    typed.assert_not_called()
    app.adaptive_learner.record_transcription.assert_called_once()


def test_last_typed_fallback_is_paced_but_never_confirmed(monkeypatch):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'editor.exe')
    app = SimpleNamespace(config={'paste_min_chars': 1}, _foreground_wants_typed_injection=lambda: True,
        _flight_foreground_process_name=lambda: 'editor.exe', _record_undoable_paste=Mock(),
        adaptive_learner=SimpleNamespace(record_transcription=Mock()), _show_outcome_chip=Mock())
    typed = Mock(return_value=True)
    def central(_text, **kwargs):
        return kwargs['incomplete_snapshot_fallback']()
    assert paste.paste_preserving_clipboard(app, 'synthetic sentence', return_delivery_confirmation=True,
        paste_with_preservation_fn=central, type_text_unicode_fn=typed,
        get_foreground_hwnd_fn=lambda: 123) == (True, False)
    typed.assert_called_once_with('synthetic sentence', key_interval=.05)
    app._show_outcome_chip.assert_not_called()  # structured caller owns feedback
    paste.show_delivery_result(app, True, False)
    app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
    app.adaptive_learner.record_transcription.assert_not_called()


def test_typed_last_resort_spaces_packet_downs_without_slowing_bursts(monkeypatch):
    import ctypes
    clock = Clock()
    calls = []
    monkeypatch.setattr(cb, 'time', clock)
    monkeypatch.setattr(cb.sys, 'platform', 'win32')
    def send(n, events, size):
        calls.append((clock.now, n))
        return n
    monkeypatch.setattr(ctypes.windll.user32, 'SendInput', send)
    assert cb.type_text_unicode('abc', key_interval=.05)
    assert [n for _, n in calls] == [2, 2, 2]
    assert all(b[0]-a[0] >= .05-1e-9 for a,b in zip(calls, calls[1:]))
    calls.clear()
    assert cb.type_text_unicode('abc')
    assert [n for _,n in calls] == [6]


@pytest.mark.parametrize('display', ['Full', 'Compact'])
def test_unconfirmed_sentence_reaches_both_displays(qapp, monkeypatch, display):
    from samsara.live_surface.feedback import schedule_feedback
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from samsara.ui.listening_indicator import ListeningIndicator
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': display == 'Full'}}},
                          _schedule_ui=lambda fn,*a,**kw: fn(*a,**kw), play_sound=Mock())
    controller = None
    if display == 'Full':
        pending = []
        monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', pending.append)
        controller = LiveSurfaceController(app, model=LiveSurfaceModel())
        app.live_surface = controller
        controller.start()
        indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        indicator = ListeningIndicator()
    app.listening_indicator = indicator
    app._show_outcome_chip = lambda label,kind,*ttl: schedule_feedback(app,label,kind,4000)
    try:
        assert paste.show_delivery_result(app, True, False)
        if controller:
            while pending:
                pending.pop(0)()
            controller.drain()
            assert controller.view().notice.message == paste.UNVERIFIED_DELIVERY
        else:
            assert indicator._active_chip() == (paste.UNVERIFIED_DELIVERY, 'warning')
        app.play_sound.assert_not_called()
        import os
        from pathlib import Path
        if output := os.environ.get('DELIVERY_474_ARTIFACTS'):
            widget = controller.widget if controller else indicator
            widget.show()
            qapp.processEvents()
            assert widget.grab().save(str(Path(output) / (display.lower() + '_unconfirmed.png')))
    finally:
        (controller or indicator).close()
        qapp.processEvents()


@pytest.mark.parametrize('confirmed', [True, False])
def test_boolean_held_seam_uses_receipt_for_feedback(monkeypatch, confirmed):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'editor.exe')
    app = SimpleNamespace(config={}, _foreground_wants_typed_injection=lambda: False,
        _flight_foreground_process_name=lambda: 'editor.exe', _record_undoable_paste=Mock(),
        adaptive_learner=SimpleNamespace(record_transcription=Mock()),
        _show_outcome_chip=Mock(), play_sound=Mock())
    def central(_text, **kwargs):
        assert kwargs['before_paste']()
        kwargs['delivery_observer'](confirmed)
        return True
    assert paste.paste_preserving_clipboard(app, 'synthetic sentence',
        paste_with_preservation_fn=central, get_foreground_hwnd_fn=lambda: 123) is True
    if confirmed:
        app._show_outcome_chip.assert_called_once_with('typed', 'success', 900)
        app.play_sound.assert_called_once_with('success')
    else:
        app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
        app.play_sound.assert_not_called()


@pytest.mark.parametrize('before,after', [
    ((8, 8, 2, 8), (8, 8, 8, 8)),  # equal length; only selection collapses
    ((20, 20, 2, 20), (8, 8, 8, 8)),  # longer selection; length shrinks
    ((8, 8, 8, 8), (25, 25, 25, 25)),  # plain insert
    ((8, 8, 8, 8), (8, 12, 12, 12)),  # same length, caret moves
])
def test_selection_replacement_never_double_pastes(transport, monkeypatch, before, after):
    editor = Editor(transport.clock, grow=False)
    editor.length, editor.caret, editor.start, editor.end = before
    editor.change = after
    monkeypatch.setattr(cb, '_user32', editor)
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', _REAL_MESSAGE_PASTE)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda hwnd=None: SimpleNamespace(confirmed_ok=True))
    receipt = []
    assert cb.paste_with_preservation('synthetic', delivery_observer=receipt.append)
    assert receipt == [cb.DeliveryOutcome.CONFIRMED]
    assert editor.messages.count(2179) == 1
    transport.hotkey.assert_not_called()


@pytest.mark.parametrize('outcome', [cb.DeliveryOutcome.CONFIRMED, cb.DeliveryOutcome.SENT, cb.DeliveryOutcome.UNVERIFIED])
def test_three_way_feedback_and_legacy_tuple(monkeypatch, outcome):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'editor.exe')
    app = SimpleNamespace(config={}, _foreground_wants_typed_injection=lambda: False,
        _flight_foreground_process_name=lambda: 'editor.exe', _record_undoable_paste=Mock(),
        adaptive_learner=SimpleNamespace(record_transcription=Mock()),
        _show_outcome_chip=Mock(), play_sound=Mock(),
        listening_indicator=SimpleNamespace(flash_success=Mock()), _schedule_ui=lambda fn: fn())
    def central(_text, **kw):
        assert kw['before_paste']()
        kw['delivery_observer'](outcome)
        return True
    result = paste.paste_preserving_clipboard(app, 'synthetic', return_delivery_confirmation=True,
        paste_with_preservation_fn=central, get_foreground_hwnd_fn=lambda: 123)
    assert result == (True, outcome == cb.DeliveryOutcome.CONFIRMED)
    assert result.outcome == outcome
    paste.show_delivery_result(app, result[0], result.outcome)
    if outcome == cb.DeliveryOutcome.UNVERIFIED:
        app.play_sound.assert_not_called()
        app.listening_indicator.flash_success.assert_not_called()
        app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
    else:
        app.play_sound.assert_called_once_with('success')
        app.listening_indicator.flash_success.assert_called_once()
        app._show_outcome_chip.assert_called_once_with('typed', 'success', 900)


@pytest.mark.parametrize('length,threshold,typed_ok', [(5, 10, True), (10, 10, True), (5, 10, False)])
def test_explicit_typed_target_restores_head_branch(monkeypatch, length, threshold, typed_ok):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'editor.exe')
    app = SimpleNamespace(config={'paste_min_chars': threshold}, _foreground_wants_typed_injection=lambda: True,
        _flight_foreground_process_name=lambda: 'editor.exe', _record_undoable_paste=Mock(),
        adaptive_learner=SimpleNamespace(record_transcription=Mock()))
    typed = Mock(return_value=typed_ok)
    def central(_text, **kw):
        return kw['before_paste']()
    clipboard = Mock(side_effect=central)
    result = paste.paste_preserving_clipboard(app, 'x'*length, return_delivery_confirmation=True,
        paste_with_preservation_fn=clipboard, type_text_unicode_fn=typed, get_foreground_hwnd_fn=lambda: 123)
    assert result == (True, length < threshold and typed_ok)
    assert result.outcome == cb.DeliveryOutcome.SENT
    if length < threshold:
        typed.assert_called_once_with('x'*length)
    else:
        typed.assert_not_called()
    assert clipboard.call_count == int(not (length < threshold and typed_ok))


def test_unknown_integrity_ordinary_paste_is_unverified(monkeypatch):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False, confirmed_ok=False))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    app = SimpleNamespace(config={}, _foreground_wants_typed_injection=lambda: False,
        _flight_foreground_process_name=lambda: 'editor.exe', _record_undoable_paste=Mock(),
        adaptive_learner=SimpleNamespace(record_transcription=Mock()), _show_outcome_chip=Mock(), play_sound=Mock())
    assert paste.paste_preserving_clipboard(app, 'synthetic', paste_with_preservation_fn=lambda *a, **k: True)
    app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
    app.play_sound.assert_not_called()



def test_unavailable_native_message_api_keeps_ordinary_sent(monkeypatch):
    monkeypatch.setattr(cb, '_user32', None)
    assert _REAL_MESSAGE_PASTE() == cb.DeliveryOutcome.SENT


def test_explicit_typed_target_focus_guard_matches_head(monkeypatch):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    app = SimpleNamespace(config={}, _foreground_wants_typed_injection=lambda: True,
                          _flight_foreground_process_name=lambda: 'editor.exe')
    typed, clipboard = Mock(), Mock()
    result = paste.paste_preserving_clipboard(app, 'synthetic', before_paste=lambda: False,
        return_delivery_confirmation=True, paste_with_preservation_fn=clipboard, type_text_unicode_fn=typed)
    assert result == (False, False)
    typed.assert_not_called()
    clipboard.assert_not_called()

@pytest.mark.parametrize('feedback,outcome,expected', [
    (False, cb.DeliveryOutcome.SENT, None),
    (False, cb.DeliveryOutcome.CONFIRMED, None),
    (False, cb.DeliveryOutcome.UNVERIFIED, paste.UNVERIFIED_DELIVERY),
    ('silent', cb.DeliveryOutcome.UNVERIFIED, None),
    ('silent', cb.DeliveryOutcome.CONFIRMED, None),
    (True, cb.DeliveryOutcome.SENT, 'typed'),
])
def test_feedback_control_keeps_warnings_except_for_partials(monkeypatch, feedback, outcome, expected):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity', lambda: None)
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'editor.exe')
    app = SimpleNamespace(config={}, _foreground_wants_typed_injection=lambda: False,
        _flight_foreground_process_name=lambda: 'editor.exe', _record_undoable_paste=Mock(),
        adaptive_learner=SimpleNamespace(record_transcription=Mock()),
        _show_outcome_chip=Mock(), play_sound=Mock(),
        listening_indicator=SimpleNamespace(flash_success=Mock()), _schedule_ui=lambda fn: fn())
    def central(_text, **kw):
        kw['before_paste']()
        kw['delivery_observer'](outcome)
        return True
    assert paste.paste_preserving_clipboard(app, 'synthetic', feedback=feedback,
        paste_with_preservation_fn=central, get_foreground_hwnd_fn=lambda: 123)
    if expected is None:
        app._show_outcome_chip.assert_not_called()
    else:
        assert app._show_outcome_chip.call_args.args[0] == expected
    assert app.play_sound.call_count == int(expected == 'typed')
    assert app.listening_indicator.flash_success.call_count == int(expected == 'typed')


def test_partial_silent_and_final_feedback_at_real_call_sites(monkeypatch):
    import ast
    import threading
    from pathlib import Path
    source = ast.parse(Path('samsara/streaming.py').read_text(encoding='utf-8'))
    app = SimpleNamespace(_paste_preserving_clipboard=Mock(return_value=True))
    session = SimpleNamespace(app=app, _paste_lock=threading.Lock(),
        cancel_event=threading.Event(), _last_pasted=False, _focus_unchanged=lambda: True)
    namespace = dict(pyautogui=SimpleNamespace(hotkey=Mock()), time=SimpleNamespace(sleep=Mock()),
        UNDO_SETTLE_S=0, PASTE_SETTLE_S=0, logger=Mock(), _release_held_modifiers=Mock())
    for name in ('_direct_paste_partial', '_direct_paste_final'):
        method = next(n for n in ast.walk(source) if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'streaming-method', 'exec'), namespace)
        namespace[name](session, 'synthetic')
    assert app._paste_preserving_clipboard.call_args_list[0].kwargs == {'feedback': 'silent'}
    guard = app._paste_preserving_clipboard.call_args_list[1].kwargs['before_paste']
    assert guard() is True
    session.cancel_event.set()
    assert guard() is False


def test_continuous_chunk_disables_success_and_wrapper_forwards_feedback():
    import ast
    from pathlib import Path
    source = ast.parse(Path('dictation.py').read_text(encoding='utf-8'))
    calls = [n for n in ast.walk(source) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == '_paste_preserving_clipboard']
    muted = [n for n in calls if any(k.arg == 'feedback' and isinstance(k.value, ast.Constant)
                                  and k.value.value is False for k in n.keywords)]
    assert len(muted) == 1
    continuous = next(n for n in ast.walk(source) if isinstance(n, ast.FunctionDef)
                      and n.name == 'transcribe_continuous_buffer')
    assert muted[0] in list(ast.walk(continuous))
    method = next(n for n in ast.walk(source) if isinstance(n, ast.FunctionDef)
                  and n.name == '_paste_preserving_clipboard')
    deliver = Mock(return_value=True)
    namespace = dict(_paste=SimpleNamespace(paste_preserving_clipboard=deliver),
        paste_with_preservation=Mock(), type_text_unicode=Mock(), _get_foreground_hwnd=Mock())
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'paste-wrapper', 'exec'), namespace)
    namespace['_paste_preserving_clipboard'](object(), 'synthetic', feedback=False)
    assert deliver.call_args.kwargs['feedback'] is False
