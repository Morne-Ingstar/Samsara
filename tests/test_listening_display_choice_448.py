"""448: legacy derivation, single construction, setup/save and restart boundary."""
import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import listening_display as display

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('legacy_full,legacy_indicator,expected,idle', [
    (False, False, 'compact', False), (False, True, 'compact', True),
    (True, False, 'full', True), (True, True, 'full', True),
])
def test_legacy_combinations_are_derived_once_without_startup_writes(tmp_path, legacy_full, legacy_indicator, expected, idle):
    config = {'listening_indicator_enabled': legacy_indicator, 'ui': {'live_surface': {'enabled': legacy_full}}}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    before = path.read_bytes()
    app = SimpleNamespace(config=config)
    selected = display.run_display(app)
    assert (selected.value, selected.show_idle) == (expected, idle)
    config['ui']['live_surface']['enabled'] = not legacy_full
    assert display.run_display(app) is selected
    assert 'listening_display' not in config['ui']
    assert path.read_bytes() == before


def test_fresh_config_remains_quiet_until_setup_or_choice():
    config = {}
    assert display.display_choice(config) == display.DisplayChoice('compact', False)
    assert config == {}
    assert display.PRESELECTED == 'full'
    assert display.display_choice(config, setup=True) == display.DisplayChoice('full', True)


def test_derived_save_retains_existing_full_idle_preference_without_notice():
    config = {'ui': {'live_surface': {'enabled': True, 'show_idle_mark': False}}}
    app = SimpleNamespace(config=config)
    original = display.run_display(app)
    config.update(display.choice_updates(config))
    assert display.display_choice(config) == original
    assert not display.request_restart_once(app, Mock())


@pytest.mark.parametrize('value', display.VALUES)
def test_setup_question_saves_both_choices_on_finish(qapp, tmp_path, monkeypatch, value):
    from samsara.ui import first_run_wizard_qt as wizard
    monkeypatch.setattr(wizard._WizardWindow, '_load_mics', lambda *_: None)
    window = wizard._WizardWindow(tmp_path / 'setup.json')
    try:
        page = window._listening_display_page
        assert page.group.checkedButton().property('display_value') == display.PRESELECTED
        for option in display.VALUES:
            button = page.group.button(display.VALUES.index(option))
            assert button.accessibleName() == display.NAMES[option]
            assert button.accessibleDescription() == display.DESCRIPTIONS[option]
            assert button.focusPolicy() != 0
        page.group.button(display.VALUES.index(value)).setChecked(True)
        window._config['microphone'] = 1
        window._step = len(wizard._STEPS) - 1
        window._finish()
        assert window.result['ui']['listening_display'] == value
        assert window.result['ui']['live_surface']['show_idle_mark'] is True
        assert window.result['first_run_complete']
    finally:
        window.deleteLater()


def _settings(qapp, tmp_path, monkeypatch, config):
    from samsara.ui import settings_qt
    from samsara.ui.settings.shared_qt import QMessageBox
    from tests._theme_stub_app import StubApp
    app = StubApp(deepcopy(config))
    path = tmp_path / 'settings.json'
    app.save_config = lambda: path.write_text(json.dumps(app.config), encoding='utf-8')
    notices = []
    monkeypatch.setattr(QMessageBox, 'information', lambda _parent, _title, text: notices.append(text))
    window = settings_qt._SettingsWindow(app)
    return app, window, path, notices


