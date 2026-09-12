# Test suite audit: tests that cannot fail

Report only. No source or test files were modified. Branch `feature/v0.22`.

Scope: every file under `tests/` was read (166 `test_*.py` files plus `conftest.py`,
`_qt_subprocess_helper.py`, `_fake_ducking_child.py`, `__init__.py`). `pytest.ini` was
checked for collection exclusions -- there are none (`testpaths = tests`,
`python_files = test_*.py`, no `collect_ignore`, no `--ignore` in `addopts`).

Environment facts verified rather than assumed, because several gates depend on them:

| Gate | State on this machine |
|---|---|
| `F:\Projects F\Agora` (test_agora_bridge) | present -- interop tests run |
| `tests/fixtures/audio` (test_long_dictation_quality) | present -- fixture tests run |
| `C:\Users\Morne\Documents\Claude\ci_smoke_dl\...\samsara.log` | **absent** -- see NEVER RUNS |
| symlink creation (test_update_customizations) | works -- those tests run |
| `winsdk` (test_tts_winrt) | installed -- those tests run |

---

## Summary

| Metric | Count |
|---|---|
| Test files read | 169 (166 `test_*.py` + conftest + 2 helper modules) |
| Test functions collected | 2943 |
| Test classes | 538 |
| **Flagged test functions** | **165** |

By category:

| # | Category | Flagged |
|---|---|---|
| 1 | TAUTOLOGY | 53 |
| 2 | MOCKED-THROUGH | 29 |
| 3 | SILENT THREAD | 8 |
| 4 | WEAK ORACLE | 72 |
| 5 | SNAPSHOT DRIFT | 0 |
| 6 | NEVER RUNS | 3 |

Category 5 came back empty on purpose-built probes: no `updated to match` /
`regenerated` / `--snapshot-update` comments anywhere in `tests/`, and no assertion
compares against a float literal with 6+ decimal places. The one thing named `SNAPSHOT`
(`test_ai_capability.py`) is a hand-authored capability dict, not a recorded golden file.

The 53 TAUTOLOGY findings are not scattered -- 45 of them sit in four clusters where a
test file defines its own copy of the production logic and asserts against the copy.
One of those copies has already drifted out of sync with production and nothing failed.

---

## Flagged tests

### 1. TAUTOLOGY (53)

