"""490: isolated fresh defaults, saved placement, live Settings seam and sizing."""
import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from samsara.onboarding import display_placement as placement
from samsara.ui import first_run_wizard_qt as wizard, tutorial_qt, theme

# 522: the picker owns the sole desktop and the shared demo loop now.

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def windows(qapp, monkeypatch):
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    created = []
    yield created
    for window in created:
        window.close()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.mark.parametrize('display', ['full', 'compact'])
def test_fresh_setup_starts_bottom_left(windows, tmp_path, display):
    win = wizard._WizardWindow(tmp_path/'new.json')
    windows.append(win)
    win._listening_display_page.group.button(('compact', 'full').index(display)).setChecked(True)
    assert placement.current_position(win._config, display) == 'bottom-left'
    assert win._display_placement_page.picker.value() == 'bottom-left'
    assert win._display_placement_page.picker.desktop.display == display
    assert win._display_placement_page.picker.desktop.display == display


def config_nodes():
    tree = ast.parse((ROOT/'dictation.py').read_text(encoding='utf-8'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'load_config')
    branch = next(n for n in ast.walk(method) if isinstance(n, ast.If) and
                  any(isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) and
                      ast.unparse(s.value.func) == 'display_placement.seed_fresh' for s in n.orelse))
    return branch


def execute(nodes, app, defaults):
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<config boundary>', 'exec'),
         dict(self=app, default_config=defaults, display_placement=placement))


@pytest.mark.parametrize('display', ['full', 'compact'])
def test_fresh_without_setup_seeds_only_true_absent_file_branch(qapp, display):
    branch = config_nodes()
    # The corrupt-file branch must retain legacy defaults; only the final else seeds.
    assert not any('seed_fresh' in ast.unparse(n) for n in branch.body)
    nodes = branch.orelse[:2]
    assert ast.unparse(nodes[0]) == 'self.config = default_config'
    defaults = {'listening_indicator_position': 'bottom-center', 'ui': {}}
    app = SimpleNamespace(config={})
    execute(nodes, app, defaults)
    assert placement.current_position(app.config, display) == 'bottom-left'


@pytest.mark.parametrize('saved', [True, False])
@pytest.mark.parametrize('display', ['full', 'compact'])
def test_existing_profile_keeps_saved_or_center(windows, tmp_path, display, saved):
    config = {'first_run_complete': True, 'ui': {'listening_display': display}}
    if saved:
        config.update(placement.placement_updates(config, 'top-right'))
    before = deepcopy(config)
    path = tmp_path/'existing.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    win = wizard._WizardWindow(path)
    windows.append(win)
    assert placement.current_position(win._config, display) == ('top-right' if saved else 'bottom-center')
    assert json.loads(path.read_text()) == before
    # Also exercise the runtime's existing-key fill; no seed runs in that branch.
    branch = config_nodes()
    outer = ast.parse((ROOT/'dictation.py').read_text(encoding='utf-8'))
    fill = next(n for n in ast.walk(outer) if isinstance(n, ast.For) and ast.unparse(n.iter) == 'default_config')
    app = SimpleNamespace(config=deepcopy(config))
    execute([fill], app, {'listening_indicator_position': 'bottom-center', 'ui': {}})
    assert placement.current_position(app.config, display) == ('top-right' if saved else 'bottom-center')


@pytest.mark.parametrize('display', ['full', 'compact'])
def test_picker_saves_monitor_and_preset_without_losing_other_records(windows, tmp_path, display):
    win = wizard._WizardWindow(tmp_path/'new.json')
    windows.append(win)
    win._listening_display_page.group.button(('compact', 'full').index(display)).setChecked(True)
    win._config['ui']['live_surface']['placement']['monitors']['unplugged'] = {'edge': 'left', 't': .3}
    win._display_placement_page.picker.choose('top-right')
    assert placement.current_position(win._config, display) == 'top-right'
    raw = win._config['ui']['live_surface']['placement']
    assert raw['monitors'][raw['preferred_monitor']] == {'edge': 'top', 't': 1.0, 'free_cx': .5, 'free_cy': .5}
    assert raw['monitors']['unplugged'] == {'edge': 'left', 't': .3}


