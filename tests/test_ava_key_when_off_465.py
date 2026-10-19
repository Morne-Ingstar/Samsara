"""Ava's configured hold must explain a disabled assistant without opening audio."""
from types import SimpleNamespace
from unittest.mock import Mock
import logging

import pytest

# Reuse 445's shutdown-and-join fixture for every test-owned coordinator.
from tests.test_tts_feedback_level_372 import _shutdown_test_coordinators

MESSAGE = "Ava is off. Turn her on in Settings > Ava."
NOTE = "Ava is off - this key does nothing until you turn her on in Settings > Ava."


@pytest.fixture
def app():
    # The proof gate exposes the source-preserving class without bootstrapping.
    from dictation import DictationApp
    host = DictationApp.__new__(DictationApp)
    host.config = {'ava_mode_key': 'right_alt', 'ava': {'provider_policy': 'off'},
                   'ava_command_session': {'enabled': False},
                   'tts': {'feedback_level': 'brief'}, 'command_mode': {'enabled': False}}
    host._ava_mode_key_held = False
    host._command_mode_key_held = False
    host.ava_mode_active = False
    host.command_mode_active = False
    host.recording = False
    host.enter_ava_mode = Mock()
    host.exit_ava_mode = Mock()
    host.start_recording = Mock()
    host._show_outcome_chip = Mock()
    host._speak_session_notice = Mock()
    return host


def event(name, down):
    return SimpleNamespace(name=name, hold_name=name, hold_keys=frozenset(down))


@pytest.mark.parametrize('binding', ['right_alt', 'f13', 'numpad0', 'scroll_lock', 'mouse4', 'ctrl+f5'])
@pytest.mark.parametrize('repeats', [0, 1, 20], ids=['press', 'tap', 'held_repeat'])
def test_disabled_key_has_one_result_per_press_and_no_session(app, binding, repeats, caplog):
    app.config['ava_mode_key'] = binding
    parts = binding.split('+')
    down = set(parts)
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            for _ in range(repeats + 1):
                app._check_command_mode_key(event(parts[-1], down), True)
            app._check_command_mode_key(event(parts[-1], set()), False)
    assert app._show_outcome_chip.call_count == 2
    assert [call.args[:2] for call in app._show_outcome_chip.call_args_list] == [(MESSAGE, 'warning')] * 2
    app.enter_ava_mode.assert_not_called()
    app.exit_ava_mode.assert_not_called()
    app.start_recording.assert_not_called()
    assert not app.ava_mode_active and not app.recording
    assert sum('Ava is off' in record.message for record in caplog.records) == 2


@pytest.mark.parametrize('level', ['silent', 'brief', 'full'])
def test_disabled_key_uses_real_feedback_level_policy(app, level):
    from tests.test_audio_coordinator import _make_coordinator
    coordinator, audio_app, engine, _ = _make_coordinator()
    coordinator._emit_caption = Mock()  # Isolate the caption-window boundary; exercise the real speech policy.
    app.config['tts'] = {'enabled': True, 'feedback_level': level}
    audio_app.config = app.config
    audio_app.command_mode_active = False
    app._speak_session_notice = coordinator.speak
    app._check_command_mode_key(event('right_alt', {'right_alt'}), True)
    app._show_outcome_chip.assert_called_once()
    assert engine.speak.called == (level != 'silent')
    if level != 'silent':
        assert engine.speak.call_args.args[0] == MESSAGE
    audio_app.play_sound.assert_not_called()
    if level == 'silent':
        coordinator._emit_caption.assert_called_once_with(MESSAGE, hold=False)


def test_unrelated_key_and_phantom_release_are_quiet(app):
    app._check_command_mode_key(event('f14', {'f14'}), True)
    app._check_command_mode_key(event('right_alt', set()), False)
    app._show_outcome_chip.assert_not_called()
    app._speak_session_notice.assert_not_called()


