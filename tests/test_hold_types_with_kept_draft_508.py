"""508: ordinary holds leave a session-exit draft and recovery untouched."""
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import ava_command_session, execution_policy, listening_display, streaming
from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.model import (CaptureState, DocumentState, Lane, LiveSurfaceModel)
from samsara.live_surface.partials import CapturePartials
from samsara.live_surface.placement import Edge, Placement, Rect, Screen, placement_record
from samsara.session_modes import SessionMode
from samsara.ui.live_surface_qt import LiveSurfaceWidget
from tests.test_cancelled_final_497 import session_stub
from tests.test_dictation_surface_recovery import capture_app
from tests.test_kept_draft_survives_505 import make_manager

HOLD = 'Synthetic words for Notepad.'


def save_picture(widget, destination, name, width, height):
    """Synthetic desktop context plus the real offscreen widget pixels."""
    from PySide6.QtGui import QColor, QPainter, QPixmap
    shot = widget.grab()
    shot.save(str(Path(destination) / (name + '.png')))
    ratio = shot.devicePixelRatio()
    canvas = QPixmap(round(width * ratio), round(height * ratio))
    canvas.setDevicePixelRatio(ratio)
    canvas.fill(QColor('#090c11'))
    painter = QPainter(canvas)
    painter.drawPixmap(widget.x(), widget.y(), shot)
    painter.end()
    canvas.save(str(Path(destination) / (name + '_desktop.png')))


def exit_session(app, monkeypatch):
    monkeypatch.setattr(execution_policy, 'stop_all', Mock())
    monkeypatch.setattr(ava_command_session, 'cancel_all', Mock())
    app._command_mode_lock = threading.Lock()
    app.command_mode_active = True
    app._command_mode_session_start = time.monotonic() - 5
    app.config['command_mode'] = {'mode': 'toggle', 'exit_earcon': False}
    for name in ('_cancel_command_mode_inactivity_timer', '_release_wake_consumer',
                 '_release_streaming_preview', '_show_outcome_chip', 'play_sound'):
        setattr(app, name, Mock())
    sys.modules['dictation'].DictationApp.exit_command_mode(app)


def snapshot(manager):
    d = manager.draft_document
    return (d.document_id, d.revision, d.segments, d.manual_commit, d.edited,
            manager.retained_draft, manager.recoverable_draft)


@pytest.fixture
def kept(monkeypatch):
    def build(full=True, live=False):
        manager, inserted = make_manager(full=full)
        manager._stash_recoverable_draft('synthetic recovery')
        session = session_stub(parking=full)
        app = session.app
        app.config['ui'] = {'live_surface': {'enabled': full}}
        app._session_mode_manager = manager
        app._ensure_session_mode_manager = lambda: manager
        app._filter_dictation_language = lambda text, info: text
        app._verbatim_rule = lambda: None
        app.live_surface = None
        if not live:
            exit_session(app, monkeypatch)
        else:
            app.command_mode_active = True
        # Exercise the final's independent pending-draft check, without a latch.
        app._hold_park_requested = False
        monkeypatch.setattr(streaming._user32, 'GetForegroundWindow', lambda: 123)
        appended = Mock(wraps=manager.stage_parked_hold)
        monkeypatch.setattr(manager, 'stage_parked_hold', appended)
        return SimpleNamespace(manager=manager, inserted=inserted, session=session,
                               app=app, appended=appended)
    return build


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
def test_kept_draft_ordinary_hold_delivers_once_without_appending(kept, full):
    r = kept(full)
    before = snapshot(r.manager)
    r.session._deliver_final(HOLD, None, 1.0, 0)
    r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    r.appended.assert_not_called()
    assert snapshot(r.manager) == before
    assert not r.inserted
    r.app._log_history.assert_called_once()
    r.app.add_to_history.assert_called_once()


