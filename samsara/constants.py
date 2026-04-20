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

# Wake word defaults (overridable via config)
WAKE_DETECTION_SILENCE = 0.8        # seconds of silence during wake word listening
WAKE_COMMAND_TIMEOUT = 5.0         # seconds to wait for command after wake word

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
CALIBRATION_FLOOR = 0.0005         # minimum threshold (guards electrical noise)
CALIBRATION_CEILING = 0.15         # maximum threshold (sanity cap)

# Clipboard
CLIPBOARD_PASTE_DELAY = 0.05       # seconds after copy, before paste
CLIPBOARD_RESTORE_DELAY = 0.15     # seconds after paste, before restore
