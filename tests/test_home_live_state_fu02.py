"""918: fake capture endpoints and blocked model factories, never desktop bootstrap."""
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtTest import QTest

from samsara import listening_display, model_load_feedback, speech_readiness
from samsara.ui import home_qt, home_signals, main_window_qt
from tests.test_home_qt import _app, _hub, _FakeSettings, _native_home_font  # noqa: F401


@pytest.fixture(autouse=True)
def isolated(monkeypatch, qt_widget_scope):
    _FakeSettings.values = {}
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    monkeypatch.setattr(main_window_qt._MainWindow, '_save_geometry', lambda self: None)
    monkeypatch.setattr(main_window_qt._MainWindow, '_restore_geometry', lambda self: None)
    monkeypatch.setattr(home_signals, 'corrections_waiting', lambda app: 0)
    monkeypatch.setattr(home_signals, '_diag_records', lambda *args: [])
    monkeypatch.setattr(home_signals, 'ava_state', lambda app:
                        home_signals.CapabilityState(home_signals.AVA, home_signals.OFF))
    monkeypatch.setattr(home_qt, '_user_word_count', lambda app: 0)


def capture_app(**overrides):
    return _app(model_loaded=True, _no_microphone=False,
                _ace_engine=SimpleNamespace(device_lost=False, capture_name='Live USB mic'),
                **overrides)


@pytest.mark.parametrize('flags', [{}, {'recording': True}, {'snoozed': True},
                                  {'continuous_active': True}, {'command_mode_active': True}])
def test_capture_loss_overrides_stale_enumeration_and_capture_flags(flags):
    app = capture_app(**flags)
    app._ace_engine.device_lost = True
    assert app.available_mics == [{'id': 7, 'name': 'USB mic'}]
    assert home_qt.listening_badge_view(app)[0] == 'No microphone'
    notice = home_signals.problem_notice(app)
    assert 'Settings > Audio > Microphone' in notice.text
    assert notice.action == home_signals.ACTION_SETUP_MICROPHONE
    assert home_signals.microphone_name(app) == 'No microphone'


def test_running_home_follows_removal_arrival_and_reopen_within_two_seconds(
        qapp, monkeypatch, qt_widget_scope):
    app = capture_app()
    with qt_widget_scope(_hub(qapp, monkeypatch, app=app)) as window:
        window._poll_timer.start()
        app._ace_engine.device_lost = True
        QTest.qWait(700)
        assert window._badge.text() == window._lbl_mic.text() == 'No microphone'
        panel = window._panel_cache['Home']
        assert panel._notice_card.isVisibleTo(window)
        panel._notice_btn.click()
        # A replug may change the numeric index; capture truth outranks that cache.
        app._ace_engine.device_lost = False
        app._ace_engine.capture_name = 'Reconnected USB mic'
        app.available_mics = []
        app.snoozed = True
        QTest.qWait(700)
        assert window._paused_btn.text() == 'Paused'
        assert window._lbl_mic.text() == 'Reconnected USB mic'
        assert panel._notice_card.isHidden()
        window.hide()
        app.snoozed = False
        app.recording = True
        window.show()
        assert window._badge.text() == 'Recording'
        window.hide()
        app._ace_engine.device_lost = True
        window.show()
        assert window._badge.text() == 'No microphone'
        assert panel._notice_card.isVisibleTo(window)
        assert window._poll_timer.interval() < 2000


def test_legacy_capture_signal_and_current_fallback_name():
    app = _app(_no_microphone=True)
    assert not home_qt.mic_present(app)
    app._no_microphone = False
    app._using_default_mic = True
    assert home_signals.microphone_name(app) == 'System default'
    app._ace_engine = SimpleNamespace(device_lost=False, capture_name='Laptop mic')
    assert home_signals.mic_available(app) is True
    assert home_signals.microphone_name(app) == 'Laptop mic'


