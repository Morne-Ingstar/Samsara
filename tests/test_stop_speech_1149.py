"""Speech-only interruption: real coordinator/UI/mic seam, fake engine/decoder."""
from types import SimpleNamespace
import threading
import time
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from samsara.audio_engine import speech_stop
from samsara.audio_engine.frame import FRAME_SIZE
from samsara.tts import speech_control as control
from samsara.tts.coordinator import AudioCoordinator
from samsara.tts.engine_base import SpeechHandle
from samsara.ui import ava_captions_qt as captions, ava_search_panel_qt as search, qt_runtime


class FakeEngine:
    """Cancel invokes completion, as real engines may do while draining."""
    def __init__(self):
        self.calls = []
        self.pending = []
        self.cancelled = 0

    def speak(self, text, **kwargs):
        handle = SpeechHandle(str(len(self.calls)), _state="playing")
        self.calls.append((text, kwargs, handle))
        self.pending.append(kwargs["on_done"])
        return handle

    def cancel_all(self):
        self.cancelled += 1
        pending, self.pending = self.pending, []
        for done in pending:
            done()

    def get_engine_state(self):
        return "playing" if self.pending else "idle"

    def complete(self):
        self.pending.pop(0)()


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app(monkeypatch, qapp):
    from samsara.session_modes import SessionMode
    # No real polling workers, Qt runtime, microphone, model or app bootstrap.
    monkeypatch.setattr(AudioCoordinator, "_start_interrupt_poll", lambda self: None)
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: False)
    state = SimpleNamespace(
        config={"tts": {"enabled": True}, "command_mode": {"mode": "toggle"}},
        command_mode_active=True, wake_word_active=True, _hotkey_recording=False,
        recording=False, is_speaking=False, silence_start=None, app_state="asleep",
        _session_mode_manager=SimpleNamespace(mode=SessionMode.AVA, draft="retained draft"),
        pending_approval=object(), pending_generation=9, model_lock=threading.Lock(),
        play_sound=Mock(), cancel_recording=Mock(), _command_executed_at=None,
        _tts_last_speaking=0.0, _vad_available=True, _vad_is_speech=Mock(return_value=True),
        _vad_reset=Mock(), _wake_detector=None, _wake_profile_detectors={},
        _oww_wake_detected=False, wake_word_triggered=False, _wake_rearm_needs_onset=False,
        echo_canceller=SimpleNamespace(is_active=False),
    )
    state.audio_coordinator = AudioCoordinator(state, FakeEngine())
    yield state
    state.audio_coordinator.shutdown()
    captions.reset_for_test()
    if search._controller._window is not None:
        search._controller._window.hide()
        search._controller._window.deleteLater()
    search._controller._window = None
    search._controller._app = None
    qapp.processEvents()


def speaking(app, text="A sample answer with information to read aloud.", **kwargs):
    app.audio_coordinator.speak(text, category="ava_response", caption_source="AVA", **kwargs)
    return app.audio_coordinator.engine


def check_preserved(app, approval):
    assert app.pending_approval is approval
    assert app.pending_generation == 9
    assert app.command_mode_active and app.wake_word_active
    assert app._session_mode_manager.draft == "retained draft"
    app.cancel_recording.assert_not_called()


@pytest.mark.parametrize("phrase", ["stop", "Quiet!", "STOP TALKING.", "That’s enough.", "stop speaking"])
def test_voice_stop_cancels_only_audio(app, phrase):
    approval = app.pending_approval
    engine = speaking(app)
    assert control.voice_control(app, phrase)
    assert engine.cancelled == 1
    assert not app.audio_coordinator.is_speaking
    check_preserved(app, approval)


@pytest.mark.parametrize("phrase", ["please stop at the shop", "quiet room", "stop talking about it", "don't stop", "cancel"])
def test_not_a_stop_phrase(app, phrase):
    engine = speaking(app)
    assert not control.voice_control(app, phrase)
    assert engine.cancelled == 0


