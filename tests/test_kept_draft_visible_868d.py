"""868d: real session transitions cannot hide a draft or capture ordinary holds."""
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import ava_command_session, execution_policy, hotkeys, streaming
from samsara.draft_document import RECOVERABLE_DRAFT_TTL_S
from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
from samsara.live_surface.model import CaptureState, Lane, LiveSurfaceModel, VisibleForm
from samsara.session_modes import SessionMode
from samsara.ui.listening_indicator import ListeningIndicator
from samsara.ui.live_surface_qt import LiveSurfaceWidget
from tests.test_cancelled_final_497 import session_stub
from tests.test_kept_draft_survives_505 import make_manager, SIGNALS, TEXT


HOLD = 'Synthetic independent hold.'


@pytest.fixture(params=[True, False], ids=['Full', 'Compact'])
def state(request, qapp, monkeypatch):
    from PySide6.QtCore import QTimer
    # Delayed Compact result reminders are covered by 508; do not leave their
    # callbacks referring to widgets destroyed at this fixture's teardown.
    single_shot = QTimer.singleShot
    monkeypatch.setattr(QTimer, 'singleShot',
                        lambda delay, *args: single_shot(delay, *args) if delay == 0 else None)
    full = request.param
    manager, inserted = make_manager(full=full)
    session = session_stub(parking=full)
    app = session.app
    app.config.update(command_mode={'enabled': True, 'mode': 'toggle',
                                   'button': 'mouse4', 'exit_earcon': False},
                      hotkey='ctrl+shift', wake_word_enabled=True,
                      listening_indicator_enabled=False,
                      ui={'listening_display': 'full' if full else 'compact',
                          'live_surface': {'enabled': full, 'show_idle_mark': False}})
    app._session_mode_manager = manager
    app._command_mode_lock = threading.Lock()
    app._command_mode_session_start = time.monotonic() - 5
    app.command_mode_active = True
    app._dictate_preview = None
    app._hold_park_requested = False
    app._filter_dictation_language = lambda text, info: text
    app._verbatim_rule = lambda: None
    app._schedule_ui = lambda callback, *args: callback(*args)
    app._config_lock = threading.Lock()
    app._wake_word_settings_guard = threading.Lock()
    app._wake_word_settings_lock = threading.Lock()
    app._wake_word_settings_generation = 0
    app.wake_word_active = True
    for name in ('_cancel_command_mode_inactivity_timer', '_release_wake_consumer',
                 '_cancel_pending_wake_start', '_stop_hands_free_idle_duck',
                 '_reset_wake_dictation', '_release_icon_chase', 'play_sound',
                 'save_config', '_update_tray_tooltip'):
        setattr(app, name, Mock())
    app.set_app_state = lambda **values: app.__dict__.update(values)
    app._get_mode_display = lambda: 'Hold'
    cls = sys.modules['dictation'].DictationApp  # Installed by the bootstrap-free gate.
    for name in ('exit_command_mode', '_on_command_mode_inactivity',
                 '_release_streaming_preview', 'stop_wake_word_mode',
                 'set_wake_word_enabled', '_apply_wake_word_enabled',
                 'apply_listening_indicator_settings'):
        setattr(app, name, getattr(cls, name).__get__(app))
    monkeypatch.setattr(execution_policy, 'stop_all', Mock(return_value={}))
    monkeypatch.setattr(ava_command_session, 'cancel_all', Mock())
    manager._on_abort = app.exit_command_mode
    manager._stop_fn = Mock(return_value={})
    monkeypatch.setattr(streaming._user32, 'GetForegroundWindow', lambda: 123)
    monkeypatch.setattr('samsara.ui.qt_runtime.post', lambda callback: callback())
    now = [0.0]
    controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: now[0]),
        widget_factory=lambda source: LiveSurfaceWidget(source, reduced_motion=True)) if full else None
    app.live_surface = controller
    if full:
        controller.start()
        indicator = LiveSurfaceIndicatorAdapter(controller)
        indicator.set_session_mode('DICTATE')
        controller.begin_capture(Lane.HANDS_FREE_DICTATE)
        controller.sync_draft(manager)
    else:
        indicator = ListeningIndicator()
        indicator.set_idle_animation(False)
        indicator.set_session_mode('DICTATE', '#ffffff')
        indicator.set_command_mode(True)
        indicator.show()
    app.listening_indicator = indicator
    app._show_outcome_chip = lambda label, kind, ttl, **kwargs: indicator.show_outcome(label, kind, ttl)
    result = SimpleNamespace(app=app, manager=manager, session=session, full=full,
                             controller=controller, indicator=indicator, now=now,
                             inserted=inserted, qapp=qapp)
    yield result
    if full:
        controller.close()
    else:
        indicator.destroy()
        indicator.deleteLater()


