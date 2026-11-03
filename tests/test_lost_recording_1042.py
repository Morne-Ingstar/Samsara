"""1042: real stop/delivery paths with synthetic PCM and isolated History."""
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import decode_recovery, failure_reasons, mic_score, recent_privacy
from samsara.history import HistoryManager
from samsara.hold_recording_recovery import EMPTY, RETRY
from samsara.streaming import StreamingSession, StreamingWorker
from samsara.ui.history_view import copy_text, list_text


@pytest.fixture
def capture(tmp_path, monkeypatch):
    db = HistoryManager(tmp_path / 'history.db')
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy('', 'editor.exe'))
    app = NS(model_rate=16000, config={'auto_paste': False, 'hotkey': 'mouse5'},
             history_db=db, recording=False, _get_foreground_app=lambda: 'Synthetic editor',
             _on_streaming_session_finished=Mock(), _show_outcome_chip=Mock(),
             add_to_history=Mock(), _notify_main_window=Mock(),
             _schedule_ui=lambda fn, *a: fn(*a))
    app._log_history = lambda **kw: mic_score.log_history(app, **kw)
    session = StreamingSession.__new__(StreamingSession)
    session.app = app
    session._state = session.STATE_FINALIZING
    session._state_lock = threading.Lock()
    session.stop_event = threading.Event()
    session.cancel_event = threading.Event()
    session._capture_cleanup_lock = threading.Lock()
    session._capture_cleaned = session._finished_notified = False
    session._direct_paste = session._last_pasted = session._parking_enabled = False
    session._last_partial = ''
    session._overlay = Mock()
    session._worker = NS(_final_params=lambda: {'language': 'en'})
    app._streaming_session = session
    pcm = np.full(19200, .02, dtype=np.float32)
    consumer = NS(stop_streaming=Mock(return_value=pcm), capture_loss=False)
    app._dictation_consumer = consumer
    yield session, app, pcm
    db.close()


def rows(app):
    return [dict(r) for r in app.history_db.recent()]


@pytest.mark.parametrize('reason', ['password_field', 'unverified_target'])
def test_redacted_streaming_row_explains_privacy_without_recovering_words(capture, monkeypatch, reason):
    session, app, _ = capture
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy(reason))
    app._log_history(raw_text='synthetic private fixture', mode='streaming')
    row = rows(app)[0]
    assert row['raw_text'] == row['display_text'] == row['app_context'] == ''
    assert 'Text hidden' in list_text(row)
    assert copy_text(row) == ''
    assert 'synthetic' not in list_text(row)


def test_delivery_redaction_of_existing_success_also_explains_blank_row(capture, monkeypatch):
    _, app, _ = capture
    app._log_history(raw_text='synthetic text', mode='streaming')
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy('unverified_target'))
    assert recent_privacy.before_delivery(app, 'synthetic text')
    assert list_text(rows(app)[0]).startswith('Text hidden')


@pytest.mark.parametrize('text', ['', '  \n '])
def test_legacy_blank_history_row_has_explanation(text):
    row = dict(raw_text=text, display_text=text, mode='streaming', status='success')
    assert list_text(row) == 'No text was saved for this recording'


def test_early_release_speech_gate_rejection_keeps_audio_and_explains_history(capture, monkeypatch):
    session, app, pcm = capture
    # Reproduce the reported 1.2-second stop with a deterministic gate rejection.
    monkeypatch.setattr('samsara.speech_presence.skip_decode', lambda *a, **kw: True)
    worker = StreamingWorker(session)
    worker._final_pass()
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)
    assert app._hold_retry_audio.audio is not pcm
    row, = rows(app)
    assert row['display_text'] == EMPTY
    assert row['duration_ms'] == 1200 and row['status'] == 'failed'
    assert RETRY in list_text(row)
    assert session._overlay.show_empty_result.call_args.args == (EMPTY,)
    assert app._show_outcome_chip.call_args.args[0] == EMPTY


def test_retry_command_decodes_the_retained_capture(capture, monkeypatch):
    session, app, pcm = capture
    session._stop_capture()
    session.on_final(None, duration_s=1.2)
    command_audio = np.ones(16000, dtype=np.float32)
    seen = []
    def transcribe(_app, audio, **params):
        seen.append(audio.copy())
        text = 'try again' if len(seen) == 1 else 'Recovered synthetic words.'
        return iter([NS(text=text)]), NS(language='en')
    monkeypatch.setattr(decode_recovery, 'transcribe_with_vocabulary', transcribe)
    worker = NS(_session=NS(app=app, cancel_event=threading.Event()), _decoder_guard=lambda *a, **kw: None)
    app.model_lock = threading.Lock()
    _, _, text = decode_recovery.stream_final(worker, command_audio, {})
    assert text == 'Recovered synthetic words.'
    np.testing.assert_array_equal(seen[1], pcm)


def test_failed_retry_retains_original_take_instead_of_retry_command_audio(capture):
    session, app, _ = capture
    original = np.full(48000, .03, dtype=np.float32)
    session._stop_capture()  # New, shorter "try again" command capture.
    session._decode_audio = original
    session._decode_params = {'language': 'en'}
    session._decode_failure = decode_recovery.UNCLEAR
    session.on_final(None)
    np.testing.assert_array_equal(app._hold_retry_audio.audio, original)
    assert rows(app)[0]['duration_ms'] == 3000


