"""285 inventory pinned independently from ROW_AUDIT.md (282).
Duplicate appearances and earcon previews collapse to a single editor;
alarm record fields are owned by the Alarms manager. 284 repairs/deletions
and 276 startup additions are explicitly reconciled. No external files needed.
"""
from collections import Counter
import copy
import pytest
from PySide6.QtWidgets import QWidget
from samsara.ui import settings_qt
from tests.test_settings_pages import _StubApp, _schema_defaults

AUDITED_KEYS = set([
    "accessibility.ava_captions",
    "accessibility.ava_mute_audio",
    "accessibility.speech_pace",
    "add_trailing_space",
    "alarms.complete_hotkey",
    "alarms.dismiss_hotkey",
    "alarms.enabled",
    "alarms.items",
    "alarms.nag_interval_seconds",
    "audio_coordinator.duck_factor",
    "audio_feedback",
    "auto_capitalize",
    "auto_paste",
    "ava.consent",
    "ava.provider_policy",
    "ava.warm_on_boot",
    "ava_command_session.backend",
    "ava_command_session.enabled",
    "ava_command_session.interpret_misses",
    "ava_command_session.keep_warm",
    "ava_command_session.model",
    "ava_edit.demo_pacing",
    "ava_edit.enabled",
    "ava_edit.model",
    "ava_edit.timeout_seconds",
    "ava_memory.max_turns",
    "ava_memory.mode",
    "ava_mode_enabled",
    "ava_mode_key",
    "ava_personality",
    "benchmark.collect_samples",
    "cal_multiplier",
    "cancel_hotkey",
    "cleanup_mode",
    "cloud_llm.api_key",
    "cloud_llm.enabled",
    "cloud_llm.model",
    "cloud_llm.provider",
    "cloud_llm.timeout_seconds",
    "cloud_llm.web_search",
    "command_hotkey",
    "command_mode.abort_phrases",
    "command_mode.button",
    "command_mode.cancel_window_all_commands",
    "command_mode.cancel_window_s",
    "command_mode.command_matching_enabled",
    "command_mode.dictate_utterance_silence_s",
    "command_mode.enabled",
    "command_mode.enter_debounce_ms",
    "command_mode.exit_earcon",
    "command_mode.inactivity_timeout_s",
    "command_mode.miss_limit",
    "command_mode.mode",
    "command_mode.preview_idle_delay_s",
    "command_mode.preview_idle_opacity",
    "command_mode.stop_phrases",
    "command_mode.suppress_button",
    "command_mode.tts_char_limit",
    "command_mode.utterance_silence_s",
    "command_packs.3d-printing",
    "command_packs.accessibility",
    "command_packs.ai",
    "command_packs.alarms",
    "command_packs.audio",
    "command_packs.browsers",
    "command_packs.core",
    "command_packs.discord",
    "command_packs.experimental",
    "command_packs.gaming",
    "command_packs.health",
    "command_packs.macros",
    "command_packs.media",
    "command_packs.screen-capture",
    "command_packs.session",
    "command_packs.smart-actions",
    "command_packs.smart-home",
    "command_packs.stremio",
    "command_packs.tasks",
    "command_packs.text-editing",
    "command_packs.utilities",
    "command_packs.window-cube-numbers",
    "command_packs.window-management",
    "compute_type",
    "continuous_commit_hotkey",
    "continuous_commit_trigger",
    "continuous_hotkey",
    "correction_hotkey",
    "device",
    "dictate_commit_hotkey",
    "ducking.hands_free_enabled",
    "ducking.hands_free_idle_level",
    "ducking.hands_free_level",
    "ducking.level",
    "ducking.recording_mode",
    "echo_cancellation.enabled",
    "echo_cancellation.latency_ms",
    "feedback.spoken_notices",
    "format_numbers",
    "gesture.enabled",
    "hints_enabled",
    "hotkey",
    "hotkeys.capture_correction",
    "intent.command_prefix",
    "language",
    "launch_at_login",
    "listening_indicator_enabled",
    "listening_indicator_position",
    "memo_hotkey",
    "microphone",
    "microphone_name",
    "min_speech_duration",
    "mode",
    "model_size",
    "music_library",
    "output_device",
    "output_device_hostapi",
    "output_device_name",
    "performance_mode",
    "silence_threshold",
    "smart_corrections.enabled",
    "smart_corrections.modes.hotkey",
    "smart_corrections.modes.streaming",
    "smart_corrections.modes.wake",
    "smart_corrections.ollama_model",
    "smart_corrections.repair_disfluencies",
    "sound_theme",
    "sound_volume",
    "start_hidden",
    "streaming_mode",
    "tasks.todoist.api_token",
    "tasks.todoist.enabled",
    "threshold_mode",
    "tts.enabled",
    "tts.engine",
    "tts.speed",
    "tts.voice_id",
    "tts.volume",
    "ui.live_surface.enabled",
    "ui.theme",
    "ui_scale",
    "undo_hotkey",
    "updates.automatic_checks",
    "wake_word_config.audio.speech_threshold",
    "wake_word_config.audio.wake_command_timeout",
    "wake_word_config.oww_threshold",
    "wake_word_config.phrase",
    "wake_word_config.quick_silence_timeout",
    "wake_word_enabled",
    "wake_word_hotkey"
])


