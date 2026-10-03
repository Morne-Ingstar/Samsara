"""263 finish2: real surface transitions with synthetic audio/delivery owners."""
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace, MethodType
from unittest.mock import Mock
import ast
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import (CaptureState, DocumentState, EventKind, Lane,
    LiveSurfaceModel, SurfaceEvent, VisibleForm)
from samsara.live_surface.partials import CapturePartials
from samsara.streaming import DictatePreviewSession, StreamingSession, _LiveSurfaceDictateOverlay
from tests.test_hold_park_220 import make_manager


@pytest.fixture
def surface(qapp, monkeypatch):
    posted = []
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', posted.append)
    clock = [10.0]
    manager, injected, _ = make_manager()
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}},
        device_type='cuda', is_speaking=False, _session_mode_manager=manager)
    c = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: clock[0]))
    app.live_surface = c
    c.start(); c.widget.set_reduced_motion(False)
    yield c, app, clock, posted, injected
    c.close(); qapp.processEvents()


def flush(posted):
    while posted:
        posted.pop(0)()


def test_hold_then_handsfree_uses_current_lane_and_vad_before_any_partial(surface):
    c, app, _, posted, _ = surface
    hold = c.begin_capture(Lane.HOLD); c.drain()
    assert c.widget._state.text() == 'Hold — recording'
    assert c.widget._mark.capture == 'recording'
    c.stop_capture(capture_id=hold)
    indicator = LiveSurfaceIndicatorAdapter(c); indicator.set_session_mode('HANDS FREE')
    session = DictatePreviewSession(app)
    c.drain(); c.refresh_expired()
    assert c.view().lane is Lane.HANDS_FREE_DICTATE
    assert c.widget._state.text() == 'Hands-free — listening'
    assert c.widget._mark.capture == 'listening' and c.widget._spin_timer.isActive()
    app.is_speaking = True
    c.refresh_expired()
    assert c.widget._state.text() == 'Hands-free — recording'
    assert c.widget._mark.capture == 'recording' and c.widget._spin_timer.isActive()
    app.is_speaking = False
    c.refresh_expired()
    assert c.widget._state.text() == 'Hands-free — listening'
    assert c.widget._mark.capture == 'listening' and c.widget._spin_timer.isActive()
    session._overlay.close()


def test_model_capture_start_derives_state_without_previous_lane():
    m = LiveSurfaceModel()
    for i, (lane, expected) in enumerate([(Lane.HOLD,CaptureState.RECORDING),
            (Lane.HANDS_FREE_DICTATE,CaptureState.HANDS_FREE_LISTENING),
            (Lane.HOLD,CaptureState.RECORDING)]):
        m.consume(SurfaceEvent(EventKind.CAPTURE_STARTED,capture_id=str(i),sequence=1,lane=lane))
        assert m.capture is expected and m.lane is lane


def test_rolling_preview_never_republishes_committed_lines_as_provisional(surface):
    c, app, _, _, _ = surface
    token = c.begin_capture(Lane.HANDS_FREE_DICTATE)
    app._session_mode_manager.stage_parked_hold('Synthetic settled sentence. ')
    c.sync_draft(app._session_mode_manager); c.drain()
    overlay = _LiveSurfaceDictateOverlay(c,CapturePartials(c,Lane.HANDS_FREE_DICTATE,token))
    overlay.set_transcript(['Synthetic settled sentence.'], 'Only new speech')
    c.drain()
    assert c.widget._transcript.toPlainText().count('Synthetic settled sentence.') == 1
    assert c.view().provisional_text == 'Only new speech'
    assert 'Synthetic settled sentence.' not in c.widget._provisional.text()
    overlay.set_transcript(['Synthetic settled sentence.'], '')
    c.drain()
    assert not c.view().provisional_text and not c.widget._provisional.isVisible()
    # A new utterance may intentionally repeat a sentence. Do not guess audio
    # identity from matching words and silently remove the new utterance.
    overlay.set_transcript(['Synthetic settled sentence.'], 'Synthetic settled sentence.')
    c.drain()
    assert c.view().provisional_text == 'Synthetic settled sentence.'


