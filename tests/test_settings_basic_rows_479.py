"""Everyday choices remain reachable without opening Advanced (479)."""
from collections import Counter
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QMenu

from samsara.ui.settings import audio_page, dictation_page, general_page, shortcuts_page

MOVED = [
    (general_page, 'listening_display', 'ui.listening_display'),
    (general_page, 'launch_at_login', 'launch_at_login'),
    (audio_page, 'tts_engine', 'tts.engine'),
    (audio_page, 'tts_voice_combo', 'tts.voice_id'),
    (audio_page, 'tts_speed', 'tts.speed'),
    (audio_page, 'adv_ducking_recording_mode', 'ducking.recording_mode'),
    (dictation_page, 'wake_word_phrase', 'wake_word_config.phrase'),
    (dictation_page, 'model_combo', 'model_size'),
    (shortcuts_page, 'command_hotkey', 'command_hotkey'),
]


@pytest.mark.parametrize('page,widget,key', MOVED, ids=[item[1] for item in MOVED])
def test_everyday_row_is_basic_once(page, widget, key):
    assert sum(key in row[3].split(',') for row in page.BASE) == 1
    assert not any(key in row[3].split(',') for row in page.ADVANCED)
    assert any(widget in row[2].split(',') for row in page.BASE)


def test_natural_neighbours():
    def names(page):
        return [row[2].split(',')[0] for row in page.BASE]
    assert names(general_page)[:3] == ['listening_display', 'ui_scale_combo', 'theme_combo']
    audio = names(audio_page)
    start = audio.index('tts_feedback_level')
    assert audio[start:start + 4] == ['tts_feedback_level', 'tts_engine', 'tts_voice_combo', 'tts_speed']
    assert audio[audio.index('sound_theme_combo') + 1] == 'adv_ducking_recording_mode'
    dictation = names(dictation_page)
    assert dictation[dictation.index('wake_word_enabled') + 1] == 'wake_word_phrase'
    assert names(shortcuts_page)[:2] == ['hotkey', 'command_hotkey']


@pytest.fixture(scope='module')
def settings(qapp):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
        patch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
        config = _schema_defaults()
        config['wake_word_enabled'] = True
        config['tts']['enabled'] = True
        config['tts']['feedback_level'] = 'brief'
        window = _SettingsWindow(_StubApp(config))
        window.show()
        yield window
        window.hide()
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize('page,widget,key', MOVED, ids=[item[1] for item in MOVED])
def test_page_navigation_search_and_help_keep_advanced_closed(settings, qapp, page, widget, key):
    settings._search_edit.clear()
    settings._apply_search_filter()
    destination = settings._pages[page.TITLE]
    destination.advanced.toggle.setChecked(False)
    settings.open_preferences(page.TITLE)
    row = settings._rows_by_widget[widget]
    qapp.processEvents()
    assert row.disclosure is None
    assert destination.base.indexOf(row) >= 0
    assert row.isVisibleTo(destination)
    settings._search_edit.setText(key)
    settings._apply_search_filter()
    qapp.processEvents()
    assert settings._stack.currentWidget() is destination
    assert row.isVisibleTo(destination)
    assert not destination.advanced.toggle.isChecked()
    if row.help:
        row.help.click()
        assert row.help_copy.isVisibleTo(row)
        row.hide_help()
    settings._search_edit.clear()
    settings._apply_search_filter()


def test_effective_config_keys_have_one_editor(settings):
    counts = Counter(key for key, _page, _row in settings.config_row_inventory())
    assert all(count == 1 for count in counts.values())
    for page in (general_page, audio_page, dictation_page, shortcuts_page):
        for _title, _description, _controls, keys in page.BASE + page.ADVANCED:
            # Echo cancellation rows were already retired by the existing builder.
            for key in filter(None, keys.split(',')):
                if not key.startswith('echo_cancellation.'):
                    assert counts[key] == 1, key


@pytest.mark.parametrize('code_changed', [False, True])
def test_restart_is_above_exit_and_uses_existing_guard(qapp, code_changed):
    from samsara.ui.tray_qt import SamsaraTrayQt
    tray = Mock()
    tray._app = Mock()
    tray._code_change_detected = code_changed
    def add(menu, label, callback):
        action = menu.addAction(label)
        action.triggered.connect(callback)
        return action
    tray._add = add
    menu = QMenu()
    actions = SamsaraTrayQt._core_actions(tray, menu)
    assert [action.text() for action in menu.actions()][-2:] == ['Restart Samsara', 'Exit']
    actions[-2].trigger()
    tray._restart_samsara.assert_called_once()
    tray._app.quit_app.assert_not_called()
    menu.deleteLater()


# Independent pre-move inventory; includes already-retired echo metadata.
# 525: exclude the retired capture editor; all other pre-move keys stay pinned.
KEYS_BEFORE = {
    'General': (
        'command_mode.preview_idle_delay_s command_mode.preview_idle_opacity hints_enabled language launch_at_login '
        'listening_indicator_position microphone microphone_name start_hidden '
        'ui.listening_display ui.theme ui_scale '
    ),
    'Audio': (
        'accessibility.ava_captions accessibility.ava_mute_audio audio_coordinator.duck_factor audio_feedback command_mode.exit_earcon '
        'command_mode.tts_char_limit ducking.hands_free_enabled ducking.hands_free_idle_level ducking.hands_free_level ducking.level '
        'ducking.recording_mode echo_cancellation.enabled echo_cancellation.latency_ms output_device output_device_hostapi '
        'output_device_name sound_theme sound_volume tts.enabled tts.engine '
        'tts.feedback_level tts.speed tts.voice_id tts.volume '
    ),
    'Dictation': (
        'accessibility.speech_pace add_trailing_space auto_capitalize auto_paste cal_multiplier '
        'cleanup_mode command_mode.abort_phrases command_mode.cancel_window_all_commands command_mode.cancel_window_s command_mode.command_matching_enabled '
        'command_mode.dictate_utterance_silence_s command_mode.dictation_literal_commands_only command_mode.enabled command_mode.enter_debounce_ms command_mode.inactivity_timeout_s '
        'command_mode.miss_limit command_mode.stop_phrases command_mode.utterance_silence_s compute_type continuous_commit_trigger '
        'device format_numbers intent.command_prefix min_speech_duration model_size '
        'performance_mode silence_threshold threshold_mode wake_word_config.audio.speech_threshold wake_word_config.audio.wake_command_timeout '
        'wake_word_config.oww_threshold wake_word_config.phrase wake_word_config.quick_silence_timeout wake_word_enabled '
    ),
    'Shortcuts': (
        'alarms.complete_hotkey alarms.dismiss_hotkey cancel_hotkey command_hotkey command_mode.button '
        'command_mode.mode command_mode.suppress_button continuous_commit_hotkey continuous_hotkey correction_hotkey '
        'dictate_commit_hotkey hotkey hotkeys.capture_correction memo_hotkey mode '
        'task_hotkey undo_hotkey wake_word_hotkey '
    ),
}


def test_pre_move_keys_are_preserved_once():
    for page in (general_page, audio_page, dictation_page, shortcuts_page):
        actual = Counter(key for row in page.BASE + page.ADVANCED for key in filter(None, row[3].split(',')))
        assert actual >= Counter(KEYS_BEFORE[page.TITLE].split())
        assert all(count == 1 for count in actual.values())