def test_live_session_hold_still_appends(kept):
    r = kept(live=True)
    before = r.manager.draft_document.text
    r.session._deliver_final(HOLD, None, 1.0, 0)
    r.app._paste_preserving_clipboard.assert_not_called()
    r.appended.assert_called_once()
    assert r.manager.draft_document.text == before + ' ' + HOLD


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
def test_cancelled_queued_final_keeps_draft_and_recovery(kept, full):
    r = kept(full)
    before = snapshot(r.manager)
    r.session.on_final(HOLD)
    callback, *args = r.app._schedule_ui.call_args.args
    r.session.cancel()
    callback(*args)
    r.app._paste_preserving_clipboard.assert_not_called()
    r.appended.assert_not_called()
    assert snapshot(r.manager) == before


@pytest.mark.parametrize('full', [True, False], ids=['Full', 'Compact'])
def test_insert_after_ordinary_hold_sends_only_original_draft(kept, full):
    r = kept(full)
    original = r.manager.draft_document.text or r.manager.retained_draft
    r.session._deliver_final(HOLD, None, 1.0, 0)
    r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    if not full:
        # Compact's existing send route is restart hands-free, then send.
        r.manager.reset(SessionMode.DICTATE)
    assert r.manager.commit_pending_dictation().kind == 'dictate_committed'
    assert r.inserted == [original]
    assert HOLD not in r.inserted[0]


@pytest.mark.parametrize('live', [False, True], ids=['kept', 'live'])
def test_capture_onset_only_arms_live_session_draft(capture_app, monkeypatch, live):
    app, manager, _ = capture_app
    monkeypatch.setitem(app._live_surface_hold_parking_enabled.__func__.__globals__,
                        'listening_display', listening_display)
    monkeypatch.setitem(app._start_recording_impl.__func__.__globals__, 'time', time)
    manager.stage_parked_hold('Synthetic original.')
    app._session_mode_manager = manager
    if not live:
        app.live_surface = None
        exit_session(app, monkeypatch)
    else:
        app.command_mode_active = True
    before = snapshot(manager)
    assert app._start_recording_impl(streaming=False, play_earcon=False)
    assert app._hold_park_requested is live
    assert snapshot(manager) == before


@pytest.mark.parametrize('failure', ['pause_draft', 'discard_retained_draft', 'reset', 'sync_draft'])
def test_exit_releases_microphone_and_preview_even_when_parking_raises(kept, monkeypatch, failure):
    r = kept(live=True)
    r.app.live_surface = SimpleNamespace(sync_draft=Mock())
    target = r.app.live_surface if failure == 'sync_draft' else r.manager
    monkeypatch.setattr(target, failure, Mock(side_effect=RuntimeError(failure)))
    with pytest.raises(RuntimeError, match=failure):
        exit_session(r.app, monkeypatch)
    r.app._release_wake_consumer.assert_called_once_with('toggle_session')
    r.app._release_streaming_preview.assert_called_once_with()


@pytest.mark.parametrize('finish', ['delivered', 'empty', 'cancelled'])
def test_full_hold_preview_and_end_restore_kept_draft(qapp, kept, monkeypatch, finish):
    r = kept()
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda fn: fn())
    clock = [0.0]
    r.app.config['ui']['live_surface']['placement'] = {
        'preferred_monitor': 'synthetic', 'monitors': {
            'synthetic': placement_record(Placement(Edge.BOTTOM, 0.0))}}
    c = LiveSurfaceController(r.app, model=LiveSurfaceModel(clock=lambda: clock[0]),
        widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True))
    c._screens = lambda: [Screen('synthetic', Rect(0, 0, 1280, 720), True)]
    r.app.live_surface = c
    before = snapshot(r.manager)
    c.sync_draft(r.manager)
    c.start()
    destination = os.environ.get('SAMSARA_508_PROOF_DIR')
    scale = os.environ.get('QT_SCALE_FACTOR', '1')

    def picture(state):
        qapp.processEvents()
        if destination:
            save_picture(c.widget, destination, f'Full_dark_{scale}_{state}', 1280, 720)
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
        picture('after_' + finish)
        assert snapshot(r.manager) == before
        assert c.view().text == r.manager.draft_document.text
        assert c.view().document is DocumentState.PARKED_DRAFT
        assert c.view().capture is CaptureState.OFF
        assert c.widget._lane.text() == 'Kept draft'
        assert c.view().provisional_text == ''
        if finish == 'delivered':
            r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
        else:
            r.app._paste_preserving_clipboard.assert_not_called()
    finally:
        c.close()


