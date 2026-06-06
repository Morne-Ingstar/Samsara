# Samsara Code Review Plan

**Total scope:** ~104 Python files, ~31,200 lines.
Largest single file: `settings_qt.py` at 3,332 lines.
The file structure groups naturally into 7 parallel tracks.

---

## Suggested parallel split

**Instance 1:** Tracks A + C + G1-G4 — command system, TTS/AI layer, core tests
**Instance 2:** Tracks B + D + E1-E4 — audio engine, system layer, main plugins
**Then converge:** Track F (UI) and G5-G7 (remaining tests)

---

## Things to flag during review

- Thread safety (audio engine, TTS coordinator, streaming)
- Command injection or shell exec without sanitization
- Hardcoded paths or credentials
- Dead code beyond the `_stale/` directory
- Inconsistent error handling patterns
- Test coverage gaps relative to complexity

---

## Track A — Entry point, command system, core dispatch

*Can start immediately, no dependencies.*

| Pass | Files | Lines |
|------|-------|-------|
| A1 | `samsara_launcher.py`, `samsara/__init__.py`, `samsara/constants.py`, `samsara/languages.py` | ~200 |
| A2 | `samsara/commands.py`, `samsara/command_registry.py`, `samsara/command_parser.py` | ~670 |
| A3 | `samsara/command_packs.py`, `samsara/handlers.py`, `samsara/command_stats.py` | ~470 |
| A4 | `samsara/plugin_commands.py`, `samsara/key_macros.py`, `samsara/phonetic_wash.py`, `samsara/cleanup.py` | ~685 |

---

## Track B — Audio engine + wake word + echo

*Can start immediately, no dependencies.*

| Pass | Files | Lines |
|------|-------|-------|
| B1 | `samsara/audio_engine/frame.py`, `samsara/audio_engine/ring.py`, `samsara/audio_engine/__init__.py` | ~400 |
| B2 | `samsara/audio_engine/engine.py`, `samsara/audio_engine/debug_recorder.py` | ~385 |
| B3 | `samsara/audio_engine/wake_consumer.py`, `samsara/audio_engine/dictation_consumer.py`, `samsara/audio_engine/continuous_consumer.py` | ~620 |
| B4 | `samsara/wake_word_matcher.py`, `samsara/wake_detector.py`, `samsara/wake_corrections.py` | ~316 |
| B5 | `samsara/echo_cancel.py` lines 1–300 | 300 |
| B6 | `samsara/echo_cancel.py` lines 300–559, `samsara/calibration.py`, `samsara/audio_switch.py` | ~390 |

---

## Track C — TTS + Ava + Smart Actions

*Can start immediately, no dependencies.*

| Pass | Files | Lines |
|------|-------|-------|
| C1 | `samsara/tts/exceptions.py`, `samsara/tts/audio_utils.py`, `samsara/tts/engine_base.py`, `samsara/tts/__init__.py` | ~155 |
| C2 | `samsara/tts/edge_tts_engine.py`, `samsara/tts/coordinator.py` | ~704 |
| C3 | `samsara/tts/winrt_engine.py` lines 1–260 | 260 |
| C4 | `samsara/tts/winrt_engine.py` lines 260–520 | 260 |
| C5 | `samsara/ava_memory.py`, `samsara/ava_corrections.py`, `samsara/ava_profile.py` | ~463 |
| C6 | `samsara/smart_actions_tools.py`, `samsara/smart_actions_bridge.py`, `samsara/smart_actions_session.py` | ~678 |
| C7 | `samsara/cloud_llm.py`, `samsara/learning.py`, `samsara/premium.py`, `samsara/cuda_detect.py`, `samsara/vision.py`, `samsara/hints.py` | ~555 |

---

## Track D — System layer + storage + platform

*Can start immediately, no dependencies.*

| Pass | Files | Lines |
|------|-------|-------|
| D1 | `samsara/platform.py`, `samsara/mouse_hook.py`, `samsara/audio_switch.py` | ~458 |
| D2 | `samsara/history.py`, `samsara/health_store.py`, `samsara/tasks_store.py` | ~398 |
| D3 | `samsara/profiles.py`, `samsara/config_watch.py` | ~577 |
| D4 | `samsara/clipboard.py`, `samsara/notifications.py` | ~680 |
| D5 | `samsara/alarms.py` lines 1–365 | 365 |
| D6 | `samsara/alarms.py` lines 365–730 | 365 |
| D7 | `samsara/streaming.py` lines 1–350 | 350 |
| D8 | `samsara/streaming.py` lines 350–704 | 354 |

---

## Track E — Plugins

*Can start immediately, no dependencies.*