@pytest.mark.parametrize('value', display.VALUES)
def test_settings_choice_is_saved_without_switching_objects_and_notices_once(qapp, tmp_path, monkeypatch, value):
    starting = 'full' if value == 'compact' else 'compact'
    config = {'listening_indicator_enabled': True, 'ui': {'listening_display': starting, 'live_surface': {'enabled': True, 'show_idle_mark': True}}}
    app, window, path, notices = _settings(qapp, tmp_path, monkeypatch, config)
    original = object()
    app.listening_indicator = original
    app.live_surface = original if starting == 'full' else None
    frozen = display.run_display(app)
    try:
        combo = window._widgets['listening_display']
        assert [combo.itemData(i) for i in range(combo.count())] == ['compact', 'full']
        assert 'adv_indicator_enabled' not in window._widgets
        assert 'adv_live_surface_enabled' not in window._widgets
        combo.setCurrentIndex(combo.findData(value))
        window._apply_and_close()
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['ui']['listening_display'] == value
        assert saved['listening_indicator_enabled'] is True
        assert saved['ui']['live_surface']['enabled'] is True
        assert app.listening_indicator is original
        assert display.run_display(app) is frozen
        assert notices == [display.RESTART_NOTICE]
        assert not display.request_restart_once(app, notices.append)
        assert notices == [display.RESTART_NOTICE]
        restarted = SimpleNamespace(config=deepcopy(saved))
        assert display.run_display(restarted).value == value
    finally:
        window.deleteLater()


def test_unrelated_settings_save_keeps_legacy_quiet_and_preserves_outside_choice(qapp, tmp_path, monkeypatch):
    app, window, path, notices = _settings(qapp, tmp_path, monkeypatch, {})
    try:
        window._apply_and_close()
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['ui']['listening_display'] == 'compact'
        assert display.display_choice(saved).show_idle is False
        assert notices == []
    finally:
        window.deleteLater()


def test_settings_does_not_overwrite_choice_made_outside_open_window(qapp, tmp_path, monkeypatch):
    app, window, path, notices = _settings(qapp, tmp_path, monkeypatch, {})
    try:
        app.config.update(display.choice_updates(app.config, 'full', chosen=True))
        window._apply_and_close()
        assert json.loads(path.read_text(encoding='utf-8'))['ui']['listening_display'] == 'full'
        assert notices == [display.RESTART_NOTICE]
    finally:
        window.deleteLater()


def test_selecting_current_compact_option_explicitly_leaves_legacy_quiet(qapp, tmp_path, monkeypatch):
    app, window, path, notices = _settings(qapp, tmp_path, monkeypatch, {})
    try:
        combo = window._widgets['listening_display']
        combo.activated.emit(combo.currentIndex())
        window._apply_and_close()
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['ui']['listening_display'] == 'compact'
        assert display.display_choice(saved).show_idle
        assert not display.run_display(app).show_idle
        assert notices == [display.RESTART_NOTICE]
    finally:
        window.deleteLater()


def test_setup_preselection_has_one_constant(qapp, monkeypatch):
    from samsara.ui.listening_display_page import ListeningDisplayPage
    monkeypatch.setattr(display, 'PRESELECTED', 'compact')
    page = ListeningDisplayPage({})
    try:
        assert page.group.checkedButton().property('display_value') == 'compact'
    finally:
        page.deleteLater()


@pytest.mark.parametrize('session_name', ['StreamingSession', 'DictatePreviewSession'])
def test_full_before_initializer_does_not_construct_a_legacy_fallback(monkeypatch, session_name):
    from samsara import streaming
    legacy = Mock()
    monkeypatch.setattr(streaming, 'StreamingOverlayQt', legacy)
    app = SimpleNamespace(config={'ui': {'listening_display': 'full'}}, live_surface=None)
    with pytest.raises(RuntimeError, match='not been constructed'):
        getattr(streaming, session_name)(app)
    legacy.assert_not_called()


