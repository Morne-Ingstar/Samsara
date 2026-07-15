import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from dictation import DictationApp
from samsara.streaming import StreamingSession


def _bare_streaming_session():
    consumer = MagicMock()
    app = SimpleNamespace(
        _dictation_consumer=consumer,
        _on_streaming_session_finished=MagicMock(),
    )
    session = StreamingSession.__new__(StreamingSession)
    session.app = app
    session._state = StreamingSession.STATE_STREAMING
    session._state_lock = threading.Lock()
    session.stop_event = threading.Event()
    session.cancel_event = threading.Event()
    session._direct_paste = False
    session._last_pasted = False
    session._overlay = MagicMock()
    session._capture_cleanup_lock = threading.Lock()
    session._capture_cleaned = False
    session._finished_notified = False
    return session, app, consumer


def test_streaming_cancel_stops_accumulator_closes_overlay_and_releases_owner():
    session, app, consumer = _bare_streaming_session()

    session.cancel()
    session.on_cancelled()  # worker callback may arrive after synchronous cancel

    assert session.cancel_event.is_set()
    assert session.stop_event.is_set()
    consumer.stop_streaming.assert_called_once_with()
    session._overlay.close.assert_called()
    app._on_streaming_session_finished.assert_called_once_with(session)


def test_cancelled_final_result_is_never_delivered_or_pasted():
    session, app, _consumer = _bare_streaming_session()
    app._schedule_ui = MagicMock()
    session.cancel_event.set()

    session.on_final("stale text")

    app._schedule_ui.assert_not_called()
    app._on_streaming_session_finished.assert_called_once_with(session)


def test_start_recording_refuses_to_overwrite_active_streaming_owner():
    app = DictationApp.__new__(DictationApp)
    app.model_loaded = True
    app.loading_model = False
    app.recording = False
    app._streaming_session = object()
    app._stop_in_flight = False
    app._duck_audio = MagicMock()

    app.start_recording(streaming=False)

    app._duck_audio.assert_not_called()


def test_escape_cancel_handles_streaming_even_if_recording_flag_was_lost():
    app = DictationApp.__new__(DictationApp)
    session = MagicMock()
    app._streaming_session = session
    app.recording = False  # reproduces detached shared-state condition
    app.hotkey_pressed = False
    app._hotkey_recording = True
    app._ace_streaming_active = True
    app.set_app_state = MagicMock()
    app.play_sound = MagicMock()

    def _cancel_and_release():
        app._on_streaming_session_finished(session)

    session.cancel.side_effect = _cancel_and_release

    app.cancel_recording()

    app.set_app_state.assert_called_once_with(recording=False)
    session.cancel.assert_called_once_with()
    assert app._streaming_session is None
    assert app._ace_streaming_active is False
    assert app._hotkey_recording is False


def test_disabling_streaming_cancels_session_before_unhooking_capslock():
    app = DictationApp.__new__(DictationApp)
    app.config = {"streaming_mode": True}
    app._config_lock = threading.Lock()
    app._capslock_lifecycle_lock = threading.Lock()
    app._streaming_session = object()
    events = []

    def _cancel():
        events.append("cancel")
        app._streaming_session = None

    app.cancel_recording = _cancel
    app.save_config = MagicMock()
    app._install_capslock_hook = MagicMock()
    app._uninstall_capslock_hook = MagicMock(side_effect=lambda: events.append("unhook"))

    app.set_streaming_mode(False)

    assert events == ["cancel", "unhook"]
    assert app.config["streaming_mode"] is False
    app.save_config.assert_called_once_with()


def test_stale_session_completion_cannot_release_a_new_owner():
    app = DictationApp.__new__(DictationApp)
    stale = object()
    current = object()
    app._streaming_session = current
    app._ace_streaming_active = True
    app.recording = True
    app._hotkey_recording = True

    app._on_streaming_session_finished(stale)

    assert app._streaming_session is current
    assert app._ace_streaming_active is True
    assert app._hotkey_recording is True


def test_capslock_release_cannot_stop_a_batch_recording_it_did_not_start():
    app = DictationApp.__new__(DictationApp)
    app._capslock_lifecycle_lock = threading.Lock()
    app._capslock_streaming_session = None
    app._streaming_session = None
    app.recording = True
    app.stop_recording = MagicMock()

    app._capslock_stop_streaming()

    app.stop_recording.assert_not_called()


def test_capslock_release_stops_only_its_owned_streaming_session():
    app = DictationApp.__new__(DictationApp)
    session = object()
    app._capslock_lifecycle_lock = threading.Lock()
    app._capslock_streaming_session = session
    app._streaming_session = session
    app.recording = True
    app.stop_recording = MagicMock()

    app._capslock_stop_streaming()

    app.stop_recording.assert_called_once_with()
    assert app._capslock_streaming_session is None
