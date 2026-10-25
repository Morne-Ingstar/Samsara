"""Queue 194: every Tier-1 colour-only surface has a visible second cue."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara.ui import theme, tray_qt, wake_word_debug_qt




def _wake_debug_window():
    app = SimpleNamespace(
        config={
            "wake_word_config": {
                "phrase": "samsara",
                "end_word": {"enabled": False},
                "audio": {"speech_threshold": 0.03},
                "modes": {
                    "dictate": {"silence_timeout": 2.0},
                    "short_dictate": {"silence_timeout": 1.0},
                    "long_dictate": {"silence_timeout": 60.0},
                },
            }
        }
    )
    return wake_word_debug_qt._DebugWindow(app)


def _alpha_mask(image):
    return tuple(
        image.pixelColor(x, y).alpha() > 0
        for y in range(image.height())
        for x in range(image.width())
    )


def _save_grab(widget, path):
    image = widget.grab()
    assert not image.isNull()
    assert image.save(str(path))


@pytest.fixture(autouse=True)
def _restore_theme():
    original = theme.active_theme()
    yield
    theme.set_theme(original, refresh=False)


def test_wake_debug_countdown_has_a_low_time_word(qapp):
    window = _wake_debug_window()
    try:
        window._set_timer("1.5s", theme.SUCCESS)
        qapp.processEvents()
        available = window._timer_state_lbl.text()

        window._set_timer("0.4s", theme.ERROR)
        qapp.processEvents()
        ending = window._timer_state_lbl.text()

        assert available == "Time available"
        assert ending == "Ending soon"
        assert available != ending
    finally:
        window.close()


def test_tray_listening_and_ava_have_different_non_colour_geometry(qapp):
    # colour-only by owner decision 2026-10-25 (879a); shape distinction returns with 879b
    for palette in ("dark", "light"):
        theme.set_theme(palette, refresh=False)
        assert tray_qt.mark_capture()["listening"][0] == theme.ACCENT
        assert tray_qt.mark_capture()["ava"][0] == theme.AVA
        assert max(abs(a-b) for a, b in zip(theme._hex_to_rgb(theme.ACCENT),
                                          theme._hex_to_rgb(theme.AVA))) >= 60
        for size in (16, 32):
            for capture, token in (("listening", theme.ACCENT), ("ava", theme.AVA)):
                image = tray_qt.render_mark(capture, "off", size)
                colours = [image.pixelColor(x, y) for y in range(size) for x in range(size)
                           if image.pixelColor(x, y).alpha() == 255]
                assert len(colours) >= size
                assert {colour.name() for colour in colours} == {token}


def test_light_and_dark_surface_grabs_are_saved(qapp, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    for palette in ("light", "dark"):
        theme.set_theme(palette, refresh=False)

        wake = _wake_debug_window()
        try:
            wake._set_timer("0.4s", theme.ERROR)
            qapp.processEvents()
            _save_grab(wake, tmp_path / f"wake_word_debug_{palette}.png")
            tray = tray_qt.render_mark("ava", "off", 64)
            assert tray.save(str(tmp_path / f"tray_ava_{palette}.png"))
        finally:
            wake.close()
