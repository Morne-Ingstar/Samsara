"""Queue 129: the `ui.theme` key actually reaches the pixels.

The contrast table proves the palettes are sound and tests/test_colour_tokens
proves nothing hard-codes a colour. Neither proves that flipping the setting
changes what the user sees -- a window whose stylesheet was built at import
time passes both and still renders dark. So this file renders real surfaces
in both palettes and asserts the pixels moved.
"""

from __future__ import annotations

import pytest

from samsara.ui import theme


@pytest.fixture
def dark_again():
    """Always hand the process back in the shipped palette, whatever fails."""
    yield
    theme.set_theme("dark", refresh=False)


# --- the config key -------------------------------------------------------------

def test_the_schema_offers_exactly_the_three_values():
    from samsara.config_schema import SETTINGS_SCHEMA

    entry = SETTINGS_SCHEMA["ui.theme"]
    assert entry["type"] == "enum"
    assert tuple(entry["options"]) == theme.THEME_CHOICES
    assert entry["default"] == "dark"
    assert entry["tab"] == "general"


def test_the_default_survives_the_defaults_table():
    from samsara import config_defaults

    assert config_defaults.DEFAULTS["ui.theme"] == "dark"


@pytest.mark.parametrize("setting, expected", [
    ("dark", "dark"),
    ("light", "light"),
    ("DARK", "dark"),
    (" Light ", "light"),
    ("", "dark"),
    (None, "dark"),
    ("solarized", "dark"),      # a hand-edited config must not cost a window
])
def test_resolve_theme_maps_a_setting_to_a_palette(setting, expected):
    assert theme.resolve_theme(setting) == expected


def test_system_resolves_to_the_os_setting(monkeypatch):
    monkeypatch.setattr(theme, "detect_os_theme", lambda: "light")
    assert theme.resolve_theme("system") == "light"
    monkeypatch.setattr(theme, "detect_os_theme", lambda: "dark")
    assert theme.resolve_theme("system") == "dark"


def test_system_reads_the_windows_app_theme_key(monkeypatch):
    """AppsUseLightTheme: 1 = light, 0 = dark, anything else = the default."""
    import winreg

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    for value, expected in ((1, "light"), (0, "dark")):
        monkeypatch.setattr(winreg, "OpenKey", lambda *a, **k: _Key())
        monkeypatch.setattr(winreg, "QueryValueEx",
                            lambda key, name, value=value: (value, 4))
        assert theme.detect_os_theme() == expected


def test_an_unreadable_os_setting_falls_back_rather_than_raising(monkeypatch):
    import winreg

    def boom(*a, **k):
        raise OSError("no such key")

    monkeypatch.setattr(winreg, "OpenKey", boom)
    assert theme.detect_os_theme() == theme.DEFAULT_THEME


def test_boot_applies_the_theme_before_the_first_window(monkeypatch, tmp_path):
    """apply_early_theme() reads the config itself -- the app object does not
    exist yet -- and must run before create_splash()."""
    import json

    from samsara import boot, paths

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"ui": {"theme": "light"}}), encoding="utf-8")
    monkeypatch.setattr(paths, "samsara_config_path", lambda: config)
    try:
        boot.apply_early_theme()
        assert theme.active_theme() == "light"
    finally:
        theme.set_theme("dark", refresh=False)


def test_boot_survives_a_missing_or_broken_config(monkeypatch, tmp_path):
    from samsara import boot, paths

    broken = tmp_path / "config.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(paths, "samsara_config_path", lambda: broken)
    boot.apply_early_theme()
    assert theme.active_theme() == "dark"


# --- the pixels -----------------------------------------------------------------

def _dominant_background(widget):
    """The most common pixel in a rendered widget -- its actual surface."""
    from collections import Counter

    image = widget.grab().toImage()
    counts = Counter()
    for y in range(0, image.height(), 4):
        for x in range(0, image.width(), 4):
            counts[image.pixel(x, y)] += 1
    return counts.most_common(1)[0][0]


def _luminance(pixel: int) -> float:
    r, g, b = (pixel >> 16) & 0xFF, (pixel >> 8) & 0xFF, pixel & 0xFF
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


#: Every independently styled surface selected as a switch gate. The three
#: queue-129 rewrites below used to be absent from both this test and the
#: shared proof table.
SURFACES = [
    "home", "settings_general", "quick_reference", "tutorial",
    "profile_manager", "voice_training", "ava_guide",
]


@pytest.mark.parametrize("surface", SURFACES)
def test_switching_the_setting_changes_what_the_surface_renders(
        surface, qapp, dark_again):
    """Build the same surface under each palette and compare the pixels.

    Each independently styled window is a separate chance to retain a frozen
    dark stylesheet, so each listed surface gets its own pixel assertion."""
    from tests._theme_surfaces import SURFACES as BUILDERS

    theme.set_theme("dark", refresh=False)
    dark_widget = BUILDERS[surface]()
    dark_widget.resize(900, 650)
    qapp.processEvents()
    dark_pixel = _dominant_background(dark_widget)

    theme.set_theme("light", refresh=False)
    light_widget = BUILDERS[surface]()
    light_widget.resize(900, 650)
    qapp.processEvents()
    light_pixel = _dominant_background(light_widget)

    assert dark_pixel != light_pixel, (
        f"{surface} renders the same surface in both palettes -- it is "
        "building its stylesheet once, at import")
    assert _luminance(light_pixel) > _luminance(dark_pixel) + 60, (
        f"{surface}: light surface luminance {_luminance(light_pixel):.0f} is "
        f"not clearly above dark's {_luminance(dark_pixel):.0f}")


def test_the_shared_stylesheet_switches_with_the_palette(dark_again):
    theme.set_theme("dark", refresh=False)
    dark = theme.build_stylesheet()
    theme.set_theme("light", refresh=False)
    light = theme.build_stylesheet()
    assert dark != light
    assert theme.PALETTES["dark"]["BG0"] not in light
    assert theme.PALETTES["light"]["BG0"] in light
    theme.set_theme("dark", refresh=False)
    assert theme.build_stylesheet() == dark, "a round trip must be exact"


def test_the_combo_arrow_is_rewritten_per_palette(dark_again):
    """Qt caches a QSS url() image by path, so the two themes' arrows cannot
    share a filename or the dark one keeps painting in the light app."""
    theme.set_theme("dark", refresh=False)
    dark_path = theme.ARROW_PATH
    theme.set_theme("light", refresh=False)
    assert theme.ARROW_PATH != dark_path
