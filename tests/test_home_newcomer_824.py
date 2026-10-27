"""Home's newcomer wording, using isolated app state and no live services."""
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QFrame

from samsara.ui import home_qt, home_signals, main_window_qt, theme
from tests.test_home_qt import _isolated, _native_home_font  # noqa: F401


@pytest.mark.parametrize("binding,expected", [
    ("right_ctrl", "Right Ctrl"),
    ("rctrl", "Right Ctrl"),
    ("right_alt", "Right Alt"),
    ("ctrl+shift", "Ctrl+Shift"),
    ("mouse4", "Mouse 4"),
])
def test_home_names_keys_like_settings(binding, expected):
    assert home_qt.hotkey_label(binding) == expected


@pytest.mark.parametrize("state,route,expected", [
    (home_signals.AVA_ASLEEP, "Ava: off", "Ava: off"),
    (home_signals.AVA_AWAKE, "Ava: cloud - DeepSeek", "Ava: ready \u2014 cloud - DeepSeek"),
    (home_signals.AVA_AWAKE, "Ava: on this PC - local-model", "Ava: ready \u2014 on this PC - local-model"),
    (home_signals.AVA_CHECKING, "Ava: cloud - DeepSeek", "Ava: checking \u2014 cloud - DeepSeek"),
    (home_signals.AVA_DIM, "Ava: cloud chosen, API key needed", "Ava: unavailable \u2014 cloud chosen, API key needed"),
    (home_signals.AVA_WARMING, "Ava: on this PC - local-model", "Ava: warming \u2014 on this PC - local-model"),
])
def test_ava_header_joins_state_and_route_once(qapp, qt_widget_scope, monkeypatch,
                                              state, route, expected):
    monkeypatch.setattr(home_signals, "ava_presence", lambda app: (state, "Test readiness."))
    monkeypatch.setattr(home_signals, "ava_route_label", lambda app: route)
    with qt_widget_scope(home_qt.AvaHeaderControl(SimpleNamespace(config={}))) as control:
        assert control.visible_text == expected
        assert control.accessibleName() == expected
        assert control.toolTip() == "Test readiness."


def test_size_reset_names_its_action_and_separates_ava_from_mic(qapp, qt_widget_scope, monkeypatch):
    from tests.test_home_qt import _hub

    resets = []
    monkeypatch.setattr(main_window_qt._MainWindow, "_reset_geometry", lambda self: resets.append(True))
    with qt_widget_scope(_hub(qapp, monkeypatch)) as window:
        button = window._reset_btn
        assert button.text() == "Reset size"
        assert button.icon().isNull()
        assert button.toolTip() == button.accessibleName() == "Reset window size"
        divider = window.findChild(QFrame, "avaMicDivider")
        assert divider.isVisibleTo(window)
        assert window._ava.x() + window._ava.width() <= divider.x() < window._badge.x()
        button.click()
        assert resets == [True]


@pytest.mark.parametrize('mode,expected', [
    ('hold', 'Hold Right Ctrl to talk'),
    ('toggle', 'Tap Right Ctrl to start and stop'),
])
@pytest.mark.parametrize('wake_on', [True, False])
def test_strip_names_configured_binding_and_only_offers_enabled_wake(
        qapp, monkeypatch, mode, expected, wake_on):
    from tests.test_home_qt import _app, _hub
    app = _app()
    app.config.update(mode=mode, hotkey='right_ctrl', wake_word_enabled=wake_on)
    # Test enabled/disabled wording with room for all fragments; narrow fitting
    # is measured separately below, against synthetic screen geometry as well.
    monkeypatch.setattr(main_window_qt._MainWindow, '_geometry_screens',
                        lambda self: ([(0, 0, 1920, 1080)], (0, 0, 1920, 1080)))
    window = _hub(qapp, monkeypatch, width=1400, app=app)
    assert window._lbl_mode.text() == expected
    assert window._lbl_wake.text() == ('or say "jarvis"' if wake_on else '')
    assert window._lbl_wake.parentWidget().isVisibleTo(window) == wake_on


@pytest.mark.parametrize('ava_on,same_key,expected', [
    (True, False, 'Ava key: Right Alt'), (False, False, ''), (True, True, ''),
])
def test_separate_ava_key_only_when_ava_is_on(ava_on, same_key, expected):
    from tests.test_home_qt import _app
    app = _app()
    app.config.update(hotkey='right_ctrl', ava_mode_key='rctrl' if same_key else 'right_alt',
                      ava={'provider_policy': 'cloud' if ava_on else 'off'},
                      ava_command_session={'enabled': ava_on})
    assert home_qt.talk_instructions(app)[2] == expected


def test_default_ready_keeps_the_key_and_wake_instruction_visible(qapp, monkeypatch):
    from tests.test_home_qt import _app, _hub
    app = _app()
    app.config['hotkey'] = 'right_ctrl'
    monkeypatch.setattr(main_window_qt._MainWindow, '_geometry_screens',
                        lambda self: ([(0, 0, 1920, 1080)], (0, 0, 1920, 1080)))
    window = _hub(qapp, monkeypatch, width=900, height=650, app=app)
    for label in (window._lbl_mode, window._lbl_wake):
        assert label.isVisibleTo(window)
        assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
    assert window._lbl_wake.text() == 'or say "jarvis"'