| file::test_name | category | reason |
|---|---|---|
| test_wake_word_pipeline.py::TestFillerWordStripping::test_strip_leading_please | TAUTOLOGY | class defines its own `_strip()` staticmethod; `samsara.command_parser.strip_fillers` is never called |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_strip_trailing_please | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_strip_leading_uh | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_strip_leading_um | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_strip_both_ends | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_multiple_leading_fillers | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_interior_filler_preserved | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_interior_please_preserved | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_no_fillers | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_only_fillers | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_empty_string | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_all_variants_yield_same_command | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_bare_dictate_with_fillers | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_long_dictate_with_fillers | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestFillerWordStripping::test_short_dictate_with_fillers | TAUTOLOGY | asserts against the test's local `_strip()` copy |
| test_wake_word_pipeline.py::TestDictationCommandParsing::test_dictate_command_recognized | TAUTOLOGY | `text = "dictate"; assert text.lower() in [...]` -- pure literal, no Samsara call |
| test_wake_word_pipeline.py::TestDictationCommandParsing::test_dictate_with_content | TAUTOLOGY | builds the string, slices it inline, asserts on its own slice |
| test_wake_word_pipeline.py::TestDictationCommandParsing::test_end_word_extraction | TAUTOLOGY | inlines `rfind` + slice, asserts on its own result |
| test_wake_word_pipeline.py::TestDictationCommandParsing::test_end_word_middle_of_text | TAUTOLOGY | same inline `rfind` reimplementation |
| test_wake_word_pipeline.py::TestDictationCommandParsing::test_cancel_word_detected | TAUTOLOGY | `assert "cancel" in "cancel this".lower()` |
| test_wake_word_pipeline.py::TestDictationCommandParsing::test_pause_word_strips_and_keeps_content | TAUTOLOGY | inline strip, and the assert `or`s both possible outcomes |
| test_command_mode.py::TestCommandModeStateMachine::test_enter_sets_active | TAUTOLOGY | `_MockApp` carries "# Inline copies of command mode methods under test" |
| test_command_mode.py::TestCommandModeStateMachine::test_enter_idempotent | TAUTOLOGY | asserts against `_MockApp`'s inline copy of `enter_command_mode` |
| test_command_mode.py::TestCommandModeStateMachine::test_exit_clears_active | TAUTOLOGY | asserts against `_MockApp`'s inline copy of `exit_command_mode` |
| test_command_mode.py::TestCommandModeStateMachine::test_exit_idempotent | TAUTOLOGY | asserts against `_MockApp`'s inline copy |
| test_command_mode.py::TestCommandModeStateMachine::test_exit_stops_recording | TAUTOLOGY | asserts against `_MockApp`'s inline copy |
| test_command_mode.py::TestCommandModeStateMachine::test_exit_earcon_disabled_no_sound | TAUTOLOGY | asserts against `_MockApp`'s inline copy |
| test_command_mode.py::TestCommandModeStateMachine::test_inactivity_timer_exits_toggle_mode | TAUTOLOGY | asserts against `_MockApp`'s inline timer copy |
| test_command_mode.py::TestCommandModeStateMachine::test_cancel_inactivity_timer | TAUTOLOGY | asserts against `_MockApp`'s inline timer copy |
| test_command_mode.py::TestCommandModeStateMachine::test_concurrent_enter_only_activates_once | TAUTOLOGY | inline copy; and `activated` is collected but never asserted |
| test_command_mode.py::TestGhostTapPrevention::test_long_hold_clears_ghost_flag | TAUTOLOGY | ghost-tap logic reimplemented in local `_enter`/`_exit` closures |
| test_command_mode.py::TestGhostTapPrevention::test_short_hold_sets_ghost_flag | TAUTOLOGY | same local closure reimplementation |
| test_command_mode.py::TestGhostTapPrevention::test_ghost_flag_cleared_after_discard | TAUTOLOGY | same local closure reimplementation |
| test_command_mode.py::TestExitEarconNoDuplication::test_no_extra_stop_when_recording_was_active | TAUTOLOGY | earcon logic lives in `_MockApp.exit_command_mode`, not dictation.py |
| test_command_mode.py::TestExitEarconNoDuplication::test_exit_earcon_plays_when_not_recording | TAUTOLOGY | same |
| test_mouse_hook.py::TestOnCommandButton::test_mouse4_press_enters_hold_mode | TAUTOLOGY | nested `_App` redefines `_on_command_button`; **already drifted** (see below) |
| test_mouse_hook.py::TestOnCommandButton::test_mouse4_release_exits_hold_mode | TAUTOLOGY | asserts against the test's own `_on_command_button` |
| test_mouse_hook.py::TestOnCommandButton::test_wrong_button_ignored | TAUTOLOGY | asserts against the test's own copy |
| test_mouse_hook.py::TestOnCommandButton::test_disabled_config_ignored | TAUTOLOGY | asserts against the test's own copy |
| test_mouse_hook.py::TestOnCommandButton::test_toggle_first_press_enters | TAUTOLOGY | asserts against the test's own copy |
| test_mouse_hook.py::TestOnCommandButton::test_toggle_second_press_exits | TAUTOLOGY | asserts against the test's own copy |
| test_mouse_hook.py::TestOnCommandButton::test_mouse5_configured | TAUTOLOGY | asserts against the test's own copy |
| test_integration.py::TestRecordingModes::test_hold_mode_flow | TAUTOLOGY | `recording = True; assert recording is True` -- no Samsara code runs |
| test_integration.py::TestRecordingModes::test_toggle_mode_flow | TAUTOLOGY | flips a local bool and asserts on it |
| test_integration.py::TestRecordingModes::test_hold_with_wake_word_flow | TAUTOLOGY | three local bools, no production import |
| test_integration.py::TestAudioProcessing::test_audio_buffer_concatenation | TAUTOLOGY | tests `numpy.concatenate` on literals, not Samsara |
| test_integration.py::TestAudioProcessing::test_empty_audio_buffer | TAUTOLOGY | `if not audio_data: result = None` then asserts `result is None` |
| test_integration.py::TestAudioProcessing::test_audio_sample_rate | TAUTOLOGY | `assert int(16000 * 1.0) == 16000` |
| test_history.py::test_confidence_band | TAUTOLOGY | defines `confidence_color()` in the test body and asserts against that copy |
| test_wake_consumer_hotkey_deafness.py::test_no_wake_transcription_dispatched_while_hotkey_recording | TAUTOLOGY | the two Mocks are assigned **after** the 5 `_process_frame()` calls; `assert_not_called()` inspects fresh Mocks |
| test_earcons.py::TestThemeSwitching::test_themes_produce_different_audio | TAUTOLOGY | body is `if a.shape == b.shape: assert ...`; shapes verified unequal, so zero assertions execute |
| test_qt_runtime_phase2.py::check_task_overlay_reopen (via test_phase2_in_isolated_subprocess) | TAUTOLOGY | "One QApplication throughout" asserted as `len(app_instances) == 1` on a list that gets exactly one append |
| test_inject_matrix_tool.py::test_sentence_comparison_is_exact | TAUTOLOGY | `compare_sentence(TEXT)` uses `expected=TEXT` by default -- compares the module constant to itself |

### 2. MOCKED-THROUGH (29)

