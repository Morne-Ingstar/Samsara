"""Physical hold mappings, capture refusal, and fake native suppression seam."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from samsara import hold_key_rules as rules


@pytest.fixture
def ava_462(qapp, monkeypatch, request, qt_widget_scope):
    from samsara.ui import ava_guide_qt as ava, theme
    previous = theme.active_theme()
    params = getattr(request.node, 'callspec', SimpleNamespace(params={})).params
    theme.set_theme(params.get('palette', 'dark'), refresh=False)
    monkeypatch.setattr(ava.thread_registry, 'spawn', lambda *a, **kw: None)
    policy = params.get('initial_policy', 'local')
    app = SimpleNamespace(config={'ava': {'provider_policy': policy},
        'ava_command_session': {'enabled': True},
        'ollama': {'enabled': True, 'model': 'qwen2.5:3b', 'host': 'http://127.0.0.1:11434'}})
    provider = ava.ai_preferences.identify_provider('ollama', app.config['ollama']['host'])
    window = ava._WizardWindow(app)
    window._go_to(window._STEP_OLLAMA)
    window._on_ollama_status(True, ['qwen2.5:3b'])
    window.show()
    qapp.processEvents()
    try:
        with qt_widget_scope(window):
            yield window, app, provider
    finally:
        theme.set_theme(previous, refresh=False)


def test_ava_bottom_bar_consent_decline_and_accept(ava_462, monkeypatch, qapp):
    from samsara.ui import ava_consent_qt
    window, app, provider = ava_462
    before = deepcopy(app.config)
    writes = []
    monkeypatch.setattr(ava_consent_qt, 'apply_accepted_ava',
                        lambda *a, **kw: writes.append(kw))
    assert window._next_btn.text() == 'Continue'
    assert window._skip_btn.text() == 'Set up later' and window._skip_btn.isVisible()
    window._next_btn.click()
    assert window._current_step == window._STEP_CONSENT
    assert not window._next_btn.isEnabled()
    window._next_btn.click()
    assert window._accepted is None and app.config == before and writes == []
    window._consent_check.setChecked(True)
    assert window._next_btn.isEnabled()
    window._next_btn.click()
    assert window._accepted == ('local', window._provider.currentData())
    assert window._current_step == window._STEP_TEST
    assert app.config == before and writes == []
    window._tested = window._signature()
    window._job_config = deepcopy(app.config)
    window._go_to(window._STEP_DONE)
    assert window._next_btn.text() == 'Use Ava'
    consent = {'accepted_at': 'fixture'}
    monkeypatch.setattr(window, '_draft', lambda: {'ava': {'consent': consent}})
    window._next_btn.click()
    assert writes == [{'policy': 'local', 'config': {'ava': {'consent': consent}}}]
    assert window._closed


@pytest.mark.parametrize('policy,running,models,reason', [
    ('local', False, [], 'Ollama is not reachable.'),
    ('local', True, [], 'The selected model is not installed.'),
    ('cloud', True, ['qwen2.5:3b'], 'Ava is set to the cloud in Settings > Ava.'),
    ('off', True, ['qwen2.5:3b'], 'Ava is set to Off in Settings > Ava.'),
])
def test_ava_done_names_missing_prerequisite(ava_462, monkeypatch, policy, running, models, reason):
    from samsara.ui import ava_guide_qt as ava
    window, app, _ = ava_462
    app.config['ava']['provider_policy'] = policy
    before = deepcopy(app.config)
    window._on_ollama_status(running, models)
    # Setup stages a new choice; the existing saved policy is not a prerequisite.
    assert window._next_btn.isEnabled() == (running and 'qwen2.5:3b' in models)
    assert window._download_ollama_btn.isHidden() == running
    if not running:
        assert window._ollama_status_lbl.text() == ava.COPY['ollama_missing']
    if not models:
        assert window._model_status_lbl.text() == ava.COPY['model_missing']
    else:
        assert window._model_status_lbl.text() == 'Model installed. Ready for a speed test.'
    assert app.config == before


@pytest.mark.parametrize('policy', ['cloud', 'off'])
@pytest.mark.parametrize('when', ['before', 'during'])
def test_ava_route_change_cannot_be_overwritten_by_consent(ava_462, monkeypatch, policy, when):
    from samsara.ui import ava_consent_qt, ava_guide_qt as ava
    window, app, _ = ava_462
    if when == 'before':
        app.config['ava']['provider_policy'] = policy
    window._job_config = deepcopy(app.config)
    window._tested = window._signature()
    window._go_to(window._STEP_DONE)
    writes = []
    monkeypatch.setattr(ava_consent_qt, 'apply_accepted_ava', lambda *a, **kw: writes.append(kw))
    if when == 'during':
        app.config['ava']['provider_policy'] = policy
    else:
        window._model_changed('another-fixture-model')
    window._finish()
    assert app.config['ava']['provider_policy'] == policy and writes == []
    assert window._save_status.text() == ava.COPY['changed']
    assert not window._next_btn.isEnabled()


@pytest.mark.parametrize('palette', ['dark', 'light'])
def test_ava_navigation_label_content_rect_and_stable_height(ava_462, qapp, palette):
    from PySide6.QtWidgets import QStyle, QStyleOptionButton
    from samsara.ui import theme
    window, _, _ = ava_462
    assert theme.active_theme() == palette
    height = window.height()
    for button, labels in ((window._back_btn, ['Back']),
                (window._skip_btn, ['Set up later']),
                (window._next_btn, ['Get started', 'Continue', 'Use Ava'])):
        original = button.text()
        for text in labels:
            button.setText(text)
            qapp.processEvents()
            option = QStyleOptionButton()
            option.initFrom(button)
            option.text = text
            content = button.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, button)
            assert content.width() >= button.fontMetrics().horizontalAdvance(text)
            assert content.height() >= button.fontMetrics().height()
        button.setText(original)
    for step in range(window._stack.count()):
        window._go_to(step)
        qapp.processEvents()
        assert window.height() == height
        assert window._stack.currentWidget().horizontalScrollBar().maximum() == 0


@pytest.mark.parametrize('initial_policy', ['local', 'cloud', 'off'])
def test_ava_initial_route_reserves_every_page_height(ava_462, qapp, initial_policy):
    window, app, _ = ava_462
    before = deepcopy(app.config)
    height = window.height()
    assert window._stack.count() == 8
    for step in range(window._stack.count()):
        window._go_to(step)
        qapp.processEvents()
        assert window.height() == height
        assert window._stack.currentWidget().horizontalScrollBar().maximum() == 0
        assert app.config == before


def test_ava_short_screen_keeps_scroll_fallback(qapp, monkeypatch, qt_deferred_delete):
    from samsara.ui import ava_guide_qt as ava
    monkeypatch.setattr(ava.thread_registry, 'spawn', lambda *a, **kw: None)
    window = ava._WizardWindow(SimpleNamespace(config={}))
    try:
        window.resize(720, 420)
        window._go_to(window._STEP_CHECK)
        window.show()
        qapp.processEvents()
        assert window.height() == 420
        assert window._stack.currentWidget().verticalScrollBar().maximum() > 0
        assert window._stack.currentWidget().horizontalScrollBar().maximum() == 0
    finally:
        window.close()
        window.deleteLater()
        qt_deferred_delete(window)


@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_ava_setup_compact_controls_and_step_clearance(qapp, monkeypatch, palette, scale,
                                                     qt_deferred_delete):
    from PySide6.QtWidgets import QScrollArea
    from samsara.ui import ava_guide_qt as ava, theme
    monkeypatch.setattr(ava.thread_registry, 'spawn', lambda *a, **kw: None)
    previous = theme.active_theme()
    theme.set_theme(palette)
    window = ava._WizardWindow(SimpleNamespace(config={}))
    try:
        # Smaller logical space represents the same screen at the larger scale.
        window.resize(max(720, round(960 / scale)), max(420, round(660 / scale)))
        window.show()
        qapp.processEvents()
        sidebar = window.findChild(ava.QWidget, 'sidebar')
        assert len(window._steps) == 5
        for label in window._steps:
            assert label.parentWidget() is sidebar and label.isVisible()
            assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
            assert label.height() >= label.fontMetrics().height()
        for index in range(window._stack.count()):
            window._go_to(index)
            qapp.processEvents()
            page = window._stack.currentWidget()
            assert isinstance(page, QScrollArea)
            assert page.horizontalScrollBar().maximum() == 0
            assert window._next_btn.isVisible() and window._skip_btn.isVisible()
        window._go_to(window._STEP_OLLAMA)
        qapp.processEvents()
        assert window._model_combo.isVisible()
        assert window._model_combo.width() <= window._stack.width()
    finally:
        window.close()
        window.deleteLater()
        qt_deferred_delete(window)
        theme.set_theme(previous)


@pytest.mark.parametrize('key', sorted(rules.ALONE))
def test_allowed_single_key_families(key):
    assert rules.validate(key).allowed
    assert rules.swallow_binding(key) == {key}


@pytest.mark.parametrize('key,reason', [
    ('a', 'typing'), ('0', 'typing'), ('space', 'typing'), ('enter', 'typing'),
    ('backspace', 'typing'), ('tab', 'typing'), ('escape', 'typing'),
    ('left', 'typing'), ('right', 'typing'), ('up', 'typing'), ('down', 'typing'),
    ('left_ctrl', 'typing'), ('win', 'win'), ('win+a', 'win'),
    ('capslock', 'capslock'), ('numlock', 'numlock'),
    ('ctrl+alt+delete', 'secure'), ('right_ctrl+right_alt+delete', 'secure'),
    ('mouse6', 'unknown'), ('secure_desktop', 'unknown'), ('a+b', 'shape'),
])
def test_refused_keys_have_plain_reason(key, reason):
    decision = rules.validate(key)
    assert not decision.allowed
    assert decision.reason == rules.REFUSALS[reason]
    assert not rules.swallow_binding(key)


@pytest.mark.parametrize('key', ['ctrl+a', 'alt+0', 'shift+space', 'ctrl+enter',
                                'alt+backspace', 'ctrl+tab', 'shift+escape',
                                'ctrl+left', 'ctrl+shift', 'right_ctrl+f24'])
def test_modifier_chords_are_allowed_with_foreground_warning(key):
    decision = rules.validate(key)
    assert decision.allowed and decision.warning == rules.WARNING


@pytest.mark.parametrize('key,label', [('f7', 'Complete alarm'), ('f8', 'Dismiss alarm'),
                                     ('pause', 'Pause display'), ('rctrl', 'Hands-free')])
def test_named_existing_assignments_refused(key, label):
    decision = rules.validate(key, rules.assignments({}), 'ava_mode_key')
    assert not decision.allowed and label in decision.reason


def test_duplicates_include_unsaved_edits_and_modifier_aliases():
    occupied = rules.assignments({}, {'hotkey': 'ctrl+f5'})
    assert not rules.validate('right_ctrl+f5', occupied, 'ava_mode_key').allowed
    assert rules.validate('ctrl+f5', occupied, 'hotkey').allowed
    assert not rules.overlapping_bindings('right_ctrl', 'left_ctrl')
    assert not rules.overlapping_bindings('numpad0', 'insert')


@pytest.mark.parametrize('scan,vk_on,vk_off,name', [
    (82, 96, 45, 'numpad0'), (79, 97, 35, 'numpad1'), (80, 98, 40, 'numpad2'),
    (81, 99, 34, 'numpad3'), (75, 100, 37, 'numpad4'), (76, 101, 12, 'numpad5'),
    (77, 102, 39, 'numpad6'), (71, 103, 36, 'numpad7'), (72, 104, 38, 'numpad8'),
    (73, 105, 33, 'numpad9'), (83, 110, 46, 'numpad_decimal'),
])
def test_numlock_on_and_off_are_the_same_physical_key(scan, vk_on, vk_off, name):
    assert rules.physical_key(vk_on, scan, 0) == rules.physical_key(vk_off, scan, 0) == name
    assert rules.physical_key(vk_off, scan, 1) != name


@pytest.mark.parametrize('binding,consumed', [('f5', True), ('numpad0', True),
    ('scroll_lock', True), ('right_alt', True), ('a', False), ('capslock', False)])
def test_swallow_press_repeat_and_release_for_saved_key_kind(binding, consumed):
    state = rules.SuppressionState(lambda: {'hotkey': binding, 'ava_mode_key': 'f13'})
    assert [state.event(binding, pressed) for pressed in (True, True, False)] == [consumed] * 3
    assert not state.down and not state.swallowed


def test_release_keeps_suppression_when_mapping_changes_or_modifier_released_first():
    config = {'hotkey': 'ctrl+f5'}
    state = rules.SuppressionState(lambda: config)
    assert not state.event('left_ctrl', True)
    assert state.event('f5', True)
    config['hotkey'] = 'f6'
    assert not state.event('left_ctrl', False)
    assert state.event('f5', True)
    assert state.event('f5', False)
    assert not state.event('f5', True)
    assert not state.event('f6', True, injected=True)


class FakeListener:
    _WM_PROCESS = 123
    _PRESS_MESSAGES = (0x100, 0x104)
    _RELEASE_MESSAGES = (0x101, 0x105)

    def __init__(self, on_press, on_release, win32_event_filter):
        self.on_press, self.on_release = on_press, on_release
        self.filter = win32_event_filter
        self.queue = []
        self._message_loop = SimpleNamespace(post=lambda *args: self.queue.append(args))

    class Suppressed(Exception):
        pass

    def suppress_event(self):
        raise self.Suppressed

    def _process(self, *args):
        raise AssertionError('Expected our physical-key message')


@pytest.mark.parametrize('vk,scan,flags', [(0x74, 63, 0), (96, 82, 0), (45, 82, 0)])
def test_listener_queues_before_native_swallow_and_routes_once(vk, scan, flags):
    app = SimpleNamespace(config={'hotkey': 'numpad0' if scan == 82 else 'f5'})
    received, forwarded = [], []
    listener = rules.hold_listener(app, SimpleNamespace(Listener=FakeListener),
                                  lambda key: received.append(('down', key)),
                                  lambda key: received.append(('up', key)))
    for msg in (0x100, 0x100, 0x101):
        try:
            listener.filter(msg, SimpleNamespace(vkCode=vk, scanCode=scan, flags=flags))
            forwarded.append(msg)
        except FakeListener.Suppressed:
            pass  # same exception seam used by pynput's native hook to return 1
    assert not forwarded and len(listener.queue) == 3
    for _, msg, packed in listener.queue:
        listener._process(msg, packed)
    assert [phase for phase, _ in received] == ['down', 'down', 'up']
    assert [bool(key.hold_keys) for _, key in received] == [True, True, False]


def test_unbound_and_injected_keys_are_forwarded():
    app = SimpleNamespace(config={'hotkey': 'f5'})
    listener = rules.hold_listener(app, SimpleNamespace(Listener=FakeListener), lambda key: None, lambda key: None)
    assert listener.filter(0x100, SimpleNamespace(vkCode=ord('A'), scanCode=30, flags=0)) is False
    assert len(listener.queue) == 1  # callback filter is not OS suppression
    assert listener.filter(0x100, SimpleNamespace(vkCode=0x74, scanCode=63, flags=16)) is True
    assert len(listener.queue) == 1


def test_chord_release_routes_even_if_other_modifier_went_up_first():
    app = SimpleNamespace()
    key = SimpleNamespace(hold_name='f5', hold_keys={'left_ctrl', 'f5'})
    assert rules.hold_event_matches(app, key, 'ctrl+f5', True)
    key.hold_keys = set()
    assert rules.hold_event_matches(app, key, 'ctrl+f5', False, was_held=True)


def test_shared_capture_preserves_legacy_and_refused_or_duplicate_choice(qapp):
    from samsara.ui.settings.shared_qt import _HotkeyButton
    config = {'ava_mode_key': 'capslock'}
    original = deepcopy(config)
    button = _HotkeyButton('capslock').configure_hold('ava_mode_key', lambda: config)
    label = button.reason_label()
    assert button.combo == 'capslock'  # never validate/rewrite on load
    assert not button.capture_candidate('space').allowed
    assert button.combo == 'capslock' and label.text() == rules.REFUSALS['typing']
    assert not button.capture_candidate('f7').allowed and 'Complete alarm' in label.text()
    assert button.capture_candidate('numpad0').allowed
    assert button.combo == 'numpad0'
    assert config == original


def test_settings_and_setup_share_capture_and_show_chord_warning(qapp):
    from samsara.ui.settings.shared_qt import _HotkeyButton
    from samsara.ui.first_run_wizard_qt import _HotkeyBtn
    assert issubclass(_HotkeyBtn, _HotkeyButton)
    for cls in (_HotkeyButton, _HotkeyBtn):
        button = cls('right_alt').configure_hold('ava_mode_key', lambda: {})
        label = button.reason_label()
        assert button.capture_candidate('ctrl+f5').allowed
        assert label.text() == rules.WARNING


def test_qt_keypad_capture_and_refused_release_keep_previous_key(qapp):
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtGui import QKeyEvent
    from samsara.ui.settings.shared_qt import _HotkeyButton
    button = _HotkeyButton('right_alt').configure_hold('ava_mode_key', lambda: {})
    for key, modifiers, expected in [(Qt.Key.Key_0, Qt.KeyboardModifier.KeypadModifier, 'numpad0'),
                                      (Qt.Key.Key_Insert, Qt.KeyboardModifier.KeypadModifier, 'numpad0'),
                                      (Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, 'numpad0')]:
        button._start_capture()
        button.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, modifiers))
        button.keyReleaseEvent(QKeyEvent(QEvent.Type.KeyRelease, key, modifiers))
        assert button.combo == expected


def test_all_hold_mouse_bindings_install_selective_suppression():
    from samsara.hotkeys import HotkeyCluster
    for action in rules.HOLD_DEFAULTS:
        config = {}
        if action == 'command_mode.button':
            config['command_mode'] = {'button': 'mouse4', 'suppress_button': True}
        else:
            config[action] = 'mouse4'
        buttons, suppress = HotkeyCluster._mouse_hook_bindings(SimpleNamespace(config=config))
        assert 'mouse4' in buttons and 'mouse4' in suppress


@pytest.mark.parametrize('binding', ['f5', 'numpad0', 'insert', 'right_shift', 'ctrl+f5', 'mouse4'])
def test_ava_and_hands_free_route_new_keys_with_one_edge_per_hold(binding, monkeypatch):
    from samsara import hotkeys
    monkeypatch.setattr(hotkeys, '_get_pynput_command_key', lambda name: None)
    for lane in ('ava', 'command'):
        config = {'ava': {'provider_policy': 'local'}, 'ava_command_session': {'enabled': True},
                  'ava_mode_key': binding,
                  'command_mode': {'enabled': lane == 'command', 'button': binding, 'mode': 'hold'}}
        calls = []
        app = SimpleNamespace(config=config, _ava_mode_key_held=False,
                              _command_mode_key_held=False, ava_mode_active=False,
                              command_mode_active=False,
                              enter_ava_mode=lambda: calls.append('ava_start'),
                              exit_ava_mode=lambda: calls.append('ava_stop'),
                              enter_command_mode=lambda: calls.append('command_start'),
                              exit_command_mode=lambda: calls.append('command_stop'),
                              _dispatch_session_transition=lambda fn: fn())
        if lane == 'command' and binding == 'mouse4':
            hotkeys.HotkeyCluster._on_command_button(app, binding, True)
            hotkeys.HotkeyCluster._on_command_button(app, binding, False)
            assert calls == ['command_start', 'command_stop']
            continue
        name = next(iter(rules.parts(binding) - rules.MODIFIERS), binding)
        if binding == 'right_shift':
            name = binding
        down = set(rules.parts(binding))
        key = SimpleNamespace(hold_name=name, hold_keys=down)
        hotkeys.HotkeyCluster._check_command_mode_key(app, key, True)
        hotkeys.HotkeyCluster._check_command_mode_key(app, key, True)
        app.ava_mode_active = lane == 'ava'
        key.hold_keys = set()
        hotkeys.HotkeyCluster._check_command_mode_key(app, key, False)
        hotkeys.HotkeyCluster._check_command_mode_key(app, key, False)
        assert calls == [lane + '_start', lane + '_stop']


def test_setup_tap_capture_cannot_take_a_hold_binding(qapp):
    from samsara.ui.first_run_wizard_qt import _HotkeyBtn
    button = _HotkeyBtn('ctrl+alt+d').configure_shortcut(
        'continuous_hotkey', lambda: {'ava_mode_key': 'f5'})
    label = button.reason_label()
    assert not button.capture_candidate('f5').allowed
    assert 'Ava' in label.text() and button.combo == 'ctrl+alt+d'


def test_qt_capture_distinguishes_right_modifier_and_f24(qapp):
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtGui import QKeyEvent
    assert rules.qt_key_name(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Control,
               Qt.KeyboardModifier.ControlModifier, 0x11D, 163, 0)) == 'right_ctrl'
    assert rules.qt_key_name(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_F24,
               Qt.KeyboardModifier.NoModifier)) == 'f24'
    assert rules.qt_key_name(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Insert,
               Qt.KeyboardModifier.NoModifier, 82, 45, 0)) == 'numpad0'
    assert rules.qt_key_name(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Insert,
               Qt.KeyboardModifier.NoModifier, 0x152, 45, 0)) == 'insert'


def test_tab_capture_is_refused_instead_of_moving_focus(qapp):
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from samsara.ui.settings.shared_qt import _HotkeyButton
    button = _HotkeyButton('right_alt').configure_hold('ava_mode_key', lambda: {})
    label = button.reason_label()
    button._start_capture()
    QApplication.sendEvent(button, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier))
    QApplication.sendEvent(button, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier))
    assert button.combo == 'right_alt' and label.text() == rules.REFUSALS['typing']


def test_hold_schema_accepts_open_capture_without_changing_defaults():
    from samsara.config_schema import SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA['command_mode.button']['type'] == 'str'
    assert 'options' not in SETTINGS_SCHEMA['command_mode.button']
    assert SETTINGS_SCHEMA['command_mode.button']['default'] == 'rctrl'
    assert SETTINGS_SCHEMA['task_hotkey']['default'] == rules.HOLD_DEFAULTS['task_hotkey'][1]


def test_current_setup_captures_all_hold_keys_and_preserves_existing_values(qapp, tmp_path, monkeypatch):
    import json
    from samsara.ui.first_run_wizard_qt import _WizardWindow, _STEPS
    from samsara.ui.first_run_pages_qt import Decisions
    monkeypatch.setattr(_WizardWindow, '_load_mics', lambda self: None)
    monkeypatch.setattr(Decisions, 'load_facts', lambda self: None)
    config_path = tmp_path / 'setup.json'
    saved = {'hotkey': 'capslock', 'ava_mode_key': 'a', 'task_hotkey': 'f23'}
    config_path.write_text(json.dumps(saved), encoding='utf-8')
    window = _WizardWindow(config_path)
    try:
        # 373's walkthrough exposes the record key and the hands-free button only (the Ava key
        # lives in the Ava guide); every other saved hold key must survive a rerun untouched.
        assert {'hotkey', 'command_mode.button'} <= set(window._hotkey_btns)
        assert window._hotkey_btns['hotkey'].combo == 'capslock'
        record = window._hotkey_btns['hotkey']
        assert not record.capture_candidate('f23').allowed
        assert record.combo == 'capslock' and 'Task' in record._capture_reason
        assert record.capture_candidate('numpad0').allowed
        window._step = next(index for index, (name, _) in enumerate(_STEPS) if name == 'Talking')
        window._collect_step()
        assert window._config['ava_mode_key'] == 'a'
        assert window._config['hotkey'] == 'numpad0'  # the capture above rebound the record key
        assert window._config['task_hotkey'] == 'f23'
        assert window._config['command_mode']['button'] == 'rctrl'
    finally:
        window.deleteLater()


def test_session_mouse_capture_warns_for_pending_suppression_opt_out(qapp):
    from PySide6.QtWidgets import QCheckBox
    from samsara.ui.settings.shared_qt import _HotkeyButton
    checkbox = QCheckBox()
    checkbox.setChecked(False)
    original = {'command_mode': {'suppress_button': True}}
    config = rules.capture_config(original, {'cmd_tab_suppress': checkbox})
    button = _HotkeyButton('rctrl').configure_hold('command_mode.button', lambda: config)
    label = button.reason_label()
    assert button.capture_candidate('mouse4').allowed
    assert label.text() == rules.WARNING
    assert original['command_mode']['suppress_button'] is True


def test_bound_keyboard_key_reaches_picker_without_app_or_foreground_action(qapp, monkeypatch):
    from samsara.ui import qt_runtime
    from samsara.ui.settings.shared_qt import _HotkeyButton
    posted, app_calls = [], []
    monkeypatch.setattr(qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(qt_runtime, 'post', posted.append)
    config = {'hotkey': 'f5'}
    button = _HotkeyButton('right_alt').configure_hold('ava_mode_key', lambda: config)
    label = button.reason_label()
    button._start_capture()
    app = SimpleNamespace(config=config, current_keys=set())
    listener = rules.hold_listener(app, SimpleNamespace(Listener=FakeListener),
                                  lambda key: app_calls.append('down'),
                                  lambda key: app_calls.append('up'))
    for msg in (0x100, 0x100, 0x101):
        with pytest.raises(FakeListener.Suppressed):
            listener.filter(msg, SimpleNamespace(vkCode=0x74, scanCode=63, flags=0))
    for _, msg, packed in listener.queue:
        listener._process(msg, packed)
    for callback in posted:
        callback()
    assert not app_calls and not listener.event_down and not app.current_keys
    assert button.combo == 'right_alt' and 'Primary dictation' in label.text()
    assert not button._capturing


def test_bound_mouse_key_and_streaming_capslock_are_routed_to_picker(qapp, monkeypatch):
    from samsara import hotkeys
    from samsara.ui import qt_runtime
    from samsara.ui.settings.shared_qt import _HotkeyButton
    posted = []
    monkeypatch.setattr(qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(qt_runtime, 'post', posted.append)
    button = _HotkeyButton('right_alt').configure_hold('ava_mode_key', lambda: {'hotkey': 'mouse4'})
    button.reason_label()
    button._start_capture()
    # No app attributes are needed: capture takes both events before hold routing.
    for pressed in (True, False):
        hotkeys.HotkeyCluster._on_mouse_button(SimpleNamespace(), 'mouse4', pressed)
    for callback in posted:
        callback()
    assert button.combo == 'right_alt' and 'Primary dictation' in button._capture_reason
    posted.clear()
    button._start_capture()
    for phase in (hotkeys.keyboard.KEY_DOWN, hotkeys.keyboard.KEY_UP):
        hotkeys.HotkeyCluster._on_capslock_event(SimpleNamespace(), SimpleNamespace(event_type=phase))
    for callback in posted:
        callback()
    assert button.combo == 'right_alt' and button._capture_reason == rules.REFUSALS['capslock']


def test_legacy_duplicate_assignment_keeps_foreground_behavior_and_config():
    config = {'hotkey': 'f7'}  # old saved key conflicts with Complete alarm
    before = deepcopy(config)
    state = rules.SuppressionState(lambda: config)
    assert [state.event('f7', pressed) for pressed in (True, True, False)] == [False] * 3
    assert config == before


def test_refused_legacy_ava_key_does_not_gain_an_activation_path(monkeypatch):
    from samsara import hotkeys
    calls = []
    monkeypatch.setattr(hotkeys, '_get_pynput_command_key', lambda key: None)
    app = SimpleNamespace(config={'ava': {'provider_policy': 'local'},
                                  'ava_command_session': {'enabled': True}, 'ava_mode_key': 'a'},
                          _ava_mode_key_held=False, ava_mode_active=False,
                          command_mode_active=False, enter_ava_mode=lambda: calls.append('start'))
    key = SimpleNamespace(hold_name='a', hold_keys={'a'})
    hotkeys.HotkeyCluster._check_command_mode_key(app, key, True)
    assert not calls and app.config['ava_mode_key'] == 'a'
