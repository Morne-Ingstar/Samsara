"""Queue 194: every Tier-1 colour-only surface has a visible second cue."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara.ui import theme, tray_qt, voice_training_qt, wake_word_debug_qt


ARTIFACTS = Path(r"C:\Users\Morne\Documents\Claude\reports\194\artifacts")


def _voice_training_window():
    app = SimpleNamespace(
        config={
            "model_size": "base",
            "language": "en",
            "initial_prompt": "",
        }
    )
    training = SimpleNamespace(
        app=app,
        _monitoring=False,
        custom_vocab=[],
        corrections_dict={},
        _rebuild_corrections_pattern=lambda: None,
        save_training_data=lambda: True,
    )
    return voice_training_qt._TrainingWindow(training)


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


def test_voice_training_volume_zone_has_text_and_shape_cues(qapp):
    window = _voice_training_window()
    try:
        labels = []
        heights = []
        for level in (10, 50, 90):
            window._on_level(level)
            qapp.processEvents()
            labels.append(window._level_state_label.text())
            heights.append(window._level_bar.height())

        assert labels == ["Quiet", "Good", "Loud"]
        assert len(set(heights)) == 3
    finally:
        window.close()


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
    listening_capture, ava_capture = tray_qt.mark_capture()["listening"], tray_qt.mark_capture()["ava"]
    listening = tray_qt.render_mark("listening", "off", 32)
    ava = tray_qt.render_mark("ava", "off", 32)

    assert listening_capture[1] != ava_capture[1]
    assert _alpha_mask(listening) != _alpha_mask(ava)


def test_light_and_dark_surface_grabs_are_saved(qapp):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    for palette in ("light", "dark"):
        theme.set_theme(palette, refresh=False)

        voice = _voice_training_window()
        wake = _wake_debug_window()
        try:
            voice._on_level(90)
            wake._set_timer("0.4s", theme.ERROR)
            qapp.processEvents()
            _save_grab(voice, ARTIFACTS / f"voice_training_{palette}.png")
            _save_grab(wake, ARTIFACTS / f"wake_word_debug_{palette}.png")
            tray = tray_qt.render_mark("ava", "off", 64)
            assert tray.save(str(ARTIFACTS / f"tray_ava_{palette}.png"))
        finally:
            voice.close()
            wake.close()