@pytest.mark.parametrize('policy,enabled', [('local', False), ('off', True)])
def test_canonical_master_gate_overrides_legacy_enable_flag(app, policy, enabled):
    app.config.update(ava_mode_enabled=True, ava={'provider_policy': policy},
                      ava_command_session={'enabled': enabled})
    app._check_command_mode_key(event('right_alt', {'right_alt'}), True)
    app._show_outcome_chip.assert_called_once()
    app.enter_ava_mode.assert_not_called()


def test_enabled_key_still_enters_and_exits_once(app):
    app.config.update(ava={'provider_policy': 'local'}, ava_command_session={'enabled': True})
    key = event('right_alt', {'right_alt'})
    app._check_command_mode_key(key, True)
    app.ava_mode_active = True
    app._check_command_mode_key(key, True)
    app._check_command_mode_key(event('right_alt', set()), False)
    app.enter_ava_mode.assert_called_once()
    app.exit_ava_mode.assert_called_once()
    app._show_outcome_chip.assert_not_called()


def test_legacy_pynput_key_gets_the_same_refusal(app):
    from pynput.keyboard import Key
    app._check_command_mode_key(Key.alt_r, True)
    app._check_command_mode_key(Key.alt_r, False)
    app._show_outcome_chip.assert_called_once()


def test_settings_note_tracks_ava_route_and_keeps_capture(qapp, monkeypatch, qt_deferred_delete):
    from samsara import ava_readiness
    # Route/capture UI behavior does not own the process-wide provider worker.
    recheck = Mock()
    monkeypatch.setattr(ava_readiness, 'recheck_after_settings', recheck)
    from samsara.ui import ava_consent_qt
    consent_request = Mock(side_effect=lambda _parent, config, **kwargs:
                           ava_consent_qt.accepted_consent(config, **kwargs))
    monkeypatch.setattr(ava_consent_qt, 'request_consent', consent_request)
    from tests.test_settings import _StubApp
    from samsara.ui.settings_qt import _SettingsWindow
    from samsara.ui import theme
    stub = _StubApp()
    stub.config = {'ava': {'provider_policy': 'off'}, 'ava_command_session': {'enabled': False}}
    window = _SettingsWindow(stub)
    qapp.processEvents()
    note = window._widgets['ava_mode_key_off_note']
    capture = window._widgets['ava_mode_key']
    assert note.text() == NOTE and not note.isHidden()
    row = window._rows_by_widget['ava_mode_key']
    assert row.isAncestorOf(note) and row.isAncestorOf(capture)
    assert capture.combo == 'right_alt'
    window._widgets['providerRoute'].setCurrentIndex(1)
    assert note.isHidden()
    consent_request.assert_called_once()
    window._widgets['providerRoute'].setCurrentIndex(0)
    assert not note.isHidden()
    assert recheck.called
    # Exercise this window's repaint without restyling other tests' windows.
    theme.set_theme('light', refresh=False)
    window.apply_theme()
    assert not note.isHidden() and note.text() == NOTE
    theme.set_theme('dark', refresh=False)
    window.hide()
    window.deleteLater()
    qt_deferred_delete(window)

@pytest.mark.parametrize('display', ['compact', 'full'])
def test_off_result_reaches_selected_display_through_shared_bridge(app, display):
    from dictation import DictationApp
    from samsara.live_surface.controller import LiveSurfaceIndicatorAdapter
    controller = SimpleNamespace(post=Mock())
    class Compact:
        def __init__(self):
            self.show_outcome = Mock()
    indicator = Compact() if display == 'compact' else LiveSurfaceIndicatorAdapter(controller)
    app.listening_indicator = indicator
    app._schedule_ui = lambda fn, *args: fn(*args)
    app._show_outcome_chip = DictationApp._show_outcome_chip.__get__(app)
    app._check_command_mode_key(event('right_alt', {'right_alt'}), True)
    if display == 'compact':
        indicator.show_outcome.assert_called_once_with(MESSAGE, 'warning', 4000)
    else:
        controller.post.assert_called_once()
        result = controller.post.call_args.args[0]
        assert result.message == MESSAGE and result.ttl_s == 4
        assert result.feedback.value == 'warning'
    assert app._outcome_ring[-1].label == MESSAGE