def test_tutorial_uses_settings_apply_and_full_controller(windows):
    app = SimpleNamespace(config={'ui': {'listening_display': 'full'}}, live_surface=SimpleNamespace(_place_widget=Mock()))
    app.update_config_and_save = Mock(side_effect=app.config.update)
    app._schedule_ui = lambda callback: callback()
    app.apply_listening_indicator_settings = Mock(side_effect=lambda: placement.apply_full(app))
    win = tutorial_qt.TutorialWindow(app)
    windows.append(win)
    win._display_page.picker.choose('top-right')
    app.update_config_and_save.assert_called_once()
    app.apply_listening_indicator_settings.assert_called_once_with()
    app.live_surface._place_widget.assert_called_once_with()
    assert placement.current_position(app.config) == 'top-right'
    tree = ast.parse((ROOT/'dictation.py').read_text(encoding='utf-8'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'apply_listening_indicator_settings')
    assert ast.unparse(method.body[1]) == 'display_placement.apply_full(self)'


@pytest.mark.parametrize('kind', ['setup', 'tutorial'])
def test_leaving_page_and_closing_stops_demo(windows, qapp, tmp_path, kind):
    win = wizard._WizardWindow(tmp_path/'new.json') if kind == 'setup' else tutorial_qt.TutorialWindow(SimpleNamespace(config={}))
    windows.append(win)
    if kind == 'setup':
        index = next(i for i, step in enumerate(wizard._STEPS) if step[0] == 'Position')
        page = win._display_placement_page
    else:
        index = next(i for i, step in enumerate(win._steps) if step[0] == 'display')
        page = win._display_page
    win._step = index
    win.show()
    win._show_step()
    qapp.processEvents()
    if page.picker.timer is not None:
        assert page.picker.timer.isActive()
    win._step = index + 1
    win._show_step()
    assert page.picker.timer is None or not page.picker.timer.isActive()
    win._step = index
    win._show_step()
    win.close()
    assert page.picker.timer is None or not page.picker.timer.isActive()


@pytest.mark.parametrize('with_try', [False, True])
@pytest.mark.parametrize('palette', ['dark', 'light'])
def test_tutorial_page_fits_default_and_order(windows, qapp, monkeypatch, with_try, palette):
    theme.set_theme(palette, refresh=False)
    original = tutorial_qt.TutorialWindow._build_step_list
    def steps(self):
        original(self)
        self._steps = [step for step in self._steps if step[0] != 'try']
        if with_try:
            self._steps.insert(-1, ('try', 'Try'))
    monkeypatch.setattr(tutorial_qt.TutorialWindow, '_build_step_list', steps)
    builder = tutorial_qt.TutorialWindow._build_page
    from PySide6.QtWidgets import QWidget
    monkeypatch.setattr(tutorial_qt.TutorialWindow, '_build_page', lambda self, key: QWidget() if key == 'try' else builder(self, key))
    win = tutorial_qt.TutorialWindow(SimpleNamespace(config={}))
    windows.append(win)
    keys = [key for key, _ in win._steps]
    assert keys[keys.index('ways')+1] == 'display'
    assert keys.index('display') < keys.index('done')
    win._step = keys.index('display')
    win.show()
    win._show_step()
    qapp.processEvents()
    assert win.height() <= win._default_window_height
    assert win._display_page.minimumSizeHint().height() <= win._display_page.height()
    for beat in range(5):
        win._display_page.picker.set_frame(beat)
        assert win._display_page.picker.desktop.work_rect().contains(win._display_page.picker.desktop.indicator_rect())


@pytest.mark.parametrize('reduced', [False, True])
def test_setup_page_fits_default(windows, qapp, tmp_path, monkeypatch, reduced):
    from samsara.ui.widgets import indicator_demo
    monkeypatch.setattr(indicator_demo, 'system_reduced_motion', lambda: reduced)
    win = wizard._WizardWindow(tmp_path/'new.json')
    windows.append(win)
    default = win.height()
    win._step = next(i for i, step in enumerate(wizard._STEPS) if step[0] == 'Position')
    win.show()
    win._show_step()
    qapp.processEvents()
    assert win.height() == default
    page = win._pages[win._step]
    assert page.widget().minimumSizeHint().height() <= page.viewport().height()
    assert page.verticalScrollBar().maximum() == 0
    assert win._display_placement_page.picker.focusProxy() is win._display_placement_page.picker.targets['bottom-left']
    controls = list(win._display_placement_page.picker.targets.values())
    assert all(not a.geometry().intersects(b.geometry()) for i, a in enumerate(controls) for b in controls[i+1:])


@pytest.mark.parametrize('display', ['full', 'compact'])
def test_actual_settings_seam_moves_selected_display(qapp, display):
    from types import MethodType
    from samsara import listening_display
    tree = ast.parse((ROOT/'dictation.py').read_text(encoding='utf-8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'apply_listening_indicator_settings')
    namespace = dict(display_placement=placement, listening_display=listening_display)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<settings apply seam>', 'exec'), namespace)
    indicator = SimpleNamespace(set_position=Mock(), show=Mock(), hide=Mock())
    app = SimpleNamespace(config={'ui': {'listening_display': display, 'live_surface': {'show_idle_mark': True}}},
                          listening_indicator=indicator)
    if display == 'full':
        app.live_surface = SimpleNamespace(_place_widget=Mock())
    app._schedule_ui = lambda callback: callback()
    app.update_config_and_save = app.config.update
    app.apply_listening_indicator_settings = MethodType(namespace[node.name], app)
    placement.choose_for_app(app, 'top-right')
    indicator.set_position.assert_called_once_with('top-right')
    if display == 'full':
        app.live_surface._place_widget.assert_called_once_with()
    assert placement.current_position(app.config) == 'top-right'


def test_seed_before_qt_does_not_resolve_windows_device_id(monkeypatch):
    def unavailable():
        raise RuntimeError("No interactive monitor")
    monkeypatch.setattr(placement, "primary_monitor", unavailable)
    config = placement.seed_fresh({}, fresh=True)
    assert placement.current_position(config, "full") == "bottom-left"