@pytest.mark.parametrize('state,flags,label,colour,action', [
    ('ready', {'wake_word_active': False}, 'Mic ready', 'TEXT_SECONDARY', 'pause'),
    # 940: preserve the ID while following 918's explicit wake-waiting label.
    pytest.param('waiting', {}, 'Waiting for "jarvis"', 'TEXT_SECONDARY', 'pause',
                 id='waiting-flags1-Mic ready-TEXT_SECONDARY-pause'),
    ('recording', {'recording': True}, 'Recording', 'ACCENT', 'stop'),
    ('recording', {'toggle_active': True}, 'Recording', 'ACCENT', 'stop'),
    ('ava_session', {'ava_command_session_active': True}, 'Ava listening', 'ACCENT', 'stop'),
    ('hands_free', {'command_mode_active': True}, 'Hands-free on', 'ACCENT', 'stop'),
    ('continuous', {'continuous_active': True}, 'Hands-free on', 'ACCENT', 'stop'),
    ('paused', {'snoozed': True}, 'Paused', 'TEXT_SECONDARY', 'resume'),
    ('unavailable', {'available_mics': []}, 'No microphone', 'ERROR', 'none'),
])
def test_listening_state_label_colour_and_click(qapp, monkeypatch, state, flags, label, colour, action):
    from tests.test_home_qt import _app, _hub
    app = _app(**flags)
    calls = []
    app.snooze_listening = lambda minutes: calls.append('pause')
    app.resume_listening = lambda: calls.append('resume')
    for method in ('stop_recording', 'exit_command_mode', 'exit_ava_command_session', 'stop_continuous_mode'):
        setattr(app, method, lambda: calls.append('stop'))
    window = _hub(qapp, monkeypatch, app=app)
    if state != 'unavailable':
        assert home_qt.listening_state_id(app) == state
    badge = window._paused_btn if state == 'paused' else window._badge
    assert badge.isVisibleTo(window)
    assert badge.text() == label
    assert f'color: {getattr(theme, colour)};' in badge.styleSheet()
    assert badge.accessibleName().startswith(label + '. ')
    assert (colour == 'ACCENT') == home_qt.capturing(app)
    if action == 'none':
        assert not badge.isEnabled()
    else:
        assert f'Click to {action} listening.' in badge.toolTip()
        assert f'Click to {action} listening.' in badge.accessibleName()
        badge.click()
        assert calls == [action]
    if state in ('ready', 'waiting'):
        assert 'Nothing is being recorded.' in badge.toolTip()
        assert 'Hold Ctrl+Shift to talk' in badge.toolTip()


@pytest.mark.parametrize('flags', [
    {'snoozed': True}, {'recording': True}, {'toggle_active': True},
    {'command_mode_active': True}, {'ava_command_session_active': True},
    {'continuous_active': True}, {'wake_word_active': False}, {'available_mics': []},
])
def test_strip_never_offers_a_suspended_or_unavailable_wake_phrase(flags):
    from tests.test_home_qt import _app
    assert home_qt.talk_instructions(_app(**flags))[1] == ''


def test_active_capture_outranks_pause_and_missing_device():
    from tests.test_home_qt import _app
    app = _app(recording=True, snoozed=True, available_mics=[])
    label, colour, _tip, action = home_qt.listening_badge_view(app)
    # 940: 918 makes confirmed microphone loss outrank stale capture flags.
    assert (label, colour, action) == ('No microphone', theme.ERROR, 'none')


@pytest.mark.parametrize('mode', ['hold', 'toggle'])
def test_minimum_width_keeps_keys_and_drops_long_wake_first(qapp, monkeypatch, mode):
    from tests.test_home_qt import _app, _hub
    from PySide6.QtWidgets import QBoxLayout
    app = _app()
    app.config.update(mode=mode, hotkey='right_ctrl', ava_mode_key='right_alt',
                      ava={'provider_policy': 'cloud'}, ava_command_session={'enabled': True})
    app.config['wake_word_config']['phrase'] = 'a very long wake phrase ' * 12
    monkeypatch.setattr(main_window_qt._MainWindow, '_geometry_screens',
                        lambda self: ([(0, 0, 1920, 1080)], (0, 0, 1920, 1080)))
    window = _hub(qapp, monkeypatch, width=main_window_qt.MIN_WIDTH, height=650, app=app)
    assert window.width() == main_window_qt.MIN_WIDTH
    assert 'Right Ctrl' in window._lbl_mode.text()
    assert 'Ava key: Right Alt' in window._lbl_mode.text()
    assert not window._lbl_wake.parentWidget().isVisibleTo(window)
    assert window._lbl_mode.parentWidget().layout().direction() == QBoxLayout.Direction.LeftToRight
    assert window._lbl_mode.width() >= window._lbl_mode.fontMetrics().horizontalAdvance(window._lbl_mode.text())