def test_clear_discards_hold_and_already_queued_final_cannot_type(surface):
    c, app, _, posted, injected = surface
    tree = ast.parse((Path(__file__).parents[1]/'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='DictationApp')
    names = {'cancel_recording','_cancel_recording_impl','_on_streaming_session_finished'}
    ns = {'logger':Mock()}
    exec(compile(ast.Module(body=[n for n in cls.body if isinstance(n,ast.FunctionDef)
                                 and n.name in names],type_ignores=[]),'host_cancel_ast','exec'),ns)
    for name in names:setattr(app,name,MethodType(ns[name],app))
    app._hold_capture_lifecycle_lock = threading.RLock()
    app.recording = True; app.hotkey_pressed = True
    app._restore_audio = Mock(); app._close_hold_capture_duck = Mock()
    app.play_sound = Mock(); app._dictation_consumer = Mock()
    app.set_app_state = lambda **values: app.__dict__.update(values)
    app._schedule_ui = lambda fn,*args: posted.append(partial(fn,*args))
    app._paste_preserving_clipboard = Mock(side_effect=lambda text: injected.append(text))
    app._log_history = Mock(); app.add_to_history = Mock()
    app.listening_indicator = LiveSurfaceIndicatorAdapter(c)
    app._streaming_session = session = StreamingSession(app)
    session.on_partial('Synthetic live words'); c.drain()
    assert c.widget._clear.isVisible()
    session.on_final('Synthetic final queued before Clear')
    c.widget._clear.click()
    session.on_partial('Synthetic late partial')
    flush(posted); c.drain()
    assert session.cancel_event.is_set()
    assert not app.recording and app._streaming_session is None
    assert app._session_mode_manager.draft_document.text == ''
    assert c.view().text == '' and c.view().provisional_text == ''
    assert c.widget._state.text() == 'Hold — cleared; nothing inserted'
    assert c.widget._mark.capture != 'recording'
    app._paste_preserving_clipboard.assert_not_called()
    assert injected == []
    app._close_hold_capture_duck.assert_called_once()


def test_clear_is_available_before_first_hold_partial(surface):
    c, app, _, _, _ = surface
    c.begin_capture(Lane.HOLD); c.drain()
    assert c.view().form is VisibleForm.STATUS
    assert c.widget._clear.isVisible()
    assert c.widget._clear.height() >= 44


def test_waiting_draft_stays_visible_and_insert_routes_to_existing_commit(surface):
    c, app, clock, _, injected = surface
    c.model.lane = Lane.HOLD
    app._session_mode_manager.stage_parked_hold('Synthetic words')
    c.sync_draft(app._session_mode_manager); c.drain()
    clock[0] += 6; c.refresh_expired()
    assert c.view().form is VisibleForm.LIVE
    assert c.widget._state.text() == 'Hold — staged'
    assert 'Synthetic words' in c.widget._transcript.toPlainText()
    assert c.widget._transcript.isVisible()
    assert c.widget._insert.isVisible() and c.widget._clear.isVisible()
    for item in (c.widget._state,c.widget._transcript,c.widget._insert,c.widget._clear):
        assert c.widget.rect().contains(item.geometry())
    c.widget._insert.click(); c.drain()
    assert injected == ['Synthetic words']
    assert app._session_mode_manager.draft_document.text == ''


@pytest.mark.parametrize('reduced',[False,True])
def test_recording_timer_in_both_lanes_obeys_reduced_motion(surface,reduced):
    c, app, _, _, _ = surface
    c.widget.set_reduced_motion(reduced)
    for lane in (Lane.HOLD,Lane.HANDS_FREE_DICTATE):
        app.is_speaking = True
        c.begin_capture(lane); c.drain()
        angle = c.widget._mark.rotation; QTest.qWait(170)
        assert c.widget._mark.capture == 'recording'
        assert c.widget._spin_timer.isActive() is not reduced
        assert (c.widget._mark.rotation == angle) is reduced
        app.is_speaking = False
        if lane is Lane.HANDS_FREE_DICTATE:c.refresh_expired()
        else:c.stop_capture();c.drain()
        assert c.widget._mark.capture != 'recording'
        # Hold eases to idle; hands-free returns to ambient listening motion.
        assert c.widget._spin_timer.isActive() is not reduced


def test_outcomes_expire_and_wake_settings_cannot_resurrect_them(surface):
    c, app, clock, _, _ = surface
    indicator = LiveSurfaceIndicatorAdapter(c)
    token = c.begin_capture(Lane.HOLD)
    words = CapturePartials(c,Lane.HOLD,token)
    words.final('Synthetic delivered text',delivered=True);words.stop();c.drain()
    assert 'inserted' in c.widget._state.text().lower()
    clock[0] += 1.1;c.refresh_expired()
    assert c.view().document is DocumentState.EMPTY and c.widget._state.text() == 'Idle'
    indicator.show_outcome('Sent','success');c.drain()
    clock[0] += 1.1;c.refresh_expired()
    indicator.set_wake_armed(True);c.drain()
    assert c.widget._state.text() == 'Hands-free — listening'
    indicator.set_wake_armed(False);c.drain()
    assert c.widget._state.text() == 'Idle'
    assert not c.view().text and not c.view().notice.message