| file::test_name | category | reason |
|---|---|---|
| test_media_keys.py::TestCommandHandlers::test_pause_handler_returns_true | MOCKED-THROUGH | `_call()` patches both `_run_async` and `_send_action`; the action string is never asserted |
| test_media_keys.py::TestCommandHandlers::test_play_handler_returns_true | MOCKED-THROUGH | same -- `handle_play_this` could send `'next'` and this passes |
| test_media_keys.py::TestCommandHandlers::test_toggle_handler_returns_true | MOCKED-THROUGH | same |
| test_media_keys.py::TestCommandHandlers::test_next_handler_returns_true | MOCKED-THROUGH | same |
| test_media_keys.py::TestCommandHandlers::test_prev_handler_returns_true | MOCKED-THROUGH | same |
| test_media_keys.py::TestCommandHandlers::test_handler_returns_true_even_when_action_fails | MOCKED-THROUGH | same |
| test_stremio_control.py::TestPublicControlFunctions::test_pause_play_sends_space | MOCKED-THROUGH | `is True` comes straight from the `_run_ahk` mock's `return_value` |
| test_stremio_control.py::TestPublicControlFunctions::test_fullscreen_sends_f | MOCKED-THROUGH | same |
| test_stremio_control.py::TestPublicControlFunctions::test_mute_sends_m | MOCKED-THROUGH | same |
| test_stremio_control.py::TestPublicControlFunctions::test_skip_forward_sends_right_6 | MOCKED-THROUGH | same |
| test_stremio_control.py::TestPublicControlFunctions::test_skip_back_sends_left_2 | MOCKED-THROUGH | same |
| test_stremio_control.py::TestPublicControlFunctions::test_volume_up_sends_up | MOCKED-THROUGH | same |
| test_stremio_control.py::TestPublicControlFunctions::test_volume_down_sends_down | MOCKED-THROUGH | same |
| test_stremio_control.py::TestPublicControlFunctions::test_switch_monitor_sends_win_shift_right | MOCKED-THROUGH | same |
| test_echo_cancel.py::TestEnabledBehaviorUnchanged::test_process_invokes_the_adaptive_filter_when_active | MOCKED-THROUGH | NLMS filter and loopback both mocked; only `assert_called_once()`, no args/effects |
| test_echo_cancel.py::TestEnabledBehaviorUnchanged::test_start_attempts_loopback_capture | MOCKED-THROUGH | `LoopbackCapture.start`/`is_running` mocked to True; asserts wiring only |
| test_handlers.py::TestLaunchHandler::test_calls_subprocess | MOCKED-THROUGH | `Popen` mocked; `assert_called_once()` never checks the `'notepad.exe'` target |
| test_handlers.py::TestMouseHandler::test_single_click | MOCKED-THROUGH | `click.assert_called_once()` -- `button='left'` never verified |
| test_handlers.py::TestKeyDownHandler::test_records_held_key | MOCKED-THROUGH | `press.assert_called_once()` with no key argument |
| test_command_executor.py::TestCommandExecution::test_execute_press_command | MOCKED-THROUGH | `assert_called_once()` on press/release, no key argument |
| test_command_executor.py::TestCommandExecution::test_execute_key_down | MOCKED-THROUGH | `assert_called_once()` with no key argument |
| test_command_executor.py::TestCommandExecution::test_execute_mouse_double_click | MOCKED-THROUGH | the double-click count `2` is never checked here |
| test_app_index.py::TestAppIndexBackgroundBuild::test_ensure_built_async_does_not_block | MOCKED-THROUGH | `threading.Thread` patched wholesale; no target/daemon/args assertion |
| test_window_manager.py::TestRestoreLayout::test_restore_calls_setwindowpos | MOCKED-THROUGH | `SetWindowPos.assert_called()` on a module-level `sys.modules` MagicMock never reset between tests |
| test_window_manager.py::TestRestoreLayout::test_restore_multiple_windows_each_matched | MOCKED-THROUGH | `call_count >= 2` on that same never-reset shared mock |
| test_tasks_no_network.py::test_show_hide_tasks_no_request | MOCKED-THROUGH | `_get_overlay` fully mocked; the hide path is never asserted |
| test_smart_actions_phase2.py::TestToolDispatch::test_tool_dispatch_tier1_auto | MOCKED-THROUGH | both `_request_confirmation` and `_execute` mocked |
| test_smart_actions_phase2.py::TestTier2ApprovalScope::test_tier2_approved_skips_confirmation | MOCKED-THROUGH | same shape |
| test_alarm_scheduler.py::test_visual_callback_failure_does_not_cancel_sound_nag | MOCKED-THROUGH | `thread_registry.spawn` stubbed to return `_DummyThread`, so `nag_thread is not None` is guaranteed by the stub |

### 3. SILENT THREAD (8) -- ALL FIXED

| file::test_name | category | reason |
|---|---|---|
| test_command_mode.py::TestCommandModeStateMachine::test_concurrent_enter_only_activates_once | [FIXED -- already satisfied] | re-verified: this test already collects `errors` from every one of the 10 threads and asserts `errors == []`; no `activated` list exists in the current file. The audit's original finding was stale for this test. Re-proved failure (temporarily asserted `len(errors) == 1`, captured `assert 0 == 1`, reverted) to confirm it is a real, live assertion. |
| test_gate_determinism.py::test_scan_result_unchanged_while_a_foreign_thread_hammers_the_lock | [FIXED] | `_foreign_hammer` now catches `BaseException` into `foreign_errors`, asserted `== []`, plus `not t.is_alive()`. Proved by injecting a `raise RuntimeError(...)` in the hammer loop -- captured `AssertionError: ... contains one more item`, reverted. |
| test_ducking_transport.py::test_second_caller_is_not_made_to_wait_a_second_full_deadline | [FIXED] | `hang()` now catches into `hang_errors`, asserted `== []`, plus `not hanger.is_alive()`. Proved via injected fault, reverted. |
| test_recording_ownership.py::test_hold_enter_worker_claim_is_atomic_with_exit | [FIXED] | both the enter-worker and exit-worker threads now catch into `worker_errors`/`exit_errors`, both asserted `== []`. Proved via injected fault (both `mode` parametrizations), reverted. |
| test_splash_startup_wiring.py::test_model_worker_waits_for_shell_before_completion | [FIXED] | worker thread now catches into `worker_errors`, asserted `== []`. This surfaced a real pre-existing bug the silent thread was hiding: the fixture's fake app was missing `config_path`, so `load()`'s post-boot `_write_last_known_good()` step raised `AttributeError` on every run and died silently. Added `app.config_path` (a nonexistent path, so the real never-raises LKG no-op path is taken) to fix the fixture. Proved the exception-collection assertion via injected fault, reverted. |
| test_hands_free_capture_ducking.py::test_blocked_capture_start_is_invalidated_by_toggle_off | [FIXED] | `_open()` now catches into `open_errors`, asserted `== []`. This also surfaced a second real pre-existing bug: the test asserted `owner["token"] == 1`, but `_open_hands_free_capture_duck`'s real "lost ownership" path (exactly what this test's name says it exercises) returns `None`, not the provisional token -- fixed the assertion to `is None`. Proved via injected fault, reverted. |
| test_hands_free_capture_ducking.py::test_owner_a_releases_while_starting_and_owner_b_keeps_ducker | [FIXED] | same treatment: `_open_a()` catches into `open_errors`, asserted `== []`. Proved via injected fault, reverted. |
| test_frozen_smoke_unit.py::test_wait_for_boot_detects_marker | [FIXED] | writer thread now catches into `writer_errors`, asserted `== []`, plus `not writer.is_alive()` (joined with a timeout instead of left daemon-orphaned). Proved via injected fault, reverted. |

