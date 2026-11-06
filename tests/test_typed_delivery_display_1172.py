"""1172: real Qt heartbeat and native pacing, with no desktop injection."""
import ctypes
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from samsara import clipboard as cb
from samsara import paste, typed_delivery


@pytest.fixture
def native(monkeypatch):
    calls = []

    def send(count, inputs, size):
        calls.append((threading.get_ident(), inputs[0].union.ki.wScan))
        return count

    monkeypatch.setattr(ctypes.windll.user32, 'SendInput', send)
    monkeypatch.setattr(cb, '_paste_target', lambda: (101, 7, 102))
    monkeypatch.setattr(cb, '_paste_target_matches', lambda target: True)
    return calls


def test_300_characters_keep_qt_heartbeat_responsive(qapp, native, record_property):
    """1169's blocked-loop reproduction, now pinning responsive delivery."""
    loop = QEventLoop()
    result = {}
    ui_thread = threading.get_ident()
    started = time.perf_counter()

    def inject():
        result['receipt'] = cb.type_text_unicode('x' * 300)
        result['finished'] = time.perf_counter() - started
        if 'heartbeat' in result:
            loop.quit()

    def heartbeat():
        result['heartbeat'] = time.perf_counter() - started
        result['during_delivery'] = 'finished' not in result
        if 'finished' in result:
            loop.quit()

    QTimer.singleShot(0, inject)
    QTimer.singleShot(100, heartbeat)
    loop.exec()
    record_property('heartbeat_s', result['heartbeat'])
    record_property('delivery_s', result['finished'])
    assert result['receipt'] and len(native) == 300
    assert result['heartbeat'] < .5
    assert result['during_delivery']
    assert all(thread != ui_thread for thread, _ in native)


@pytest.fixture
def app(qapp, native, monkeypatch, fake_delivery_target):
    """Use the real app admission seam and paste receipt, never bootstrap."""
    from dictation import DictationApp
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda: NS(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste, '_editor_snapshot', lambda: None)
    monkeypatch.setattr(paste.delivery_verify, 'snapshot', lambda: None)
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    monkeypatch.setattr('dictation._get_foreground_hwnd', lambda: 101)
    monkeypatch.setattr('dictation.paste_with_preservation', Mock(
        side_effect=AssertionError('No whole-text paste after cancelled typing')))
    host = NS(config={'paste_min_chars': 1000}, _running=False, recording=False,
              _hold_capture_lifecycle_lock=threading.RLock(), command_mode_active=False,
              _foreground_wants_typed_injection=lambda: True,
              _flight_foreground_process_name=lambda: 'synthetic.exe',
              _record_undoable_paste=Mock(), adaptive_learner=NS(record_transcription=Mock()),
              _show_outcome_chip=Mock(), play_sound=Mock(),
              listening_indicator=NS(flash_success=Mock()),
              _schedule_ui=lambda fn, *args: QTimer.singleShot(0, qapp, lambda: fn(*args)))
    host._paste_preserving_clipboard = DictationApp._paste_preserving_clipboard.__get__(host)
    return host


def test_real_app_receipt_and_feedback_return_to_qt_thread(app, native):
    ui_thread = threading.get_ident()
    feedback_threads = []
    app._show_outcome_chip.side_effect = lambda *args: feedback_threads.append(threading.get_ident())
    text = 'Ab\U0001f600\r\nc' * 3
    assert app._paste_preserving_clipboard(text) is True
    expected = [scan for _, scan, _ in cb.build_unicode_key_events(text)[::2]]
    assert [scan for _, scan in native] == expected
    assert all(thread != ui_thread for thread, _ in native)
    assert feedback_threads == [ui_thread]
    app._show_outcome_chip.assert_called_once_with(paste.UNVERIFIED_DELIVERY, 'warning')
    app.play_sound.assert_not_called()
    app.listening_indicator.flash_success.assert_not_called()
    assert app._typed_delivery_cancel is None