def test_idle_stop_does_not_clear_other_audio_state(app):
    app.audio_coordinator.transition_to("LISTENING")
    assert not control.stop_speaking(app)
    assert app.audio_coordinator.get_state() == "LISTENING"
    assert app.audio_coordinator.engine.cancelled == 0


def test_cancel_drains_all_audio_without_chaining_next_utterance(app):
    callback = Mock(side_effect=lambda: speaking(app, "Unwanted next chunk."))
    engine = speaking(app, on_done=callback)
    speaking(app, "Queued sentence.", on_done=callback)
    assert control.stop_speaking(app)
    callback.assert_not_called()
    assert len(engine.calls) == 2 and not engine.pending


def test_natural_completion_still_calls_producer(app):
    done = Mock()
    engine = speaking(app, on_done=done)
    engine.complete()
    done.assert_called_once()


def test_repeat_stopped_text_does_not_repeat_actions_or_callbacks(app):
    callback = Mock()
    engine = speaking(app, "A complete answer.", on_done=callback)
    assert control.stop_speaking(app)
    assert control.voice_control(app, "repeat that")
    assert engine.calls[-1][0] == "A complete answer."
    assert engine.calls[-1][1]["category"] == "ava_response"
    engine.complete()
    callback.assert_not_called()


def test_repeat_command_retains_agent_followup_priority_unless_stopped(app, monkeypatch):
    from plugins.commands import message_claude
    agent = Mock(return_value=True)
    monkeypatch.setattr(message_claude.claude_reply, "followup", agent)
    speaking(app)
    assert message_claude.handle_claude_repeat(app, "")
    agent.assert_called_once()
    agent.reset_mock()
    control.stop_speaking(app)
    assert message_claude.handle_claude_repeat(app, "")
    agent.assert_not_called()


def test_plugin_and_ava_entry_points_use_speech_only_stop(app):
    from plugins.commands.speech_control import handle_stop_speaking
    from plugins.commands.dispatch_voice import route_ava_utterance
    engine = speaking(app)
    assert not handle_stop_speaking(app, "about the book")
    assert handle_stop_speaking(app, "")
    speaking(app)
    assert route_ava_utterance(app, "quiet")
    assert route_ava_utterance(app, "repeat that")
    assert engine.cancelled == 2


def test_plugin_cannot_consume_dictated_stop_phrases(app):
    from samsara.session_modes import SessionMode
    from plugins.commands.speech_control import handle_stop_speaking
    engine = speaking(app)
    app._session_mode_manager.mode = SessionMode.DICTATE
    assert not handle_stop_speaking(app, "")
    assert engine.cancelled == 0


@pytest.mark.parametrize("surface", ["caption", "answer", "tray", "keyboard"])
def test_each_ui_path_stops_real_coordinator(app, qapp, surface):
    from samsara.ui.speech_controls_qt import add_tray_controls
    approval = app.pending_approval
    engine = speaking(app)
    if surface == "tray":
        menu = QMenu()
        add_tray_controls(menu, app)
        assert menu.actions()[0].text() == "Stop speaking"
        menu.actions()[0].trigger()
        menu.close()
    elif surface == "answer":
        search.bind_speech(app)
        search._controller._show_on_qt_thread("Example", "Answer", [])
        search._controller._window._speech_controls.stop.click()
    else:
        captions.show_caption(app, "Example readout", source="AVA")
        window = captions.active_window()
        assert window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        if surface == "keyboard":
            menu = QMenu()
            add_tray_controls(menu, app)
            menu.actions()[1].trigger()
            qapp.processEvents()
            assert not window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
            assert window._speech_controls.stop.hasFocus()
            QTest.keyClick(window._speech_controls.stop, Qt.Key.Key_Space)
        else:
            window._speech_controls.stop.click()
    qapp.processEvents()
    assert engine.cancelled == 1 and not app.audio_coordinator.is_speaking
    check_preserved(app, approval)


def test_tray_has_no_stop_when_idle(app):
    from samsara.ui.speech_controls_qt import add_tray_controls
    menu = QMenu()
    add_tray_controls(menu, app)
    assert menu.actions() == []