### 4. WEAK ORACLE (72)

**4a. Zero assertions at all -- can only fail by raising (29).** Found by AST scan;
delegating helpers that do assert internally (`run_isolated`, `_run_guarded_probe`) were
excluded as false positives.

| file::test_name | category | reason |
|---|---|---|
| test_hands_free_duck_wiring.py::TestOpenHandsFreeDuckSafe::test_noop_when_app_lacks_the_method | WEAK ORACLE | no assertion; a fully gutted method still passes |
| test_hands_free_duck_wiring.py::TestOpenHandsFreeDuckSafe::test_swallows_exception_from_app_method | WEAK ORACLE | no assertion |
| test_hands_free_duck_wiring.py::TestCloseHandsFreeDuckSafe::test_noop_when_app_lacks_the_method | WEAK ORACLE | no assertion |
| test_hands_free_duck_wiring.py::TestCloseHandsFreeDuckSafe::test_swallows_exception_from_app_method | WEAK ORACLE | no assertion |
| test_smart_corrections.py::test_warm_up_never_raises_when_backend_resolution_fails | WEAK ORACLE | no assertion |
| test_smart_corrections.py::test_warm_up_request_failure_is_swallowed | WEAK ORACLE | no assertion |
| test_smart_corrections.py::test_no_notification_manager_does_not_raise | WEAK ORACLE | no assertion |
| test_window_manager.py::TestSavedLayouts::test_delete_nonexistent_is_noop | WEAK ORACLE | no assertion |
| test_window_manager.py::TestRestoreLayout::test_restore_missing_app_skipped | WEAK ORACLE | no assertion |
| test_window_manager.py::TestRestoreLayout::test_restore_nonexistent_layout_noop | WEAK ORACLE | no assertion |
| test_mouse_hook.py::TestCallbackException::test_exception_in_callback_does_not_propagate | WEAK ORACLE | no assertion |
| test_mouse_hook.py::TestLifecycle::test_stop_without_start_does_not_raise | WEAK ORACLE | no assertion |
| test_history_store.py::TestUnavailableStore::test_delete_does_not_raise | WEAK ORACLE | no assertion |
| test_history_store.py::TestUnavailableStore::test_clear_does_not_raise | WEAK ORACLE | no assertion |
| test_ava_command_session_migration.py::TestFirstRunMigrationNotice::test_no_hints_manager_is_a_safe_noop | WEAK ORACLE | no assertion |
| test_ava_command_session_migration.py::TestFirstRunMigrationNotice::test_tts_exception_is_swallowed | WEAK ORACLE | no assertion |
| test_listening_indicator_move.py::TestTrayEntersMoveMode::test_enter_indicator_move_mode_safe_without_indicator | WEAK ORACLE | no assertion |
| test_listening_indicator_move.py::TestLiveIndicatorSettings::test_safe_before_indicator_initialization | WEAK ORACLE | no assertion |
| test_dictate_streaming_preview.py::test_dictate_lane_final_with_no_preview_running_is_safe | WEAK ORACLE | no assertion |
| test_stress_wizard_qt.py::TestUnhookDiscipline::test_closed_window_ignores_late_hook_fire | WEAK ORACLE | no assertion; the guard being removed would still pass |
| test_logging_rotation.py::test_rollover_failure_is_never_raised_to_the_caller | WEAK ORACLE | no assertion |
| test_stremio_control.py::TestKillStremio::test_never_raises_on_taskkill_failure | WEAK ORACLE | no assertion |
| test_tray_qt.py::TestIconGeometryRefresh::test_refresh_failure_never_raises | WEAK ORACLE | no assertion |
| test_main_window_qt.py::TestShowAndRaiseRestoresMinimizedWindow::test_noop_when_window_is_none | WEAK ORACLE | no assertion |
| test_wake_consumer_lifecycle.py::TestReleaseWakeConsumer::test_none_consumer_is_safe | WEAK ORACLE | no assertion |
| test_diagnostics.py::TestOneShotHooks::test_remove_is_idempotent_no_error_if_never_registered | WEAK ORACLE | no assertion |
| test_browser_bridge.py::test_stop_before_start_is_safe | WEAK ORACLE | no assertion |
| test_ducking_transport.py::TestShutdown::test_shutdown_is_idempotent | WEAK ORACLE | no assertion |
| test_audio_ducking.py::test_stop_without_start_is_noop | WEAK ORACLE | no assertion |

