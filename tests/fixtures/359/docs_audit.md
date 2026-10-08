# Snapshot from reports/358/artifacts/docs_audit.md; source dated 2026-10-03.
# Copied 2026-10-08; only rows consumed by test_settings_help_359.py retained.

| Ava route master (343) | MISSING | ava/#routes, #consent; settings/#ava.provider_policy | ai_preferences.py:141-199, ava_cloud_qt.py:722-728 |
| Ava memory + what/forget/erase (342) | MISSING | ava/#memory; settings/#ava_memory.mode | ask_ollama.py:314-363, ava_memory.py |
| images by voice (320) | MISSING | ava/#images; settings/#comfy.base_url | plugins/commands/comfy.py |
| Spotify import (348) | MISSING | ava/#music; settings/#tools-music | music_import.py, music_qt.py |
| design scale (314) | MISSING | settings/#ui_scale | design_scale.py, ui_scale.py (fixed scale, 44 px targets; Interface size 100/115/130%) |
| `comfy.base_url` | config.json only | ui-less: Local ComfyUI server address; loopback only. | settings/#comfy.base_url |
| `comfy.default_workflow` | config.json only | ui-less: Name of an explicitly imported local image workflow. | settings/#comfy.default_workflow |
| `comfy.workflows` | config.json only | ui-less: Imported API graphs and their exact input mappings; configured by the  | settings/#comfy.workflows |
| `comfy.prompt_prefix` | config.json only | ui-less: Owner-editable text prepended verbatim to image prompts; empty by defa | settings/#comfy.prompt_prefix |
| `comfy.prompt_suffix` | config.json only | ui-less: Owner-editable text appended verbatim to image prompts; empty by defau | settings/#comfy.prompt_suffix |
| `comfy.auto_open` | config.json only | ui-less: Advanced image completion preference; open the image remains available | settings/#comfy.auto_open |
| `comfy.timeout_s` | config.json only | ui-less: Local image rendering deadline in seconds. | settings/#comfy.timeout_s |
| `queue_voice.queue_root` | config.json only | ui-less: Owner-maintained prompt queue location; advanced config-only path. | settings/#queue_voice.queue_root |
| `queue_voice.reports_root` | config.json only | ui-less: Owner-maintained prompt reports location; advanced config-only path. | settings/#queue_voice.reports_root |
| `queue_voice.python` | config.json only | ui-less: Interpreter for the existing queue note script; advanced config-only p | settings/#queue_voice.python |
| `dispatch_voice.enabled` | config.json only | ui-less: Agora voice dispatch and its event feed. | settings/#dispatch_voice.enabled |
| `dispatch_voice.poll_s` | config.json only | ui-less: Seconds between Agora dispatch feed checks. | settings/#dispatch_voice.poll_s |
| `agent_channel.cwd` | config.json only | ui-less: Working directory for subscription CLI agents. | settings/#agent_channel.cwd |
| `agent_channel.allow_writes` | config.json only | ui-less: CLI agents are read-only by default. | settings/#agent_channel.allow_writes |
| `agent_channel.voice_preamble` | config.json only | ui-less: Spoken-response instruction sent with each CLI question. | settings/#agent_channel.voice_preamble |
| `agent_channel.timeout_s` | config.json only | ui-less: Maximum seconds to wait for a CLI answer. | settings/#agent_channel.timeout_s |
| `claude_voice.enabled` | config.json only | ui-less: Desktop Claude reply reading is enabled by default; advanced config-on | settings/#claude_voice.enabled |
| `claude_voice.spoken_word_cap` | config.json only | ui-less: Advanced config-only limit for each spoken Claude reply chunk. | settings/#claude_voice.spoken_word_cap |
| `claude_voice.reply_timeout_s` | config.json only | ui-less: Advanced config-only timeout for a desktop Claude reply. | settings/#claude_voice.reply_timeout_s |
| `tasks.todoist.enabled` | Tools > integrations | had a row | settings/#tasks.todoist.enabled |
| `tasks.todoist.api_token` | Tools > integrations | had a row | settings/#tasks.todoist.api_token |
| `wake_profiles` | Settings > Dictation > Wake profiles (Advanced) | had a row | settings/#wake_profiles |
| `model_size` | Settings > Dictation > Recognition model (Advanced) | had a row | settings/#model_size |
| `language` | Settings > General > Language | had a row | settings/#language |
| `ui.theme` | Settings > General > Theme | had a row | settings/#ui.theme |
| `auto_paste` | Settings > Dictation > Insertion / output | had a row | settings/#auto_paste |
| `add_trailing_space` | Settings > Dictation > Trailing space (Advanced) | had a row | settings/#add_trailing_space |
| `auto_capitalize` | Settings > Dictation > Capitalisation (Advanced) | had a row | settings/#auto_capitalize |
| `format_numbers` | Settings > Dictation > Number formatting (Advanced) | had a row | settings/#format_numbers |
| `hints_enabled` | Settings > General > Contextual hints | had a row | settings/#hints_enabled |
| `launch_at_login` | Settings > General > Start at sign-in (Advanced) | had a row | settings/#launch_at_login |
| `start_hidden` | Settings > General > Start hidden (Advanced) | had a row | settings/#start_hidden |
| `updates.automatic_checks` | Settings > Help > Updates | had a row | settings/#updates.automatic_checks |
| `formatting_tokens.enabled` | config.json only | ui-less: Config-only escape hatch for spoken formatting-token interpretation; n | settings/#formatting_tokens.enabled |
| `accessibility.ava_captions` | Settings > Audio > Ava captions | had a row | settings/#accessibility.ava_captions |
| `accessibility.ava_mute_audio` | Settings > Audio > Mute Ava's voice | had a row | settings/#accessibility.ava_mute_audio |
| `accessibility.ava_captions_linger_s` | config.json only | ui-less: Config-only caption timing override; the existing captions UI delibera | settings/#accessibility.ava_captions_linger_s |
| `accessibility.ava_captions_position` | config.json only | ui-less: Placement state written by dragging the caption window; not a separate | settings/#accessibility.ava_captions_position |
| `cleanup_mode` | Settings > Dictation > Cleanup mode | had a row | settings/#cleanup_mode |
| `dictation.signoff_filter` | config.json only | ui-less: Developer escape hatch for the default-on hallucinated-signoff filter  | settings/#dictation.signoff_filter |
| `mode` | Settings > Shortcuts > Primary dictation | had a row | settings/#mode |
| `wake_word_enabled` | Settings > Dictation > Wake activation | had a row | settings/#wake_word_enabled |
| `threshold_mode` | Settings > Dictation > Microphone calibration (Advanced) | had a row | settings/#threshold_mode |
| `cal_multiplier` | Settings > Dictation > Calibration multiplier (Advanced) | had a row | settings/#cal_multiplier |
| `wake_word_config.audio.wake_command_timeout` | Settings > Dictation > Timeout after wake (Advanced) | had a row | settings/#wake_word_config.audio.wake_command_timeout |
| `wake_word_config.quick_silence_timeout` | Settings > Dictation > Quick silence timeout (Advanced) | had a row | settings/#wake_word_config.quick_silence_timeout |
| `wake_word_config.opens_session` | config.json only | ui-less: Config-only opt-in to the alternative latched wake route, which also r | settings/#wake_word_config.opens_session |
| `wake_word_config.oww_threshold` | Settings > Dictation > Wake threshold (Advanced) | had a row | settings/#wake_word_config.oww_threshold |
| `command.trigger_mode` | config.json only | ui-less: Phase 2A compatibility key; command_mode.mode is the current Settings  | settings/#command.trigger_mode |
| `command.button` | config.json only | ui-less: Phase 2A compatibility key; command_mode.button is the current Setting | settings/#command.button |
| `command_mode.enabled` | Settings > Dictation > Session button | had a row | settings/#command_mode.enabled |
| `command_mode.button` | Settings > Shortcuts > Session button | had a row | settings/#command_mode.button |
| `command_mode.mode` | Settings > Shortcuts > Session button | had a row | settings/#command_mode.mode |
| `command_mode.suppress_button` | Settings > Shortcuts > Mouse suppression (Advanced) | had a row | settings/#command_mode.suppress_button |
| `command_mode.enter_debounce_ms` | Settings > Dictation > Command debounce (Advanced) | had a row | settings/#command_mode.enter_debounce_ms |
| `command_mode.abort_phrases` | Settings > Dictation > Exit phrases (Advanced) | had a row | settings/#command_mode.abort_phrases |
| `command_mode.dictate_commit_word` | config.json only | ui-less: Config-only single-word session protocol override; changing the commit | settings/#command_mode.dictate_commit_word |
| `command_mode.stop_phrases` | Settings > Dictation > Stop words (Advanced) | had a row | settings/#command_mode.stop_phrases |
| `command_mode.inactivity_timeout_s` | Settings > Dictation > Session timeout (Advanced) | had a row | settings/#command_mode.inactivity_timeout_s |
| `command_mode.dictate_utterance_silence_s` | Settings > Dictation > Dictation pause (Advanced) | had a row | settings/#command_mode.dictate_utterance_silence_s |
| `command_mode.miss_limit` | Settings > Dictation > Command miss limit (Advanced) | had a row | settings/#command_mode.miss_limit |
| `command_mode.personal_alias_offer_timeout_s` | config.json only | ui-less: Config-only tuning for the specialist spoken alias-offer flow, separat | settings/#command_mode.personal_alias_offer_timeout_s |
| `command_mode.session_streaming_preview` | config.json only | ui-less: Config-only legacy session-preview switch; separate from the dedicated | settings/#command_mode.session_streaming_preview |
| `command_mode.preview_idle_delay_s` | Settings > General > Preview fades after (Advanced) | had a row | settings/#command_mode.preview_idle_delay_s |
| `command_mode.preview_idle_opacity` | Settings > General > Faded preview visibility (Advanced) | had a row | settings/#command_mode.preview_idle_opacity |
| `command_mode.preview_position` | config.json only | ui-less: Placement state owned by the draggable preview and its Settings reset  | settings/#command_mode.preview_position |
| `command_mode.ava_tts_half_duplex` | config.json only | ui-less: Developer override for echo suppression while Ava speaks; keep the def | settings/#command_mode.ava_tts_half_duplex |
| `ava_command_session.enabled` | Settings > Ava > Ava runs | had a row | settings/#ava_command_session.enabled |
| `ava_command_session.backend` | Settings > Ava > Ava runs | had a row | settings/#ava_command_session.backend |
| `ava_command_session.model` | Settings > Ava > Local model (Advanced) | had a row | settings/#ava_command_session.model |
| `ava_command_session.shortlist_size` | config.json only | ui-less: Interpreter candidate-budget tuning; the supported user choice is whet | settings/#ava_command_session.shortlist_size |
| `ava_command_session.keep_warm` | Settings > Ava > Keep model warm (Advanced) | had a row | settings/#ava_command_session.keep_warm |
| `ava_command_session.interpret_misses` | Settings > Ava > Ava interprets commands | had a row | settings/#ava_command_session.interpret_misses |
| `click.type` | config.json only | ui-less: Compatibility command metadata; actual click actions belong to command | settings/#click.type |
| `click.button` | config.json only | ui-less: Compatibility command metadata; actual mouse buttons belong to command | settings/#click.button |
| `audio_feedback` | Settings > Audio > Feedback sounds | had a row | settings/#audio_feedback |
| `sound_volume` | Settings > Audio > Sound volume | had a row | settings/#sound_volume |
| `sound_theme` | Settings > Audio > Sound theme | had a row | settings/#sound_theme |
| `feedback.spoken_notices` | Settings > Audio > Spoken notices (Advanced) | had a row | settings/#feedback.spoken_notices |
| `tts.enabled` | Settings > Audio > Text-to-speech | had a row | settings/#tts.enabled |
| `tts.self_hear_grace_ms` | config.json only | ui-less: Capture interlock grace after Ava speech ends. | settings/#tts.self_hear_grace_ms |
| `tts.engine` | Settings > Audio > Speech engine (Advanced) | had a row | settings/#tts.engine |
| `tts.voice_id` | Settings > Audio > Voice (Advanced) | had a row | settings/#tts.voice_id |
| `tts.speed` | Settings > Audio > Speech speed (Advanced) | had a row | settings/#tts.speed |
| `tts.rate` | config.json only | ui-less: Phase 2A compatibility name; tts.speed is the current speech-speed pre | settings/#tts.rate |
| `tts.volume` | Settings > Audio > Speech volume (Advanced) | had a row | settings/#tts.volume |
| `audio_coordinator.duck_factor` | Settings > Audio > Speech during feedback (Advanced) | had a row | settings/#audio_coordinator.duck_factor |
| `alarms.enabled` | Tools > alarms | had a row | settings/#alarms.enabled |
| `alarms.nag_interval_seconds` | Tools > alarms | had a row | settings/#alarms.nag_interval_seconds |
| `device` | Settings > Dictation > Compute device (Advanced) | had a row | settings/#device |
| `compute_type` | Settings > Dictation > Precision metadata (Advanced) | had a row | settings/#compute_type |
| `performance_mode` | Settings > Dictation > Performance (Advanced) | had a row | settings/#performance_mode |
| `silence_threshold` | Settings > Dictation > Continuous silence (Advanced) | had a row | settings/#silence_threshold |
| `min_speech_duration` | Settings > Dictation > Minimum speech (Advanced) | had a row | settings/#min_speech_duration |
| `accessibility.speech_pace` | Settings > Dictation > Speech pace | had a row | settings/#accessibility.speech_pace |
| `wake_word_config.audio.speech_threshold` | Settings > Dictation > Manual threshold (Advanced) | had a row | settings/#wake_word_config.audio.speech_threshold |
| `echo_cancellation.enabled` | Settings > Audio > Echo cancellation (Advanced) | had a row | settings/#echo_cancellation.enabled |
| `echo_cancellation.latency_ms` | Settings > Audio > Echo latency (Advanced) | had a row | settings/#echo_cancellation.latency_ms |
| `ducking.enabled` | config.json only | ui-less: Legacy fallback for configurations without ducking.recording_mode; the | settings/#ducking.enabled |
| `ducking.recording_mode` | Settings > Audio > While recording (Advanced) | had a row | settings/#ducking.recording_mode |
| `ducking.level` | Settings > Audio > Ducked volume (Advanced) | had a row | settings/#ducking.level |
| `ducking.hands_free_enabled` | Settings > Audio > Hands-free audio (Advanced) | had a row | settings/#ducking.hands_free_enabled |
| `ducking.hands_free_level` | Settings > Audio > Hands-free capture volume (Advanced) | had a row | settings/#ducking.hands_free_level |
| `ducking.hands_free_idle_level` | Settings > Audio > Hands-free idle volume (Advanced) | had a row | settings/#ducking.hands_free_idle_level |
| `listening_indicator_enabled` | Settings > General > Listening indicator (Advanced) | had a row | settings/#listening_indicator_enabled |
| `ui.idle_animation` | config.json only | ui-less: Config-only animation override for the legacy listening indicator, sep | settings/#ui.idle_animation |
| `ui.live_surface.enabled` | Settings > General > Live surface (Advanced) | had a row | settings/#ui.live_surface.enabled |
| `ui.live_surface.schema_version` | config.json only | ui-less: Internal live-surface migration version, not a user preference. | settings/#ui.live_surface.schema_version |
| `ui.live_surface.idle_delay_s` | config.json only | ui-less: Experimental live-surface tuning; its public Settings surface remains  | settings/#ui.live_surface.idle_delay_s |
| `ui.live_surface.show_idle_mark` | config.json only | ui-less: Experimental live-surface tuning; its public Settings surface remains  | settings/#ui.live_surface.show_idle_mark |
| `ui.live_surface.partials` | config.json only | ui-less: Experimental partial-transcription policy; not a second control for th | settings/#ui.live_surface.partials |
| `ui.live_surface.focus_hotkey` | config.json only | ui-less: Experimental live-surface binding; config-only until it joins the vali | settings/#ui.live_surface.focus_hotkey |
| `ui.live_surface.reset_hotkey` | config.json only | ui-less: Experimental live-surface binding; config-only until it joins the vali | settings/#ui.live_surface.reset_hotkey |
| `ui.live_surface.pause_hotkey` | config.json only | ui-less: Experimental live-surface binding; config-only until it joins the vali | settings/#ui.live_surface.pause_hotkey |
| `ui.live_surface.placement` | config.json only | ui-less: Per-monitor geometry written by the live surface itself, not a text-ed | settings/#ui.live_surface.placement |
| `ui.live_surface.legacy` | config.json only | ui-less: Internal migration backup of previous floating-window settings; never  | settings/#ui.live_surface.legacy |
| `listening_indicator_position` | Settings > General > Indicator position (Advanced) | had a row | settings/#listening_indicator_position |
| `audio.input_sensitivity` | config.json only | ui-less: Compatibility schema value with no current capture consumer; microphon | settings/#audio.input_sensitivity |
| `transcription.mode` | config.json only | ui-less: Compatibility schema value; cleanup_mode is the current dictation clea | settings/#transcription.mode |
| `ava_edit.enabled` | Settings > Ava > Editing | had a row | settings/#ava_edit.enabled |
| `ava_edit.demo_pacing` | Settings > Ava > Demo pacing (Advanced) | had a row | settings/#ava_edit.demo_pacing |
| `ava_edit.model` | Settings > Ava > Local editing model (Advanced) | had a row | settings/#ava_edit.model |
| `ava_edit.timeout_seconds` | Settings > Ava > Editing timeout (Advanced) | had a row | settings/#ava_edit.timeout_seconds |
| `smart_corrections.enabled` | Settings > Ava > Automatic corrections (Advanced) | had a row | settings/#smart_corrections.enabled |
| `smart_corrections.ollama_model` | Settings > Ava > Correction model (Advanced) | had a row | settings/#smart_corrections.ollama_model |
| `smart_corrections.timeout_s` | config.json only | ui-less: Config-only correction-backend deadline tuning; retain the bounded run | settings/#smart_corrections.timeout_s |
| `smart_corrections.min_words` | config.json only | ui-less: Config-only correction eligibility threshold; the visible controls cho | settings/#smart_corrections.min_words |
| `smart_corrections.keep_alive` | config.json only | ui-less: Config-only Ollama duration override; it accepts backend duration synt | settings/#smart_corrections.keep_alive |
| `smart_corrections.modes.hotkey` | Settings > Ava > Correct hold dictation (Advanced) | had a row | settings/#smart_corrections.modes.hotkey |
| `smart_corrections.modes.wake` | Settings > Ava > Correct wake dictation (Advanced) | had a row | settings/#smart_corrections.modes.wake |
| `smart_corrections.modes.streaming` | Settings > Ava > Correct streaming (Advanced) | had a row | settings/#smart_corrections.modes.streaming |
| `smart_corrections.repair_disfluencies` | Settings > Ava > Repair disfluencies (Advanced) | had a row | settings/#smart_corrections.repair_disfluencies |
| `diagnostics.write_jsonl` | Settings > Help > Diagnostic event log (Advanced) | had a row | settings/#diagnostics.write_jsonl |
| `diagnostics.stage_timing` | Settings > Help > Stage timing (Advanced) | had a row | settings/#diagnostics.stage_timing |
| `debug.dump_hotkey_buffers` | config.json only | ui-less: Developer-only raw audio capture for buffer investigations; deliberate | settings/#debug.dump_hotkey_buffers |
| `benchmark.collect_samples` | Settings > Help > Benchmark recording (Advanced) | had a row | settings/#benchmark.collect_samples |
| `benchmark.max_samples` | config.json only | ui-less: Config-only benchmark storage cap; Help exposes collection consent whi | settings/#benchmark.max_samples |
| `correction_capture.max_edit_ratio` | config.json only | ui-less: Config-only correction-training rejection threshold; not an ordinary t | settings/#correction_capture.max_edit_ratio |
| `intent.shadow_enabled` | config.json only | ui-less: Developer-only shadow execution experiment that records dictated text  | settings/#intent.shadow_enabled |
| `intent.shadow_retention_days` | config.json only | ui-less: Retention tuning for the config-only shadow execution experiment, not  | settings/#intent.shadow_retention_days |
| `intent.command_prefix` | Settings > Dictation > Command word (Advanced) | had a row | settings/#intent.command_prefix |
| `cloud_llm.enabled` | Settings > Ava > Ava runs | had a row | settings/#cloud_llm.enabled |
| `cloud_llm.provider` | Settings > Ava > Cloud provider (Advanced) | had a row | settings/#cloud_llm.provider |
| `cloud_llm.timeout_seconds` | Settings > Ava > Cloud timeout (Advanced) | had a row | settings/#cloud_llm.timeout_seconds |
| `cloud_llm.web_search` | Settings > Ava > Web search (Advanced) | had a row | settings/#cloud_llm.web_search |
| `ava_personality` | Settings > Ava > Ava personality (Advanced) | had a row | settings/#ava_personality |
| `ava.warm_on_boot` | Settings > Ava > Warm at startup (Advanced) | had a row | settings/#ava.warm_on_boot |
| `ava.consent` | Settings > Ava > Ava runs | had a row | settings/#ava.consent |
| `ava.provider_policy` | Settings > Ava > Ava runs | had a row | settings/#ava.provider_policy |
| `ava.catalog_answers` | config.json only | ui-less: Local answers to command catalog questions. | settings/#ava.catalog_answers |
| `onboarding.version` | config.json only | ui-less: Setup workflow version owned by onboarding migrations. | settings/#onboarding.version |
| `onboarding.setup_status` | config.json only | ui-less: Setup progress owned by the first-run workflow; not a manually editabl | settings/#onboarding.setup_status |
| `onboarding.next_step` | config.json only | ui-less: Resume location owned by the first-run workflow. | settings/#onboarding.next_step |
| `onboarding.requested_ai` | config.json only | ui-less: Pending provider choice owned by setup; the Ava page owns the active c | settings/#onboarding.requested_ai |
| `onboarding.lessons` | config.json only | ui-less: Practice progress recorded by onboarding lessons; not a free-form Sett | settings/#onboarding.lessons |
| `ava_invocations` | config.json only | ui-less: Config-only whole-utterance invocation vocabulary; kept outside Settin | settings/#ava_invocations |
| `ava_memory.mode` | Settings > Ava > Memory retention | had a row | settings/#ava_memory.mode |
| `ava_memory.max_turns` | Settings > Ava > Remembered turns (Advanced) | had a row | settings/#ava_memory.max_turns |
| `ava_memory.sessions_kept` | config.json only | ui-less: Config-only limit for dated summaries in assistant memory. | settings/#ava_memory.sessions_kept |
| `ava_memory.recall_sessions` | config.json only | ui-less: Config-only limit for summaries recalled in a reply. | settings/#ava_memory.recall_sessions |
| `ollama.enabled` | config.json only | ui-less: Legacy provider flag; the Ava master and consent-aware provider policy | settings/#ollama.enabled |
| `ollama.host` | config.json only | ui-less: Config-only local-server endpoint override; normal setup uses the loca | settings/#ollama.host |
| `ollama.model` | config.json only | ui-less: Local conversational model selection is owned by the Ava setup guide;  | settings/#ollama.model |
| `ollama.timeout_seconds` | config.json only | ui-less: Config-only local conversational request deadline; separate from cloud | settings/#ollama.timeout_seconds |
| `ollama.max_response_length` | config.json only | ui-less: Config-only conversational response-length cap; not the spoken acknowl | settings/#ollama.max_response_length |
| `ollama.safety_gate_enabled` | config.json only | ui-less: Developer compatibility override for local-model safety checks; not an | settings/#ollama.safety_gate_enabled |
| `music_library` | Tools > music | had a row | settings/#music_library |
| `diagnostics.watch_code_changes` | config.json only | no UI at all, no ui_less_reason | settings/#diagnostics.watch_code_changes |
| `ui_scale` | Settings > General > Interface size | row key, NOT in schema | settings/#ui_scale |
| `microphone` | Settings > General > Microphone | row key, NOT in schema | settings/#microphone |
| `microphone_name` | Settings > General > Microphone | row key, NOT in schema | settings/#microphone_name |
| `streaming_mode` | Settings > General > Streaming capture (Advanced) | row key, NOT in schema | settings/#streaming_mode |
| `command_mode.command_matching_enabled` | Settings > Dictation > Commands while dictating | row key, NOT in schema | settings/#command_mode.command_matching_enabled |
| `wake_word_config.phrase` | Settings > Dictation > Wake phrase (Advanced) | row key, NOT in schema | settings/#wake_word_config.phrase |
| `command_mode.utterance_silence_s` | Settings > Dictation > Command pause (Advanced) | row key, NOT in schema | settings/#command_mode.utterance_silence_s |
| `continuous_commit_trigger` | Settings > Dictation > Continuous preset (Advanced) | row key, NOT in schema | settings/#continuous_commit_trigger |
| `command_mode.cancel_window_s` | Settings > Dictation > Cancel window (Advanced) | row key, NOT in schema | settings/#command_mode.cancel_window_s |
| `command_mode.cancel_window_all_commands` | Settings > Dictation > Cancel window (Advanced) | row key, NOT in schema | settings/#command_mode.cancel_window_all_commands |
| `output_device` | Settings > Audio > Output device | row key, NOT in schema | settings/#output_device |
| `output_device_name` | Settings > Audio > Output device | row key, NOT in schema | settings/#output_device_name |
| `output_device_hostapi` | Settings > Audio > Output device | row key, NOT in schema | settings/#output_device_hostapi |
| `command_mode.exit_earcon` | Settings > Audio > Session-end sound (Advanced) | row key, NOT in schema | settings/#command_mode.exit_earcon |
| `command_mode.tts_char_limit` | Settings > Audio > Spoken reply limit (Advanced) | row key, NOT in schema | settings/#command_mode.tts_char_limit |
| `ava_mode_enabled` | Settings > Ava > Ava runs | row key, NOT in schema | settings/#ava_mode_enabled |
| `command_packs.ai` | Settings > Ava > Ava runs | row key, NOT in schema | settings/#command_packs.ai |
| `cloud_llm.api_key` | Settings > Ava > Cloud credential (Advanced) | row key, NOT in schema | settings/#cloud_llm.api_key |
| `cloud_llm.model` | Settings > Ava > Cloud model override (Advanced) | row key, NOT in schema | settings/#cloud_llm.model |
| `hotkey` | Settings > Shortcuts > Primary dictation | row key, NOT in schema | settings/#hotkey |
| `ava_mode_key` | Settings > Shortcuts > Ava held key | row key, NOT in schema | settings/#ava_mode_key |
| `cancel_hotkey` | Settings > Shortcuts > Cancel | row key, NOT in schema | settings/#cancel_hotkey |
| `undo_hotkey` | Settings > Shortcuts > Undo | row key, NOT in schema | settings/#undo_hotkey |
| `dictate_commit_hotkey` | Settings > Shortcuts > Paste staged thought | row key, NOT in schema | settings/#dictate_commit_hotkey |
| `memo_hotkey` | Settings > Shortcuts > Quick memo | row key, NOT in schema | settings/#memo_hotkey |
| `hotkeys.capture_correction` | Settings > Shortcuts > Corrections | row key, NOT in schema | settings/#hotkeys.capture_correction |
| `alarms.complete_hotkey` | Settings > Shortcuts > Complete alarm | row key, NOT in schema | settings/#alarms.complete_hotkey |
| `alarms.dismiss_hotkey` | Settings > Shortcuts > Dismiss alarm | row key, NOT in schema | settings/#alarms.dismiss_hotkey |
| `command_hotkey` | Settings > Shortcuts > Command-only key (Advanced) | row key, NOT in schema | settings/#command_hotkey |
| `wake_word_hotkey` | Settings > Shortcuts > Wake toggle (Advanced) | row key, NOT in schema | settings/#wake_word_hotkey |
| `continuous_hotkey` | Settings > Shortcuts > Continuous toggle (Advanced) | row key, NOT in schema | settings/#continuous_hotkey |
| `continuous_commit_hotkey` | Settings > Shortcuts > Continuous commit (Advanced) | row key, NOT in schema | settings/#continuous_commit_hotkey |
| `correction_hotkey` | Settings > Shortcuts > Correction report shortcut (Advanced) | row key, NOT in schema | settings/#correction_hotkey |
| `command_packs.core` | Tools > command-library | row key, NOT in schema | settings/#command_packs.core |
| `command_packs.session` | Tools > command-library | row key, NOT in schema | settings/#command_packs.session |
| `command_packs.experimental` | Tools > command-library | row key, NOT in schema | settings/#command_packs.experimental |
| `command_packs.text-editing` | Tools > command-library | row key, NOT in schema | settings/#command_packs.text-editing |
| `command_packs.window-management` | Tools > command-library | row key, NOT in schema | settings/#command_packs.window-management |
| `command_packs.window-cube-numbers` | Tools > command-library | row key, NOT in schema | settings/#command_packs.window-cube-numbers |
| `command_packs.browsers` | Tools > command-library | row key, NOT in schema | settings/#command_packs.browsers |
| `command_packs.word` | Tools > command-library | row key, NOT in schema | settings/#command_packs.word |
| `command_packs.media` | Tools > command-library | row key, NOT in schema | settings/#command_packs.media |
| `command_packs.smart-home` | Tools > command-library | row key, NOT in schema | settings/#command_packs.smart-home |
| `command_packs.3d-printing` | Tools > command-library | row key, NOT in schema | settings/#command_packs.3d-printing |
| `command_packs.stremio` | Tools > command-library | row key, NOT in schema | settings/#command_packs.stremio |
| `command_packs.screen-capture` | Tools > command-library | row key, NOT in schema | settings/#command_packs.screen-capture |
| `command_packs.macros` | Tools > command-library | row key, NOT in schema | settings/#command_packs.macros |
| `command_packs.gaming` | Tools > command-library | row key, NOT in schema | settings/#command_packs.gaming |
| `command_packs.audio` | Tools > command-library | row key, NOT in schema | settings/#command_packs.audio |
| `command_packs.utilities` | Tools > command-library | row key, NOT in schema | settings/#command_packs.utilities |
| `command_packs.smart-actions` | Tools > command-library | row key, NOT in schema | settings/#command_packs.smart-actions |
| `command_packs.tasks` | Tools > command-library | row key, NOT in schema | settings/#command_packs.tasks |
| `command_packs.health` | Tools > command-library | row key, NOT in schema | settings/#command_packs.health |
| `command_packs.alarms` | Tools > command-library | row key, NOT in schema | settings/#command_packs.alarms |
| `command_packs.accessibility` | Tools > command-library | row key, NOT in schema | settings/#command_packs.accessibility |
| `command_packs.discord` | Tools > command-library | row key, NOT in schema | settings/#command_packs.discord |
| `gesture.enabled` | Tools > components | row key, NOT in schema | settings/#gesture.enabled |
