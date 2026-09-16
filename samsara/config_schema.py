"""
Machine-readable settings schema for Samsara.

Extracted from samsara/ui/settings_qt.py widget parameters. Importable without
instantiating the Qt UI -- no Qt imports here.

Format per entry:
    type:       'int' | 'float' | 'bool' | 'str' | 'enum' | 'list'
    min, max:   numeric bounds (int/float only)
    step:       increment step (numeric, optional)
    options:    allowed values list (enum only)
    item_type:  element type for 'list' entries (currently always 'str')
    default:    app default when key absent from config
    tab:        settings tab where this appears
    depends_on: string condition for cross-field dependency (optional)
                e.g. "echo_cancellation.enabled" means the setting is
                only active when that key is True.

'list' entries (e.g. ava_invocations) have no settings-UI widget yet --
config-file-editable only. Most other list-valued config (wake_abort_phrase)
is intentionally NOT represented here at all; it lives as an inline default
at its point of use instead, which remains the pattern for anything that
doesn't need this schema's cross-field/AI-capability introspection.

Cross-field dependencies use one of two forms:
    "some.key"            -- active when some.key is truthy
    "some.key=='value'"   -- active when some.key equals 'value'
"""

SETTINGS_SCHEMA = {
    # -------------------------------------------------------------------------
    # General tab
    # -------------------------------------------------------------------------
    "model_size": {
        "type": "enum",
        "options": ["tiny", "tiny.en", "base", "base.en", "small", "small.en",
                    "medium", "medium.en", "large-v3"],
        "default": "base",
        "tab": "general",
    },
    "language": {
        "type": "str",
        "default": "en",
        "tab": "general",
    },
    # Queue 129. Dark is not universally accessible: astigmatism, some
    # low-vision conditions and a bright room all read light-on-dark worse,
    # not better, so an accessibility tool with one forced theme has a gap.
    #   "dark"    the shipped palette, unchanged. DEFAULT.
    #   "light"   the same tokens with the polarity flipped.
    #   "system"  follow Windows' own app-theme setting.
    # Resolved by samsara.ui.theme.resolve_theme() and applied at startup;
    # see the Settings control, which says out loud that a change lands on
    # restart rather than leaving half the windows on the old palette.
    "ui.theme": {
        "type": "enum",
        "options": ["dark", "light", "system"],
        "default": "dark",
        "tab": "general",
    },
    "auto_paste":           {"type": "bool", "default": True,  "tab": "general"},
    "add_trailing_space":   {"type": "bool", "default": True,  "tab": "general"},
    "auto_capitalize":      {"type": "bool", "default": True,  "tab": "general"},
    "format_numbers":       {"type": "bool", "default": True,  "tab": "general"},
    "hints_enabled":        {"type": "bool", "default": True,  "tab": "general"},
    "updates.automatic_checks": {
        "type": "bool",
        "default": False,
        "tab": "general",
    },
    "formatting_tokens.enabled": {"type": "bool", "default": True, "tab": "general"},
    "cleanup_mode": {
        "type": "enum",
        "options": ["clean", "verbatim"],
        "default": "clean",
        "tab": "general",
    },

    # -------------------------------------------------------------------------
    # Hotkeys tab
    # -------------------------------------------------------------------------
    "mode": {
        "type": "enum",
        "options": ["hold", "toggle", "continuous"],
        "default": "hold",
        "tab": "hotkeys",
    },
    "wake_word_enabled": {"type": "bool", "default": False, "tab": "hotkeys"},
    "threshold_mode": {
        "type": "enum",
        "options": ["auto", "manual"],
        "default": "auto",
        "tab": "hotkeys",
    },
    "cal_multiplier": {
        "type": "float",
        "min": 1.0,
        "max": 10.0,
        "step": 0.1,
        "default": 3.0,
        "tab": "hotkeys",
    },
    "wake_word_config.audio.wake_command_timeout": {
        "type": "float",
        "min": 1.0,
        "max": 30.0,
        "step": 0.5,
        "default": 5.0,
        "tab": "hotkeys",
        "depends_on": "wake_word_enabled",
    },
    "wake_word_config.quick_silence_timeout": {
        "type": "float",
        "min": 0.2,
        "max": 5.0,
        "step": 0.1,
        "default": 1.0,
        "tab": "hotkeys",
        "depends_on": "wake_word_enabled",
    },
    "wake_word_config.opens_session": {
        # True: a wake-word hit opens the latched hands-free session (the
        # same entry the command-mode toggle tap uses) instead of the
        # one-command wake window. Needs command_mode.mode 'toggle' -- the
        # latched session only exists there; otherwise ignored (logged).
        # No settings-UI widget yet (config-file-editable); see
        # docs/WAKE_WORD_GUIDE.md "Wake phrase opens the hands-free session".
        "type": "bool",
        "default": False,
        "tab": "hotkeys",
        "depends_on": "wake_word_enabled",
    },
    "wake_word_config.oww_threshold": {
        "type": "float",
        "min": 0.05,
        "max": 1.0,
        "step": 0.05,
        "default": 0.20,
        "tab": "hotkeys",
        "depends_on": "wake_word_enabled",
    },

    # command.trigger_mode and command.button kept for Phase 2A compatibility;
    # command_mode.* are the canonical keys from the Commands tab.
    "command.trigger_mode": {
        "type": "enum",
        "options": ["hold", "toggle", "continuous"],
        "default": "hold",
        "tab": "hotkeys",
    },

    # -------------------------------------------------------------------------
    # Commands tab
    # -------------------------------------------------------------------------
    "command.button": {
        "type": "enum",
        "options": ["mouse4", "mouse5", "rctrl", "lctrl", "ralt", "lalt", "rshift", "lshift"],
        "default": "rctrl",
        "tab": "commands",
    },
    "command_mode.enabled": {"type": "bool", "default": False, "tab": "commands"},
    "command_mode.button": {
        "type": "enum",
        "options": ["mouse4", "mouse5", "rctrl", "f13", "right_alt"],
        "default": "rctrl",
        "tab": "commands",
    },
    "command_mode.mode": {
        "type": "enum",
        "options": ["hold", "toggle"],
        "default": "hold",
        "tab": "commands",
    },
    "command_mode.suppress_button": {"type": "bool", "default": True, "tab": "commands"},
    "command_mode.enter_debounce_ms": {
        "type": "int",
        "min": 0,
        "max": 2000,
        "step": 50,
        "default": 200,
        "tab": "commands",
    },
    "command_mode.abort_phrases": {
        # Extra whole-utterance phrases that end the latched hands-free
        # session exactly like the built-in sleep phrases
        # (session_modes.SESSION_SLEEP_PHRASES: "go to sleep", "samsara
        # sleep", "sleep now"): any lane, staged draft retained. Adds to the
        # built-in list; it cannot remove from it. Config-file-editable only.
        "type": "list",
        "item_type": "str",
        "default": [],
        "tab": "commands",
    },
    "command_mode.stop_phrases": {
        # Queue 116. The emergency stop's whole-utterance phrases. REPLACES
        # session_modes.SESSION_STOP_PHRASES rather than adding to it, so a
        # word can be changed and not only appended -- if one of the defaults
        # transcribes badly on this machine the owner swaps it without a
        # release. Saying one of these advances the execution generation,
        # cancels the answer being spoken, the staged confirmation, both Ava
        # queues and the schedule; it keeps the draft, the mode and the
        # microphone. It is not an abort and not a sleep.
        # Empty or unusable falls back to the built-ins: a typo here must not
        # be a way to lose the stop.
        "type": "list",
        "item_type": "str",
        "default": ["halt", "cease"],
        "tab": "commands",
    },
    "command_mode.inactivity_timeout_s": {
        "type": "int",
        "min": 5,
        "max": 1800,
        "step": 5,
        "default": 300,
        "tab": "commands",
    },
    "command_mode.dictate_utterance_silence_s": {
        "type": "float",
        "min": 0.3,
        "max": 3.0,
        "step": 0.05,
        "default": 0.65,
        "tab": "commands",
    },
    "command_mode.miss_limit": {
        "type": "int",
        "min": 1,
        "max": 20,
        "step": 1,
        "default": 5,
        "tab": "commands",
    },
    # Live streaming-partials overlay preview for the toggle-DICTATE lane
    # (hands-free session). Preview only -- injection stays per-utterance
    # finals on the silence boundary; see samsara/streaming.py's
    # DictatePreviewSession and dictation.py's _ensure_streaming_preview.
    "command_mode.session_streaming_preview": {
        "type": "bool",
        "default": True,
        "tab": "commands",
        "depends_on": "command_mode.enabled",
    },
    # Queue 75: that preview fades to this opacity after this many seconds
    # with no speech and no new text, and lets clicks through while faded.
    # 0.0 is "fully hidden" -- an explicit choice, never the default. Modes
    # tab controls; samsara/streaming.py IdleSettings reads them.
    "command_mode.preview_idle_delay_s": {
        "type": "float",
        "min": 1.0,
        "max": 60.0,
        "step": 0.5,
        "default": 5.0,
        "tab": "commands",
    },
    "command_mode.preview_idle_opacity": {
        "type": "float",
        "min": 0.0,
        "max": 0.9,
        "step": 0.05,
        "default": 0.25,
        "tab": "commands",
    },
    # Ava Front Door P1: D3 command-first latched session (waterfall
    # resolver -- exact/alias match, then ACTION2 grammar, then one LLM
    # fallback pass). Replaces the deleted ai_command_mode.* block; see
    # dictation.py's _migrate_ai_command_mode_config for the one-time
    # key-carryover migration from the old block.
    "ava_command_session.enabled": {"type": "bool", "default": True, "tab": "commands"},
    "ava_command_session.backend": {
        "type": "enum",
        "options": ["ollama", "cloud"],
        "default": "ollama",
        "tab": "commands",
    },
    "ava_command_session.model": {
        "type": "str",
        "default": "llama3.2:3b",
        "tab": "commands",
    },
    "ava_command_session.queue_depth_cap": {
        "type": "int",
        "min": 1,
        "max": 10,
        "step": 1,
        "default": 3,
        "tab": "commands",
    },
    "ava_command_session.miss_limit": {
        "type": "int",
        "min": 1,
        "max": 20,
        "step": 1,
        "default": 3,
        "tab": "commands",
    },
    "ava_command_session.inactivity_timeout_s": {
        "type": "int",
        "min": 5,
        "max": 1800,
        "step": 5,
        "default": 60,
        "tab": "commands",
    },
    "ava_command_session.shortlist_size": {
        "type": "int",
        "min": 1,
        "max": 50,
        "step": 1,
        "default": 12,
        "tab": "commands",
    },
    "ava_command_session.keep_warm": {"type": "bool", "default": True, "tab": "commands"},
    "ava_command_session.ready_cue_enabled": {"type": "bool", "default": True, "tab": "commands"},
    "ava_command_session.ready_cue_dir": {
        "type": "str",
        "default": "assets/sounds/ava_cues",
        "tab": "commands",
    },

    "click.type": {
        "type": "enum",
        "options": ["click", "double_click"],
        "default": "click",
        "tab": "commands",
    },
    "click.button": {
        "type": "enum",
        "options": ["left", "right", "middle"],
        "default": "left",
        "tab": "commands",
    },

    # -------------------------------------------------------------------------
    # Sounds tab
    # -------------------------------------------------------------------------
    "audio_feedback": {"type": "bool", "default": True, "tab": "sounds"},
    "sound_volume": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.5,
        "tab": "sounds",
    },
    "sound_theme": {
        "type": "enum",
        "options": ["cute", "warm", "zen", "classic", "chirpy"],
        "default": "cute",
        "tab": "sounds",
    },
    # Queue 103. Which of the session's own spoken notices are actually
    # spoken. Nothing in this app talks except Ava, so a voice arriving after
    # a MOUSE CLICK reads as a malfunction -- the incident was clicking
    # "Clear draft" and being told out loud to say "bring back my draft",
    # a sentence the chip was already showing.
    #
    #   "questions"   -- the app speaks only when it is WAITING on an answer
    #                    (the clear-draft yes/no, the spelling prompt). What
    #                    it has already done is left to the chip. DEFAULT.
    #   "everything"  -- today's behaviour, kept for a user who cannot see
    #                    the chip at all. This is the accessibility escape
    #                    hatch and must never be removed.
    #
    # There is deliberately no "off". See the queue 103 report: neither
    # QUESTION-class prompt carries an earcon, and the spelling prompt has no
    # outcome chip at all, so "off" would leave the app waiting on an answer
    # it had given no sign of wanting. Adding earcons is out of scope here,
    # so the value is not offered rather than shipped as a trap.
    "feedback.spoken_notices": {
        "type": "enum",
        "options": ["questions", "everything"],
        "default": "questions",
        "tab": "sounds",
    },

    # -------------------------------------------------------------------------
    # TTS tab
    # -------------------------------------------------------------------------
    "tts.enabled":  {"type": "bool", "default": False, "tab": "tts"},
    "tts.engine": {
        "type": "enum",
        "options": ["winrt", "edge"],
        "default": "winrt",
        "tab": "tts",
    },
    "tts.speed": {
        "type": "float",
        "min": 0.5,
        "max": 2.0,
        "step": 0.1,
        "default": 1.0,
        "tab": "tts",
    },
    # tts.rate: Phase 2A name; same widget bounds as tts.speed but wider range
    # per the original schema entry. Kept for backward compatibility.
    "tts.rate": {
        "type": "float",
        "min": 0.2,
        "max": 5.0,
        "default": 1.0,
        "tab": "tts",
    },
    "tts.pitch": {
        "type": "float",
        "min": 0.5,
        "max": 2.0,
        "step": 0.1,
        "default": 1.0,
        "tab": "tts",
    },
    "tts.volume": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.8,
        "tab": "tts",
    },
    "tts.use_for_agent_responses":   {"type": "bool", "default": True,  "tab": "tts"},
    "tts.use_for_confirmations":     {"type": "bool", "default": True,  "tab": "tts"},
    "tts.use_for_warnings":          {"type": "bool", "default": True,  "tab": "tts"},
    "tts.use_for_status_updates":    {"type": "bool", "default": True,  "tab": "tts"},
    "tts.use_for_dictation_readback":{"type": "bool", "default": False, "tab": "tts"},
    "tts.use_for_errors":            {"type": "bool", "default": True,  "tab": "tts"},
    "audio_coordinator.enabled":     {"type": "bool", "default": True,  "tab": "tts"},
    "audio_coordinator.duck_factor": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "default": 0.7,
        "tab": "tts",
        "depends_on": "audio_coordinator.enabled",
    },

    # -------------------------------------------------------------------------
    # Alarms tab
    # -------------------------------------------------------------------------
    "alarms.enabled": {"type": "bool", "default": True, "tab": "alarms"},
    "alarms.nag_interval_seconds": {
        "type": "int",
        "min": 15,
        "max": 300,
        "step": 15,
        "default": 60,
        "tab": "alarms",
    },

    # -------------------------------------------------------------------------
    # Advanced tab
    # -------------------------------------------------------------------------
    # Queue 124 (documentation defect found by 118): this said `cpu` while the
    # app's own defaults ship `device: "auto"` (dictation.py:3512), which
    # resolves to CUDA when ctranslate2 reports it (:5587). The app's default
    # wins at runtime, so the schema was describing a first run that never
    # happens. Corrected to state what actually ships. The resolution logic is
    # UNCHANGED -- this is documentation, not behaviour. compute_type is also
    # derived there ("float16" if cuda else "int8", :5601) and is NOT read
    # from this schema for the main model load; see the queue 124 report.
    "device": {
        "type": "enum",
        "options": ["auto", "cpu", "cuda"],
        "default": "auto",
        "tab": "advanced",
    },
    "compute_type": {
        "type": "enum",
        "options": ["float16", "int8", "float32"],
        "default": "float16",
        "tab": "advanced",
    },
    # Queue 124: `balanced` was the shipped default and it silently LOST
    # long-form speech -- on five minutes of continuous speech it returned
    # 94% of the words on `base` and 66% on `small`, with no warning. The
    # cause is `without_timestamps=True` (measured per-parameter; see the
    # queue 124 report), which is now fixed in every profile -- but `accurate`
    # is also the most accurate on BOTH corpora measured, so it is the
    # default. Cost on `base`, 120 short clips: WER 3.61% -> 3.91% and decode
    # p50 66 ms -> 100 ms, about 5% of the end-to-end p50 queue 118 measured.
    "performance_mode": {
        "type": "enum",
        "options": ["fast", "balanced", "accurate"],
        "default": "accurate",
        "tab": "advanced",
    },
    "silence_threshold": {
        "type": "float",
        "min": 0.5,
        "max": 10.0,
        "step": 0.5,
        "default": 2.0,
        "tab": "advanced",
    },
    "min_speech_duration": {
        "type": "float",
        "min": 0.1,
        "max": 2.0,
        "step": 0.1,
        "default": 0.3,
        "tab": "advanced",
    },
    "wake_word_config.audio.speech_threshold": {
        "type": "float",
        "min": 0.005,
        "max": 0.20,
        "step": 0.005,
        "default": 0.03,
        "tab": "advanced",
        "depends_on": "threshold_mode=='manual'",
    },
    # Default OFF (2026-07-10): the homegrown NLMS adaptive filter
    # (samsara/echo_cancel.py) converges to only 3-8% echo reduction --
    # its latency-alignment assumption doesn't hold in practice -- and
    # adversarial review concluded it likely adds artifacts/distortion to
    # the capture path, i.e. net-negative. Retired pending evaluation of
    # WebRTC AEC3 / Windows communications-mode capture as a replacement
    # (separate, post-release item). The code is intentionally NOT
    # deleted -- this only flips the default; see samsara/echo_cancel.py
    # for the still-functional implementation.
    "echo_cancellation.enabled":  {"type": "bool", "default": False, "tab": "advanced"},
    "echo_cancellation.latency_ms": {
        "type": "float",
        "min": 0.0,
        "max": 500.0,
        "step": 5.0,
        "default": 30.0,
        "tab": "advanced",
        "depends_on": "echo_cancellation.enabled",
    },
    # Audio ducking (2026-07-10) -- attenuates OTHER apps' audio sessions
    # while dictating instead of subtracting echo after capture (the
    # echo_cancellation entries above). Off by default -- opt-in, like
    # echo_cancellation. See samsara/audio_ducking.py.
    "ducking.enabled": {"type": "bool", "default": False, "tab": "advanced"},
    "ducking.level": {
        "type": "float",
        "min": 0.0,
        "max": 1.0,
        "step": 0.05,
        "default": 0.2,
        "tab": "advanced",
        "depends_on": "ducking.enabled",
    },
    "listening_indicator_enabled": {"type": "bool", "default": False, "tab": "advanced"},
    # Idle blink/glance on the listening indicator's mark (queue 09b3). Only
    # the idle motion -- state animation (spin, pulse, heard flash) always runs.
    "ui.idle_animation": {"type": "bool", "default": True, "tab": "advanced"},
    "listening_indicator_position": {
        "type": "enum",
        "options": ["top-left", "top-center", "top-right",
                    "bottom-left", "bottom-center", "bottom-right", "custom"],
        "default": "bottom-center",
        "tab": "advanced",
        "depends_on": "listening_indicator_enabled",
    },
    # listening_indicator_custom_position (not itemized here -- a free-form
    # dict like gesture/command_mode/smart_corrections, only meaningful when
    # listening_indicator_position == "custom"): {'screen': QScreen.name(),
    # 'cx': 0..1, 'cy': 0..1}. Written by dictation.py when
    # ListeningIndicator.placement_committed fires from a drag.
    "audio.input_sensitivity": {
        "type": "float",
        "min": 0.05,
        "max": 1.0,
        "default": 0.3,
        "tab": "advanced",
    },
    "transcription.mode": {
        "type": "enum",
        "options": ["clean", "verbatim"],
        "default": "clean",
        "tab": "general",
    },

    # Ava's edit proposals (queue 102): "make that more formal" over the text
    # just dictated. Nothing is ever applied without the spoken word "apply",
    # so `enabled` gates whether the PROPOSAL is offered at all, not whether
    # something can happen behind the user's back.
    "ava_edit.enabled": {"type": "bool", "default": True, "tab": "advanced"},
    # Choreography only -- see samsara/ava_edit/pacing.py, which is built so
    # this cannot change WHAT is applied. "cinematic" adds dwell either side
    # of the deletion so a screen recording reads; "instant" is daily use.
    "ava_edit.demo_pacing": {
        "type": "enum",
        "options": ["instant", "cinematic"],
        "default": "instant",
        "tab": "advanced",
        "depends_on": "ava_edit.enabled",
    },
    "ava_edit.model": {
        "type": "str",
        "default": "llama3.2:3b",
        "tab": "advanced",
        "depends_on": "ava_edit.enabled",
    },
    "ava_edit.timeout_s": {
        "type": "float",
        "default": 12.0,
        "tab": "advanced",
        "depends_on": "ava_edit.enabled",
    },

    # Smart Corrections: optional LLM post-processing pass over dictation
    # output (homophones/misrecognitions/punctuation). Off by default.
    "smart_corrections.enabled": {"type": "bool", "default": False, "tab": "advanced"},
    "smart_corrections.backend": {
        "type": "enum",
        "options": ["auto", "ollama", "cloud"],
        "default": "auto",
        "tab": "advanced",
        "depends_on": "smart_corrections.enabled",
    },
    "smart_corrections.ollama_model": {
        "type": "str",
        "default": "qwen2.5:3b",
        "tab": "advanced",
        "depends_on": "smart_corrections.enabled",
    },
    "smart_corrections.timeout_s": {
        "type": "float",
        "min": 1.0,
        "max": 30.0,
        "step": 0.5,
        "default": 6.0,
        "tab": "advanced",
        "depends_on": "smart_corrections.enabled",
    },
    "smart_corrections.min_words": {
        "type": "int",
        "min": 1,
        "max": 20,
        "default": 3,
        "tab": "advanced",
        "depends_on": "smart_corrections.enabled",
    },
    "smart_corrections.allow_cloud_fallback": {
        "type": "bool",
        "default": False,
        "tab": "advanced",
        "depends_on": "smart_corrections.enabled",
    },
    "smart_corrections.keep_alive": {
        "type": "str",
        "default": "30m",
        "tab": "advanced",
        "depends_on": "smart_corrections.enabled",
    },
    "smart_corrections.modes.hotkey":    {"type": "bool", "default": True,  "tab": "advanced", "depends_on": "smart_corrections.enabled"},
    "smart_corrections.modes.wake":      {"type": "bool", "default": True,  "tab": "advanced", "depends_on": "smart_corrections.enabled"},
    "smart_corrections.modes.streaming": {"type": "bool", "default": False, "tab": "advanced", "depends_on": "smart_corrections.enabled"},
    "smart_corrections.repair_disfluencies": {
        "type": "bool",
        "default": False,
        "tab": "advanced",
        "depends_on": "smart_corrections.enabled",
    },

    # Dictation Diagnostics: per-utterance pipeline instrumentation viewer.
    "diagnostics.write_jsonl": {"type": "bool", "default": False, "tab": "advanced"},

    # Opt-in: dump the exact assembled hotkey buffer (post-prepend,
    # pre-fade) to ~/.samsara/debug/hotkey_*.wav on every hotkey
    # transcription. 2026-07-10 hotkey word-loss investigation.
    "debug.dump_hotkey_buffers": {"type": "bool", "default": False, "tab": "advanced"},

    # Personal WER benchmark: opt-in local sample collection for the
    # offline accuracy harness (samsara/benchmark_store.py, tools/benchmark_eval.py).
    "benchmark.collect_samples": {"type": "bool", "default": False, "tab": "advanced"},
    "benchmark.max_samples": {
        "type": "int",
        "min": 10,
        "max": 2000,
        "default": 200,
        "tab": "advanced",
        "depends_on": "benchmark.collect_samples",
    },

    # Correction capture: hotkey-triggered "fix my last dictation" flow
    # (samsara/correction_capture.py, samsara/ui/correction_capture_qt.py).
    "correction_capture.max_edit_ratio": {
        "type": "float",
        "min": 0.1,
        "max": 1.0,
        "step": 0.05,
        "default": 0.5,
        "tab": "advanced",
    },

    # Shadow intent gate (samsara/intent/shadow.py): observer only -- records
    # what the tier-2 gate WOULD have done for each DICTATE utterance to
    # ~/.samsara/shadow/intent-YYYY-MM-DD.jsonl; changes nothing the app does.
    # No settings-UI widget (config-file-editable); intent.shadow_dir, the
    # folder override, is read inline at its point of use.
    "intent.shadow_enabled": {"type": "bool", "default": True, "tab": "advanced"},

    # Queue 93 escape hatch: a word that, spoken first, forces the rest of the
    # utterance to be read as a command -- "<prefix> copy" runs copy even
    # though execution rule 1 (no single-word command executes) would
    # otherwise type it. Empty is OFF and is the default: no prefix word is
    # hard-coded, the owner picks one from shadow data. It never waives rule 2
    # (only an exact match executes), so it cannot promote a homophone.
    "intent.command_prefix": {"type": "str", "default": "", "tab": "advanced"},

    # -------------------------------------------------------------------------
    # Ava / Cloud tab
    # -------------------------------------------------------------------------
    "cloud_llm.enabled": {
        "type": "bool",
        "default": False,
        "tab": "ava",
    },
    "cloud_llm.provider": {
        "type": "enum",
        "options": ["deepseek", "openai", "anthropic", "openrouter"],
        "default": "deepseek",
        "tab": "ava",
        "depends_on": "cloud_llm.enabled",
    },
    "cloud_llm.timeout_seconds": {
        "type": "int",
        "min": 5,
        "max": 120,
        "step": 5,
        "default": 30,
        "tab": "ava",
        "depends_on": "cloud_llm.enabled",
    },
    # Queue 59: Ava web search through DeepSeek's Anthropic-compatible
    # endpoint. Explicit opt-in (sends the question to DeepSeek's servers);
    # DeepSeek only. Config-file-only tuning read by cloud_llm.send_web_search:
    # cloud_llm.search_max_uses (default 3, clamped 1-10),
    # cloud_llm.search_max_tokens (default 1024, clamped 256-4096),
    # cloud_llm.search_model (default "deepseek-flash").
    "cloud_llm.web_search": {
        "type": "bool",
        "default": False,
        "tab": "ava",
        "depends_on": "cloud_llm.enabled",
    },
    "ava_personality": {
        "type": "enum",
        "options": ["relaxed", "strict"],
        "default": "relaxed",
        "tab": "ava",
    },
    "ava_invocations": {
        # Exact whole-utterance phrases that switch into Ava mode -- see
        # samsara/session_modes.py's match_ava_invocation(). No settings-UI
        # widget yet (config-file-editable only this pass); the Ava/Cloud
        # tab near ava_personality above is the obvious future home if this
        # gets exposed -- likely a small comma-separated/list editor, since
        # this schema has no other 'list'-typed entry precedent to follow.
        "type": "list",
        "item_type": "str",
        "default": ["hey ava", "so ava", "oracle"],
        "tab": "ava",
    },
    "ava_memory.mode": {
        "type": "enum",
        "options": ["clear", "last"],
        "default": "clear",
        "tab": "ava",
    },
    "ava_memory.max_turns": {
        "type": "int",
        "min": 5,
        "max": 500,
        "step": 5,
        "default": 20,
        "tab": "ava",
    },

    # Ollama / local LLM (also in Ava tab)
    "ollama.enabled":             {"type": "bool", "default": True,                        "tab": "ava"},
    "ollama.host":                {"type": "str",  "default": "http://localhost:11434",     "tab": "ava"},
    "ollama.model":               {"type": "str",  "default": "llama3",                    "tab": "ava"},
    "ollama.timeout_seconds":     {"type": "int",  "min": 5,   "max": 300,  "default": 30, "tab": "ava"},
    "ollama.max_response_length": {"type": "int",  "min": 100, "max": 4000, "default": 800,"tab": "ava"},
    "ollama.safety_gate_enabled": {"type": "bool", "default": True,                        "tab": "ava"},
}