**4b. Specific value was available but a loose predicate was used (43).**

| file::test_name | category | reason |
|---|---|---|
| test_ava_command_session_shortlist_cap.py::TestShortlistSizeCap::test_shortlist_never_exceeds_configured_size | WEAK ORACLE | `len(shortlist) <= 12` passes on an empty shortlist |
| test_ava_command_session_shortlist_cap.py::TestShortlistSizeCap::test_shortlist_respects_a_larger_configured_size | WEAK ORACLE | `len <= 50` against a 288-command registry; empty passes |
| test_gate_determinism.py::test_two_scans_with_no_interference_agree | WEAK ORACLE | `r1 == r2` passes if the gate always returns False |
| test_gate_determinism.py::test_scan_result_unchanged_while_a_foreign_thread_hammers_the_lock | WEAK ORACLE | compares to a baseline whose value is never asserted |
| test_settings_accessibility.py::test_scaled_settings_window_uses_work_area_friendly_minimum_and_refresh_fits | WEAK ORACLE | `maximumWidth() > minimumWidth()` is Qt's default sentinel (16777215) |
| test_settings_accessibility.py::test_commands_page_scrolls_vertically_without_horizontal_overflow | WEAK ORACLE | same sentinel check on 5 buttons plus `pack_scroll` |
| test_theme_accessibility.py::test_representative_caption_and_ghost_button_fit_their_content | WEAK ORACLE | `heightForWidth > 0` / `sizeHint >= fontMetrics` hold for any Qt widget |
| test_splash_animation_qt.py::test_timer_is_capped_at_thirty_frames_per_second | WEAK ORACLE | `interval() >= 33` has no upper bound; 100000 passes |
| test_splash_animation_qt.py::test_system_motion_preference_is_a_safe_boolean | WEAK ORACLE | `isinstance(..., bool)` only |
| test_single_instance_mutex.py::test_home_override_is_read_at_call_time | WEAK ORACLE | compares the function to itself; a constant-returning impl passes |
| test_single_instance_mutex.py::test_equivalent_profile_paths_get_the_same_mutex | WEAK ORACLE | same self-comparison shape |
| test_single_instance_mutex.py::test_distinct_home_overrides_get_distinct_mutexes | WEAK ORACLE | asserts inequality only; no expected name |
| test_single_instance_lock.py::test_no_lock_file_is_a_noop | WEAK ORACLE | postcondition is identical to the precondition asserted two lines earlier |
| test_single_instance_lock.py::test_alive_other_process_pid_lock_refuses_and_exits | WEAK ORACLE | name promises refuse+exit; body is a copy of `test_dead_pid_lock_gets_stolen` with a different PID |
| test_mouse_hook.py::TestLifecycle::test_start_fails_gracefully_when_set_hook_returns_zero | WEAK ORACLE | `is None or == 0` accepts both plausible outcomes |
| test_mouse_hook.py::TestSuppression::test_suppress_mouse4_does_not_suppress_mouse5 | WEAK ORACLE | `result != 1`; the real expected return (0) was available |
| test_mouse_hook.py::TestSuppression::test_suppress_none_passes_mouse4_through | WEAK ORACLE | `result != 1` |
| test_app_index.py::TestLogTop3::test_logs_top_three_and_no_more | WEAK ORACLE | `out.count("=") <= 3 or "top-3" in out` -- the `or` makes it near-unfalsifiable |
| test_diagnostics_verdict.py::TestEmpty::test_headline_and_detail_are_both_nonempty_strings | WEAK ORACLE | `isinstance(str)` + truthy only |
| test_diagnostics_verdict.py::TestDuckTyping::test_missing_optional_fields_default_gracefully | WEAK ORACLE | `isinstance(str)` + truthy only |
| test_main_window_qt.py::TestShowAndRaiseRestoresMinimizedWindow::test_non_minimized_window_is_just_shown | WEAK ORACLE | postcondition identical to precondition |
| test_small_ui_control_fixes.py::test_live_log_existing_window_posts_restore_then_focus | WEAK ORACLE | `posted == ["<lambda>","<lambda>","<lambda>"]` -- any three lambdas pass |
| test_wake_profiles.py::TestCountSyllables::test_single_word_minimum_one_syllable | WEAK ORACLE | `count_syllables("go") >= 1`; exact value available |
| test_wake_word_matcher.py::TestSuffixMatch::test_suffix_with_period | WEAK ORACLE | unpacks `(m, t, i)` and asserts only `m is True` |
| test_history_store.py::TestAppendAndQuery::test_append_returns_row_id | WEAK ORACLE | `isinstance(int)` and `> 0` |
| test_history_store.py::TestDayLabel::test_default_now_does_not_raise | WEAK ORACLE | `isinstance(result, str)` only |
| test_history_view.py::TestConstruction::test_construction_with_no_store_and_no_legacy_shows_empty_state | WEAK ORACLE | `_list.count() >= 1`; the exact value 1 is known |
| test_command_executor.py::TestCommandExecution::test_execute_hotkey_command | WEAK ORACLE | `call_count >= 2`; the exact ctrl+c sequence was available |
| test_command_executor.py::TestPluginCommands::test_missing_plugins_dir_is_noncrash | WEAK ORACLE | bare truthy `assert executor.commands` |
| test_integration.py::TestTranscriptionPipeline::test_transcription_to_command_flow | WEAK ORACLE | `call_count >= 2`; test_handlers asserts the exact sequence |
| test_integration.py::TestFullCommandExecution::test_hotkey_command_full_flow | WEAK ORACLE | mocks press/release then asserts only `was_command is True` |
| test_integration.py::TestFullCommandExecution::test_text_command_full_flow | WEAK ORACLE | only `was_command is True`; the inserted text is never checked |
| test_app_overrides.py::TestHotkeyHandlerOverrides::test_no_override_sends_base_keys | WEAK ORACLE | `press.assert_called()` / `release.assert_called()` with no key arguments |
| test_handlers.py::TestRegistry::test_get_handler_for_each_type | WEAK ORACLE | `is not None` for 10 types; the concrete handler class was available |
| test_handlers.py::TestMacroHandler::test_unknown_action_logged_not_fatal | WEAK ORACLE | name claims "logged" but no log assertion exists |
| test_dictation_app.py::TestMicrophoneManagement::test_get_available_microphones | WEAK ORACLE | `len(input_mics) >= 1` |
| test_tts_winrt.py::TestWinRTEngineInit::test_initializes_without_error | WEAK ORACLE | `assert engine is not None` |
| test_tts_winrt.py::TestWinRTEngineInit::test_voice_list_not_empty | WEAK ORACLE | `len(voices) > 0` |
| test_ai_capability.py::TestSettingsConstraints::test_returns_dict | WEAK ORACLE | `isinstance(dict)` + `len > 0` |
| test_ai_capability.py::TestSettingsConstraints::test_numeric_entries_have_min_max | WEAK ORACLE | `min < max`; the documented bounds were available |
| test_ai_capability.py::TestSettingsConstraints::test_enum_entries_have_options | WEAK ORACLE | `len(options) >= 2` |
| test_ai_capability.py::TestExpandedConfigSchema::test_total_key_count_exceeds_phase2a | WEAK ORACLE | `len(s) > 13`; the exact count is knowable |
| test_ai_capability.py::TestGetCapabilitySnapshot::test_version_is_hex_string | WEAK ORACLE | `isinstance(str)` only; no hex-shape check |
| test_audio_coordinator.py::TestShutdown::test_coordinator_survives_engine_crash | WEAK ORACLE | the crash is swallowed by `except: pass`, and the only assertion is guaranteed by the `shutdown()` on the line above |
| test_audio_coordinator.py::TestThresholdManagement::test_threshold_raised_on_speaking_entry | WEAK ORACLE | `current > original`; the multiplier is a known constant |
| test_frozen_smoke_unit.py::test_make_min_config_skips_wizard | WEAK ORACLE | `cfg["microphone"] is not None` |
| test_media_keys.py::TestSendAction::test_toggle_calls_try_toggle_async | WEAK ORACLE | unpacks `(ok, msg)` and asserts on neither |
| test_media_keys.py::TestSendAction::test_next_calls_try_skip_next_async | WEAK ORACLE | same |
| test_media_keys.py::TestSendAction::test_previous_calls_try_skip_previous_async | WEAK ORACLE | same |
| test_inject_matrix_tool.py::test_hold_hotkey_abort_detects_ctrl | WEAK ORACLE | bare truthy assert on the return value |
| test_log_viewer.py::TestInvalidBytesNeverRaise::test_invalid_bytes_at_arbitrary_seek_point_in_large_file | WEAK ORACLE | `len(lines) > 0` |
| test_diagnostics.py::TestOneShotHooks::test_remove_is_idempotent_no_error_if_already_fired | WEAK ORACLE | the trailing `remove_one_shot_hook` call has no assertion |

