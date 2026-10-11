"""516: a primary hold delivers independently of the hands-free draft."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import kept_session_draft, session_modes, streaming
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.model import CaptureState, Lane, LiveSurfaceModel
from samsara.live_surface.partials import CapturePartials
from samsara.live_surface.placement import Edge, Placement, Rect, Screen, placement_record
from samsara.ui.live_surface_qt import LiveSurfaceWidget
from tests.test_hold_types_with_kept_draft_508 import kept, save_picture, snapshot
from tests.test_hold_during_hands_free import _loud_frame, _make_consumer
from tests.test_dictation_surface_recovery import capture_app
from tests.test_kept_draft_survives_505 import SIGNALS


HOLD = 'Synthetic independent hold.'
NEXT = 'Synthetic next session sentence.'


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
@pytest.mark.parametrize('send', ['button', 'paste word'])
def test_live_hold_delivers_once_then_session_sends_only_its_words(kept, full, send, monkeypatch):
    monkeypatch.setattr(session_modes, 'DICTATE_COMMIT_PHRASE', 'paste')
    monkeypatch.setattr(session_modes, '_DICTATE_COMMIT_HOMOPHONES', frozenset({'paste'}))
    r = kept(full, live=True)
    before = snapshot(r.manager)
    original = r.manager.dictate_pending_buffer
    r.session._live_session_capture = True
    r.session._deliver_final(HOLD, None, 1.0, 0)
    r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    r.appended.assert_not_called()
    assert snapshot(r.manager) == before
    assert r.app.command_mode_active
    r.app._log_history.assert_called_once()
    r.app.add_to_history.assert_called_once_with(HOLD, is_command=False)
    assert r.manager.dispatch_utterance(NEXT, SIGNALS).kind == 'dictate_staged'
    outcome = (r.manager.commit_pending_dictation() if send == 'button' else
               r.manager.dispatch_utterance('paste', SIGNALS))
    assert outcome.kind == 'dictate_committed'
    assert r.inserted == [original + ' ' + NEXT]
    assert HOLD not in r.inserted[0]


@pytest.mark.parametrize('reason', ['pause', 'voice review', 'focus changed', 'failed delivery'])
def test_live_refused_paused_or_failed_hold_preserves_session_snapshot(kept, monkeypatch, reason):
    r = kept(live=True)
    r.session._live_session_capture = True
    r.app._show_outcome_chip = Mock()
    before = snapshot(r.manager)
    text = HOLD
    if reason == 'pause':
        r.app._hold_park_requested = True
    elif reason == 'voice review':
        text += ' pause draft'
    elif reason == 'focus changed':
        monkeypatch.setattr(streaming._user32, 'GetForegroundWindow', lambda: 456)
    else:
        r.app._paste_preserving_clipboard.return_value = False
    r.session._deliver_final(text, None, 1.0, 0)
    assert snapshot(r.manager) == before
    r.appended.assert_not_called()
    r.app.add_to_history.assert_called_once_with(text, is_command=False)
    r.app._log_history.assert_called_once()
    if reason == 'failed delivery':
        r.app._paste_preserving_clipboard.assert_called_once_with(text)
    else:
        r.app._paste_preserving_clipboard.assert_not_called()
        r.app._show_outcome_chip.assert_called_once()
    assert r.app.command_mode_active


@pytest.mark.parametrize('finish', ['delivered', 'empty', 'cancelled'])
def test_full_live_hold_preview_returns_to_waiting_session(qapp, kept, monkeypatch, finish):
    r = kept(live=True)
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda fn: fn())
    clock = [0.0]
    r.app.config['ui']['live_surface']['placement'] = {
        'preferred_monitor': 'synthetic', 'monitors': {
            'synthetic': placement_record(Placement(Edge.BOTTOM, 0.0))}}
    c = LiveSurfaceController(r.app, model=LiveSurfaceModel(clock=lambda: clock[0]),
        widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True))
    c._screens = lambda: [Screen('synthetic', Rect(0, 0, 1280, 720), True)]
    c.idle_capture = CaptureState.HANDS_FREE_LISTENING
    c.idle_lane = Lane.HANDS_FREE_DICTATE
    r.app.live_surface = c
    before = snapshot(r.manager)
    c.begin_capture(Lane.HANDS_FREE_DICTATE)
    c.sync_draft(r.manager)
    c.start()
    destination = os.environ.get('SAMSARA_516_PROOF_DIR')

    def picture(state):
        qapp.processEvents()
        if destination and finish == 'delivered':
            save_picture(c.widget, destination, 'Full_dark_100_' + state, 1280, 720)

    try:
        picture('before')
        token = c.begin_capture(Lane.HOLD)
        partials = CapturePartials(c, Lane.HOLD, token)
        r.session._overlay = streaming._LiveSurfaceStreamingOverlay(partials)
        partials.partial('Synthetic hold in progress.')
        picture('during')
        assert c.view().provisional_text == 'Synthetic hold in progress.'
        assert c.view().text == ''
        assert snapshot(r.manager) == before
        if finish == 'cancelled':
            r.session.cancel()
        else:
            r.session._deliver_final(HOLD if finish == 'delivered' else '', None, 1.0, 0)
        clock[0] = 3.0
        c.refresh_expired()
        picture('after')
        assert snapshot(r.manager) == before
        assert c.view().text == r.manager.draft_document.text
        assert c.view().provisional_text == ''
        assert c.view().capture is CaptureState.HANDS_FREE_LISTENING
        assert c.view().lane is Lane.HANDS_FREE_DICTATE
        if finish == 'delivered':
            r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    finally:
        c.close()


def test_live_capture_onset_flushes_pending_audio_once_and_excludes_hold_frames(kept, monkeypatch):
    r = kept(live=True)
    app = r.app
    app.config.update(mode='hold', command_mode={'mode': 'toggle'})
    app.is_speaking = True
    app.app_state = 'idle'
    app._hotkey_recording = False
    app._vad_reset = Mock()
    app._close_hands_free_capture_duck = Mock()
    wake = object.__new__(WakeConsumer)
    wake._app = app
    frames = [np.ones(1600, dtype=np.float32)]
    wake._utterance_frames = frames
    wake._buffer_rms_history = [0.1]
    wake._speech_start_ns = 1
    wake._last_voice_ns = 2
    wake._onset_context = None
    wake._hands_free_capture_duck_token = 7
    queued = []
    wake._enqueue_toggle_utterance = lambda *args: queued.append(args)
    app._wake_consumer = wake
    app._pause_command_mode_inactivity_for_hold = Mock()
    app._resume_command_mode_inactivity_after_hold = Mock()
    wake._hotkey_suppressed_last = False
    wake._last_epoch = 0
    wake._log_frame = Mock()
    app._hotkey_recording = True
    kept_session_draft.suspend_hands_free_for_hold(app)
    assert len(queued) == 1
    assert queued[0][0] is frames and queued[0][1] == 7
    assert not wake._utterance_frames
    # A hold frame reaches the actual wake admission gate, before VAD/decode.
    wake._process_frame(SimpleNamespace(device_epoch=0))
    assert not wake._utterance_frames and len(queued) == 1
    app._pause_command_mode_inactivity_for_hold.assert_called_once()
    # The queued chunk finishes while the independent hold owns the display.
    assert r.manager.dispatch_utterance('Synthetic in-flight session chunk.', SIGNALS).kind == 'dictate_staged'
    r.session._deliver_final(HOLD, None, 1.0, 0)
    r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    assert r.manager.dictate_pending_buffer.count('Synthetic in-flight session chunk.') == 1
    assert HOLD not in r.manager.dictate_pending_buffer
    assert app.command_mode_active
    # Exercise the real post-hold wake gate: new speech resumes, held speech
    # never accumulated, and the toggle session's inactivity timer resumes.
    resumed, reader, resumed_app = _make_consumer(hotkey_recording=True)
    resumed._process_frame(_loud_frame())
    assert not resumed._utterance_frames
    resumed_app._hotkey_recording = False
    resumed._process_frame(_loud_frame())
    resumed._process_frame(_loud_frame())
    assert len(resumed._utterance_frames) == 1
    resumed_app._resume_command_mode_inactivity_after_hold.assert_called_once()
    reader.snap_to_head.assert_not_called()


@pytest.mark.parametrize('reason', ['pause', 'voice review', 'focus changed'])
def test_batch_refused_or_paused_hold_uses_history_without_modifying_session(kept, reason):
    r = kept(live=True)
    r.app._park_hold_feedback = Mock()
    before = snapshot(r.manager)
    kept_session_draft.park_batch_hold(r.app, r.manager, HOLD, HOLD, 1.0, source=reason)
    assert snapshot(r.manager) == before
    r.appended.assert_not_called()
    r.app._paste_preserving_clipboard.assert_not_called()
    r.app.add_to_history.assert_called_once_with(HOLD, is_command=False)
    r.app._log_history.assert_called_once()
    r.app._park_hold_feedback.assert_called_once()


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
def test_hold_capture_boundary_cannot_rewind_into_flushed_session_audio(kept, full, monkeypatch):
    from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
    from samsara.hold_capture_onset import HoldOnset
    r = kept(full, live=True)
    r.app.config['command_mode'] = {'mode': 'toggle'}
    r.app._wake_consumer = Mock()
    r.app._hold_capture_duck_token = 3
    r.app._hold_capture_duck_confirmed_at = 10.0
    r.app._hold_capture_onset = HoldOnset(3, 10.0, 10.0, True, Mock())
    monkeypatch.setattr('time.perf_counter', lambda: 10.1)
    kept_session_draft.suspend_hands_free_for_hold(r.app)
    c = object.__new__(DictationSessionConsumer)
    c._app = r.app
    c._capture_not_before = c._select_capture_boundary()
    c._capture_not_after = None
    assert c._capture_not_before == 10.1
    assert not c._frame_inside_capture_duck(SimpleNamespace(t_capture=10.05))
    assert c._frame_inside_capture_duck(SimpleNamespace(t_capture=10.3))
    r.app._wake_consumer.flush_utterance.assert_called_once()
    r.app._wake_consumer.discard_stale_wake_utterance.assert_called_once()


def test_capture_start_hands_older_chunk_off_before_opening_hold_audio(capture_app, monkeypatch):
    import time
    from samsara import listening_display
    app, manager, _ = capture_app
    monkeypatch.setitem(app._start_recording_impl.__func__.__globals__, 'time', time)
    monkeypatch.setitem(app._live_surface_hold_parking_enabled.__func__.__globals__,
                        'listening_display', listening_display)
    manager.dispatch_utterance('Synthetic session draft.', SIGNALS)
    app._session_mode_manager = manager
    app.command_mode_active = True
    events = []
    def flush():
        assert app._hotkey_recording
        events.append('session chunk')
    app._wake_consumer = SimpleNamespace(flush_utterance=flush,
        discard_stale_wake_utterance=lambda: events.append('wake reset'))
    app._dictation_consumer.activate.side_effect = lambda: events.append('hold audio') or True
    before = snapshot(manager)
    assert app._start_recording_impl(streaming=False, play_earcon=False)
    assert events == ['session chunk', 'wake reset', 'hold audio']
    assert app._hold_park_requested is False
    assert snapshot(manager) == before


def test_resumed_hands_free_preroll_excludes_the_hold_tail(monkeypatch):
    from samsara.audio_engine.ring import EMPTY
    wake, reader, app = _make_consumer(hotkey_recording=True)
    monkeypatch.setattr('time.perf_counter', lambda: 10.0)
    wake._process_frame(_loud_frame())
    held = _loud_frame()
    held.t_capture = 9.9
    fresh = _loud_frame()
    fresh.t_capture = 10.3
    fresh.pcm[:] = 16000
    reader.read_next.side_effect = [held, fresh, EMPTY]
    app._hotkey_recording = False
    wake._process_frame(fresh)
    assert len(wake._utterance_frames) == 1
    np.testing.assert_array_equal(wake._utterance_frames[0], fresh.pcm.astype(np.float32) / 32767.0)


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
def test_pause_control_does_not_mark_the_session_document_parked(kept, full):
    r = kept(full, live=True)
    before = snapshot(r.manager)
    kept_session_draft.pause_hold(r.app, r.manager, source='pause button')
    assert snapshot(r.manager) == before


def test_older_session_final_defers_projection_while_hold_owns_full(kept, monkeypatch):
    r = kept(live=True)
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda fn: fn())
    c = LiveSurfaceController(r.app)
    r.app.live_surface = c
    c.sync_draft(r.manager)
    token = c.begin_capture(Lane.HOLD)
    partials = CapturePartials(c, Lane.HOLD, token)
    kept_session_draft.begin_hold(partials)
    partials.partial('Synthetic hold in progress.')
    r.manager.dispatch_utterance(NEXT, SIGNALS)
    kept_session_draft.sync_session_draft(c, r.manager)
    assert c.view().text == ''
    assert c.view().provisional_text == 'Synthetic hold in progress.'
    kept_session_draft.finish_hold(partials)
    assert c.view().text == r.manager.dictate_pending_buffer
    assert NEXT in c.view().text and HOLD not in c.view().text


def test_compact_live_preview_pauses_then_returns_to_original_session(qapp, kept, monkeypatch):
    from samsara.ui.listening_indicator import ListeningIndicator
    r = kept(False, live=True)
    monkeypatch.setattr(streaming.StreamingOverlayQt, '_post', staticmethod(lambda fn: fn()))
    r.app.config['command_mode'] = {'preview_position': 'bottom-left'}
    r.app.is_speaking = False
    r.app._hotkey_recording = False
    r.app._streaming_session = None
    preview = streaming.DictatePreviewSession(r.app)
    indicator = ListeningIndicator()
    indicator.set_idle_animation(False)
    indicator.set_command_mode(True)
    indicator.show()
    r.app.listening_indicator = indicator
    original = r.manager.dictate_pending_buffer
    before = snapshot(r.manager)
    destination = os.environ.get('SAMSARA_516_PROOF_DIR')

    def picture(state):
        qapp.processEvents()
        if destination:
            from PySide6.QtGui import QColor, QPainter, QPixmap
            widget = preview._overlay._widget._w
            area = widget.screen().availableGeometry()
            save_picture(widget, destination, 'Compact_dark_100_' + state,
                         area.width(), area.height())
            # Both real Compact widgets, arranged on a synthetic desktop so
            # the pill cannot cover the waiting words in this proof image.
            canvas = QPixmap(area.width(), area.height())
            canvas.fill(QColor('#090c11'))
            painter = QPainter(canvas)
            painter.drawPixmap(12, area.height() - widget.height() - 12, widget.grab())
            painter.drawPixmap(12, area.height() - widget.height() - indicator.height() - 24,
                               indicator.grab())
            painter.end()
            canvas.save(os.path.join(destination, 'Compact_dark_100_' + state + '_desktop.png'))

    try:
        preview._overlay.show()
        preview.on_utterance_final(original)
        picture('before')
        r.app._hotkey_recording = True
        r.app._streaming_session = r.session
        indicator.set_listening(True)
        assert preview._suspended_for_hold()
        preview._enter_hold_pause(False)
        picture('during')
        assert 'Paused (hold)' in preview._overlay._widget._w._label.text()
        # An older chunk can finish decoding after the hold took ownership.
        # Its text stays in the session, and Compact still reports the pause.
        r.manager.dispatch_utterance(NEXT, SIGNALS)
        kept_session_draft.on_session_final(preview, NEXT)
        assert 'Paused (hold)' in preview._overlay._widget._w._label.text()
        before = snapshot(r.manager)
        original += ' ' + NEXT
        r.session._deliver_final(HOLD, None, 1.0, 0)
        indicator.set_listening(False)
        assert not preview._suspended_for_hold()
        preview._render('')
        picture('after')
        from PySide6.QtGui import QTextDocument
        visible = QTextDocument()
        visible.setHtml(preview._overlay._widget._w._label.text())
        visible_text = ' '.join(visible.toPlainText().split())
        assert original in visible_text
        assert HOLD not in visible_text
        assert 'Paused (hold)' not in visible_text
        assert snapshot(r.manager) == before
        r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    finally:
        preview.stop()
        indicator.destroy()
        indicator.deleteLater()


def test_full_insert_button_after_hold_sends_only_session_words(qapp, kept, monkeypatch):
    r = kept(live=True)
    r.manager.pause_draft(source='session review')
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda fn: fn())
    c = LiveSurfaceController(r.app, widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True))
    r.app.live_surface = c
    c.idle_capture = CaptureState.HANDS_FREE_LISTENING
    c.idle_lane = Lane.HANDS_FREE_DICTATE
    c.begin_capture(Lane.HANDS_FREE_DICTATE)
    c.sync_draft(r.manager)
    c.start()
    original = r.manager.dictate_pending_buffer
    before = snapshot(r.manager)
    try:
        token = c.begin_capture(Lane.HOLD)
        r.session._overlay = streaming._LiveSurfaceStreamingOverlay(CapturePartials(c, Lane.HOLD, token))
        r.session._deliver_final(HOLD, None, 1.0, 0)
        assert snapshot(r.manager) == before
        r.manager.dispatch_utterance(NEXT, SIGNALS)
        c.sync_draft(r.manager)
        assert c.widget._insert.isVisible()
        c.widget._insert.click()
        assert r.inserted == [original + ' ' + NEXT]
        assert HOLD not in r.inserted[0]
        r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    finally:
        c.close()
