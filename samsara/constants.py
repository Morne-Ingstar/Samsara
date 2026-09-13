"""
Samsara shared constants.

Single source of truth for magic numbers used across modules.
Values here are defaults -- user config overrides where applicable.
"""

# Audio
MODEL_SAMPLE_RATE = 16000          # Whisper expects 16kHz input
DEFAULT_CAPTURE_RATE = 48000       # fallback if device rate query fails
PREBUFFER_SECONDS = 1.5            # rolling buffer before hotkey press
PREBUFFER_CHUNK_MS = 100           # chunk duration in milliseconds

# Speech detection defaults (overridable via config)
DEFAULT_SPEECH_THRESHOLD = 0.03    # RMS level to detect speech
DEFAULT_MIN_SPEECH_DURATION = 0.3  # seconds of speech before transcription
DEFAULT_SILENCE_TIMEOUT = 2.0      # seconds of silence to end recording

# Continuous-mode commit trigger defaults (overridable via config)
DEFAULT_CONTINUOUS_COMMIT_TRIGGER = 'silence'  # 'silence' (auto, today's behavior) | 'key' (manual)
DEFAULT_CONTINUOUS_COMMIT_HOTKEY = 'ctrl+space'  # hotkey that commits when trigger == 'key'
DEFAULT_CONTINUOUS_MAX_BUFFER_S = 60.0  # safety cap: auto-commit an un-committed 'key'-mode
                                        # session past this many seconds of accumulated speech

# Voice-activity / speech-gate thresholds shared across the hold and
# hands-free capture paths. Centralized here so the two paths cannot
# silently drift from each other -- see the "duplicated gate constants"
# finding in docs/reviews/hands_free_path_review.md. Each value is still
# tuned for its own gate; unifying the DECLARATION does not mean the
# hold and hands-free gates share one threshold.
LIVE_VAD_PROB_THRESHOLD = 0.5        # single-frame "is anyone talking" Silero gate
CONTIGUOUS_VAD_PROB_THRESHOLD = 0.45  # contiguous-run confidence gate (same Silero model)
ADAPTIVE_SPEECH_FLOOR_RATIO = 1.5     # speech passes when rms >= ambient floor * this ratio
HOLD_RELEASE_TAIL_SPEECH_THRESHOLD = 0.008  # hold-mode release-tail RMS floor
WAKE_SPEECH_THRESHOLD_CAP = 0.01      # hands-free speech_threshold cap when VAD is unavailable

# Wake word defaults (overridable via config)
WAKE_DETECTION_SILENCE = 0.8        # seconds of silence during wake word listening
WAKE_COMMAND_TIMEOUT = 5.0         # seconds to wait for command after wake word
# Mirrors dictation.py's load_config() default_config['wake_word_config']
# ['phrase']/['phrase_options'] exactly -- re-verify against load_config()
# if the real default ever changes. Exists so every UI display fallback
# (tutorial, wizards, tray menu, Ava guide, quick reference) reads one
# canonical value instead of each hardcoding its own literal.
DEFAULT_WAKE_PHRASE = 'jarvis'
DEFAULT_WAKE_PHRASE_OPTIONS = ['jarvis', 'hey jarvis', 'computer', 'hey computer', 'samsa', 'hey samsa']

# Tray icon animation speeds
ICON_TICK_FAST = 0.08              # seconds per frame (recording)
ICON_TICK_MEDIUM = 0.08            # seconds per frame (continuous)
ICON_TICK_SLOW = 0.12              # seconds per frame (wake word)
ICON_SPIN_FAST = 0.15              # rotation step (recording)
ICON_SPIN_MEDIUM = 0.1             # rotation step (continuous)
ICON_SPIN_SLOW = 0.05              # rotation step (wake word)
ICON_CHASE_FAST = 6                # ticks between color shifts (recording)
ICON_CHASE_MEDIUM = 10             # ticks between color shifts (continuous)
ICON_CHASE_SLOW = 14               # ticks between color shifts (wake word)

# Calibration
CALIBRATION_DURATION = 1.5         # seconds of ambient noise measurement
CALIBRATION_CHUNK_MS = 100         # chunk size in milliseconds
CALIBRATION_MULTIPLIER = 3.0       # multiplier above ambient median
CALIBRATION_FLOOR = 0.00044         # minimum threshold (guards electrical noise)
CALIBRATION_CEILING = 0.15         # maximum threshold (sanity cap)

# Clipboard
CLIPBOARD_PASTE_DELAY = 0.05        # seconds after copy, before paste
CLIPBOARD_RESTORE_DELAY = 0.139     # seconds after paste, before restore (tray earcon path)
CLIPBOARD_POST_PASTE_SETTLE = 0.4   # seconds after ctrl+v, before restore (PasteHandler)