@pytest.mark.parametrize('config,expected', [
    ({}, 'compact'),
    ({'listening_indicator_enabled': False, 'ui': {'live_surface': {'enabled': False}}}, 'compact'),
    ({'listening_indicator_enabled': True, 'ui': {'live_surface': {'enabled': False}}}, 'compact'),
    ({'listening_indicator_enabled': False, 'ui': {'live_surface': {'enabled': True}}}, 'full'),
    ({'listening_indicator_enabled': True, 'ui': {'live_surface': {'enabled': True}}}, 'full'),
    ({'ui': {'listening_display': 'compact', 'live_surface': {'enabled': True}}}, 'compact'),
    ({'ui': {'listening_display': 'full', 'live_surface': {'enabled': False}}}, 'full'),
])
def test_real_startup_delegates_to_exactly_one_display(monkeypatch, config, expected):
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    host = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    init = next(n for n in host.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    indicator_init = next(n for n in ast.walk(init) if isinstance(n, ast.FunctionDef) and n.name == '_init_indicator')
    calls = [n for n in ast.walk(indicator_init) if isinstance(n, ast.Call)]
    delegation = [n for n in calls if ast.unparse(n.func) == 'listening_display.build_display']
    assert len(delegation) == 1
    assert [ast.unparse(arg) for arg in delegation[0].args] == ['self', 'ListeningIndicator']
    assert not any(ast.unparse(n.func) in ('ListeningIndicator', 'LiveSurfaceController') for n in calls)
    built = []
    def compact():
        built.append('compact')
        return Mock()
    def full(app):
        built.append('full')
        return Mock()
    import samsara.live_surface.controller as controller
    monkeypatch.setattr(controller, 'LiveSurfaceController', full)
    monkeypatch.setattr(controller, 'LiveSurfaceIndicatorAdapter', lambda c: SimpleNamespace(controller=c))
    app = SimpleNamespace(config=deepcopy(config), live_surface=None, listening_indicator=None, wake_word_active=False,
                          _get_mode_display=lambda: 'Hold', _idle_animation_enabled=lambda: False,
                          _on_indicator_placement_committed=lambda *_: None)
    display.build_display(app, compact)
    assert built == [expected]
    assert (app.live_surface is not None) == (expected == 'full')


@pytest.mark.parametrize('starting', display.VALUES)
def test_hold_preview_and_hotkeys_keep_frozen_runtime_after_config_change(monkeypatch, starting):
    from samsara import streaming, hotkeys
    built = []
    class LegacyOverlay:
        def __init__(self, **kwargs): built.append(self)
        def set_interaction_callbacks(self, *args): pass
        def set_correction_choice_callbacks(self, *args): pass
    monkeypatch.setattr(streaming, 'StreamingOverlayQt', LegacyOverlay)
    controller = SimpleNamespace(begin_capture=lambda lane: 1)
    app = SimpleNamespace(config={'ui': {'listening_display': starting}}, device_type='cpu',
                          live_surface=controller if starting == 'full' else None)
    frozen = display.run_display(app)
    app.config['ui']['listening_display'] = 'compact' if starting == 'full' else 'full'
    hold = streaming.StreamingSession(app)
    preview = streaming.DictatePreviewSession(app)
    assert display.run_display(app) is frozen
    assert hotkeys.HotkeyCluster._live_surface_enabled(app) == frozen.full
    assert hold._parking_enabled == frozen.full
    if frozen.full:
        assert not built
        assert isinstance(hold._overlay, streaming._LiveSurfaceStreamingOverlay)
        assert isinstance(preview._overlay, streaming._LiveSurfaceDictateOverlay)
    else:
        assert len(built) == 2


def _legacy_reads(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    aliases = set()

    def surface(receiver):
        if isinstance(receiver, (ast.Name, ast.Attribute)) and ast.unparse(receiver) in aliases:
            return True
        if isinstance(receiver, ast.BoolOp):
            return any(surface(value) for value in receiver.values)
        key = None
        if isinstance(receiver, ast.Subscript):
            key = receiver.slice
        elif (isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute)
              and receiver.func.attr == 'get' and receiver.args):
            key = receiver.args[0]
        return isinstance(key, ast.Constant) and key.value == 'live_surface'

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and surface(node.value):
            aliases.update(ast.unparse(target) for target in node.targets
                           if isinstance(target, (ast.Name, ast.Attribute)))
    reads = []
    for node in ast.walk(tree):
        key = receiver = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and node.args:
            key, receiver = node.args[0], node.func.value
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            key, receiver = node.slice, node.value
        if not isinstance(key, ast.Constant):
            continue
        if key.value in ('listening_indicator_enabled', 'ui.live_surface.enabled'):
            reads.append(node.lineno)
        elif key.value == 'enabled' and surface(receiver):
            reads.append(node.lineno)
    return sorted(set(reads))


def test_no_other_executable_legacy_key_reader():
    allowed = {'samsara/listening_display.py', 'samsara/config_schema.py', 'samsara/live_surface/config.py'}
    paths = [ROOT / 'dictation.py']
    for folder in ('samsara', 'plugins', 'tools'):
        paths.extend((ROOT / folder).rglob('*.py'))
    bad = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if relative not in allowed:
            bad.extend(f'{relative}:{line}' for line in _legacy_reads(path))
    assert not bad, '\n'.join(bad)


def test_legacy_reader_detector_checks_code_and_ignores_comments(tmp_path):
    path = tmp_path / 'reads.py'
    path.write_text("# listening_indicator_enabled\na = config.get('listening_indicator_enabled')\nb = config['ui']['live_surface'].get('enabled')\nlive = config.get('ui', {}).get('live_surface', {})\nc = live['enabled']\nfixture = {'ui': {'live_surface': {'enabled': True}}}\nd = fixture.get('plugin', {}).get('enabled')\n", encoding='utf-8')
    assert _legacy_reads(path) == [2, 3, 5]


@pytest.mark.parametrize('step', range(8), ids=[
    'welcome', 'use-case', 'microphone', 'components',
    'model', 'shortcuts', 'listening-display', 'complete',
])
def test_every_setup_page_keeps_stage_titles_above_rule(qapp, tmp_path, monkeypatch, step):
    from PySide6.QtWidgets import QFrame
    from samsara.ui import first_run_wizard_qt as wizard
    from samsara.ui.listening_display_page import STEP_LABEL_GAP
    monkeypatch.setattr(wizard._WizardWindow, '_load_mics', lambda *_: None)
    monkeypatch.setattr(wizard._WizardWindow, '_start_meter', lambda *_: None)
    window = wizard._WizardWindow(tmp_path / 'setup.json')
    try:
        window.resize(860, 760)
        window._step = step
        window._show_step()
        window.show()
        qapp.processEvents()
        root = window.centralWidget().layout()
        indicator = window._step_indicator
        index = next(i for i in range(root.count()) if root.itemAt(i).widget() is indicator)
        rule = next(root.itemAt(i).widget() for i in range(index + 1, root.count())
                    if isinstance(root.itemAt(i).widget(), QFrame))
        gap = rule.y() - (indicator.y() + indicator.height())
        assert STEP_LABEL_GAP == 12  # Same accepted gap as tutorial 378.
        assert rule.frameShape() == QFrame.Shape.HLine
        assert gap >= STEP_LABEL_GAP, (wizard._STEPS[step][0], gap)
        # Device pixels must preserve the margin on scaled displays too.
        assert gap * indicator.devicePixelRatioF() >= STEP_LABEL_GAP * indicator.devicePixelRatioF()
    finally:
        window.hide()
        window.deleteLater()


def test_long_setup_descriptions_fit_the_scroll_body(qapp):
    from PySide6.QtWidgets import QLabel
    from samsara.ui.listening_display_page import ListeningDisplayPage
    page = ListeningDisplayPage({})
    try:
        page.resize(720, 180)
        page.show()
        qapp.processEvents()
        labels = [label for label in page.findChildren(QLabel)
                  if label.text() in display.DESCRIPTIONS.values()]
        assert len(labels) == 2
        for label in labels:
            assert label.wordWrap()
            assert label.height() >= label.heightForWidth(label.width()), label.text()
        assert page.verticalScrollBar().maximum() > 0
    finally:
        page.hide()
        page.deleteLater()
