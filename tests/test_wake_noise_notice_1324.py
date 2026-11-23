"""Wake noise and the idle duck, without starting the app or audio hardware."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.config_migrations import migrate_loaded_config
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.config_seed import build_default_config
from samsara.mic_silence_watch import SilenceWatch
from samsara.wake_noise_notice import (
    HIGH_NOISE_RMS, MESSAGE, REMINDER_SECONDS, TITLE, WINDOW_FRAMES,
    WakeNoiseNotice, noise_threshold,
)
from tests.test_mic_silence_watch_1252 import Engine, write


@pytest.mark.parametrize('legacy, expected', [({}, 1.), ({'hands_free_idle_level': .8}, 1.),
                                            ({'hands_free_idle_level': .5}, .5)])
def test_startup_migrates_idle_level_once(legacy, expected):
    app = SimpleNamespace(config={'ducking': {'hands_free_level': .25, **legacy}},
                          save_config=Mock())
    for name in ('_migrate_wake_word_config', '_migrate_command_matching_enabled_flag',
                 '_migrate_ai_command_mode_config', '_migrate_retired_ava_command_session_config',
                 '_migrate_recording_media_mode'):
        setattr(app, name, Mock())
    migrate_loaded_config(app, {})
    assert app.config['ducking'] == {'hands_free_level': .25, 'hands_free_idle_level': expected}
    app.save_config.assert_called()
    app.save_config.reset_mock()
    migrate_loaded_config(app, {})
    app.save_config.assert_not_called()


def test_fresh_default_leaves_mixer_alone(tmp_path):
    config = build_default_config(home=tmp_path, macro_defaults={}, notification_defaults={})
    schema = SETTINGS_SCHEMA['ducking.hands_free_idle_level']
    assert config['ducking']['hands_free_idle_level'] == schema['default'] == 1.
    assert schema['settings_ui'] is False

    # Execute the production entry point without importing the app bootstrap.
    path = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    app_class = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == 'DictationApp')
    start = next(node for node in app_class.body
                 if isinstance(node, ast.FunctionDef) and node.name == '_start_hands_free_idle_duck')
    mixer = Mock()
    namespaces = {'audio_ducking': mixer}
    exec(compile(ast.Module(body=[start], type_ignores=[]), str(path), 'exec'), namespaces)
    app = SimpleNamespace(config=config, _hands_free_idle_ducker=None)
    namespaces[start.name](app)
    assert not mixer.mock_calls
    assert app._hands_free_idle_ducker is None


def noise_app(preference='all'):
    return SimpleNamespace(config={'notifications_level': preference}, wake_word_active=True,
                           tray_icon=SimpleNamespace(notify_info=Mock()))


def read_window(notice, stamp, level, *, seq=0):
    if notice.begin_poll(stamp, active=notice.app.wake_word_active):
        for offset in range(WINDOW_FRAMES):
            rms = level if isinstance(level, float) else level[offset]
            notice.observe(rms, stamp + offset / 10, seq + offset, 'same mic', skip=False)


@pytest.mark.parametrize('preference', ['off', 'important', 'all'])
def test_only_sustained_noise_at_enabled_notification_level(preference):
    app = noise_app(preference)
    notice = WakeNoiseNotice(app)
    read_window(notice, 5., [.001] * 5 + [.06] * 10)
    app.tray_icon.notify_info.assert_not_called()
    read_window(notice, 7. + REMINDER_SECONDS, .06)
    if preference == 'all':
        app.tray_icon.notify_info.assert_called_once_with(TITLE, MESSAGE)
    else:
        app.tray_icon.notify_info.assert_not_called()


def test_notice_throttle_survives_toggle_and_rechecks_each_hour():
    app = noise_app()
    notice = WakeNoiseNotice(app)
    read_window(notice, 5., .06)
    app.wake_word_active = False
    read_window(notice, 20., .06)
    app.wake_word_active = True
    read_window(notice, 25., .06)
    read_window(notice, 6.3 + REMINDER_SECONDS, .06)
    app.tray_icon.notify_info.assert_called_once_with(TITLE, MESSAGE)
    read_window(notice, 6.4 + REMINDER_SECONDS, .06)
    assert app.tray_icon.notify_info.call_count == 2
    read_window(notice, 8. + REMINDER_SECONDS * 2, .001)
    assert app.tray_icon.notify_info.call_count == 2


def test_threshold_uses_calibrated_floor_and_speech_threshold():
    audio = {'measured_noise_floor': .012, 'speech_threshold': .03, 'calibrated_microphone': 2}
    config = {'wake_word_config': {'audio': audio}, 'microphone': 2}
    assert noise_threshold(config) == pytest.approx(.024)
    audio['speech_threshold'] = .020
    assert noise_threshold(config) == pytest.approx(.020)
    config['microphone'] = 3
    assert noise_threshold(config) == pytest.approx(HIGH_NOISE_RMS)


def test_real_mic_health_reader_detects_enable_without_a_second_reader(monkeypatch):
    app = noise_app()
    app.wake_word_active = False
    engine = app._ace_engine = Engine()
    clock = SimpleNamespace(now=0.)
    monkeypatch.setattr('samsara.mic_silence_watch.time.perf_counter', lambda: clock.now)
    watch = SilenceWatch(app)
    watch.poll()

    def feed(start, count, *, epoch=0):
        for offset in range(count):
            clock.now = (start + offset) / 10
            write(engine, clock.now, value=np.int16(2000), epoch=epoch)
            if offset % 5 == 4 or offset == count - 1:
                watch.poll()

    feed(1, 20)
    app.tray_icon.notify_info.assert_not_called()
    app.wake_word_active = True
    watch.poll()
    feed(21, 10)
    feed(31, 10, epoch=1)  # Reset the window when the input stream changes.
    app.tray_icon.notify_info.assert_not_called()
    feed(41, 5, epoch=1)
    app.tray_icon.notify_info.assert_called_once_with(TITLE, MESSAGE)
    assert len(engine.registered) == 1