def test_wake_waiting_listening_and_paused_are_distinct():
    app = capture_app()
    app.config['wake_word_config']['phrase'] = 'hey jarvis'
    assert home_qt.listening_badge_view(app)[0] == 'Waiting for "hey jarvis"'
    app.command_mode_active = True
    assert home_qt.listening_badge_view(app)[0] == 'Hands-free on'
    app.command_mode_active = False
    app.snoozed = True
    assert home_qt.listening_badge_view(app)[0] == 'Paused'


@pytest.mark.parametrize('phase,message', [('loading', speech_readiness.LOADING),
                                          ('downloading', speech_readiness.DOWNLOADING)])
def test_home_explains_readiness_and_hides_repair_actions(
        qapp, monkeypatch, qt_widget_scope, phase, message):
    app = capture_app()
    app.model_loaded = False
    app._speech_model_phase = phase
    with qt_widget_scope(_hub(qapp, monkeypatch, app=app)) as window:
        panel = window._panel_cache['Home']
        assert panel._notice_card.isVisibleTo(window)
        assert panel._notice_text.text() == window._lbl_mode.text() == message
        assert panel._notice_btn.parentWidget().isHidden()
        assert window._badge.text() == 'Getting ready'
        assert window._badge.toolTip() == message
        app.model_loaded = True
        window._refresh_status()
        assert panel._notice_card.isHidden()
        assert window._badge.text() == 'Waiting for "jarvis"'
        app._ace_engine.device_lost = True
        window._refresh_status()
        assert panel._notice_btn.parentWidget().isVisibleTo(window)


class Indicator:
    def __init__(self):
        self.messages = []
        self.clears = []

    def show_feedback(self, *args):
        self.messages.append(args)

    def clear_feedback(self, owner):
        self.clears.append(owner)


class BootApp:
    def __init__(self):
        self.config = {'hotkey': 'ctrl+shift'}
        self.model_loaded = False
        self.listening_indicator = Indicator()
        self._schedule_ui = lambda fn, *args: fn(*args)

    def update_splash(self, *args):
        pass


def test_loader_publishes_download_and_ready_to_both_home_and_display():
    app = BootApp()

    def factory(*args, **kwargs):
        assert speech_readiness.status(app) == speech_readiness.LOADING
        kwargs['_on_download']()
        assert home_signals.problem_notice(app).text == speech_readiness.DOWNLOADING
        assert app.listening_indicator.messages[-1][0] == speech_readiness.DOWNLOADING
        return object()

    model, elapsed = model_load_feedback.load_speech_model(
        app.update_splash, factory, 'base', 'cpu', 'int8')
    assert model is not None and elapsed >= 0
    assert app.listening_indicator.clears == ['speech-model']
    app.model_loaded = True
    assert speech_readiness.status(app) == ''


def test_failed_model_load_never_says_ready():
    app = BootApp()

    def factory(*args, **kwargs):
        raise RuntimeError('fake load failure')

    with pytest.raises(RuntimeError, match='fake load failure'):
        model_load_feedback.load_speech_model(app.update_splash, factory, 'base', 'cpu', 'int8')
    assert speech_readiness.status(app) == speech_readiness.FAILED
    assert app.listening_indicator.messages[-1][0] == speech_readiness.FAILED