def test_caption_repeat_button_and_stop_status(app, qapp):
    engine = speaking(app)
    captions.show_caption(app, "Example", source="AVA")
    controls = captions.active_window()._speech_controls
    controls.refresh()
    controls.stop.click()
    assert not controls.stop.isEnabled()
    assert controls.repeat.isEnabled()
    controls.repeat.click()
    assert controls.stop.isEnabled()
    assert len(engine.calls) == 2


def test_hint_shown_once_across_readings_and_restarts(app, monkeypatch, tmp_path, qapp):
    from samsara import hints
    monkeypatch.setattr(hints, "_DATA_DIR", tmp_path)
    app.hints = hints.HintManager(app)
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: True)
    monkeypatch.setattr(qt_runtime, "post", lambda fn: fn())
    speaking(app, "A longer sample reading. " * 20)
    window = captions.active_window()
    qapp.processEvents()
    assert window._speech_controls.hint.isVisible()
    assert window._speech_controls.hint.text() == "Say stop to stop"
    assert control.HINT_ID in app.hints._shown
    assert "ava_captions" not in app.config.get("accessibility", {})
    control.stop_speaking(app)
    speaking(app, "Another long sample. " * 20)
    assert not window._speech_controls.hint.isVisible()
    app.hints = hints.HintManager(app)
    control.stop_speaking(app)
    speaking(app, "After a restart. " * 20)
    assert not window._speech_controls.hint.isVisible()


def test_hint_not_consumed_without_visible_reading(app, monkeypatch, tmp_path):
    from samsara import hints
    monkeypatch.setattr(hints, "_DATA_DIR", tmp_path)
    manager = hints.HintManager(app)
    assert not manager.show_embedded_once(control.HINT_ID, lambda: False)
    assert control.HINT_ID not in manager._shown
    assert manager.show_embedded_once(control.HINT_ID, lambda: True)


def test_long_reading_keeps_stop_visible_and_replaces_old_caption(app, monkeypatch, qapp):
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: True)
    monkeypatch.setattr(qt_runtime, "post", lambda fn: fn())
    captions.show_caption(app, "Previous caption", source="AVA")
    speaking(app, "Longer reading with captions disabled. " * 20)
    window = captions.active_window()
    assert window._text.text() == "Reading aloud"
    window._timer.timeout.emit()
    assert window.isVisible() and window._timer.isActive()
    control.stop_speaking(app)
    window._timer.timeout.emit()
    assert not window.isVisible()


def frame(seq=0, epoch=0, age=0):
    return SimpleNamespace(seq=seq, device_epoch=epoch, t_capture=time.perf_counter() - age,
                           pcm=np.full(FRAME_SIZE, 2000, dtype=np.int16))


def decoder(app, text="stop", **signals):
    segment = SimpleNamespace(text=text, no_speech_prob=.01, avg_logprob=-.1, end=.35)
    for key, value in signals.items():
        setattr(segment, key, value)
    app.model = Mock(transcribe=Mock(return_value=([segment], None)))
    return app.model


def immediate_worker(monkeypatch):
    monkeypatch.setattr(speech_stop.thread_registry, "spawn", lambda name, fn, **kw: fn())


@pytest.mark.parametrize("phrase", ["stop", "quiet", "stop talking", "that's enough"])
def test_pcm_reaches_stop_before_ava_half_duplex_guard(app, monkeypatch, phrase):
    from samsara.audio_engine.wake_consumer import WakeConsumer
    immediate_worker(monkeypatch)
    model = decoder(app, phrase)
    engine = speaking(app)
    consumer = WakeConsumer(Mock(), app)
    for i in range(speech_stop.MIN_FRAMES):
        consumer._process_frame(frame(i))
    assert engine.cancelled == 1
    assert model.transcribe.call_count == 1
    pcm = model.transcribe.call_args.args[0]
    assert pcm.dtype == np.float32 and len(pcm) == FRAME_SIZE * speech_stop.MIN_FRAMES
    app._vad_is_speech.assert_not_called()
    assert consumer._utterance_frames == []


