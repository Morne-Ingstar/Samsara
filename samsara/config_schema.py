"""
Machine-readable settings schema for Samsara.

Extracted from samsara/ui/settings_qt.py widget parameters. Importable without
instantiating the Qt UI -- no Qt imports here.

Format per entry:
    type:       'int' | 'float' | 'bool' | 'str' | 'enum'
    min, max:   numeric bounds (int/float only)
    step:       increment step (numeric, optional)
    options:    allowed values list (enum only)
    default:    app default when key absent from config
    tab:        settings tab where this appears
    depends_on: string condition for cross-field dependency (optional)
                e.g. "echo_cancellation.enabled" means the setting is
                only active when that key is True.

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
    "device": {
        "type": "enum",
        "options": ["cpu", "cuda"],
        "default": "cpu",
        "tab": "advanced",
    },
    "compute_type": {
        "type": "enum",
        "options": ["float16", "int8", "float32"],
        "default": "float16",
        "tab": "advanced",
    },
    "performance_mode": {
        "type": "enum",
        "options": ["fast", "balanced", "accurate"],
        "default": "balanced",
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
    "ava_personality": {
        "type": "enum",
        "options": ["relaxed", "strict"],
        "default": "relaxed",
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
