"""1166s (T84 P1): paced Unicode typing and honest delivery receipts.

Every test drives the production type_text_unicode dispatch loop (or the real
paste seam around it) against a fake SendInput, a fake clock and a fake focus
reader. Nothing here types into a real window or touches a real clipboard;
these are simulations, not real Electron/Chromium end-to-end tests, and they
do not prove what a real target inserts.
"""
import ctypes
import logging
import math
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard as cb, delivery_verify as dv, paste
from tests.test_scratch_after_unacknowledged_paste_783 import GOOD, deliver, rig  # noqa: F401

KEYUP = 0x0002
UNICODE = 0x0004
VK_RETURN = 0x0D
MIN = cb.TYPE_MIN_PAIR_INTERVAL_S


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    perf_counter = monotonic

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)


class Keyboard:
    """Fake user32.SendInput: records each call's events and its clock time."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.focus = {'value': (101, 7, 102)}
        self.accept = lambda index, count: count

    def __call__(self, count, events, size):
        index = len(self.calls)
        decoded = [(events[i].type, events[i].union.ki.wVk, events[i].union.ki.wScan,
                    events[i].union.ki.dwFlags) for i in range(count)]
        self.calls.append(SimpleNamespace(at=self.clock.now, count=count,
                                          events=decoded, size=size))
        result = self.accept(index, count)
        if isinstance(result, BaseException):
            raise result
        return result

    def gaps(self):
        return [b.at - a.at for a, b in zip(self.calls, self.calls[1:])]

    def text(self):
        """Rebuild the typed text from the recorded pairs, asserting pair shape."""
        data = b''
        for call in self.calls:
            assert call.count == 2
            (t1, vk1, scan1, f1), (t2, vk2, scan2, f2) = call.events
            assert t1 == t2 == 1  # INPUT_KEYBOARD
            assert (vk1, scan1) == (vk2, scan2)  # the up releases the same key
            assert not f1 & KEYUP and f2 & KEYUP  # down first, then up
            if vk1 == VK_RETURN:
                assert not f1 & UNICODE
                data += '\n'.encode('utf-16-le')
            else:
                assert vk1 == 0 and f1 & UNICODE
                data += struct.pack('<H', scan1)
        return data.decode('utf-16-le')


@pytest.fixture
def keyboard(monkeypatch):
    clock = Clock()
    kb = Keyboard(clock)
    monkeypatch.setattr(cb, 'time', clock)
    monkeypatch.setattr(cb.sys, 'platform', 'win32')
    monkeypatch.setattr(ctypes.windll.user32, 'SendInput', kb)
    monkeypatch.setattr(cb, '_paste_target', lambda: kb.focus['value'])
    return kb


def assert_paced(kb, interval):
    assert all(call.count == 2 for call in kb.calls)
    assert kb.gaps(), 'need at least two calls to measure spacing'
    assert min(kb.gaps()) >= interval - 1e-9
    assert max(kb.gaps()) == pytest.approx(interval)


# ---------------------------------------------------------------- primitive

@pytest.mark.parametrize('call', [
    lambda t: cb.type_text_unicode(t),
    lambda t: cb.type_text_unicode(t, key_interval=0),
    lambda t: cb.type_text_unicode(t, 64),
    lambda t: cb.type_text_unicode(t, 64, key_interval=0),
    lambda t: cb.type_text_unicode(t, 1),
    lambda t: cb.type_text_unicode(t, 3, key_interval=0.0),
    lambda t: cb.type_text_unicode(t, chunk=0),
], ids=['default', 'zero', 'legacy64', 'legacy64_zero', 'chunk1', 'chunk3', 'chunk0'])
def test_default_zero_and_legacy_chunks_send_one_pair_per_call_at_minimum_spacing(keyboard, call):
    receipt = call('abcdef')
    assert receipt and receipt.state == 'complete'
    assert (receipt.accepted, receipt.total) == (12, 12)
    assert [c.count for c in keyboard.calls] == [2] * 6
    assert keyboard.text() == 'abcdef'
    assert_paced(keyboard, MIN)
    assert MIN == 0.005


def test_explicit_050_interval_is_kept(keyboard):
    assert cb.type_text_unicode('abc', key_interval=.05)
    assert_paced(keyboard, .05)


@pytest.mark.parametrize('value,expected', [
    (float('nan'), MIN), (-1, MIN), (0.001, MIN), ('fast', MIN), (None, MIN),
    (float('inf'), cb.TYPE_MAX_PAIR_INTERVAL_S), (1e9, cb.TYPE_MAX_PAIR_INTERVAL_S),
    (0.02, 0.02),
])
def test_invalid_or_nonfinite_pacing_never_bursts_or_hangs(keyboard, value, expected):
    assert cb.unicode_pair_interval(value) == expected
    assert cb.type_text_unicode('xyz', key_interval=value)
    assert_paced(keyboard, expected)
    assert all(math.isfinite(s) for s in keyboard.clock.sleeps)
    assert keyboard.clock.now < 1.0  # three pairs: bounded, no hang


def test_settle_precedes_the_first_pair(keyboard):
    assert cb.type_text_unicode('a')
    assert keyboard.calls[0].at >= 0.03 - 1e-9


def test_long_text_with_repeats_and_punctuation_arrives_in_exact_order(keyboard):
    text = ('aaaa bbbb!!! ,,,, ..?? ' * 6) + 'zz'
    assert len(text) >= 100
    receipt = cb.type_text_unicode(text)
    assert receipt and receipt.accepted == receipt.total == 2 * len(text)
    assert keyboard.text() == text
    assert len(keyboard.calls) == len(text)
    assert_paced(keyboard, MIN)


def test_astral_character_is_two_utf16_units_sent_in_surrogate_order(keyboard):
    receipt = cb.type_text_unicode('a\U0001F600b')
    # One astral character = two UTF-16 code units = two down/up pairs.
    assert [c.events[0][2] for c in keyboard.calls] == [ord('a'), 0xD83D, 0xDE00, ord('b')]
    assert receipt.total == 8
    assert keyboard.text() == 'a\U0001F600b'


def test_crlf_becomes_one_enter_pair(keyboard):
    assert cb.type_text_unicode('a\r\nb')
    assert keyboard.text() == 'a\nb'
    assert len(keyboard.calls) == 3
    assert keyboard.calls[1].events[0][1] == VK_RETURN


def test_empty_text_sends_nothing_and_succeeds(keyboard):
    receipt = cb.type_text_unicode('')
    assert receipt and receipt.state == 'complete' and receipt.total == 0
    assert keyboard.calls == []


def test_input_struct_matches_the_platform_abi(keyboard):
    assert cb.type_text_unicode('a')
    assert keyboard.calls[0].size == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_non_windows_types_nothing(monkeypatch):
    send = Mock(side_effect=AssertionError('no SendInput off Windows'))
    monkeypatch.setattr(ctypes.windll.user32, 'SendInput', send)
    monkeypatch.setattr(cb.sys, 'platform', 'linux')
    receipt = cb.type_text_unicode('abc')
    assert not receipt and receipt.state == 'none'
    send.assert_not_called()


def test_zero_accepted_is_a_definite_nothing_sent(keyboard):
    keyboard.accept = lambda index, count: 0
    receipt = cb.type_text_unicode('abc')
    assert not receipt
    assert (receipt.state, receipt.accepted, receipt.reason) == ('none', 0, 'short_count')
    assert len(keyboard.calls) == 1  # stopped; never retried


@pytest.mark.parametrize('short', [0, 1])
def test_short_count_after_a_prefix_is_partial(keyboard, short):
    keyboard.accept = lambda index, count: count if index < 2 else short
    receipt = cb.type_text_unicode('abcdef')
    assert not receipt
    assert receipt.state == 'partial'
    assert receipt.accepted == 4 + short and receipt.total == 12
    assert len(keyboard.calls) == 3


@pytest.mark.parametrize('failing_call,accepted', [(0, 0), (2, 4)])
def test_exception_during_dispatch_is_never_reported_as_nothing_sent(keyboard, failing_call, accepted):
    keyboard.accept = lambda index, count: (OSError('synthetic') if index == failing_call else count)
    receipt = cb.type_text_unicode('abcdef')
    assert not receipt
    # The failing call's own result is unknown: it may have inserted events.
    assert receipt.state == 'partial' and receipt.unknown
    assert receipt.accepted == accepted
    assert len(keyboard.calls) == failing_call + 1


def test_focus_switch_during_paced_dispatch_stops_at_the_next_pair(keyboard):
    def accept(index, count):
        if index == 1:
            keyboard.focus['value'] = (999, 7, 102)  # another window took focus
        return count
    keyboard.accept = accept
    receipt = cb.type_text_unicode('abcdef')
    assert receipt.state == 'partial' and receipt.reason == 'target_changed'
    assert receipt.accepted == 4
    assert keyboard.text() == 'ab'


def test_focus_switch_during_the_settle_sends_nothing(keyboard, monkeypatch):
    real_sleep = keyboard.clock.sleep

    def sleep(seconds):
        real_sleep(seconds)
        keyboard.focus['value'] = (101, 7, 555)  # same window, other control
    monkeypatch.setattr(keyboard.clock, 'sleep', sleep)
    receipt = cb.type_text_unicode('abc')
    assert (receipt.state, receipt.reason, receipt.accepted) == ('none', 'target_changed', 0)
    assert keyboard.calls == []


def test_receipt_stays_bool_compatible():
    assert bool(cb.TypedDispatch(4, 4)) is True
    assert bool(cb.TypedDispatch(2, 4, 'short_count')) is False
    assert bool(cb.TypedDispatch(0, 4, 'short_count')) is False
    assert 'partial' in repr(cb.TypedDispatch(2, 4, 'short_count'))
    # The dispatch lives in its own module; clipboard re-exports the same objects.
    from samsara import typed_dispatch
    assert cb.TypedDispatch is typed_dispatch.TypedDispatch
    assert cb.unicode_pair_interval is typed_dispatch.unicode_pair_interval


# ---------------------------------------------------------------- app seam

def _app(typed_processes=()):
    return SimpleNamespace(
        config={'typed_injection_processes': list(typed_processes)} if typed_processes else {},
        _foreground_wants_typed_injection=lambda: bool(typed_processes),
        _flight_foreground_process_name=lambda: 'obsidian.exe',
        _record_undoable_paste=Mock(), adaptive_learner=SimpleNamespace(record_transcription=Mock()),
        _show_outcome_chip=Mock(), play_sound=Mock(),
        listening_indicator=SimpleNamespace(flash_success=Mock()),
        _schedule_ui=lambda fn, *a, **kw: fn(*a, **kw))


@pytest.fixture
def seam(monkeypatch, keyboard, fake_delivery_target):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda *a: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'obsidian.exe')
    monkeypatch.setattr(paste, '_editor_snapshot', lambda: None)
    monkeypatch.setattr(paste.delivery_verify, 'snapshot', lambda: None)  # no readback
    receipts = []
    monkeypatch.setattr('samsara.hold_diagnostics.paste_result',
                        lambda text, sent, confirmed, reason=None: receipts.append((sent, confirmed, reason)))
    keyboard.diagnostics = receipts
    return keyboard


def _incomplete_clipboard(monkeypatch):
    """Real paste_with_preservation with a snapshot that cannot be restored."""
    def snapshot():
        saved = cb.ClipboardSnapshot()
        saved.lost[cb.CF_BITMAP] = 'synthetic bitmap owner gave no data'
        return saved
    monkeypatch.setattr(cb, 'save_clipboard', snapshot)
    monkeypatch.setattr(cb, 'restore_clipboard', Mock(side_effect=AssertionError('nothing to restore')))
    monkeypatch.setattr(cb, 'copy_private_text', Mock(side_effect=AssertionError('clipboard must not be replaced')))


def test_ordinary_electron_target_incomplete_snapshot_fallback_is_paced_and_unverified(seam, monkeypatch):
    _incomplete_clipboard(monkeypatch)
    app = _app()  # not on the typed allowlist: the audit's Obsidian/Claude/Hermes case
    result = paste.paste_preserving_clipboard(app, 'Synthetic fallback sentence.',
        return_delivery_confirmation=True, paste_with_preservation_fn=cb.paste_with_preservation,
        type_text_unicode_fn=cb.type_text_unicode, get_foreground_hwnd_fn=lambda: 101)
    assert result == (True, False) and result.outcome == cb.DeliveryOutcome.UNVERIFIED
    assert result.dispatch is None
    assert seam.text() == 'Synthetic fallback sentence.'
    assert_paced(seam, MIN)
    assert seam.diagnostics == [(True, False, None)]
    paste.show_delivery_result(app, result[0], result.outcome)
    app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
    app.play_sound.assert_not_called()
    app.listening_indicator.flash_success.assert_not_called()


def test_allowlisted_target_fallback_keeps_050_pacing(seam, monkeypatch):
    _incomplete_clipboard(monkeypatch)
    app = _app(['editor.exe'])
    app.config['paste_min_chars'] = 1  # skip typed-first: exercise the fallback
    result = paste.paste_preserving_clipboard(app, 'abc', return_delivery_confirmation=True,
        paste_with_preservation_fn=cb.paste_with_preservation,
        type_text_unicode_fn=cb.type_text_unicode, get_foreground_hwnd_fn=lambda: 101)
    assert result == (True, False)
    assert_paced(seam, .05)


def test_typed_first_opt_in_is_paced_and_unverified_without_readback(seam):
    app = _app(['obsidian.exe'])
    central = Mock(side_effect=AssertionError('typed-first must not paste'))
    result = paste.paste_preserving_clipboard(app, 'Typed first.', return_delivery_confirmation=True,
        paste_with_preservation_fn=central, type_text_unicode_fn=cb.type_text_unicode,
        get_foreground_hwnd_fn=lambda: 101)
    assert result == (True, False) and result.outcome == cb.DeliveryOutcome.UNVERIFIED
    assert seam.text() == 'Typed first.'
    assert_paced(seam, MIN)
    assert seam.diagnostics == [(True, False, None)]


def test_typed_first_feedback_is_a_warning_not_a_green_typed_chip(seam):
    app = _app(['obsidian.exe'])
    assert paste.paste_preserving_clipboard(app, 'Typed first.', type_text_unicode_fn=cb.type_text_unicode,
        paste_with_preservation_fn=Mock(), get_foreground_hwnd_fn=lambda: 101) is True
    app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
    app.play_sound.assert_not_called()
    app.listening_indicator.flash_success.assert_not_called()


def test_typed_first_partial_send_is_never_followed_by_a_whole_text_paste(seam, caplog):
    seam.accept = lambda index, count: count if index < 3 else 0
    app = _app(['obsidian.exe'])
    central = Mock(side_effect=AssertionError('no whole-text paste over a typed prefix'))
    with caplog.at_level(logging.INFO, logger='Samsara'):
        result = paste.paste_preserving_clipboard(app, 'Synthetic partial.', return_delivery_confirmation=True,
            paste_with_preservation_fn=central, type_text_unicode_fn=cb.type_text_unicode,
            get_foreground_hwnd_fn=lambda: 101)
    assert result == (True, False) and result.outcome == cb.DeliveryOutcome.UNVERIFIED
    assert result.dispatch == 'partial'
    assert len(seam.calls) == 4  # three accepted pairs, one refused; no retry
    central.assert_not_called()
    assert seam.diagnostics == [(True, False, 'typed_partial')]
    app._record_undoable_paste.assert_not_called()
    assert app._last_delivery is None
    assert 'Synthetic partial.' not in caplog.text
    assert 'Typing stopped after 6/36 events' in caplog.text


def test_typed_first_partial_shows_the_unverified_warning(seam):
    seam.accept = lambda index, count: count if index < 3 else OSError('synthetic')
    app = _app(['obsidian.exe'])
    assert paste.paste_preserving_clipboard(app, 'Synthetic partial.', paste_with_preservation_fn=Mock(),
        type_text_unicode_fn=cb.type_text_unicode, get_foreground_hwnd_fn=lambda: 101) is True
    app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
    app.play_sound.assert_not_called()


def test_typed_first_definite_zero_send_still_falls_back_to_the_clipboard(seam):
    seam.accept = lambda index, count: 0
    app = _app(['obsidian.exe'])
    def central(text, **kw):
        assert kw['before_paste']()
        kw['delivery_observer'](cb.DeliveryOutcome.UNVERIFIED)
        return True
    result = paste.paste_preserving_clipboard(app, 'Zero.', return_delivery_confirmation=True,
        paste_with_preservation_fn=Mock(side_effect=central), type_text_unicode_fn=cb.type_text_unicode,
        get_foreground_hwnd_fn=lambda: 101)
    assert result == (True, False) and result.dispatch is None
    assert len(seam.calls) == 1


def test_typed_first_focus_change_before_any_pair_refuses_without_pasting(seam, monkeypatch):
    app = _app(['obsidian.exe'])
    real_sleep = seam.clock.sleep

    def sleep(seconds):
        real_sleep(seconds)
        seam.focus['value'] = (999, 7, 102)
    monkeypatch.setattr(seam.clock, 'sleep', sleep)
    central = Mock(side_effect=AssertionError('focus moved: nothing may be pasted'))
    result = paste.paste_preserving_clipboard(app, 'Moved.', return_delivery_confirmation=True,
        paste_with_preservation_fn=central, type_text_unicode_fn=cb.type_text_unicode,
        get_foreground_hwnd_fn=lambda: 101)
    assert result == (False, False)
    assert seam.calls == [] and seam.diagnostics == [(False, False, 'target_changed')]


def test_fallback_partial_send_reports_sent_unverified_and_is_not_retried(seam, monkeypatch):
    _incomplete_clipboard(monkeypatch)
    seam.accept = lambda index, count: count if index < 2 else 0
    app = _app()
    typed = Mock(side_effect=cb.type_text_unicode)
    result = paste.paste_preserving_clipboard(app, 'Fallback partial.', return_delivery_confirmation=True,
        paste_with_preservation_fn=cb.paste_with_preservation, type_text_unicode_fn=typed,
        get_foreground_hwnd_fn=lambda: 101)
    assert result == (True, False) and result.outcome == cb.DeliveryOutcome.UNVERIFIED
    assert result.dispatch == 'partial'
    typed.assert_called_once()
    assert len(seam.calls) == 3
    assert seam.diagnostics == [(True, False, 'typed_partial')]
    app._record_undoable_paste.assert_not_called()  # no undo without proof


def test_fallback_focus_change_is_not_reported_as_inserted(seam, monkeypatch):
    _incomplete_clipboard(monkeypatch)
    real_sleep = seam.clock.sleep

    def sleep(seconds):
        real_sleep(seconds)
        seam.focus['value'] = (999, 7, 102)
    monkeypatch.setattr(seam.clock, 'sleep', sleep)
    result = paste.paste_preserving_clipboard(_app(), 'Moved.', return_delivery_confirmation=True,
        paste_with_preservation_fn=cb.paste_with_preservation, type_text_unicode_fn=cb.type_text_unicode,
        get_foreground_hwnd_fn=lambda: 101)
    assert result == (False, False)
    assert seam.diagnostics == [(False, False, 'target_changed')]


def test_legacy_boolean_typers_keep_their_old_contract(seam):
    app = _app(['obsidian.exe'])
    typed = Mock(return_value=False)  # legacy: cannot express a partial send
    def central(text, **kw):
        assert kw['before_paste']()
        return True
    result = paste.paste_preserving_clipboard(app, 'Legacy.', return_delivery_confirmation=True,
        paste_with_preservation_fn=Mock(side_effect=central), type_text_unicode_fn=typed,
        get_foreground_hwnd_fn=lambda: 101)
    assert result[0] is True
    typed.assert_called_once_with('Legacy.')


# ------------------------------------------- readback, standalone and scratch

def _typer(editor, inserted):
    def type_fn(text):
        if inserted:
            editor._replace(inserted)
            editor.tick += 1
        events = len(text.encode('utf-16-le'))  # two events per UTF-16 unit
        return cb.TypedDispatch(events, events)
    return type_fn


def _typed_delivery(rig, text, inserted, **kwargs):
    editor, app, m = rig
    app._foreground_wants_typed_injection = lambda: True
    return paste.paste_preserving_clipboard(app, text, paste_with_preservation_fn=Mock(
        side_effect=AssertionError('typed-first must not paste')),
        type_text_unicode_fn=_typer(editor, inserted),
        get_foreground_hwnd_fn=lambda: editor.window, **kwargs)


READBACKS = ['exact', 'absent', 'partial']
TYPED = 'Synthetic typed sentence.'


def _inserted(readback):
    return {'exact': TYPED, 'absent': '', 'partial': TYPED[:9]}[readback]


@pytest.mark.parametrize('readback', READBACKS)
def test_typed_tuple_and_diagnostics_match_readback(rig, monkeypatch, readback):
    editor, app, m = rig
    receipts = []
    monkeypatch.setattr('samsara.hold_diagnostics.paste_result',
                        lambda text, sent, confirmed, reason=None: receipts.append((sent, confirmed)))
    result = _typed_delivery(rig, TYPED, _inserted(readback), return_delivery_confirmation=True)
    exact = readback == 'exact'
    assert result == (True, exact)
    assert receipts == [(True, exact)]
    if exact:
        # The unchanged line returns SENT today; 1165's adapter reports CONFIRMED.
        assert result.outcome in (cb.DeliveryOutcome.SENT, cb.DeliveryOutcome.CONFIRMED)
        assert app._last_delivery is not None
    else:
        assert result.outcome == cb.DeliveryOutcome.UNVERIFIED
        assert app._last_delivery is None
    paste.show_delivery_result(app, result[0], result.outcome)
    if exact:
        app._show_outcome_chip.assert_called_once_with('typed', 'success', 900)
    else:
        app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
        app.play_sound.assert_not_called()


@pytest.mark.parametrize('readback', READBACKS)
def test_typed_standalone_receipt_and_feedback_match_readback(rig, caplog, readback):
    editor, app, m = rig
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert _typed_delivery(rig, TYPED, _inserted(readback)) is True
    exact = readback == 'exact'
    assert f'[DELIVERY] Standalone sent=True confirmed={exact} chars={len(TYPED)}' in caplog.text
    assert TYPED not in caplog.text
    if exact:
        app._show_outcome_chip.assert_called_once_with('typed', 'success', 900)
        app.play_sound.assert_called_once_with('success')
    else:
        app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
        app.play_sound.assert_not_called()


@pytest.mark.parametrize('readback', ['absent', 'partial'])
def test_unverified_typed_delivery_fences_an_older_scratch_receipt(rig, readback):
    editor, app, m = rig
    deliver(rig, 'First.')  # verified clipboard delivery: scratch would remove it
    assert app._undo_unit is not None
    second = 'Second sentence.'
    _typed_delivery(rig, second, '' if readback == 'absent' else second[:3])
    document = editor.document
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'
    assert editor.document == document
    assert editor.document.decode('utf-16-le').startswith('First.')
    assert editor.messages == []


def test_partial_typed_delivery_fences_an_older_scratch_receipt(rig):
    editor, app, m = rig
    deliver(rig, 'First.')
    app._foreground_wants_typed_injection = lambda: True
    assert paste.paste_preserving_clipboard(app, 'Second.',
        paste_with_preservation_fn=Mock(side_effect=AssertionError('no whole-text paste')),
        type_text_unicode_fn=lambda text: cb.TypedDispatch(4, 14, 'short_count'),
        get_foreground_hwnd_fn=lambda: editor.window) is True
    app._show_outcome_chip.assert_called_with(paste.UNVERIFIED_DELIVERY, 'warning')
    assert m.dispatch_utterance('scratch that', GOOD).kind == 'scratch_refuse'
    assert editor.document.decode('utf-16-le') == 'First.'
    assert editor.messages == []


# ------------------------------------------------------- Compact and Full

@pytest.mark.parametrize('display', ['Full', 'Compact'])
def test_unverified_typed_delivery_reaches_both_displays_as_a_warning(qapp, monkeypatch, seam, display):
    from samsara.live_surface.feedback import schedule_feedback
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from samsara.ui.listening_indicator import ListeningIndicator
    app = _app(['obsidian.exe'])
    app.config['ui'] = {'live_surface': {'enabled': display == 'Full'}}
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
    indicator.flash_success = Mock(side_effect=AssertionError('no success flash'))
    app.listening_indicator = indicator
    app._show_outcome_chip = lambda label, kind, *ttl: schedule_feedback(app, label, kind, 4000)
    try:
        assert paste.paste_preserving_clipboard(app, 'Typed first.', paste_with_preservation_fn=Mock(),
            type_text_unicode_fn=cb.type_text_unicode, get_foreground_hwnd_fn=lambda: 101) is True
        if controller:
            while pending:
                pending.pop(0)()
            controller.drain()
            assert controller.view().notice.message == paste.UNVERIFIED_DELIVERY
        else:
            assert indicator._active_chip() == (paste.UNVERIFIED_DELIVERY, 'warning')
        app.play_sound.assert_not_called()
    finally:
        (controller or indicator).close()
        qapp.processEvents()
