# Synthetic settings help decisions

| `comfy.base_url` | DROP | The setting is self-explanatory or configured outside Settings. |
| `comfy.default_workflow` | DROP | The setting is self-explanatory or configured outside Settings. |
| `comfy.workflows` | DROP | The setting is self-explanatory or configured outside Settings. |
| `comfy.prompt_prefix` | DROP | The setting is self-explanatory or configured outside Settings. |
| `comfy.prompt_suffix` | DROP | The setting is self-explanatory or configured outside Settings. |
| `comfy.auto_open` | DROP | The setting is self-explanatory or configured outside Settings. |
| `comfy.timeout_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `queue_voice.queue_root` | DROP | The setting is self-explanatory or configured outside Settings. |
| `queue_voice.reports_root` | DROP | The setting is self-explanatory or configured outside Settings. |
| `queue_voice.python` | DROP | The setting is self-explanatory or configured outside Settings. |
| `dispatch_voice.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `dispatch_voice.poll_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `agent_channel.cwd` | DROP | The setting is self-explanatory or configured outside Settings. |
| `agent_channel.allow_writes` | DROP | The setting is self-explanatory or configured outside Settings. |
| `agent_channel.voice_preamble` | DROP | The setting is self-explanatory or configured outside Settings. |
| `agent_channel.timeout_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `agent_voice.digest` | DROP | Config-only choice of a local spoken digest or a word cut for long agent replies. |
| `agent_voice.digest_budget_s` | DROP | Config-only maximum wait for the local spoken digest before using the word cut. |
| `agent_voice.digest_model` | DROP | Config-only local digest model override; empty uses Ava's Ollama model. |
| `claude_voice.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `claude_voice.spoken_word_cap` | DROP | The setting is self-explanatory or configured outside Settings. |
| `claude_voice.reply_timeout_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `chatgpt_voice.enabled` | DROP | Config-only override for reading desktop ChatGPT replies aloud. |
| `chatgpt_voice.spoken_word_cap` | DROP | Config-only maximum words per spoken ChatGPT reply chunk. |
| `chatgpt_voice.reply_timeout_s` | DROP | Config-only maximum wait for a desktop ChatGPT reply. |
| `chatgpt_voice.settle_s` | DROP | Config-only duration of unchanged ChatGPT reply text before completion. |
| `tasks.todoist.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `tasks.todoist.api_token` | DROP | The setting is self-explanatory or configured outside Settings. |
| `wake_profiles` | KEEP | Contextual help is expected for this setting. |
| `model_size` | KEEP | Contextual help is expected for this setting. |
| `language` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.theme` | DROP | The setting is self-explanatory or configured outside Settings. |
| `auto_paste` | KEEP | Contextual help is expected for this setting. |
| `add_trailing_space` | DROP | The setting is self-explanatory or configured outside Settings. |
| `auto_capitalize` | DROP | The setting is self-explanatory or configured outside Settings. |
| `format_numbers` | DROP | The setting is self-explanatory or configured outside Settings. |
| `hints_enabled` | KEEP | Contextual help is expected for this setting. |
| `launch_at_login` | DROP | The setting is self-explanatory or configured outside Settings. |
| `start_hidden` | DROP | The setting is self-explanatory or configured outside Settings. |
| `updates.automatic_checks` | DROP | The setting is self-explanatory or configured outside Settings. |
| `formatting_tokens.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `accessibility.ava_captions` | DROP | The setting is self-explanatory or configured outside Settings. |
| `accessibility.ava_mute_audio` | DROP | The setting is self-explanatory or configured outside Settings. |
| `accessibility.ava_captions_linger_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `accessibility.ava_captions_position` | DROP | The setting is self-explanatory or configured outside Settings. |
| `cleanup_mode` | KEEP | Contextual help is expected for this setting. |
| `dictation.signoff_filter` | DROP | The setting is self-explanatory or configured outside Settings. |
| `mode` | DROP | The setting is self-explanatory or configured outside Settings. |
| `wake_word_enabled` | KEEP | Contextual help is expected for this setting. |
| `threshold_mode` | KEEP | Contextual help is expected for this setting. |
| `cal_multiplier` | KEEP | Contextual help is expected for this setting. |
| `wake_word_config.audio.wake_command_timeout` | DROP | The setting is self-explanatory or configured outside Settings. |
| `wake_word_config.quick_silence_timeout` | DROP | The setting is self-explanatory or configured outside Settings. |
| `wake_word_config.opens_session` | DROP | The setting is self-explanatory or configured outside Settings. |
| `wake_word_config.oww_threshold` | KEEP | Contextual help is expected for this setting. |
| `command.trigger_mode` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command.button` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.enabled` | KEEP | Contextual help is expected for this setting. |
| `command_mode.button` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.mode` | KEEP | Contextual help is expected for this setting. |
| `command_mode.suppress_button` | KEEP | Contextual help is expected for this setting. |
| `command_mode.enter_debounce_ms` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.abort_phrases` | KEEP | Contextual help is expected for this setting. |
| `command_mode.dictate_commit_word` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.stop_phrases` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.inactivity_timeout_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.dictate_utterance_silence_s` | KEEP | Contextual help is expected for this setting. |
| `command_mode.miss_limit` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.personal_alias_offer_timeout_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.session_streaming_preview` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.preview_idle_delay_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.preview_idle_opacity` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.preview_position` | DROP | The setting is self-explanatory or configured outside Settings. |
| `command_mode.ava_tts_half_duplex` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_command_session.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_command_session.backend` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_command_session.model` | KEEP | Contextual help is expected for this setting. |
| `ava_command_session.shortlist_size` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_command_session.keep_warm` | KEEP | Contextual help is expected for this setting. |
| `ava_command_session.interpret_misses` | KEEP | Contextual help is expected for this setting. |
| `click.type` | DROP | The setting is self-explanatory or configured outside Settings. |
| `click.button` | DROP | The setting is self-explanatory or configured outside Settings. |
| `audio_feedback` | DROP | The setting is self-explanatory or configured outside Settings. |
| `sound_volume` | DROP | The setting is self-explanatory or configured outside Settings. |
| `sound_theme` | DROP | The setting is self-explanatory or configured outside Settings. |
| `feedback.spoken_notices` | DROP | The setting is self-explanatory or configured outside Settings. |
| `tts.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `tts.self_hear_grace_ms` | DROP | The setting is self-explanatory or configured outside Settings. |
| `tts.engine` | KEEP | Contextual help is expected for this setting. |
| `tts.voice_id` | DROP | The setting is self-explanatory or configured outside Settings. |
| `tts.speed` | DROP | The setting is self-explanatory or configured outside Settings. |
| `tts.rate` | DROP | The setting is self-explanatory or configured outside Settings. |
| `tts.volume` | DROP | The setting is self-explanatory or configured outside Settings. |
| `audio_coordinator.duck_factor` | DROP | The setting is self-explanatory or configured outside Settings. |
| `alarms.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `alarms.nag_interval_seconds` | DROP | The setting is self-explanatory or configured outside Settings. |
| `device` | KEEP | Contextual help is expected for this setting. |
| `compute_type` | DROP | The setting is self-explanatory or configured outside Settings. |
| `performance_mode` | KEEP | Contextual help is expected for this setting. |
| `silence_threshold` | DROP | The setting is self-explanatory or configured outside Settings. |
| `min_speech_duration` | DROP | The setting is self-explanatory or configured outside Settings. |
| `accessibility.speech_pace` | DROP | The setting is self-explanatory or configured outside Settings. |
| `wake_word_config.audio.speech_threshold` | DROP | The setting is self-explanatory or configured outside Settings. |
| `echo_cancellation.enabled` | KEEP | Contextual help is expected for this setting. |
| `echo_cancellation.latency_ms` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ducking.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ducking.recording_mode` | KEEP | Contextual help is expected for this setting. |
| `ducking.level` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ducking.hands_free_enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ducking.hands_free_level` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ducking.hands_free_idle_level` | DROP | The setting is self-explanatory or configured outside Settings. |
| `listening_indicator_enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.idle_animation` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.enabled` | KEEP | Contextual help is expected for this setting. |
| `ui.live_surface.schema_version` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.idle_delay_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.show_idle_mark` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.partials` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.focus_hotkey` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.reset_hotkey` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.pause_hotkey` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.placement` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ui.live_surface.legacy` | DROP | The setting is self-explanatory or configured outside Settings. |
| `listening_indicator_position` | DROP | The setting is self-explanatory or configured outside Settings. |
| `audio.input_sensitivity` | DROP | The setting is self-explanatory or configured outside Settings. |
| `transcription.mode` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_edit.enabled` | KEEP | Contextual help is expected for this setting. |
| `ava_edit.demo_pacing` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_edit.model` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_edit.timeout_seconds` | DROP | The setting is self-explanatory or configured outside Settings. |
| `smart_corrections.enabled` | KEEP | Contextual help is expected for this setting. |
| `smart_corrections.ollama_model` | DROP | The setting is self-explanatory or configured outside Settings. |
| `smart_corrections.timeout_s` | DROP | The setting is self-explanatory or configured outside Settings. |
| `smart_corrections.min_words` | DROP | The setting is self-explanatory or configured outside Settings. |
| `smart_corrections.keep_alive` | DROP | The setting is self-explanatory or configured outside Settings. |
| `smart_corrections.modes.hotkey` | DROP | The setting is self-explanatory or configured outside Settings. |
| `smart_corrections.modes.wake` | DROP | The setting is self-explanatory or configured outside Settings. |
| `smart_corrections.modes.streaming` | DROP | The setting is self-explanatory or configured outside Settings. |
| `smart_corrections.repair_disfluencies` | DROP | The setting is self-explanatory or configured outside Settings. |
| `diagnostics.write_jsonl` | KEEP | Contextual help is expected for this setting. |
| `diagnostics.stage_timing` | DROP | The setting is self-explanatory or configured outside Settings. |
| `debug.dump_hotkey_buffers` | DROP | The setting is self-explanatory or configured outside Settings. |
| `benchmark.collect_samples` | KEEP | Contextual help is expected for this setting. |
| `benchmark.max_samples` | DROP | The setting is self-explanatory or configured outside Settings. |
| `correction_capture.max_edit_ratio` | DROP | The setting is self-explanatory or configured outside Settings. |
| `intent.shadow_enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `intent.shadow_retention_days` | DROP | The setting is self-explanatory or configured outside Settings. |
| `intent.command_prefix` | KEEP | Contextual help is expected for this setting. |
| `cloud_llm.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `cloud_llm.provider` | KEEP | Contextual help is expected for this setting. |
| `cloud_llm.timeout_seconds` | DROP | The setting is self-explanatory or configured outside Settings. |
| `cloud_llm.web_search` | KEEP | Contextual help is expected for this setting. |
| `ava_personality` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava.warm_on_boot` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava.consent` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava.provider_policy` | KEEP | Contextual help is expected for this setting. |
| `ava.catalog_answers` | DROP | The setting is self-explanatory or configured outside Settings. |
| `onboarding.version` | DROP | The setting is self-explanatory or configured outside Settings. |
| `onboarding.setup_status` | DROP | The setting is self-explanatory or configured outside Settings. |
| `onboarding.next_step` | DROP | The setting is self-explanatory or configured outside Settings. |
| `onboarding.requested_ai` | DROP | The setting is self-explanatory or configured outside Settings. |
| `onboarding.lessons` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_invocations` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_memory.mode` | KEEP | Contextual help is expected for this setting. |
| `ava_memory.max_turns` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_memory.sessions_kept` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ava_memory.recall_sessions` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ollama.enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ollama.host` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ollama.model` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ollama.timeout_seconds` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ollama.max_response_length` | DROP | The setting is self-explanatory or configured outside Settings. |
| `ollama.safety_gate_enabled` | DROP | The setting is self-explanatory or configured outside Settings. |
| `music_library` | DROP | The setting is self-explanatory or configured outside Settings. |
| `diagnostics.watch_code_changes` | DROP | The setting is self-explanatory or configured outside Settings. |