@pytest.mark.parametrize('trigger', ['new_hold', 'speech_onset', 'stop', 'escape'])
def test_capture_and_stop_cancel_between_pairs_without_retry(app, native, monkeypatch, trigger):
    from dictation import DictationApp
    from samsara import execution_policy
    if trigger == 'new_hold':
        monkeypatch.setattr('samsara.vocabulary_decode.log_vocabulary', lambda host: None)
        stop = lambda: DictationApp.start_recording(app)
    elif trigger == 'speech_onset':
        stop = lambda: DictationApp._touch_session_activity(app, speech_onset=True)
    elif trigger == 'stop':
        monkeypatch.setattr(execution_policy, '_stop_all', lambda *args, **kw: {})
        stop = lambda: execution_policy.stop_all(app, chip=False)
    else:
        app._cancel_recording_impl = app._restore_audio = app._close_hold_capture_duck = Mock()
        stop = lambda: DictationApp.cancel_recording(app)
    QTimer.singleShot(90, stop)
    result = app._paste_preserving_clipboard('x' * 300, return_delivery_confirmation=True)
    assert result == (True, False) and result.dispatch == 'partial'
    assert result.outcome == cb.DeliveryOutcome.UNVERIFIED
    assert 0 < len(native) < 300
    app._record_undoable_paste.assert_not_called()
    assert app._last_delivery is None
    app.play_sound.assert_not_called()


def test_cancel_before_first_pair_never_falls_back_to_a_paste(app, native, monkeypatch):
    def snapshot():
        typed_delivery.cancel(app)
        return None

    monkeypatch.setattr(paste, '_editor_snapshot', snapshot)
    result = app._paste_preserving_clipboard('abc', return_delivery_confirmation=True)
    assert result == (False, False)
    assert native == []
    app._record_undoable_paste.assert_not_called()


def test_incomplete_clipboard_fallback_can_be_cancelled(app, native, monkeypatch):
    app.config['paste_min_chars'] = 1
    called = []

    def incomplete(text, **kw):
        assert kw['before_paste']()
        called.append('fallback')
        return kw['incomplete_snapshot_fallback']()

    monkeypatch.setattr('dictation.paste_with_preservation', incomplete)
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'synthetic.exe')
    QTimer.singleShot(100, lambda: typed_delivery.cancel(app))
    result = app._paste_preserving_clipboard('x' * 300, return_delivery_confirmation=True)
    assert result == (True, False) and result.dispatch == 'partial'
    assert 0 < len(native) < 300
    assert called == ['fallback']
    app._record_undoable_paste.assert_not_called()


@pytest.mark.parametrize('new_text', ['NEW', 'Y' * 1001], ids=['typed', 'clipboard'])
def test_reentrant_delivery_cancels_old_and_refuses_overlap_then_allows_next(app, native, new_text):
    newer = []
    QTimer.singleShot(90, lambda: newer.append(app._paste_preserving_clipboard(
        new_text, return_delivery_confirmation=True)))
    old = app._paste_preserving_clipboard('x' * 300, return_delivery_confirmation=True)
    assert newer == [(False, False)]  # Caller retains its newer draft/History.
    assert old == (True, False) and old.dispatch == 'partial'
    prefix = len(native)
    assert 0 < prefix < 300
    assert app._paste_preserving_clipboard('NEW', return_delivery_confirmation=True) == (True, False)
    assert [scan for _, scan in native] == [ord('x')] * prefix + list(map(ord, 'NEW'))


def test_slow_pair_wait_is_interrupted_instead_of_waiting_out_interval(app, native, monkeypatch):
    stop_at = []
    original = cb.type_text_unicode
    monkeypatch.setattr('dictation.type_text_unicode',
                        lambda text: original(text, key_interval=.25))

    def stop():
        stop_at.append(time.perf_counter())
        typed_delivery.cancel(app)

    QTimer.singleShot(90, stop)
    result = app._paste_preserving_clipboard('abc', return_delivery_confirmation=True)
    assert result == (True, False) and result.dispatch == 'partial'
    assert len(native) == 1
    assert time.perf_counter() - stop_at[0] < .15


def test_shutdown_cancels_native_lane_without_an_app_scope(qapp, native):
    QTimer.singleShot(90, typed_delivery._cancel_all)
    receipt = cb.type_text_unicode('abc', key_interval=.25)
    assert receipt.state == 'partial' and receipt.reason == 'cancelled'
    assert len(native) == 1


def test_background_deliveries_share_a_native_lane(qapp, native):
    """Non-GUI callers stay synchronous; their pairs cannot interleave."""
    from samsara.runtime import thread_registry
    start = threading.Barrier(3)
    receipts = []

    def send(text):
        start.wait(timeout=2)
        receipts.append(cb.type_text_unicode(text))

    threads = [thread_registry.spawn('test.typing.1172', send, args=(text,))
               for text in ('aaa', 'BBB')]
    start.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert len(receipts) == 2 and all(receipts)
    actual = ''.join(chr(scan) for _, scan in native)
    assert actual in ('aaaBBB', 'BBBaaa')