| Pass | Files | Lines |
|------|-------|-------|
| E1 | `plugins/commands/ask_ollama.py` lines 1–520 | 520 |
| E2 | `plugins/commands/ask_ollama.py` lines 520–1039 | 519 |
| E3 | `plugins/commands/window_switcher.py`, `plugins/commands/windows.py` | ~1487 |
| E4 | `plugins/commands/show_numbers.py`, `plugins/commands/demo_commands.py` | ~1066 |
| E5 | `plugins/commands/volume.py`, `plugins/commands/media_keys.py`, `plugins/commands/music.py`, `plugins/commands/stremio.py`, `plugins/commands/audio_switch.py` | ~728 |
| E6 | `plugins/commands/health_tracker.py`, `plugins/commands/tasks.py`, `plugins/commands/reminders.py`, `plugins/commands/alarm_commands.py`, `plugins/commands/timer.py` | ~792 |
| E7 | `plugins/commands/smart_actions.py`, `plugins/commands/screen_gif.py`, `plugins/commands/text_marker.py`, `plugins/commands/scroll.py` | ~1023 |
| E8 | `plugins/commands/macros.py`, `plugins/commands/core_utils.py`, `plugins/commands/tab_finder.py`, `plugins/commands/web_shortcuts.py`, `plugins/commands/quick_ask.py`, `plugins/commands/gif_search.py`, `plugins/commands/example_greet.py`, `plugins/commands/hyperion_lights.py`, `plugins/commands/flashforge_printer.py` | ~720 |
| E9 | `plugins/drafts/file_manager.py`, `plugins/assets/sleep_overlay.py` | ~358 |

---

## Track F — UI

*`settings_qt.py` needs 4 passes. Other large wizards need 2 each.*

| Pass | Files | Lines |
|------|-------|-------|
| F1 | `samsara/ui/tray_qt.py`, `samsara/ui/splash_qt.py`, `samsara/ui/hint_toast.py`, `samsara/ui/numbers_overlay_qt.py`, `samsara/ui/__init__.py` | ~540 |
| F2 | `samsara/ui/main_window_qt.py`, `samsara/ui/status_overlay.py` | ~942 |
| F3 | `samsara/ui/listening_indicator.py`, `samsara/ui/history_qt.py`, `samsara/ui/command_cheatsheet_qt.py` | ~1180 |
| F4 | `samsara/ui/task_overlay.py`, `samsara/ui/profile_manager_qt.py`, `samsara/ui/dictionary_panel_qt.py` | ~1216 |
| F5 | `samsara/ui/settings_qt.py` lines 1–835 (audio/mic/TTS panels) | 835 |
| F6 | `samsara/ui/settings_qt.py` lines 835–1668 (profiles/commands/dictionaries) | 833 |
| F7 | `samsara/ui/settings_qt.py` lines 1668–2499 (health/alarms/Ava/advanced) | 831 |
| F8 | `samsara/ui/settings_qt.py` lines 2499–3332 (signals, apply logic, misc) | 833 |
| F9 | `samsara/ui/first_run_wizard_qt.py`, `samsara/ui/mic_setup_wizard_qt.py` lines 1–456 | ~912 |
| F10 | `samsara/ui/mic_setup_wizard_qt.py` lines 456–912, `samsara/ui/tutorial_qt.py` lines 1–387 | ~774 |
| F11 | `samsara/ui/tutorial_qt.py` lines 387–774, `samsara/ui/wake_word_debug_qt.py` lines 1–583 | ~770 |
| F12 | `samsara/ui/wake_word_debug_qt.py` lines 583–1166, `samsara/ui/voice_training_qt.py`, `samsara/ui/ava_guide_qt.py` | ~1490 |

---

## Track G — Tests

*Best done last — evaluate test quality in context of the code being tested.*

| Pass | Files | Lines |
|------|-------|-------|
| G1 | `tests/conftest.py`, `tests/test_modules.py` | ~527 |
| G2 | `tests/audio_engine/` all 4 files | ~898 |
| G3 | `tests/test_wake_word.py`, `tests/test_wake_word_matcher.py`, `tests/test_wake_word_pipeline.py`, `tests/test_command_mode.py` | ~928 |
| G4 | `tests/test_handlers.py`, `tests/test_command_executor.py`, `tests/test_command_parser.py`, `tests/test_command_registry.py`, `tests/test_command_packs.py` | ~919 |
| G5 | `tests/test_smart_actions.py`, `tests/test_smart_actions_phase2.py`, `tests/test_audio_coordinator.py`, `tests/test_tts_winrt.py` | ~1264 |
| G6 | `tests/test_integration.py`, `tests/test_dictation_app.py`, `tests/test_pipeline.py`, `tests/test_history.py` | ~889 |
| G7 | `tests/test_earcons.py`, `tests/test_voice_training.py`, `tests/test_calibration.py`, `tests/test_settings.py`, `tests/test_phonetic_wash.py`, `tests/test_app_overrides.py`, `tests/test_show_numbers.py`, `tests/test_media_keys.py`, `tests/test_window_manager.py`, `tests/test_mouse_hook.py`, `tests/diagnostics/` | ~1100 |