@pytest.fixture
def window(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **kw: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    win = settings_qt._SettingsWindow(_StubApp(_schema_defaults()))
    yield win
    win.deleteLater()


def test_every_audited_key_has_one_reachable_editor(window):
    assert len(AUDITED_KEYS) == 149
    bindings = window.config_row_inventory()
    counts = Counter(key for key, _page, _widget in bindings)
    assert AUDITED_KEYS <= counts.keys(), sorted(AUDITED_KEYS - counts.keys())
    assert all(counts[key] == 1 for key in AUDITED_KEYS)
    for key, page, widget in bindings:
        root = window._pages.get(page) or window._tool_pages[page]
        assert root.isAncestorOf(widget), (key, page)
    # The source factories must not create a second visible editor.
    assert window._source_store.isHidden()


def test_six_pages_keep_approved_base_budgets(window):
    assert settings_qt._TAB_NAMES == ['General', 'Dictation', 'Audio', 'Ava', 'Shortcuts', 'Help']
    assert [len(window._pages[name].base_rows) for name in settings_qt._TAB_NAMES] == [5, 6, 7, 5, 10, 5]
    assert set(window._tool_pages) == {'Command library', 'Alarms', 'Health', 'Music', 'Components', 'Integrations'}


def test_preferences_do_not_apply_tool_optins(window):
    before = copy.deepcopy(window.app.config)
    window._widgets['todoist_enabled'].setChecked(True)
    window._widgets['alarms_enabled'].setChecked(False)
    window._widgets['adv_gesture_enabled'].setChecked(False)
    updates = window._collect_destination_updates({})
    assert 'tasks' not in updates
    assert 'gesture' not in updates
    assert 'music_library' not in updates
    assert updates['alarms']['enabled'] == before['alarms']['enabled']


def test_integration_apply_cannot_change_preferences(window):
    window._tool_name = 'Integrations'
    window._widgets['launch_at_login'].setChecked(True)
    window._widgets['todoist_enabled'].setChecked(True)
    updates = window._collect_destination_updates({})
    assert set(updates) == {'tasks'}
    assert updates['tasks']['todoist']['enabled'] is True


def test_provider_controls_follow_actual_consent_result(window, monkeypatch):
    from samsara.ui import ava_consent_qt
    monkeypatch.setattr(ava_consent_qt, 'request_consent', lambda *a, **k: None)
    monkeypatch.setattr(ava_consent_qt, 'consent_required', lambda *a, **k: True)
    window._widgets['cloud_enabled'].setChecked(False)
    window._widgets['providerRoute'].setCurrentIndex(1)
    assert not window._widgets['cloud_enabled'].isChecked()
    assert window._widgets['providerRoute'].currentIndex() == 0
    assert window._rows_by_widget['cloud_api_key'].isHidden()


def test_search_reveals_advanced_setting(window):
    window._search_edit.setText('launch_at_login')
    window._apply_search_filter()
    assert window._pages['General'].advanced.toggle.isChecked()
    assert not window._rows_by_widget['launch_at_login'].isHidden()


def test_help_keeps_accessible_text_and_large_target(window):
    row = window._rows_by_widget['launch_at_login']
    assert row.help.width() >= 44 and row.help.height() >= 44
    assert row.help.accessibleDescription() == row.full_help
    row.help.click()
    assert not row.help_panel.isHidden()


def test_precision_metadata_is_read_only(window):
    from PySide6.QtWidgets import QLabel
    status = window._widgets['computePrecisionStatus']
    assert isinstance(status, QLabel)
    assert window._pages['Dictation'].isAncestorOf(status)
    assert not window._pages['Dictation'].isAncestorOf(window._widgets['adv_compute_type'])


@pytest.mark.parametrize('width', [720, 1160])
def test_pages_fit_at_compact_and_normal_width(window, qapp, width):
    window.resize(width, 700)
    window.show()
    for index, name in enumerate(settings_qt._TAB_NAMES):
        window._sidebar.setCurrentRow(index)
        page = window._pages[name]
        for expanded in (False, True):
            page.advanced.toggle.setChecked(expanded)
            for _ in range(3):
                qapp.processEvents()
            assert page.container.width() <= page.viewport().width(), (name, width, expanded)
    window.hide()


def test_keyboard_help_and_disclosure(window, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    window.show()
    advanced = window._pages['General'].advanced
    advanced.toggle.setFocus()
    QTest.keyClick(advanced.toggle, Qt.Key.Key_Space)
    assert advanced.toggle.isChecked()
    row = window._rows_by_widget['launch_at_login']
    row.help.setFocus()
    qapp.processEvents()
    assert row.help_panel.isVisibleTo(window)
    QTest.keyClick(row.help, Qt.Key.Key_Escape)
    assert row.help_panel.isHidden()
    assert window._widgets['launch_at_login'].accessibleName()
    window.hide()
