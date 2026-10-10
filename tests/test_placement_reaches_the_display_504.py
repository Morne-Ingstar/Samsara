"""Placement producers must reach the real controller across monitor identities."""
import ast
from copy import deepcopy
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication

from samsara import listening_display
from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.placement import INSET_DIP
from samsara.onboarding import display_placement as placement
from samsara.ui import first_run_wizard_qt as wizard
from samsara.ui.live_surface_qt import LiveSurfaceWidget
from samsara.ui.tutorial_display_qt import DisplayPlacementPage
from samsara.ui.widgets.indicator_place import IndicatorPlacePicker


@pytest.fixture
def desktop(qapp, monkeypatch):
    screens = [SimpleNamespace(name=lambda: 'ASUS VG289Q1A',
                               availableGeometry=lambda: QRect(0, 0, 1200, 800)),
               SimpleNamespace(name=lambda: 'Second friendly screen',
                               availableGeometry=lambda: QRect(-1200, -100, 1200, 800))]
    monkeypatch.setattr(QGuiApplication, 'screens', lambda: screens)
    monkeypatch.setattr(QGuiApplication, 'primaryScreen', lambda: screens[0])
    monkeypatch.setattr(placement, 'primary_monitor', lambda: r'\\.\DISPLAY1')
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda *a, **k: None)
    return screens


def app_for(config):
    config.setdefault('ui', {}).setdefault('live_surface', {})['enabled'] = True
    app = SimpleNamespace(config=config, _schedule_ui=lambda callback: callback())
    app.update_config_and_save = config.update
    app.live_surface = LiveSurfaceController(app)
    app.live_surface.widget = LiveSurfaceWidget(app.live_surface, reduced_motion=True)
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == 'apply_listening_indicator_settings')
    namespace = dict(display_placement=placement, listening_display=listening_display)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<settings seam>', 'exec'), namespace)
    app.apply_listening_indicator_settings = MethodType(namespace[node.name], app)
    return app


def assert_corner(controller, screen, *, top_right=False):
    controller._place_widget()
    widget = controller.widget
    area = screen.availableGeometry()
    if top_right:
        assert widget.x() + widget.width() == area.x() + area.width() - INSET_DIP
        assert widget.y() == area.y() + INSET_DIP
    else:
        assert widget.x() == area.x() + INSET_DIP
        assert widget.y() + widget.height() == area.y() + area.height() - INSET_DIP


def dispose(widget):
    widget.hide()
    widget.deleteLater()


def test_setup_default_and_click_reach_friendly_primary(desktop, tmp_path):
    window = wizard._WizardWindow(tmp_path / 'absent.json')
    app = app_for(window._config)
    try:
        assert_corner(app.live_surface, desktop[0])
        window._display_placement_page.picker.choose('top-right')
        assert_corner(app.live_surface, desktop[0], top_right=True)
        assert app.config['ui']['live_surface']['placement']['preferred_monitor'] == desktop[0].name()
    finally:
        dispose(app.live_surface.widget)
        dispose(window)


@pytest.mark.parametrize('secondary', [False, True])
def test_tutorial_click_moves_full_on_its_selected_screen(desktop, secondary):
    screen = desktop[int(secondary)]
    config = {'ui': {'listening_display': 'full', 'live_surface': {'enabled': True,
        'placement': {'preferred_monitor': screen.name(), 'monitors': {
            screen.name(): {'edge': 'bottom', 't': .5},
            'unplugged': {'edge': 'left', 't': .2}}}}}}
    app = app_for(config)
    page = DisplayPlacementPage(config, app=app)
    try:
        page.picker.choose('top-right')
        assert_corner(app.live_surface, screen, top_right=True)
        page.picker.choose('bottom-left')
        assert_corner(app.live_surface, screen)
        assert config['ui']['live_surface']['placement']['monitors']['unplugged'] == {'edge': 'left', 't': .2}
    finally:
        dispose(page)
        dispose(app.live_surface.widget)


def test_no_setup_seed_is_adopted_once_by_real_controller(desktop, monkeypatch):
    config = {'ui': {'listening_display': 'full', 'live_surface': {'enabled': True}}}
    placement.seed_fresh(config, fresh=True)
    app = app_for(config)
    try:
        assert_corner(app.live_surface, desktop[0])
        raw = config['ui']['live_surface']['placement']
        assert raw['preferred_monitor'] == desktop[0].name()
        assert 'seed' not in raw
        before = deepcopy(config)
        app.live_surface._place_widget()
        assert config == before
    finally:
        dispose(app.live_surface.widget)


def test_seed_never_queries_platform_monitor(monkeypatch):
    def unavailable():
        raise RuntimeError('no interactive monitor')
    monkeypatch.setattr(placement, 'primary_monitor', unavailable)
    config = placement.seed_fresh({}, fresh=True)
    assert placement.current_position(config, 'full') == 'bottom-left'


@pytest.mark.parametrize('display', ['full', 'compact'])
@pytest.mark.parametrize('saved', [False, True])
def test_existing_profile_is_never_seeded(desktop, display, saved):
    config = {'ui': {'listening_display': display}}
    if saved:
        config.update(placement.placement_updates(config, 'top-right', desktop[1].name()))
    before = deepcopy(config)
    placement.seed_fresh(config, fresh=False)
    assert config == before
    assert placement.current_position(config, display) == ('top-right' if saved else 'bottom-center')


def test_compact_drag_then_picker_uses_real_settings_seam(desktop):
    from samsara.ui.listening_indicator import ListeningIndicator
    config = {'listening_indicator_position': 'custom',
              'listening_indicator_custom_position': {'screen': desktop[0].name(), 'cx': .3, 'cy': .4},
              'ui': {'listening_display': 'compact'}}
    app = app_for(config)
    app.listening_indicator = ListeningIndicator()
    app.listening_indicator.set_custom_position(desktop[0].name(), .3, .4)
    page = DisplayPlacementPage(config, app=app)
    try:
        assert page.picker.value() == 'custom'
        assert not any(button.isChecked() for button in page.picker.targets.values())
        page.picker.choose('top-right')
        assert app.listening_indicator._corner == 'top-right'
        assert app.listening_indicator._custom_position is None
        assert placement.current_position(config, 'compact') == 'top-right'
    finally:
        dispose(page)
        dispose(app.listening_indicator)
        dispose(app.live_surface.widget)


def test_unexpected_picker_value_clears_selection(qapp):
    picker = IndicatorPlacePicker('bottom-left')
    try:
        for value in ('unexpected', None, {'malformed': True}):
            picker.set_value(value)
            assert not any(button.isChecked() for button in picker.targets.values())
            assert picker.focusProxy() is None
    finally:
        dispose(picker)
