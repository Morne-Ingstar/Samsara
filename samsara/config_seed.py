"""Fresh configuration construction without app bootstrap or profile I/O."""
from copy import deepcopy

from samsara.constants import (
    DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
    DEFAULT_CONTINUOUS_COMMIT_TRIGGER,
    DEFAULT_CONTINUOUS_MAX_BUFFER_S,
    DEFAULT_MIN_SPEECH_DURATION,
    DEFAULT_SILENCE_TIMEOUT,
    DEFAULT_SPEECH_THRESHOLD,
    WAKE_COMMAND_TIMEOUT,
    WAKE_DETECTION_SILENCE,
)


def structural_error(config_obj):
    if not isinstance(config_obj, dict):
        return (
            "top-level config must be a JSON object (dict), got "
            f"{type(config_obj).__name__}"
        )
    required_containers = {
        "wake_word_config": dict,
        "wake_profiles": list,
        "command_mode": dict,
        "ducking": dict,
        "smart_corrections": dict,
        "wake_targets": list,
        "ai_command_mode": dict,
    }
    for key, expected_type in required_containers.items():
        if key in config_obj and not isinstance(config_obj[key], expected_type):
            return (
                f"top-level '{key}' must be {expected_type.__name__} "
                f"when present, got {type(config_obj[key]).__name__}"
            )
    return None


def incomplete_setup_config(defaults):
    """Use fresh-profile defaults without claiming that setup was finished."""
    return {**defaults, "first_run_complete": False}