@pytest.mark.parametrize('finish', ['delivered', 'cancelled'])
def test_compact_result_returns_to_retained_draft_reminder(qapp, kept, monkeypatch, finish):
    from PySide6.QtCore import QTimer
    from samsara.ui.listening_indicator import ListeningIndicator
    r = kept(False)
    before = snapshot(r.manager)
    indicator = ListeningIndicator()
    indicator.set_idle_animation(False)
    indicator.set_position('bottom-left')
    r.app.listening_indicator = indicator
    reminder = 'Kept draft: restart hands-free to send or clear.'
    indicator.show_outcome(reminder, 'accent', None)
    indicator.show()
    destination = os.environ.get('SAMSARA_508_PROOF_DIR')
    scale = os.environ.get('QT_SCALE_FACTOR', '1')
    def picture(state):
        qapp.processEvents()
        if destination:
            area = indicator.screen().availableGeometry()
            save_picture(indicator, destination, f'Compact_dark_{scale}_{state}',
                         area.width(), area.height())
    timers = []
    monkeypatch.setattr('samsara.ui.qt_runtime.post', lambda fn: fn())
    monkeypatch.setattr(QTimer, 'singleShot', lambda delay, fn: timers.append(fn))
    try:
        picture('before')
        indicator.set_listening(True)
        indicator.show_outcome('Synthetic hold in progress.', 'live', None)
        picture('during')
        indicator.set_listening(False)
        indicator.show_outcome('typed' if finish == 'delivered' else 'Cancelled', 'success', 900)
        if finish == 'delivered':
            r.session._deliver_final(HOLD, None, 1.0, 0)
        else:
            r.session.cancel()
        for callback in timers:
            callback()
        picture('after_' + finish)
        assert indicator._chip_label == reminder
        assert snapshot(r.manager) == before
        r.appended.assert_not_called()
        if finish == 'delivered':
            r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
        else:
            r.app._paste_preserving_clipboard.assert_not_called()
    finally:
        indicator.destroy()
        indicator.deleteLater()


@pytest.mark.parametrize('reason', ['failed delivery', 'focus changed', 'voice review'])
def test_refused_or_reviewed_hold_cannot_modify_kept_draft(kept, monkeypatch, reason):
    r = kept()
    before = snapshot(r.manager)
    text = HOLD
    if reason == 'failed delivery':
        r.app._paste_preserving_clipboard.return_value = False
    elif reason == 'focus changed':
        monkeypatch.setattr(streaming._user32, 'GetForegroundWindow', lambda: 456)
    else:
        text += ' pause draft'
    r.session._deliver_final(text, None, 1.0, 0)
    assert snapshot(r.manager) == before
    r.appended.assert_not_called()
    r.app.add_to_history.assert_called_once_with(text, is_command=False)
    r.app._log_history.assert_called_once()
    if reason == 'failed delivery':
        r.app._paste_preserving_clipboard.assert_called_once_with(text)
    else:
        r.app._paste_preserving_clipboard.assert_not_called()


def test_hold_captured_in_live_session_still_appends_if_session_ends_before_final(kept, monkeypatch):
    r = kept(live=True)
    r.session._live_session_capture = True
    r.app._hold_park_requested = True
    exit_session(r.app, monkeypatch)
    before = r.manager.draft_document.text
    r.session._deliver_final(HOLD, None, 1.0, 0)
    r.app._paste_preserving_clipboard.assert_not_called()
    r.appended.assert_called_once()
    assert r.manager.draft_document.text == before + ' ' + HOLD

