"""Runtime theme and Cloud AI status regressions for Settings (queue 162)."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from samsara.ui import theme


@pytest.fixture
def dark_again():
    yield
    theme.set_theme("dark", refresh=False)


def _dominant_pixel(widget) -> int:
    image = widget.grab().toImage()
    counts = Counter(
        image.pixel(x, y)
        for y in range(0, image.height(), 4)
        for x in range(0, image.width(), 4)
    )
    return counts.most_common(1)[0][0]


def _luminance(pixel: int) -> float:
    return (0.2126 * ((pixel >> 16) & 0xFF)
            + 0.7152 * ((pixel >> 8) & 0xFF)
            + 0.0722 * (pixel & 0xFF))


def _max_rendered_contrast(image, rect, background: int) -> float:
    """Highest contrast pixel in an item rect, including antialiased glyphs."""
    background_luminance = _luminance(background)
    ratios = []
    for y in range(max(0, rect.top()), min(image.height(), rect.bottom() + 1)):
        for x in range(max(0, rect.left()), min(image.width(), rect.right() + 1)):
            foreground_luminance = _luminance(image.pixel(x, y))
            ratios.append(
                (max(foreground_luminance, background_luminance) + 0.05)
                / (min(foreground_luminance, background_luminance) + 0.05)
            )
    return max(ratios, default=0.0)


def _clear_headless_windows(qapp) -> None:
    """Remove test-only windows constructed by earlier targeted modules."""
    for widget in qapp.topLevelWidgets():
        widget.hide()
        widget.deleteLater()
    qapp.processEvents()


def test_apply_theme_setting_repaints_the_open_settings_window(qapp, dark_again):
    """Apply & Close reaches a live window through theme.set_theme(refresh=True)."""
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    _clear_headless_windows(qapp)
    theme.set_theme("dark", refresh=False)
    app = _StubApp()
    window = _SettingsWindow(app)
    window.resize(920, 700)
    window.show()
    qapp.processEvents()
    dark_pixel = _dominant_pixel(window)
    dark_rail_pixel = _dominant_pixel(window._sidebar)

    window._widgets['theme_combo'].setCurrentIndex(1)  # Light
    window._apply_and_close()
    window.show()  # _apply_and_close hides rather than destroys Settings.
    qapp.processEvents()
    light_pixel = _dominant_pixel(window)
    light_rail_pixel = _dominant_pixel(window._sidebar)

    assert app.config['ui']['theme'] == 'light'
    assert theme.active_theme() == 'light'
    assert _luminance(light_pixel) > _luminance(dark_pixel) + 60
    assert _luminance(light_rail_pixel) > _luminance(dark_rail_pixel) + 60
    window.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("palette", ("dark", "light"))
def test_navigation_group_headers_have_rendered_text_contrast(qapp, dark_again, palette):
    """The rail is a rendered surface, not just a token-presence assertion."""
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp

    theme.set_theme(palette, refresh=False)
    window = _SettingsWindow(_StubApp())
    window.resize(920, 700)
    window.show()
    qapp.processEvents()

    sidebar = window._sidebar
    viewport = sidebar.viewport()
    image = viewport.grab().toImage()
    for row in range(sidebar.count()):
        if row in window._sidebar_row_to_stack_index:
            continue
        rect = sidebar.visualItemRect(sidebar.item(row))
        background = image.pixel(4, max(0, rect.center().y()))
        assert _max_rendered_contrast(image, rect, background) >= 4.5, (
            f"{sidebar.item(row).text()} is not readable in {palette}"
        )
    window.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("cloud, reachable, expected", [
    ({}, None, "Cloud AI not configured — no API key"),
    ({"api_key": "test-key", "enabled": False}, None,
     "Cloud AI configured — disabled"),
    ({"api_key": "test-key", "enabled": True}, True,
     "Cloud AI enabled — reachable"),
    ({"api_key": "test-key", "enabled": True}, False,
     "Cloud AI enabled — unreachable"),
])
def test_cloud_ai_status_distinguishes_all_configuration_states(
        cloud, reachable, expected):
    from samsara.ui.settings_qt import _cloud_ai_state

    state, message = _cloud_ai_state(
        SimpleNamespace(config={"cloud_llm": cloud}), reachable=reachable
    )

    assert state
    assert message == expected
    assert "test-key" not in message


def test_system_theme_follows_windows_change_while_running(
        monkeypatch, qapp, dark_again):
    choices = iter(("light", "dark"))
    monkeypatch.setattr(theme, "detect_os_theme", lambda: next(choices))

    theme.set_theme("system", refresh=True)
    assert theme.active_theme() == "light"
    assert theme._SYSTEM_THEME_TIMER is not None
    assert theme._SYSTEM_THEME_TIMER.isActive()

    theme._follow_system_theme()
    assert theme.active_theme() == "dark"
