# Snapshot from reports/359/artifacts/help_decisions.md; source dated 2026-10-03.
# Copied 2026-10-08; only rows consumed by test_settings_help_359.py retained.

| `comfy.base_url` | DROP | No Settings control; Local ComfyUI server address; loopback only. |
| `comfy.default_workflow` | DROP | No Settings control; Name of an explicitly imported local image workflow. |
| `comfy.workflows` | DROP | No Settings control; Imported API graphs and their exact input mappings; configured by the workflow import tool. |
| `comfy.prompt_prefix` | DROP | No Settings control; Owner-editable text prepended verbatim to image prompts; empty by default. |
| `comfy.prompt_suffix` | DROP | No Settings control; Owner-editable text appended verbatim to image prompts; empty by default. |
| `comfy.auto_open` | DROP | No Settings control; Advanced image completion preference; open the image remains available by voice. |
| `comfy.timeout_s` | DROP | No Settings control; Local image rendering deadline in seconds. |
| `queue_voice.queue_root` | DROP | No Settings control; Owner-maintained prompt queue location; advanced config-only path. |
| `queue_voice.reports_root` | DROP | No Settings control; Owner-maintained prompt reports location; advanced config-only path. |
| `queue_voice.python` | DROP | No Settings control; Interpreter for the existing queue note script; advanced config-only path. |
| `dispatch_voice.enabled` | DROP | No Settings control; Agora voice dispatch and its event feed. |
| `dispatch_voice.poll_s` | DROP | No Settings control; Seconds between Agora dispatch feed checks. |
| `agent_channel.cwd` | DROP | No Settings control; Working directory for subscription CLI agents. |
| `agent_channel.allow_writes` | DROP | No Settings control; CLI agents are read-only by default. |
| `agent_channel.voice_preamble` | DROP | No Settings control; Spoken-response instruction sent with each CLI question. |
| `agent_channel.timeout_s` | DROP | No Settings control; Maximum seconds to wait for a CLI answer. |
| `claude_voice.enabled` | DROP | No Settings control; Desktop Claude reply reading is enabled by default; advanced config-only override. |
| `claude_voice.spoken_word_cap` | DROP | No Settings control; Advanced config-only limit for each spoken Claude reply chunk. |
| `claude_voice.reply_timeout_s` | DROP | No Settings control; Advanced config-only timeout for a desktop Claude reply. |
| `tasks.todoist.enabled` | DROP | No ordinary Settings page row to attach a button; the docs reference covers this key. |
| `tasks.todoist.api_token` | DROP | No ordinary Settings page row to attach a button; the docs reference covers this key. |
| `wake_profiles` | KEEP | Explains phrase, target, and trained-model dependency. |
| `model_size` | KEEP | Explains the accuracy and resource trade-off. |
| `language` | DROP | The row already says: Choose the language you dictate in; another panel would repeat it. |
| `ui.theme` | DROP | The row already says: Choose Samsara's appearance; another panel would repeat it. |
| `auto_paste` | KEEP | Corrects the row's misleading clipboard implication. |
| `add_trailing_space` | DROP | The row already says: Add a space after inserted dictation; another panel would repeat it. |
| `auto_capitalize` | DROP | The row already says: Capitalise dictated text automatically; another panel would repeat it. |
| `format_numbers` | DROP | The row already says: Format spoken numbers as digits; another panel would repeat it. |
| `hints_enabled` | KEEP | Explains the reset action and default. |
| `launch_at_login` | DROP | The row already says: Launch Samsara when you sign in to Windows; another panel would repeat it. |
| `start_hidden` | DROP | The row already says: Start in the tray with the main window hidden; another panel would repeat it. |
| `updates.automatic_checks` | DROP | The row already says: Check GitHub for updates; daily checks are optional; another panel would repeat it. |
| `formatting_tokens.enabled` | DROP | No Settings control; Config-only escape hatch for spoken formatting-token interpretation; normal dictation keeps formatting tokens enabled. |
| `accessibility.ava_captions` | DROP | The row already says: Show Ava's responses as text; another panel would repeat it. |
| `accessibility.ava_mute_audio` | DROP | The row already says: Silence Ava while keeping other speech available; another panel would repeat it. |
| `accessibility.ava_captions_linger_s` | DROP | No Settings control; Config-only caption timing override; the existing captions UI deliberately exposes enable and mute, not timing tuning. |
| `accessibility.ava_captions_position` | DROP | No Settings control; Placement state written by dragging the caption window; not a separate Settings preference. |
| `cleanup_mode` | KEEP | Explains processing choices and the default. |
| `dictation.signoff_filter` | DROP | No Settings control; Developer escape hatch for the default-on hallucinated-signoff filter during false-positive evaluation. |
| `mode` | DROP | The row already says: Choose the dictation key and its behaviour; another panel would repeat it. |
| `wake_word_enabled` | KEEP | Explains always-listening and model dependency. |
| `threshold_mode` | KEEP | Explains mode dependency and calibration trade-off. |
| `cal_multiplier` | KEEP | Explains direction and dependency. |
| `wake_word_config.audio.wake_command_timeout` | DROP | The timeout row states the behavior; the default belongs in the docs. |
| `wake_word_config.quick_silence_timeout` | DROP | The row already says: Adjust recognition silence timing; another panel would repeat it. |
| `wake_word_config.opens_session` | DROP | No Settings control; Config-only opt-in to the alternative latched wake route, which also requires command_mode.mode=toggle. |
| `wake_word_config.oww_threshold` | KEEP | Explains false-wake versus missed-wake trade-off. |
| `command.trigger_mode` | DROP | No Settings control; Phase 2A compatibility key; command_mode.mode is the current Settings control. |
| `command.button` | DROP | No Settings control; Phase 2A compatibility key; command_mode.button is the current Settings control. |
| `command_mode.enabled` | KEEP | Explains session behavior and shortcut dependency. |
| `command_mode.button` | DROP | The row already says: Choose the hands-free button and its behaviour; another panel would repeat it. |
| `command_mode.mode` | KEEP | Explains the non-obvious default and input behavior. |
| `command_mode.suppress_button` | KEEP | Explains browser side effect and default. |
| `command_mode.enter_debounce_ms` | DROP | The row already says: Avoid repeated activation from one press; another panel would repeat it. |
| `command_mode.abort_phrases` | KEEP | Explains phrase collision risk. |
| `command_mode.dictate_commit_word` | DROP | No Settings control; Config-only single-word session protocol override; changing the commit vocabulary needs the session token validation, not an unrestricted text field. |
| `command_mode.stop_phrases` | DROP | The stop-words row is self-explanatory; exit phrases retain collision guidance. |
| `command_mode.inactivity_timeout_s` | DROP | The session-timeout row already states the effect. |
| `command_mode.dictate_utterance_silence_s` | KEEP | Explains pacing trade-off. |
| `command_mode.miss_limit` | DROP | The row identifies the miss limit; a panel adds little beyond its value. |
| `command_mode.personal_alias_offer_timeout_s` | DROP | No Settings control; Config-only tuning for the specialist spoken alias-offer flow, separate from ordinary session inactivity. |
| `command_mode.session_streaming_preview` | DROP | No Settings control; Config-only legacy session-preview switch; separate from the dedicated CapsLock streaming-capture preference. |
| `command_mode.preview_idle_delay_s` | DROP | The row already says: Delay before the idle dictation preview fades; another panel would repeat it. |
| `command_mode.preview_idle_opacity` | DROP | The row already says: Choose how visible the idle preview stays; another panel would repeat it. |
| `command_mode.preview_position` | DROP | No Settings control; Placement state owned by the draggable preview and its Settings reset action. |
| `command_mode.ava_tts_half_duplex` | DROP | No Settings control; Developer override for echo suppression while Ava speaks; keep the default protection for ordinary use. |
| `ava_command_session.enabled` | DROP | The row already says: Choose Off, on this PC, or your cloud provider; another panel would repeat it. |
| `ava_command_session.backend` | DROP | The row already says: Choose Off, on this PC, or your cloud provider; another panel would repeat it. |
| `ava_command_session.model` | KEEP | Explains installed-model dependency. |
| `ava_command_session.shortlist_size` | DROP | No Settings control; Interpreter candidate-budget tuning; the supported user choice is whether command-miss interpretation is enabled. |
| `ava_command_session.keep_warm` | KEEP | Explains latency versus memory trade-off. |
| `ava_command_session.interpret_misses` | KEEP | Explains fallback and execution boundary. |
| `click.type` | DROP | No Settings control; Compatibility command metadata; actual click actions belong to command records, not global preferences. |
| `click.button` | DROP | No Settings control; Compatibility command metadata; actual mouse buttons belong to command records, not global preferences. |
| `audio_feedback` | DROP | The row already says: Hear signals for recording and actions; another panel would repeat it. |
| `sound_volume` | DROP | The row already says: Adjust feedback volume and test it; another panel would repeat it. |
| `sound_theme` | DROP | The row already says: Choose the feedback sound set; another panel would repeat it. |
| `feedback.spoken_notices` | DROP | The row already says: Choose which session notices are spoken; another panel would repeat it. |
| `tts.enabled` | DROP | The row already says: Let Samsara speak responses aloud; another panel would repeat it. |
| `tts.self_hear_grace_ms` | DROP | No Settings control; Capture interlock grace after Ava speech ends. |
| `tts.engine` | KEEP | Explains network and voice dependency. |
| `tts.voice_id` | DROP | The row already says: Choose a voice and hear a sample; another panel would repeat it. |
| `tts.speed` | DROP | The row already says: Adjust spoken response speed; another panel would repeat it. |
| `tts.rate` | DROP | No Settings control; Phase 2A compatibility name; tts.speed is the current speech-speed preference. |
| `tts.volume` | DROP | The row already says: Adjust speech independently of feedback sounds; another panel would repeat it. |
| `audio_coordinator.duck_factor` | DROP | The speech-during-feedback row already describes the overlap. |
| `alarms.enabled` | DROP | No ordinary Settings page row to attach a button; the docs reference covers this key. |
| `alarms.nag_interval_seconds` | DROP | The repeat-interval row already says when alarms replay. |
| `device` | KEEP | Explains hardware dependency and automatic default. |
| `compute_type` | DROP | The precision row already explains that the loader chooses actual precision. |
| `performance_mode` | KEEP | Explains speed and accuracy trade-off. |
| `silence_threshold` | DROP | The row already says: Silence before a continuous phrase completes; another panel would repeat it. |
| `min_speech_duration` | DROP | The row already says: Ignore speech shorter than this duration; another panel would repeat it. |
| `accessibility.speech_pace` | DROP | The speech-pace row explains the pause allowance; no separate panel is needed. |
| `wake_word_config.audio.speech_threshold` | DROP | The row already says: Set the speech threshold for manual calibration; another panel would repeat it. |
| `echo_cancellation.enabled` | KEEP | Explains hardware-dependent experimental behavior. |
| `echo_cancellation.latency_ms` | DROP | The echo-latency row already says it compensates measured playback delay. |
| `ducking.enabled` | DROP | No Settings control; Legacy fallback for configurations without ducking.recording_mode; the Audio recording-mode selector owns the current behavior. |
| `ducking.recording_mode` | KEEP | Explains side effect on other apps. |
| `ducking.level` | DROP | The row already says: Remaining other-app volume while recording; another panel would repeat it. |
| `ducking.hands_free_enabled` | DROP | The hands-free audio row already describes reduction of other apps. |
| `ducking.hands_free_level` | DROP | The row already says: Remaining other-app volume while capturing speech; another panel would repeat it. |
| `ducking.hands_free_idle_level` | DROP | The row already says: Remaining other-app volume while listening; another panel would repeat it. |
| `listening_indicator_enabled` | DROP | The row already says: Show when Samsara is listening; another panel would repeat it. |
| `ui.idle_animation` | DROP | No Settings control; Config-only animation override for the legacy listening indicator, separate from indicator visibility. |
| `ui.live_surface.enabled` | KEEP | Explains experimental display and default. |
| `ui.live_surface.schema_version` | DROP | No Settings control; Internal live-surface migration version, not a user preference. |
| `ui.live_surface.idle_delay_s` | DROP | No Settings control; Experimental live-surface tuning; its public Settings surface remains the opt-in switch until the replacement UI is released. |
| `ui.live_surface.show_idle_mark` | DROP | No Settings control; Experimental live-surface tuning; its public Settings surface remains the opt-in switch until the replacement UI is released. |
| `ui.live_surface.partials` | DROP | No Settings control; Experimental partial-transcription policy; not a second control for the existing streaming capture path. |
| `ui.live_surface.focus_hotkey` | DROP | No Settings control; Experimental live-surface binding; config-only until it joins the validated Settings shortcut/collision editor. |
| `ui.live_surface.reset_hotkey` | DROP | No Settings control; Experimental live-surface binding; config-only until it joins the validated Settings shortcut/collision editor. |
| `ui.live_surface.pause_hotkey` | DROP | No Settings control; Experimental live-surface binding; config-only until it joins the validated Settings shortcut/collision editor. |
| `ui.live_surface.placement` | DROP | No Settings control; Per-monitor geometry written by the live surface itself, not a text-editable preference. |
| `ui.live_surface.legacy` | DROP | No Settings control; Internal migration backup of previous floating-window settings; never a preference editor. |
| `listening_indicator_position` | DROP | The row already says: Choose the indicator's starting position; another panel would repeat it. |
| `audio.input_sensitivity` | DROP | No Settings control; Compatibility schema value with no current capture consumer; microphone calibration and speech threshold are the active controls. |
| `transcription.mode` | DROP | No Settings control; Compatibility schema value; cleanup_mode is the current dictation cleanup preference. |
| `ava_edit.enabled` | KEEP | Explains consent and model dependency. |
| `ava_edit.demo_pacing` | DROP | The row already says: Use slower editing animations for demonstrations; another panel would repeat it. |
| `ava_edit.model` | DROP | The row already says: Choose the local model for editing; another panel would repeat it. |
| `ava_edit.timeout_seconds` | DROP | The row already says: Limit how long an editing request can take; another panel would repeat it. |
| `smart_corrections.enabled` | KEEP | Explains extra pass, mode dependency, and delay. |
| `smart_corrections.ollama_model` | DROP | The row already says: Use this local model for automatic corrections; another panel would repeat it. |
| `smart_corrections.timeout_s` | DROP | No Settings control; Config-only correction-backend deadline tuning; retain the bounded runtime default for ordinary use. |
| `smart_corrections.min_words` | DROP | No Settings control; Config-only correction eligibility threshold; the visible controls choose correction modes and repair behavior. |
| `smart_corrections.keep_alive` | DROP | No Settings control; Config-only Ollama duration override; it accepts backend duration syntax, not an ordinary Settings on/off value. |
| `smart_corrections.modes.hotkey` | DROP | The row already says: Apply automatic corrections to held dictation; another panel would repeat it. |
| `smart_corrections.modes.wake` | DROP | The row already says: Apply automatic corrections to wake dictation; another panel would repeat it. |
| `smart_corrections.modes.streaming` | DROP | The row already says: Apply automatic corrections to streaming dictation; another panel would repeat it. |
| `smart_corrections.repair_disfluencies` | DROP | The row already says: Remove filler words and spoken self-corrections; another panel would repeat it. |
| `diagnostics.write_jsonl` | KEEP | Explains private-data side effect. |
| `diagnostics.stage_timing` | DROP | The row already says: Record how long each dictation step takes in a local file; another panel would repeat it. |
| `debug.dump_hotkey_buffers` | DROP | No Settings control; Developer-only raw audio capture for buffer investigations; deliberately excluded from everyday Settings. |
| `benchmark.collect_samples` | KEEP | Explains recorded-audio implication. |
| `benchmark.max_samples` | DROP | No Settings control; Config-only benchmark storage cap; Help exposes collection consent while retaining the bounded default cap. |
| `correction_capture.max_edit_ratio` | DROP | No Settings control; Config-only correction-training rejection threshold; not an ordinary text-editing preference. |
| `intent.shadow_enabled` | DROP | No Settings control; Developer-only shadow execution experiment that records dictated text without changing actions; kept config-only. |
| `intent.shadow_retention_days` | DROP | No Settings control; Retention tuning for the config-only shadow execution experiment, not general log retention. |
| `intent.command_prefix` | KEEP | Explains command/dictation trade-off. |
| `cloud_llm.enabled` | DROP | The row already says: Choose Off, on this PC, or your cloud provider; another panel would repeat it. |
| `cloud_llm.provider` | KEEP | Explains cloud routing and dependency. |
| `cloud_llm.timeout_seconds` | DROP | The row already says: Limit how long a cloud request can take; another panel would repeat it. |
| `cloud_llm.web_search` | KEEP | Explains network and provider implications. |
| `ava_personality` | DROP | The row already says: Choose the style of Ava's answers; another panel would repeat it. |
| `ava.warm_on_boot` | DROP | The warm-at-startup row already explains the startup request. |
| `ava.consent` | DROP | The row already says: Choose Off, on this PC, or your cloud provider; another panel would repeat it. |
| `ava.provider_policy` | KEEP | Explains route, privacy, and consent dependency. |
| `ava.catalog_answers` | DROP | No Settings control; Local answers to command catalog questions. |
| `onboarding.version` | DROP | No Settings control; Setup workflow version owned by onboarding migrations. |
| `onboarding.setup_status` | DROP | No Settings control; Setup progress owned by the first-run workflow; not a manually editable readiness claim. |
| `onboarding.next_step` | DROP | No Settings control; Resume location owned by the first-run workflow. |
| `onboarding.requested_ai` | DROP | No Settings control; Pending provider choice owned by setup; the Ava page owns the active consent-aware provider policy. |
| `onboarding.lessons` | DROP | No Settings control; Practice progress recorded by onboarding lessons; not a free-form Settings dictionary. |
| `ava_invocations` | DROP | No Settings control; Config-only whole-utterance invocation vocabulary; kept outside Settings until an editor can validate session phrase collisions. |
| `ava_memory.mode` | KEEP | Explains retention trade-off and persistence. |
| `ava_memory.max_turns` | DROP | The remembered-turns row already describes its context cap. |
| `ava_memory.sessions_kept` | DROP | No Settings control; Config-only limit for dated summaries in assistant memory. |
| `ava_memory.recall_sessions` | DROP | No Settings control; Config-only limit for summaries recalled in a reply. |
| `ollama.enabled` | DROP | No Settings control; Legacy provider flag; the Ava master and consent-aware provider policy own current activation. |
| `ollama.host` | DROP | No Settings control; Config-only local-server endpoint override; normal setup uses the local default and provider consent checks. |
| `ollama.model` | DROP | No Settings control; Local conversational model selection is owned by the Ava setup guide; distinct from the command-miss model in Settings. |
| `ollama.timeout_seconds` | DROP | No Settings control; Config-only local conversational request deadline; separate from cloud and editing timeouts. |
| `ollama.max_response_length` | DROP | No Settings control; Config-only conversational response-length cap; not the spoken acknowledgement character limit. |
| `ollama.safety_gate_enabled` | DROP | No Settings control; Developer compatibility override for local-model safety checks; not an ordinary user preference. |
| `music_library` | DROP | No ordinary Settings page row to attach a button; the docs reference covers this key. |
| `diagnostics.watch_code_changes` | DROP | No ordinary Settings page row to attach a button; the docs reference covers this key. |