def test_accepted_preview_survives_empty_final(capture):
    session, app, pcm = capture
    session._last_partial = 'Accepted synthetic preview.'
    session._stop_capture()
    session.on_final(None, duration_s=1.2)
    row, = rows(app)
    assert row['raw_text'] == row['display_text'] == session._last_partial
    assert session._fallback_notice_rejected is True
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)


def test_cancel_keeps_preview_and_audio_once_without_insertion(capture):
    session, app, pcm = capture
    session._last_partial = 'Accepted synthetic preview.'
    session.cancel()
    retained = app._hold_retry_audio
    session.on_cancelled()
    session.on_final('Late discarded decoder output')
    row, = rows(app)
    assert row['display_text'] == session._last_partial and row['status'] == 'failed'
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)
    assert app._hold_retry_audio is retained
    app._dictation_consumer.stop_streaming.assert_called_once()
    app._on_streaming_session_finished.assert_called_once_with(session)
    app.add_to_history.assert_not_called()
    assert app._show_outcome_chip.call_count == 1


def test_cancel_without_preview_leaves_retry_guidance(capture):
    session, app, _ = capture
    session.cancel()
    assert RETRY in rows(app)[0]['display_text']


def test_stop_racing_cancel_still_retains_returned_pcm(capture):
    session, app, pcm = capture
    # Cancel arrives inside the stop operation, after cleanup was claimed.
    def stop():
        session.cancel()
        return pcm
    app._dictation_consumer.stop_streaming.side_effect = stop
    session._stop_capture()
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)
    assert len(rows(app)) == 1


def test_decoder_exception_keeps_stopped_pcm(capture, monkeypatch):
    session, app, pcm = capture
    monkeypatch.setattr(failure_reasons, 'finish_streaming_if_empty', lambda *a: False)
    monkeypatch.setattr('samsara.hold_final_policy.final_decode', Mock(side_effect=RuntimeError('synthetic failure')))
    StreamingWorker(session)._final_pass()
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)
    assert RETRY in rows(app)[0]['display_text']


def test_missing_pcm_never_claims_audio_was_kept(capture):
    session, app, _ = capture
    app._dictation_consumer.stop_streaming.return_value = None
    session._stop_capture()
    session.on_final(None)
    assert not hasattr(app, '_hold_retry_audio')
    assert rows(app)[0]['display_text'] == 'No audio was captured. Check the microphone.'


def test_capture_loss_keeps_only_the_audio_that_arrived(capture):
    session, app, pcm = capture
    app._dictation_consumer.capture_loss = True
    StreamingWorker(session)._final_pass()
    np.testing.assert_array_equal(app._hold_retry_audio.audio, pcm)
    assert rows(app)[0]['display_text'] == 'Audio capture stopped. ' + RETRY


def test_late_cancel_callback_cannot_replace_a_newer_retry(capture):
    session, app, _ = capture
    session.cancel()
    newer = object()
    app._hold_retry_audio = newer
    session.on_cancelled()
    assert app._hold_retry_audio is newer


def test_success_is_one_history_row_and_does_not_replace_previous_retry(capture):
    session, app, _ = capture
    previous = object()
    app._hold_retry_audio = previous
    session._stop_capture()
    session.on_final('Successful synthetic words.', duration_s=1.2)
    assert len(rows(app)) == 1
    assert app._hold_retry_audio is previous


def test_failed_capture_still_respects_history_privacy(capture, monkeypatch):
    session, app, _ = capture
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy('password_field'))
    session._stop_capture()
    session.on_final(None)
    row, = rows(app)
    assert row['raw_text'] == row['display_text'] == row['app_context'] == ''
    assert list_text(row) == 'Text hidden for a password field'


@pytest.mark.parametrize('injected', [False, True])
def test_delivered_mouse_release_ends_hold_regardless_of_injection_flag(capture, monkeypatch, injected):
    from samsara import hotkeys
    from samsara.mouse_hook import MouseHook
    session, app, _ = capture
    app.recording = True
    app._hold_down_mouse = True
    app._main_hotkey_source = 'mouse'
    app._stop_in_flight = False
    app.stop_recording = Mock(side_effect=session.finalize)
    monkeypatch.setattr(hotkeys.thread_registry, 'spawn', lambda name, fn, **kw: fn())
    monkeypatch.setattr(hotkeys.flight_recorder, 'record', Mock())
    hook = MouseHook.__new__(MouseHook)
    hook._recover_from_callbacks = Mock()
    hook.on_button_event = lambda name, pressed: hotkeys.HotkeyCluster._on_main_hotkey_mouse(app, pressed)
    session._state = session.STATE_STREAMING
    hook._deliver(('mouse5', False, 0., injected))
    app.stop_recording.assert_called_once()
    assert session.stop_event.is_set()
    assert session._state == session.STATE_FINALIZING


def test_history_context_and_later_terminal_undo_can_differ(capture, monkeypatch, caplog):
    import logging
    from samsara import paste
    _, app, _ = capture
    app._get_foreground_app = lambda: 'Synthetic Obsidian window'
    app._log_history(raw_text='Synthetic delivered words.', mode='streaming')
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'pwsh.exe')
    app._record_undoable_paste = Mock()
    with caplog.at_level(logging.INFO):
        paste._arm_undo_after_paste(app, 'Synthetic delivered words.', 123, lambda: 123)
    assert rows(app)[0]['app_context'] == 'Synthetic Obsidian window'
    assert '[PASTE] undo not armed: terminal' in caplog.text
    app._record_undoable_paste.assert_not_called()