@pytest.mark.parametrize("block", ["hold", "recording", "dictate", "wake_dictation", "snooze", "mic_off"])
def test_stop_decoder_never_owns_dictation_or_unarmed_mic(app, monkeypatch, block):
    from samsara.session_modes import SessionMode
    immediate_worker(monkeypatch)
    decoder(app)
    engine = speaking(app)
    if block == "hold": app._hotkey_recording = True
    if block == "recording": app.recording = True
    if block == "dictate": app._session_mode_manager.mode = SessionMode.DICTATE
    if block == "wake_dictation": app.app_state = "wake_session"
    if block == "snooze": app.snoozed = True
    if block == "mic_off": app.command_mode_active = app.wake_word_active = False
    listener = speech_stop.SpeechStopListener(app)
    for i in range(20): listener.consume(frame(i))
    app.model.transcribe.assert_not_called()
    assert engine.cancelled == 0


@pytest.mark.parametrize("text,signals", [
    ("stop at the next street", {}), ("quiet room", {}), ("stop", {"avg_logprob": -1.5}),
    ("stop", {"no_speech_prob": .9}), ("stop", {"end": .59}),
])
def test_no_partial_low_confidence_or_sentence_stop(app, monkeypatch, text, signals):
    immediate_worker(monkeypatch)
    decoder(app, text, **signals)
    engine = speaking(app)
    listener = speech_stop.SpeechStopListener(app)
    for i in range(6): listener.consume(frame(i))
    assert engine.cancelled == 0


def test_words_in_playback_are_not_a_user_stop(app, monkeypatch):
    immediate_worker(monkeypatch)
    decoder(app, "stop")
    engine = speaking(app, "The instructions say stop when the light changes.")
    listener = speech_stop.SpeechStopListener(app)
    for i in range(6): listener.consume(frame(i))
    assert engine.cancelled == 0
    assert control.stop_speaking(app)  # The visible control remains unconditional.


@pytest.mark.parametrize("change", ["new_answer", "epoch", "gap", "hold", "old_result", "old_frame"])
def test_stale_pcm_cannot_stop_new_or_repurposed_audio(app, monkeypatch, change):
    jobs = []
    monkeypatch.setattr(speech_stop.thread_registry, "spawn", lambda name, fn, **kw: jobs.append(fn))
    decoder(app)
    engine = speaking(app)
    listener = speech_stop.SpeechStopListener(app)
    for i in range(6): listener.consume(frame(i, age=1 if change == "old_frame" else 0))
    if change == "old_frame":
        assert not jobs
        return
    assert len(jobs) == 1
    if change == "new_answer": speaking(app, "A newer answer.")
    if change == "epoch": listener.consume(frame(6, epoch=1))
    if change == "gap": listener.consume(frame(9))
    if change == "hold": app._hotkey_recording = True
    if change == "old_result":
        now = time.perf_counter()
        monkeypatch.setattr(speech_stop.time, "perf_counter", lambda: now + 3)
    jobs[0]()
    assert engine.cancelled == 0


def test_busy_model_does_not_block_capture(app, monkeypatch):
    immediate_worker(monkeypatch)
    decoder(app)
    engine = speaking(app)
    listener = speech_stop.SpeechStopListener(app)
    app.model_lock.acquire()
    try:
        for i in range(6): listener.consume(frame(i))
    finally:
        app.model_lock.release()
    app.model.transcribe.assert_not_called()
    assert not listener.busy.locked() and engine.cancelled == 0


def test_overview_producer_exits_when_stopped_without_completion(app):
    from samsara.window_overview_flow import ReadSequence
    finished = Mock()
    sequence = ReadSequence(app.audio_coordinator, [(1, "First"), (2, "Second")],
                            lambda number: control.stop_speaking(app) if number else None, finished)
    sequence.run()
    assert len(app.audio_coordinator.engine.calls) == 1
    finished.assert_called_once()