def assert_independent_hold(r, *, ended):
    assert r.app.command_mode_active is not ended
    before = (r.manager.draft_document.text, r.manager.retained_draft,
              r.manager.draft_document.revision, r.manager.draft_document.segments)
    assert any(text.startswith(TEXT.rstrip('.')) for text in before[:2])
    if r.full:
        # Waiting drafts do not inherit the recovery slot's five-minute expiry.
        r.now[0] += RECOVERABLE_DRAFT_TTL_S + 1
        r.controller.refresh_expired()
        r.qapp.processEvents()
        assert r.controller.view().form is VisibleForm.LIVE
        assert r.controller.widget.isVisible()
        assert r.controller.widget._clear.isVisible()
        assert r.controller.view().text == before[0]
        if ended:
            assert r.controller.widget._insert.isVisible()
            assert r.controller.view().capture is CaptureState.OFF
    elif ended:
        assert r.indicator.isVisible()
        assert 'Kept draft' in r.indicator._chip_label
    r.session._deliver_final(HOLD, None, 1.0, 0)
    r.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    assert (r.manager.draft_document.text, r.manager.retained_draft,
            r.manager.draft_document.revision, r.manager.draft_document.segments) == before
    assert not r.inserted


def test_toggle_off_keeps_draft_visible_and_hold_types(state):
    hotkeys.HotkeyCluster._on_command_button(state.app, 'mouse4', True)
    assert_independent_hold(state, ended=True)


def test_stop_keeps_session_and_hold_types(state):
    # Current defaults reserve halt/cease; bare stop remains dictation.
    assert state.manager.dispatch_utterance('stop', SIGNALS).kind == 'dictate_staged'
    if state.full:
        state.controller.sync_draft(state.manager)
    assert_independent_hold(state, ended=False)


def test_timeout_keeps_draft_visible_and_hold_types(state):
    state.app._on_command_mode_inactivity()
    assert_independent_hold(state, ended=True)


def test_sleep_keeps_draft_visible_and_hold_types(state):
    outcome = state.manager.dispatch_utterance('go to sleep', SIGNALS)
    assert outcome.detail['sleep']
    assert_independent_hold(state, ended=True)


def test_wake_word_off_keeps_session_and_hold_types(state):
    state.app._apply_wake_word_enabled(False)
    assert not state.app.wake_word_active
    state.app._release_wake_consumer.assert_called_once_with('wake_word')
    assert_independent_hold(state, ended=False)


def test_ctrl_alt_w_keeps_session_and_hold_types(state, monkeypatch):
    app = state.app
    app.current_keys = {'ctrl', 'alt'}
    app.key_press_times = {}
    app.snoozed = app.hotkey_pressed = False
    app.get_key_name = lambda key: key
    app._check_command_mode_key = Mock()
    app.check_hotkey_state = lambda combo: combo == 'ctrl+alt+w'
    monkeypatch.setattr(hotkeys.thread_registry, 'spawn',
                        lambda name, callback, args=(), **kwargs: callback(*args))
    hotkeys.HotkeyCluster.on_key_press(app, 'w')
    assert not app.wake_word_active
    assert app.config['wake_word_enabled'] is False
    assert_independent_hold(state, ended=False)


def test_app_focus_change_keeps_session_and_hold_types(state):
    # A new foreground window changes the delivery target, not session ownership.
    state.manager._foreground_hwnd_resolver = lambda: 456
    assert state.manager.mode is SessionMode.DICTATE
    assert_independent_hold(state, ended=False)


def test_expired_recovery_cannot_capture_an_ordinary_hold(state):
    state.manager.confirm_clear_draft('synthetic clear')
    state.manager._draft._clock = lambda: float('inf')
    assert state.manager.recoverable_draft == ''
    state.app.exit_command_mode()
    state.session._deliver_final(HOLD, None, 1.0, 0)
    state.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
    assert state.manager.dictate_pending_buffer == state.manager.retained_draft == ''


def test_full_empty_idle_can_still_hide(state):
    if not state.full:
        return
    state.manager.confirm_clear_draft('synthetic clear')
    state.controller.sync_draft(state.manager)
    state.app.exit_command_mode()
    state.app.apply_listening_indicator_settings()
    assert not state.controller.widget.isVisible()


def test_idle_visibility_setting_cannot_hide_waiting_full_draft(state):
    state.app.exit_command_mode()
    state.app.apply_listening_indicator_settings()
    if state.full:
        assert_independent_hold(state, ended=True)
    else:
        # Compact has no editor. Ordinary holds still bypass its retained copy.
        before = state.manager.retained_draft
        state.session._deliver_final(HOLD, None, 1.0, 0)
        state.app._paste_preserving_clipboard.assert_called_once_with(HOLD)
        assert state.manager.retained_draft == before == TEXT