### 5. SNAPSHOT DRIFT (0)

None found. No `updated to match` / `regenerated` / `--snapshot-update` markers in
`tests/`; no assertion compares against a float literal carrying 6+ decimal places
(the signature of a value pasted from program output). Fixture-heavy files
(`test_hf_bench.py`, `test_flight_digest.py`, `test_hf_corpus_record.py`) build their
expected values from first principles or `pytest.approx` with explicit tolerances.

### 6. NEVER RUNS (3) -- 1 FIXED, 2 DELETED

| file::test_name | category | reason |
|---|---|---|
| test_ci_smoke.py::TestLogScannerRealCiLog::test_classifies_mic_less_tracebacks_as_benign_but_still_catches_real_crash | [FIXED -- now runs everywhere] | `skipif` on a hardcoded absolute path `C:\Users\Morne\Documents\Claude\ci_smoke_dl\ci-smoke-log-dev-f3dd59a\samsara.log` -- **verified absent on this machine** and unreachable on any other; the fixture is not in the repo. The real log cannot be recovered, so replaced with a checked-in, explicitly-labelled SYNTHETIC reconstruction (`tests/fixtures/ci_smoke_synthetic_regression.txt` -- `.log` would be
   silently gitignored by this repo's `*.log` rule, so `.txt` is used to
   ensure the fixture is actually committed) reproducing the same shape (3 distinct benign PortAudioError tracebacks -- device-rate query, calibration, sound-stream start -- followed, after enough lines to clear LogScanner's 15-line lookback, by the genuine ModuleNotFoundError crash). `skipif` removed entirely; test now runs on every machine. Proved it can still fail (temporarily asserted `len(scanner.benign_seen) == 4`, captured `AssertionError: assert 3 == 4`, reverted). |
| test_ava_command_session_g2_matrix.py::TestOrchestrationSemanticsOutOfScopeForP1::test_no_token_zero_orchestration_surface | [DELETED] | body is an unconditional `pytest.skip()` (declared P2 placeholder). D3 has no Agora token/orchestration surface at all in P1 -- there is no production code for this test to ever exercise, and building that surface is a production change out of scope for a tests-only fix. Deleted rather than left as a permanent no-op. |
| test_ava_command_session_g2_matrix.py::TestOrchestrationSemanticsOutOfScopeForP1::test_token_present_listener_dead_hidden_verbs_no_error_ui | [DELETED] | same reason -- unconditional `pytest.skip()`, no P1 production surface to test. |

Checked and *not* flagged: `test_agora_bridge.py`, `test_long_dictation_quality.py`,
`test_update_customizations.py` symlink tests, `test_tts_winrt.py`,
`test_clipboard_preserve.py` and `test_ducking_transport.py` all gate on conditions that
are satisfied on this machine, so they do execute. One naming note: `pytest.ini` declares
`slow`, `integration` and `clipboard` markers but not `audio`, which `test_tts_winrt.py`
uses -- that produces a warning, not a skip, so those tests still run.

---

## MISSING -- public functions with no test at all

### samsara/quick_memo.py
No gaps. `memo_dir`, `memo_file`, `append_memo`, `retain_audio` are all exercised
(plus the private `_replace_with_retry`).

### samsara/torch_guard.py
No gaps. `install`, `uninstall`, `_TorchGuardFinder.find_spec` are all exercised,
including in fresh subprocesses.

### samsara/audio_engine/wake_consumer.py
- `WakeConsumer.deactivate` -- **no test**. Not referenced anywhere in `tests/`.
- `WakeConsumer.start` -- **no test of the real method**. The only `.start()` assertions
  in the suite are against `FakeWakeConsumer` (test_wake_consumer_lifecycle.py,
  test_ava_command_session_ghost_tap.py) or a `Mock()`. The real polling thread is never
  started by any test.
- `WakeConsumer.stop` -- exercised exactly once (test_wake_consumer_hotkey_deafness.py
  `test_stop_closes_capture_duck`) and only for its capture-duck side effect; the frame
  drain and thread teardown are untested.

Covered: `wake_session_policy`, `abort_utterance`, `discard_stale_wake_utterance`,
`snapshot_dictate_preview_audio` (and the private `_process_frame`, `_flush`, `_poll_loop`).

### samsara/clipboard.py
- `copy_text` -- **no test**. Not referenced anywhere in `tests/`.
- `type_text_unicode` -- **no test of the real function**. Its only appearance is
  `monkeypatch.setattr(dictation, 'type_text_unicode', lambda text: False)` in
  test_dictation_clipboard_delegation.py, which patches it out. `test_typed_injection.py`
  covers `build_unicode_key_events` (the pure event builder) but nothing calls the
  injector itself.
- `test_clipboard_preservation` -- **no test**, and it is a `test_`-prefixed public
  function living in production code. `pytest.ini` (`testpaths = tests`,
  `python_files = test_*.py`) means it is never collected, so the name is a trap: it
  reads like a test and is not one.

Covered: `is_snapshot_eligible_format`, `is_nonempty_payload`,
`get_clipboard_sequence_number`, `save_clipboard`, `restore_clipboard`,
`paste_with_preservation`, `build_unicode_key_events`.

### samsara/echo_cancel.py
- `EchoCanceller.set_enabled` -- **no test**.
- `EchoCanceller.set_latency` -- **no test**.
- `EchoCanceller.calibrate_and_cache` -- **no test**.
- `EchoCanceller.calibrate_lag` -- **no test**.
- `EchoCanceller.stop` -- **no test**. `test_echo_cancel.py` only ever calls
  `EchoCanceller(...)`, `.start()`, `.process()`, `.is_active`.
- `AdaptiveEchoCanceller.process` -- **no test of the real method**. Every reference
  patches it (`patch.object(AdaptiveEchoCanceller, "process")`); the NLMS filter body
  never executes under test.
- `AdaptiveEchoCanceller.reset` -- **no test**.
- `LoopbackCapture.start` / `.stop` / `.get_recent` / `.is_running` -- **no test of the
  real methods**. All four are only ever patched in `test_echo_cancel.py`; the WASAPI
  loopback path never runs.

Effectively the only real coverage of this module is the disabled-path bypass.

### Wake-session policy code in dictation.py
No gaps found. `wake_session_policy` (in wake_consumer.py) plus
`_confirm_wake_capture`, `_start_wake_session`, `_restart_wake_session_timer`,
`_expire_wake_session`, `_end_wake_session`, `_reset_wake_dictation`,
`_wake_audio_is_below_gate`, `_warn_wake_fallback_once`, `_dispatch_wake_profile`,
`_emit_wake_trace`, `_load_oww_model`, `_load_wake_profile_models`,
`process_wake_word_buffer`, `start_wake_word_mode`, `stop_wake_word_mode` are all
exercised, mostly by `test_wake_session_policy.py` against real bound methods with a fake
clock. This is the best-covered area audited.

---

## Fix first

The ten whose weakness hides the most risk, ordered by how much real behaviour they
falsely claim to protect.

1. **test_command_mode.py `_MockApp` cluster (14 tests)** -- the file's own comment reads
   "# Inline copies of command mode methods under test". Enter/exit/ghost-tap/inactivity
   for the hands-free session -- the accessibility path a motor-impaired user depends on
   -- are asserted against a copy. `dictation.py` could break entirely and all 14 pass.
2. **test_mouse_hook.py::TestOnCommandButton (7 tests)** -- same defect and it has
   *already drifted*: the real `_on_command_button` defaults to `cfg.get('button','rctrl')`
   (dictation.py:5859) while the test's copy defaults to `'mouse4'`. Live proof the
   pattern silently rots.
3. **test_wake_consumer_hotkey_deafness.py::test_no_wake_transcription_dispatched_while_hotkey_recording**
   -- the two Mocks are assigned *after* the frames are processed, so `assert_not_called()`
   inspects fresh objects. Delete the entire hotkey-deafness guard and this still passes;
   it is the named guard for the word-loss incident.
4. **test_wake_word_pipeline.py::TestFillerWordStripping (15 tests)** -- reimplements
   `strip_fillers` as a local staticmethod. Fifteen green tests, zero production
   exercise; identical in kind to the `test_ab_delta_math` case that prompted this audit.
5. **test_earcons.py::test_themes_produce_different_audio** -- `if a.shape == b.shape:`
   guards the only assertion, and the shapes are unequal (cute 8731 vs warm 12127
   samples). The test executes zero assertions. Per-theme audio tuning is unverified.
6. **test_media_keys.py::TestCommandHandlers (6 tests)** -- `_send_action` is mocked and
   the action string is never asserted, so `handle_pause_this` could dispatch `next` and
   every handler test still passes. These are the only tests of the media-key routing.
7. **[FIXED] test_integration.py::TestRecordingModes + TestAudioProcessing (6 tests)** -- six
   tests in the file named "integration tests for the full transcription pipeline" that
   set local variables and assert on them. Nothing about the pipeline is covered here.
   TestRecordingModes now binds the real `DictationApp.on_key_press`/`on_key_release`
   (via `DictationApp.__new__` + a minimal fake-app fixture, no module-level `import
   dictation`) so hold/toggle mode dispatch, `start_recording`/`stop_recording` call
   args, and the `thread_registry.spawn`-deferred hold-release stop all run for real.
   TestAudioProcessing now drives the real `DictationSessionConsumer.snapshot_streaming_audio()`
   (samsara/audio_engine/dictation_consumer.py) and the real `samsara.audio_engine.frame`
   constants (SAMPLE_RATE/FRAME_SIZE) instead of calling `np.concatenate` on literals.
   All 6 proved capable of failing (each assertion flipped once, failure captured, reverted).
8. **[FIXED] test_echo_cancel.py::test_process_invokes_the_adaptive_filter_when_active** --
   combined with the MISSING list, the real `AdaptiveEchoCanceller.process` and the whole
   `LoopbackCapture` are never executed. The module's enabled path has no real coverage
   at all, only mock wiring. Removed the `patch.object(AdaptiveEchoCanceller, "process")`
   mock -- only the hardware boundary (`LoopbackCapture.is_running`/`get_recent`) stays
   mocked. Asserts the real NLMS filter ran (`ec._aec._diag_count == 1`), that the output
   is the correct shape/dtype, and that it is not bit-identical to the input. Proved
   capable of failing (flipped the diag-count assertion, captured `assert 1 == 2`, reverted).
9. **[FIXED] test_ci_smoke.py::TestLogScannerRealCiLog** -- the regression fixture for a
   real CI false-positive is gated on an absolute path outside the repo that does not
   exist. The release-gate log classifier's only real-world test never runs, on any
   machine. Same fix as NEVER RUNS #1 below: replaced with a checked-in synthetic
   reconstruction, `skipif` removed, test now runs everywhere.
10. **[FIXED] test_window_manager.py::test_restore_multiple_windows_each_matched** -- asserts
    `SetWindowPos.call_count >= 2` against a `sys.modules`-level MagicMock that is never
    reset between tests, so earlier tests' calls already satisfy it. Layout restore can
    stop calling `SetWindowPos` entirely and this stays green. Added `win32gui.reset_mock()`
    before the call under test and replaced the loose count check with an exact
    `call_args_list` match (one `SetWindowPos` call per window, in order, with the real
    per-monitor restore rect) plus an exact `ShowWindow` maximize-call assertion. Proved
    capable of failing (swapped the expected call order, captured the `call(...)` diff,
    reverted).

Common thread across 1, 2, 4 and 7: a test file that imports nothing from the module it
claims to test. That is mechanically detectable and worth a CI check.
