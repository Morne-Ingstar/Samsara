"""461: safe hub placement and the accessible reset control, no live app."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication, QWidget

from samsara.ui import main_window_qt as hub
from samsara.ui.window_geometry import (
    contains, default_geometry, outer_rect, reset_geometry, restore_geometry,
)

PRIMARY = (0, 0, 1920, 1040)
SECONDARY = (1920, -200, 1600, 900)
DEFAULT = (hub.DEFAULT_WIDTH, hub.DEFAULT_HEIGHT)


@pytest.mark.parametrize('stored', [
    (-800, 0, 1920, 1040),  # partly off the left of every screen
    (0, 0, 3840, 1600),  # oversized but title reachable
    (4000, 100, 1100, 800),  # unplugged monitor
    (100, 100, 1800, 1200),  # monitor's logical area shrank after scaling
    (1600, 100, 900, 650),  # straddles monitors
    (200, 600, 900, 650),  # body behind taskbar, title fully visible
    (0, -10, 900, 650),  # part of title above screen
    (10, 10, 0, 650),
    None,
])
def test_invalid_saved_geometry_uses_primary_default(stored):
    assert restore_geometry(stored, [PRIMARY, SECONDARY], PRIMARY, DEFAULT) == (510, 195, 900, 650)


@pytest.mark.parametrize('screen', [PRIMARY, SECONDARY, (-1600, 0, 1600, 900)])
def test_valid_secondary_and_negative_coordinates_are_preserved(screen):
    rect = (screen[0] + 100, screen[1] + 100, 900, 650)
    assert restore_geometry(rect, [PRIMARY, screen], PRIMARY, DEFAULT) == rect


def test_native_title_and_frame_must_fit_too():
    margins = (8, 32, 8, 8)
    result = restore_geometry((0, 0, 900, 650), [PRIMARY], PRIMARY, DEFAULT, margins)
    assert result == default_geometry(PRIMARY, DEFAULT, margins)
    assert contains(PRIMARY, outer_rect(result, margins))


def test_small_available_area_caps_default_including_native_frame():
    screen = (-500, 40, 600, 420)
    margins = (8, 32, 8, 8)
    result = restore_geometry(None, [screen], screen, DEFAULT, margins)
    assert result == (-492, 72, 584, 380)
    assert outer_rect(result, margins) == screen


@pytest.mark.parametrize('current,expected_screen', [
    ((1700, 0, 1200, 650), SECONDARY),  # more area on secondary
    ((1600, 600, 900, 650), PRIMARY),  # area, not window-centre rule
    ((-10000, -10000, 900, 650), PRIMARY),
    ((1470, 0, 900, 650), PRIMARY),  # equal overlap prefers primary
])
def test_reset_chooses_screen_by_largest_area(current, expected_screen):
    assert reset_geometry(current, [SECONDARY, PRIMARY], PRIMARY, DEFAULT) == default_geometry(expected_screen, DEFAULT)


@pytest.fixture
def make_window(qapp, monkeypatch, tmp_path):
    # The suite home and this app's config/save sink are disposable. Home is
    # replaced to avoid its stores/QSettings; header/status are real widgets.
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    monkeypatch.setattr(hub._MainWindow, '_make_panel', lambda *args: QWidget())
    monkeypatch.setattr(hub._MainWindow, '_frame_margins', lambda self: (0, 0, 0, 0))
    screens = [SimpleNamespace(availableGeometry=lambda r=r: QRect(*r))
               for r in [PRIMARY, SECONDARY]]
    monkeypatch.setattr(QApplication, 'screens', lambda: screens)
    monkeypatch.setattr(QApplication, 'primaryScreen', lambda: screens[0])
    windows = []

    def make(**config):
        app = SimpleNamespace(config=dict(config), saved=[])

        def update(changes):
            app.saved.append(dict(changes))
            app.config.update(changes)

        app.update_config = update
        window = hub._MainWindow(app)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        windows.append(window)
        return window, app

    yield make
    for window in windows:
        window._geom_timer.stop()
        window._poll_timer.stop()
        window._header_mark.stop()
        window.hide()
        window.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize('config', [
    dict(window_x=-800, window_y=0, window_width=1920, window_height=1040),
    dict(window_x=0, window_y=0, window_width=3840, window_height=1600),
    dict(window_x=4000, window_y=100, window_width=1100, window_height=800),
    dict(window_x=100, window_y=100, window_width=1800, window_height=1200),
    dict(window_width=3840, window_height=1600),
    dict(window_x='invalid', window_y=0, window_width='invalid'),
])
def test_startup_uses_rule_with_fake_screen_rectangles(make_window, config):
    window, app = make_window(**config)
    assert window.geometry().getRect() == (510, 195, 900, 650)
    assert app.saved == []


def test_button_is_immediately_left_of_ava_and_keyboard_reachable(make_window):
    window, _ = make_window()
    button = window._reset_btn
    assert button.text() == 'Reset size'  # 824: the bare arrow glyph read as History
    assert button.icon().isNull()  # 824: text replaces the glyph
    assert button.accessibleName() == button.toolTip() == 'Reset window size'
    assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    header = window.findChild(QWidget, 'hubHeader').layout()
    assert header.indexOf(button) + 1 == header.indexOf(window._ava)
    assert 'underline' not in button.styleSheet()


def test_startup_on_small_screen_stays_inside_available_area(make_window, qapp, monkeypatch):
    screen = (0, 0, 600, 420)
    monkeypatch.setattr(hub._MainWindow, '_geometry_screens', lambda self: ([screen], screen))
    window, _ = make_window()
    window.show()
    qapp.processEvents()
    assert contains(screen, window.geometry().getRect())


@pytest.mark.parametrize('state,rect,target', [
    ('maximized', (2050, -100, 1000, 700), SECONDARY),
    ('oversized', (1700, -100, 1800, 800), SECONDARY),
    ('offscreen', (-10000, -10000, 1100, 800), PRIMARY),
])
def test_button_resets_window_and_saves_only_to_fake_app(make_window, qapp, state, rect, target):
    window, app = make_window()
    window.show()
    window.setGeometry(*rect)
    if state == 'maximized':
        window.showMaximized()
        assert window.isMaximized()
        # Offscreen's sole platform screen differs from our fake arrangement.
        # This frame seam models the maximized window on the chosen monitor.
        window.frameGeometry = lambda: QRect(*SECONDARY)
    window._reset_btn.click()
    qapp.processEvents()
    assert not window.isMaximized()
    assert window.geometry().getRect() == default_geometry(target, DEFAULT)
    expected = dict(zip(('window_x', 'window_y', 'window_width', 'window_height'),
                        window.geometry().getRect()))
    assert app.saved[-1] == expected
    assert app.config == expected


def test_maximised_size_is_not_saved_as_the_normal_size(make_window):
    window, app = make_window()
    normal = (200, 100, 900, 650)
    window.setGeometry(*normal)
    window.showMaximized()
    window._save_geometry()
    assert tuple(app.saved[-1][key] for key in ('window_x', 'window_y', 'window_width', 'window_height')) == normal


def test_widget_uses_native_margins_when_placing_default(make_window, monkeypatch):
    monkeypatch.setattr(hub._MainWindow, '_frame_margins', lambda self: (8, 32, 8, 8))
    window, _ = make_window()
    assert window.geometry().getRect() == default_geometry(PRIMARY, DEFAULT, (8, 32, 8, 8))
