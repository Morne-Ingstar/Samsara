"""Cube geometry uses Qt logical pixels, fake screens and an isolated Qt app."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QWidget

from plugins.commands import window_cube as commands
from samsara.ui import window_cube_qt as cube


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self):
        for slot in self.slots:
            slot()


class Screen:
    def __init__(self, name, rect, scale=1.0):
        self._name, self.rect, self.scale = name, rect, scale
        self.availableGeometryChanged, self.geometryChanged = _Signal(), _Signal()

    def name(self):
        return self._name

    def availableGeometry(self):
        return self.rect

    def devicePixelRatio(self):
        return self.scale


def desktop(width=3840, height=2160, scale=1.0, origin=0):
    """Physical display dimensions converted once, as QScreen already does."""
    w, h, taskbar = round(width / scale), round(height / scale), round(48 / scale)
    return [Screen("main", QRect(origin, 0, w, h - taskbar), scale),
            Screen("right", QRect(origin + w, 0, w, h - taskbar), scale)]


def layout(screens):
    return [(screen.name(), screen.availableGeometry()) for screen in screens]


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def fake_desktop(monkeypatch, qapp):
    state = SimpleNamespace(screens=desktop(), cursor=0)
    monkeypatch.setattr(QApplication, "screens", lambda: state.screens)
    monkeypatch.setattr(QApplication, "screenAt", lambda _: state.screens[state.cursor])
    monkeypatch.setattr(QApplication, "primaryScreen", lambda: state.screens[0])
    monkeypatch.setattr(cube, "QCursor", SimpleNamespace(pos=lambda: QPoint(100, 100)))
    monkeypatch.setattr(cube, "apply_toolwindow_exstyle", lambda *a, **kw: True)
    monkeypatch.setattr(cube.qt_runtime, "post", lambda fn: fn())
    monkeypatch.setattr(cube.qt_runtime, "ensure_started", lambda: None)
    return state


@pytest.fixture
def window(fake_desktop):
    widget = cube._CubeWindow(None, 1.0, None)
    yield widget
    widget.hide()
    widget.deleteLater()


@pytest.mark.parametrize("old_scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("new_scale", [1.0, 1.5, 2.0])
def test_4k_anchor_stays_on_same_monitor_after_1080p_change(old_scale, new_scale):
    old = desktop(scale=old_scale)
    name, rect = cube.resolve_position(None, layout(old), "main", (260, 420))
    saved = cube.remember_position(name, old[0].rect, rect)
    new = desktop(1920, 1080, new_scale)
    name, placed = cube.resolve_position(saved, layout(new), "right", (260, 420))
    assert name == "main", "saved screen identity must beat the neighbouring monitor"
    assert new[0].rect.contains(placed)
    assert new[0].rect.bottom() - placed.bottom() == round(
        saved["offset"][1] * new[0].rect.height())


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_legacy_4k_position_below_1080p_taskbar_falls_back_to_cursor(scale):
    screens = desktop(1920, 1080, scale)
    # The old x now points to the next monitor; its y is below both work areas.
    saved = [round(3840 / scale) - 284, round(2160 / scale) - 444]
    name, placed = cube.resolve_position(saved, layout(screens), "main", (260, 420))
    assert name == "main"
    assert screens[0].rect.contains(placed)


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("resolution", [(3840, 2160), (1920, 1080)])
def test_default_follows_cursor_and_avoids_taskbar(scale, resolution):
    screens = desktop(*resolution, scale=scale)
    name, rect = cube.resolve_position(None, layout(screens), "right", (260, 420))
    assert name == "right"
    assert screens[1].rect.contains(rect)
    assert screens[1].rect.right() - rect.right() == 24
    assert screens[1].rect.bottom() - rect.bottom() == 24


@pytest.mark.parametrize("anchor", ["top-left", "top-right", "bottom-left", "bottom-right"])
def test_saved_corner_survives_negative_origin_and_work_area_changes(anchor):
    available = QRect(-1920, -200, 1880, 1000)
    saved = {"screen": "left", "anchor": anchor, "offset": [0.1, 0.1]}
    name, rect = cube.resolve_position(saved, [("left", available)], "left", (260, 420))
    assert name == "left" and available.contains(rect)
    assert cube.remember_position(name, available, rect) == saved


@pytest.mark.parametrize("position", [
    None, [], ["bad", 4], [True, 4], [float("nan"), 4], [float("inf"), 0],
    {"screen": "missing", "anchor": "bottom-right", "offset": [0.1, 0.1]},
    {"screen": "main", "anchor": "bad", "offset": [0, 0]},
    {"screen": "main", "anchor": "bottom-right", "offset": [float("nan"), 0]},
    {"screen": "main", "anchor": "bottom-right", "offset": [0.99, 0.99]},
    {"screen": "main", "anchor": "bottom-right", "offset": [-1, 0]},
])
def test_invalid_or_unfitting_saved_position_uses_default(position):
    screens = desktop(1920, 1080)
    actual = cube.resolve_position(position, layout(screens), "right", (260, 420))
    assert actual == cube.resolve_position(None, layout(screens), "right", (260, 420))


def test_valid_legacy_position_on_either_screen_is_honoured():
    screens = desktop(1920, 1080)
    assert cube.resolve_position([2100, 100], layout(screens), "main", (260, 420)) == (
        "right", QRect(2100, 100, 260, 420))


def test_legacy_straddling_monitors_or_taskbar_is_rejected():
    screens = desktop(1920, 1080)
    for position in ([1800, 100], [100, 900], [-500, 0]):
        assert cube.resolve_position(position, layout(screens), "main", (260, 420)) == (
            cube.resolve_position(None, layout(screens), "main", (260, 420)))


def test_no_screens_is_safe():
    assert cube.resolve_position(None, [], None, (260, 420)) is None


@pytest.mark.parametrize("available", [QRect(48, 0, 1872, 1080),
                                       QRect(0, 48, 1920, 1032),
                                       QRect(-1000, -500, 200, 200)])
def test_all_edges_clamp_even_when_panel_is_larger_than_work_area(available):
    _, rect = cube.resolve_position(None, [("main", available)], "main", (260, 420))
    assert available.contains(rect)


def rows(count):
    return [cube.CubeRow(i + 1, "Browser", f"Window {i + 1}") for i in range(count)]


@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_real_layout_keeps_bottom_corner_as_rows_grow_and_shrink(window, fake_desktop, scale):
    fake_desktop.screens = desktop(scale=scale)
    available = fake_desktop.screens[0].rect
    for count in (1, 9, 3, 0):
        window._on_render(rows(count))
        assert available.contains(window.geometry())
        assert available.right() - window.geometry().right() == 24
        assert available.bottom() - window.geometry().bottom() == 24


def test_short_desktop_scrolls_all_rows_above_taskbar(window, fake_desktop, qapp):
    fake_desktop.screens = desktop(1920, 1080, scale=2.0)
    # max_rows is configurable; force real overflow rather than assuming the
    # default nine rows exceed this desktop's logical height on every font.
    window._on_render(rows(18))
    window.show()
    qapp.processEvents()
    assert fake_desktop.screens[0].rect.contains(window.geometry())
    assert len(window.findChildren(cube._Row)) == 18
    scrollbar = window._scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum())
    last = window.findChildren(cube._Row)[-1]
    assert window._scroll.viewport().rect().contains(
        last.mapTo(window._scroll.viewport(), last.rect().bottomLeft()))


def test_visible_cube_reacts_to_resolution_change_and_monitor_removal(window, fake_desktop, qapp):
    fake_desktop.cursor = 1
    window._on_render(rows(9))
    window.show()
    qapp.processEvents()
    second = fake_desktop.screens[1]
    second.rect = QRect(1920, 0, 1920, 1032)
    second.availableGeometryChanged.emit()
    assert second.rect.contains(window.geometry())
    fake_desktop.screens = [Screen("other", QRect(-1920, 0, 1920, 1032)),
                            Screen("cursor", QRect(0, 0, 1920, 1032))]
    window._screen_geometry_changed()
    assert fake_desktop.screens[1].rect.contains(window.geometry())


@pytest.mark.parametrize("display_class", ["ListeningIndicator", "LiveSurfaceWidget"])
def test_visible_compact_and_full_displays_choose_opposite_corner(window, fake_desktop, display_class):
    # Same Qt top-level geometry seam as the real two passive listening displays.
    display = type(display_class, (QWidget,), {})()
    available = fake_desktop.screens[0].rect
    display.setGeometry(available.right() - 500, available.bottom() - 200, 480, 180)
    display.show()
    try:
        window._on_render(rows(9))
        assert window.x() == available.x() + 24
        assert not window.geometry().intersects(display.geometry())
        display.hide()
        window._on_render(rows(9))
        assert available.right() - window.geometry().right() == 24
    finally:
        display.hide()
        display.deleteLater()


def test_refresh_keeps_selected_monitor_when_cursor_moves(window, fake_desktop):
    window._on_render(rows(1))
    fake_desktop.cursor = 1
    window._on_render(rows(9))
    assert fake_desktop.screens[0].rect.contains(window.geometry())


def test_reused_panel_accepts_new_position_and_explicit_reset(fake_desktop):
    panel = cube.CubePanel()
    panel.configure(position=[100, 100])
    panel.show(rows(1))
    try:
        assert panel._window.pos() == QPoint(100, 100)
        panel.hide()
        panel.configure(position={"screen": "right", "anchor": "bottom-right", "offset": [0, 0]})
        panel.show(rows(9))
        assert fake_desktop.screens[1].rect.contains(panel._window.geometry())
        panel.hide()
        panel.configure(position=None)
        panel.show(rows(3))
        assert fake_desktop.screens[0].rect.contains(panel._window.geometry())
    finally:
        panel.hide()
        panel._window.deleteLater()


def test_command_persists_anchor_without_duplicate_writes_or_losing_other_config(monkeypatch):
    saved = {"screen": "main", "anchor": "bottom-right", "offset": [0.01, 0.02]}
    monkeypatch.setattr(cube, "get_panel", lambda: SimpleNamespace(current_position=lambda: saved))
    app = SimpleNamespace(config={"window_cube": {"position": [5000, 2000], "opacity": 0.7}})
    writes = []
    def save(update):
        writes.append(update)
        app.config.update(update)
    app.update_config_and_save = save
    commands._remember_position(app)
    commands._remember_position(app)
    assert writes == [{"window_cube": {"position": saved, "opacity": 0.7}}]


def test_position_snapshot_can_be_read_without_cross_thread_qwidget_access(fake_desktop):
    panel = cube.CubePanel()
    snapshot = {"screen": "main", "anchor": "bottom-right", "offset": [0.1, 0.2]}
    panel._window = SimpleNamespace(_position_snapshot=snapshot)
    actual = panel.current_position()
    actual["offset"][0] = 0.9
    assert snapshot["offset"][0] == 0.1


def test_existing_panel_show_is_posted_before_any_qt_mutation(monkeypatch):
    panel, posted = cube.CubePanel(), []
    panel._window = object()  # no QWidget methods available on this caller thread
    monkeypatch.setattr(cube.qt_runtime, "post", posted.append)
    panel.show(rows(1))
    assert posted == [panel._show_rows]