@pytest.mark.parametrize('source', ['main_keyboard', 'command_keyboard', 'main_mouse'])
@pytest.mark.parametrize('mode', ['hold', 'toggle'])
def test_hotkey_readiness_preserves_failed_start_press_and_release(source, mode):
    from samsara.hotkeys import HotkeyCluster
    app = BootApp()
    app.config.update(command_hotkey='f9', mode=mode)
    app.current_keys = set()
    app.key_press_times = {}
    app.get_key_name = lambda key: key
    app.parse_hotkey = MethodType(HotkeyCluster.parse_hotkey, app)
    app.check_hotkey_state = lambda binding: app.parse_hotkey(binding).issubset(app.current_keys)
    for flag in ('snoozed', 'hotkey_pressed', 'recording', 'command_mode_recording',
                 '_stop_in_flight', '_memo_recording', 'toggle_active', '_hold_down_key',
                 '_hold_down_mouse'):
        setattr(app, flag, False)
    app._check_command_mode_key = Mock()
    app._hands_free_dictation_commit_available = lambda: False
    app._start_recording_declined = MethodType(HotkeyCluster._start_recording_declined, app)
    app._mouse_guard = Mock()
    app._hotkey_state_text = lambda: 'synthetic readiness'
    app.start_recording = Mock()  # The unloaded model declines capture, as in production.

    def press():
        if source == 'main_mouse':
            HotkeyCluster._on_main_hotkey_mouse(app, True)
        else:
            if source == 'main_keyboard':
                app.current_keys.add('ctrl')
            HotkeyCluster.on_key_press(app, 'f9' if source == 'command_keyboard' else 'shift')

    press()
    assert app.listening_indicator.messages[-1][0] == speech_readiness.LOADING
    assert app.hotkey_pressed is (source != 'main_mouse')
    assert app._hold_down_key is (source == 'main_keyboard')
    assert app._hold_down_mouse is (source == 'main_mouse')
    assert not app.recording and not app.command_mode_recording and not app.toggle_active
    app.start_recording.assert_called_once_with(
        streaming=False, **({'capture_path': 'command_key'} if source == 'command_keyboard' else {}))
    if source != 'main_mouse':
        app._check_command_mode_key.assert_called_once_with(
            'f9' if source == 'command_keyboard' else 'shift', pressed=True)

    # Download progress still appears on repeat, without retrying a declined start.
    app._speech_model_phase = 'downloading'
    press()
    assert app.listening_indicator.messages[-1][0] == speech_readiness.DOWNLOADING
    assert app.start_recording.call_count == 1
    if source == 'main_mouse':
        HotkeyCluster._on_main_hotkey_mouse(app, False)
    else:
        HotkeyCluster.on_key_release(app, 'f9' if source == 'command_keyboard' else 'shift')
    assert not app.hotkey_pressed and not app._hold_down_key and not app._hold_down_mouse
    assert app.start_recording.call_count == 1
    app.current_keys = {'a'}
    count = len(app.listening_indicator.messages)
    assert speech_readiness.show_for_keyboard_hotkey(app) is False
    assert len(app.listening_indicator.messages) == count


@pytest.mark.parametrize('display', ['compact', 'full'])
def test_build_display_publishes_model_wait_without_a_hotkey(display):
    app = BootApp()
    app.config['ui'] = {'listening_display': display}
    full = SimpleNamespace(start=lambda: None)
    listening_display.build_display(app, Indicator, lambda app: full, lambda controller: Indicator())
    message, kind, ttl, feedback = app.listening_indicator.messages[-1]
    assert message == speech_readiness.LOADING
    assert ttl is None and feedback.owner == 'speech-model'
    assert feedback.alive()
    app.model_loaded = True
    assert not feedback.alive()


@pytest.mark.parametrize('display', ['compact', 'full'])
def test_real_displays_keep_progress_then_clear_only_model_owner(
        qapp, monkeypatch, qt_widget_scope, display):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.ui import qt_runtime
    from samsara.ui.listening_indicator import ListeningIndicator
    app = BootApp()
    monkeypatch.setattr(qt_runtime, 'post', lambda fn, *args: fn(*args))
    if display == 'compact':
        indicator = ListeningIndicator()
    else:
        controller = LiveSurfaceController(app)
        indicator = LiveSurfaceIndicatorAdapter(controller)
    app.listening_indicator = indicator
    speech_readiness.show(app)
    if display == 'compact':
        with qt_widget_scope(indicator):
            assert indicator._chip_label == speech_readiness.LOADING
            assert indicator._chip_feedback.owner == 'speech-model'
            app.model_loaded = True
            speech_readiness.phase(app.update_splash, 'ready')
            assert not indicator._chip_label
    else:
        assert controller.view().notice.message == speech_readiness.LOADING
        assert controller.view().notice.owner == 'speech-model'
        app.model_loaded = True
        speech_readiness.phase(app.update_splash, 'ready')
        assert not controller.view().notice.message