def build_default_config(*, home, macro_defaults, notification_defaults):
    """Build independent defaults using the caller's home and factory values."""
    return {
        "hotkey": "ctrl+shift",
        "continuous_hotkey": "ctrl+alt+d",
        "wake_word_hotkey": "ctrl+alt+w",
        "command_hotkey": "ctrl+alt+c",
        "undo_hotkey": "ctrl+alt+z",
        "dictate_commit_hotkey": DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
        "correction_hotkey": "ctrl+alt+r",
        "cancel_hotkey": "escape",
        "memo_hotkey": "ctrl+alt+m",
        "task_hotkey": "ctrl+alt+t",
        # Queue 92: memo audio is now kept BY DEFAULT. The flag was
        # written for the dictation path, where retention is clearly
        # wrong; for a memo the recording IS the feature -- "record
        # whatever I said" -- and a mis-transcribed name in a memo is
        # not reconstructable from the words. Nothing leaves the
        # machine, and the retention policy below bounds the cost.
        # Anyone who had set this False keeps False.
        "memo_retain_audio": True,
        # The retention policy (samsara.quick_memo.prune_audio): audio
        # older than this many days goes, then the oldest goes until the
        # total fits the size cap. 0 disables either limit. A memo the
        # user pinned in the memo list is never pruned, and a pruned
        # memo keeps its transcript -- only the WAV is removed.
        "memo_audio_retention_days": 90,
        "memo_audio_max_mb": 500,
        "memo_file": None,
        # Nested hotkey namespace (new features land here rather than as
        # more top-level *_hotkey keys). capture_correction opens the
        # correction-capture window (samsara/ui/correction_capture_qt.py)
        # pre-filled with the last dictation. Verified against every
        # other hotkey default above (ctrl+shift, ctrl+alt+d/w/c/z/r,
        # escape, capslock, ctrl+space) -- ctrl+alt+x is free.
        "hotkeys": {
            "capture_correction": "ctrl+alt+x",
        },
        "mode": "hold",  # Options: "hold", "toggle", "continuous"
        "model_size": "base",
        "language": "en",
        "auto_paste": True,
        "add_trailing_space": True,
        "auto_capitalize": True,
        "format_numbers": True,
        "cleanup_mode": "clean",  # "clean" (filler removal + spacing) or "verbatim"
        "streaming_mode": False,  # live overlay partials in 'hold' mode
        "streaming_direct_paste": False,  # also paste partials into focused app
        "device": "auto",
        "microphone": None,
        "output_device": None,
        "output_device_name": None,
        "output_device_hostapi": None,
        "silence_threshold": DEFAULT_SILENCE_TIMEOUT,
        "min_speech_duration": DEFAULT_MIN_SPEECH_DURATION,
        # Continuous mode commit trigger: "silence" is today's fixed
        # 2s-auto-commit behavior (unchanged). "key" lets the user talk
        # with unlimited thinking pauses and commit each utterance by
        # tapping continuous_commit_hotkey instead. Set to "key" in
        # config/Settings to enable -- not turned on by default.
        "continuous_commit_trigger": DEFAULT_CONTINUOUS_COMMIT_TRIGGER,
        # Hotkey that commits the accumulated speech when trigger == "key".
        # Only ever active while continuous mode is running with that
        # trigger -- never live in hold/toggle modes.
        "continuous_commit_hotkey": DEFAULT_CONTINUOUS_COMMIT_HOTKEY,
        # Safety cap (seconds of accumulated speech): bounds an
        # un-committed "key"-mode session so it can't grow unbounded if
        # the user forgets to tap the commit hotkey. No effect in
        # "silence" mode.
        "continuous_max_buffer_s": DEFAULT_CONTINUOUS_MAX_BUFFER_S,
        "command_packs": {
            "core": True,
            "text-editing": True,
            "window-management": True,
            "browsers": True,
            "media": True,
            "smart-home": False,
            "3d-printing": False,
            "stremio": False,
            "screen-capture": False,
            "macros": False,
            "gaming": False,
            "mouse": False,
            "audio": False,
            "utilities": False,
            "smart-actions": True,
            "tasks": True,
        },
        "show_all_audio_devices": False,
        "audio_feedback": True,
        "sound_volume": 0.5,
        "sound_theme": "cute",
        "ui_scale": 1.0,
        "first_run_complete": True,
        "premium_license": "",
        # New nested wake word config
        "wake_word_config": {
            "phrase": "jarvis",
            "phrase_options": ["jarvis", "hey jarvis", "computer", "hey computer", "samsa", "hey samsa"],
            "quick_silence_timeout": 1.0,
            "end_words": ["over", "done", "end dictation"],
            "wake_abort_phrase": ["cancel", "cancel dictation", "abort"],
            "pause_words": ["pause", "hold on", "wait"],
            "resume_words": ["resume", "continue", "go on"],
            "audio": {
                "speech_threshold": DEFAULT_SPEECH_THRESHOLD,
                "min_speech_duration": DEFAULT_MIN_SPEECH_DURATION,
                "wake_detection_silence": WAKE_DETECTION_SILENCE,
                "wake_command_timeout": WAKE_COMMAND_TIMEOUT,
            },
            "feedback": {
                "play_sound_on_wake": True,
                "play_sound_on_end": True
            }
        },
        # Multi-wakeword: phrase -> target_process -> focus+dictate.
        # Each entry binds a spoken phrase to a target application by process name.
        # Missing oww_model -> Whisper-transcript fallback (match_wake_phrase).
        # Drop trained .onnx files into samsara/wake_models/ to enable OWW pre-filter.
        # mode: 'focus_dictate' types live and presses Enter on send_word;
        #       'stage_send' buffers utterances (never types until send_word) and
        #       never presses Enter -- for agentic targets (see samsara/wake_profiles.py).
        # send_word: THIS profile's own terminator -- must be distinct across
        #       profiles (agentic-safety requirement, see docs design review
        #       arc_20260629_170545.md). Never shared with another profile.
        # Phrases are validated at config load (samsara.wake_profiles.validate_wake_profiles):
        # under 3 syllables or a duplicate of another enabled profile's phrase disables it.
        "wake_profiles": [
            {
                "id": "claude",
                "phrase": "okay claude",
                "oww_model": "okay_claude.onnx",
                "target_process": "claude.exe",
                "enabled": True,
                "mode": "focus_dictate",
                "send_word": "over",
            },
            {
                "id": "hermes",
                "phrase": "activate hermes",
                "oww_model": "activate_hermes.onnx",
                "target_process": "Hermes.exe",
                "enabled": True,
                "mode": "stage_send",
                "send_word": "send",
            },
            {
                "id": "warp",
                "phrase": "activate warp",
                "oww_model": "activate_warp.onnx",
                "target_process": "warp.exe",
                "enabled": False,
                "mode": "focus_dictate",
                "send_word": "over",
            },
        ],
        # Echo cancellation (removes system audio from mic input).
        # Default OFF (2026-07-10): the homegrown NLMS adaptive filter
        # converges to only 3-8% echo reduction and adversarial review
        # concluded it likely adds artifacts/distortion -- net-negative.
        # Retired pending WebRTC AEC3 / Windows communications-mode
        # evaluation (separate, post-release item). Code kept intact,
        # just not on by default -- see samsara/echo_cancel.py and
        # samsara/config_schema.py's echo_cancellation.enabled entry.
        "echo_cancellation": {
            "enabled": False,
            "latency_ms": 30.0,
        },
        # Audio ducking (2026-07-10) -- attenuates OTHER apps' audio
        # sessions while dictating instead of subtracting echo after
        # capture. Off by default, opt-in -- see samsara/audio_ducking.py
        # and samsara/config_schema.py's ducking.enabled entry.
        #
        # hands_free_* (2026-07-24) -- SEPARATE from the hotkey-path
        # enabled/level above: wires the SessionDucker engine
        # (samsara/audio_ducking.py, previously unwired) to hands-free
        # listening instead. Two-stage, both keyed to ACTIVE CAPTURE
        # windows and the wake-word toggle, never to any session-end
        # event (see _open_hands_free_capture_duck/_close_hands_free_
        # capture_duck and start_wake_word_mode/stop_wake_word_mode):
        #   hands_free_level: applied only DURING a capture window
        #     (speech onset through transcription complete/discard +
        #     a short debounced tail) -- the deep duck.
        #   hands_free_idle_level: applied for the WHOLE DURATION the
        #     wake-word toggle is on -- a PERSISTENT MILD DUCK, not an
        #     opt-in extra (2026-07-24 amendment; default 0.8, set to
        #     1.0 to disable). WHY this exists even though it's not
        #     tied to active capture: the echo canceller does not
        #     converge reliably (see samsara/echo_cancel.py), so the
        #     1500ms rolling pre-buffer (samsara.constants.
        #     PREBUFFER_SECONDS, captured BEFORE a capture-duck can
        #     possibly engage -- ducking starts only once speech onset
        #     is already detected) and the wake-word detector itself
        #     both hear media at whatever level was playing BEFORE
        #     onset. This idle duck is what actually gives the wake
        #     word and pre-buffer their SNR; lowering it trades idle
        #     media volume for wake-word/onset recognition accuracy
        #     until AEC is fixed. Composes via the engine's documented
        #     RELATIVE/MIN-style layering: a capture duck started
        #     while an idle duck is active restores back to
        #     idle_level (not full) when the capture window closes;
        #     only the idle duck's own stop (wake-word toggle OFF)
        #     restores to true full volume.
        "ducking": {
            "enabled": False,
            "recording_mode": "duck",
            "level": 0.2,
            "hands_free_enabled": True,
            "hands_free_level": 0.15,
            "hands_free_idle_level": 0.8,
        },
        # Voice memo capture (2026-07-24): "voice memo" arms a one-shot
        # divert of the NEXT hold-to-dictate recording -- instead of
        # injecting text, the audio + transcript are saved into an
        # Obsidian vault (see samsara/voice_memo.py). vault_dir/
        # note_relpath/attachments_relpath are all relative-to-vault
        # except vault_dir itself, which is an absolute path.
        "voice_memo": {
            "vault_dir": "",
            "note_relpath": "Voice Memos.md",
            "attachments_relpath": "Attachments/Memos",
            "arm_timeout_s": 120,
            # Queue 92: the vault is a MIRROR, not the store of record.
            # Off by default because vault_dir does not exist for most
            # users and Samsara must not append to someone's notes
            # uninvited. Switch it on and every memo is also written
            # into the vault as an ![[embed]] plus transcript, which
            # Obsidian plays inline and syncs to a phone for free.
            "mirror_memos": False,
        },
        # Hub window geometry (size/position persist across sessions)
        "window_width": 900,
        "window_height": 650,
        "window_x": None,
        "window_y": None,
        # Performance mode for transcription speed/accuracy tradeoff
        "performance_mode": "balanced",  # "fast", "balanced", or "accurate"
        # Key macro system for accessibility (e.g., triple-tap W for auto-run)
        "key_macros": deepcopy(macro_defaults),
        # Notification system for reminders (medication, breaks, hydration)
        "notifications": deepcopy(notification_defaults),
        # Listening state indicator overlay
        "listening_indicator_enabled": False,
        "listening_indicator_position": "bottom-center",
        # Vision bridge (local Ollama vision model, opt-in)
        "vision": {
            "enabled": False,
            "model": "qwen2.5vl:3b",
            "warmup": True,
            "timeout": 90,
        },
        # Wake word listener (independent of capture mode)
        "wake_word_enabled": False,
        # Speech threshold calibration
        "threshold_mode": "auto",    # "auto" or "manual"
        "cal_multiplier": 3.0,       # multiplier above ambient for auto mode
        # Friendly aliases for Windows audio devices. Keys are spoken names
        # (match the voice command remainder); values are exact Windows
        # device names (Win+R -> mmsys.cpl to find them). Users customize
        # these by editing config.json -- no code change needed to add a device.
        "audio_devices": {
            "speakers": "Speakers",
            "headphones": "Headphones",
            "headset": "Headset Earphone",
            "earbuds": "Earbuds",
            "monitor": "DELL U2722D"
        },
        # Smart Actions: voice-to-markdown brain dump (Phase 1).
        # Per-user default lands in ~/Documents/Samsara Brain Dump.md.
        # Settings UI lets the user pick another path or disable earcons.
        "smart_actions": {
            "enabled": False,
            "brain_dump_path": str(home / "Documents" / "Samsara Brain Dump.md"),
            "earcons_enabled": True,
            "endpoint_url": "",
            "auth_header": "",
            "timeout_s": 30,
            "session_window_minutes": 5,
            "allowed_directories": [str(home / "Documents")],
            "allowed_domains": [],
            "tier2_approvals": {},
            "routing_verbs": ["ask", "plan", "summarize"],
        },
        # Smart Corrections: optional LLM post-processing pass over
        # dictation output (homophones/misrecognitions/punctuation).
        # Off by default. See samsara/smart_corrections.py.
        "smart_corrections": {
            "enabled": False,
            "backend": "auto",            # "auto" | "ollama" | "cloud"
            "ollama_model": "qwen2.5:3b",  # already pulled on this machine
            "timeout_s": 6.0,
            "min_words": 3,
            "allow_cloud_fallback": False,  # opt-in: auto may route to cloud when local AI is down
            "keep_alive": "30m",            # Ollama model residency after each call
            "modes": {"hotkey": True, "wake": True, "streaming": False},
            # Opt-in: strip filler words, immediate self-corrections, and
            # abandoned fragments. Widens the sanitizer's shrink allowance
            # and suspends its punctuation floor while on -- see
            # samsara/smart_corrections.py.
            "repair_disfluencies": False,
        },
        # Inline formatting tokens ("new line" -> \n, "new paragraph" ->
        # \n\n, "insert tab" -> \t, "bullet"/"bullet point" -> \n• ) applied to
        # DICTATE output only, after smart_correct, before delivery. See
        # samsara/formatting_tokens.py.
        "formatting_tokens": {
            "enabled": True,
        },
        # Dictation Diagnostics: per-utterance pipeline instrumentation
        # (samsara/diagnostics.py). Ring buffer always active; this only
        # gates the optional on-disk JSONL append.
        "diagnostics": {
            "write_jsonl": False,
        },
        # Opt-in debug tooling. dump_hotkey_buffers: write the exact
        # assembled hotkey buffer (post-prepend, pre-fade -- what
        # Whisper actually receives) to ~/.samsara/debug/hotkey_*.wav
        # on every hotkey transcription. Off by default -- 2026-07-10
        # hotkey word-loss investigation; see
        # DictationSessionConsumer._log_seam_diagnostics.
        "debug": {
            "dump_hotkey_buffers": False,
        },
        # Personal WER benchmark: opt-in local (user's real audio, gold
        # transcript) sample collection for the offline accuracy harness.
        # Off by default -- audio never leaves the machine either way.
        # See samsara/benchmark_store.py and tools/benchmark_eval.py.
        "benchmark": {
            "collect_samples": False,
            "max_samples": 200,
        },
        # Correction capture (samsara/correction_capture.py +
        # samsara/ui/correction_capture_qt.py): the hotkey-triggered
        # "fix my last dictation" flow. max_edit_ratio gates the
        # whole-text rewrite check -- if more than this fraction of the
        # words changed, extraction offers zero learnable pairs.
        "correction_capture": {
            "max_edit_ratio": 0.5,
        },
        # TTS subsystem (WinRTEngine + AudioCoordinator)
        "tts": {
            "enabled": True,
            "feedback_level": "brief",
            "voice_id": None,   # None = OS default voice
            "speed": 1.0,
            "pitch": 1.0,
            "volume": 0.8,
            # Per-context toggles — read by Phase 2 category-driven behavior.
            # Saved here from Settings UI but not yet acted on at runtime.
            "use_for_agent_responses": True,
            "use_for_confirmations": True,
            "use_for_warnings": True,
            "use_for_status_updates": True,
            "use_for_dictation_readback": False,
            "use_for_errors": True,
        },
        "audio_coordinator": {
            "enabled": True,
            "duck_factor": 0.7,
            "duck_default_duration_ms": 300,
            "duck_fade_ms": 5,
            "interrupt_grace_period_ms": 200,
            "speaking_wake_threshold_multiplier": 1.5,
            "speaking_vad_threshold_multiplier": 0.6,
            "thinking_pulse_interval_ms": 1000,
            "thinking_pulse_enabled": False,
        },
        # Walkie-talkie command mode (also hosts the unified session --
        # dictate/Ava voice switching -- when mode is "toggle")
        "command_mode": {
            "enabled": False,           # opt-in; see Modes tab -> Command Mode
            "command_matching_enabled": False,  # matching during ordinary dictation
            "dictation_literal_commands_only": True,  # 415; False restores argument matching
            "mode": "toggle",           # Fresh installs; legacy missing values still fall back to hold.
            "button": "rctrl",          # "rctrl" (default), "mouse4"/"mouse5" (XButton1/2), or other keys -- see _CMD_BUTTON_OPTIONS
            "enter_debounce_ms": 200,   # delay before playing enter earcon
            "exit_earcon": True,        # play stop earcon on release/exit
            "miss_limit": 5,            # toggle: exit after N unmatched recordings
            "inactivity_timeout_s": 300, # toggle: exit after N seconds silence
            "tts_char_limit": 50,       # suppress TTS responses longer than this
            "suppress_button": True,    # consume mouse4/5 click so browsers don't navigate back
            "utterance_silence_s": 1.0,         # toggle, COMMAND sub-mode: per-utterance VAD silence gap
            "dictate_utterance_silence_s": 2.0, # toggle, DICTATE sub-mode: longer gap for mid-sentence pauses
        },
        # Web shortcuts for "go to X" voice commands. Keys are spoken
        # aliases; values are target URLs. Users add their own by editing
        # config.json -- no code change needed.
        "web_shortcuts": {
            "mail": "https://mail.google.com",
            "email": "https://mail.google.com",
            "youtube": "https://youtube.com",
            "amazon": "https://amazon.com",
            "my orders": "https://www.amazon.com/gp/your-account/order-history",
            "github": "https://github.com",
            "reddit": "https://reddit.com"
        },
        # Gesture input lane (webcam hand-pose -> command). Opt-in; disabled
        # by default. Requires mediapipe and opencv-python in the environment.
        "gesture": {
            "enabled": False,
            "device_index": 0,
            "hold_ms": 350,
            "refractory_neutral_frames": 8,
            "min_detection_confidence": 0.6,
            "min_tracking_confidence": 0.5,
            "profile": {
                "width": 640,
                "height": 480,
                "fps": 30,
            },
            "poses": {
                "open_palm": "dictation_toggle",
                "peace":     "ava_mode",
                "fist":      "stop_cancel",
                "shaka":     "window_chooser",
            },
        },
    }
