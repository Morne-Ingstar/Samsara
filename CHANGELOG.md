# Changelog

All notable changes to Samsara will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Wake word observability** -- Debug window upgraded to structured trace pipeline
  - Wake Word Evaluation panel shows match decision (type, index, YES/NO) per utterance
  - Decision Timeline groups all pipeline stages per utterance in a scrollable view
  - Export Timeline button writes trace to `docs/wake_word_trace_YYYYMMDD_HHMMSS.txt`
  - Main app pipeline events appear in debug window when open (optional trace callback)
  - New shared module: `samsara/wake_word_matcher.py` -- token-aware phrase matching
- **Snooze listening** -- Tray submenu to temporarily pause all listening for 5/15/30/60 min
  or indefinitely, then auto-resume; hotkeys are ignored while snoozed
  - Active streams (continuous, wake word) are stopped and restored on resume
  - Tray tooltip shows snooze state and resume time
  - Alarm hotkeys still work while snoozed
- **Listening state indicator overlay** -- Small always-on-top pill window shows current mode
  and pulses teal (#00CED1) while audio is actively captured
  - Flashes green on successful dictation, red on errors/cancellation (fades back smoothly)
  - Configurable position: top/bottom + left/center/right (default: bottom-center)
  - Settings in General tab: enable/disable toggle and position dropdown
  - Toggleable via tray menu "Show Listening Indicator" (persisted in config)
  - Dismissable with middle-click; positions within work area to avoid taskbar overlap
  - New file: `samsara/ui/listening_indicator.py`
- **Audio pre-buffer system** — Rolling 1.5s circular buffer captures audio before hotkey press
  - First words are never lost to startup delay (sound cue, stream initialization)
  - Pre-buffer audio prepended to recording data automatically
  - Standalone pre-buffer stream for hold/toggle modes
  - Wake word stream feeds pre-buffer in combined/wake_word modes
  - Log prefix `[PRE]` shows captured pre-buffer duration
- **Macro command example** — "jump five times" demonstrates chained/repeated actions
- **Standalone Windows EXE distribution** — No Python installation required
  - Single-folder executable built with PyInstaller
  - Reduced from 5.8 GB to 1.9 GB (1.07 GB compressed)
  - Whisper models download on first run (not bundled)
- **Tray mode switching** — Switch between all 5 modes (Hold, Toggle, Wake Word,
  Combined, Continuous) directly from tray right-click menu with radio checkmarks;
  no settings dialog needed. Central `apply_mode()` handles all state transitions.
- **Wake word correction map** — `samsara/wake_corrections.py` provides a
  token-level substitution map for known Whisper misrecognitions (e.g.
  "charvis" → "jarvis"). Applied before matching in both main app and debug window.
  Trace pipeline shows both RAW and CORRECTED text for easy pattern discovery.
- **Plugin command system** (scaffold) — `samsara/plugin_commands.py` with
  `@command` decorator, global registry, alias support, and auto-loader for
  `plugins/commands/*.py`. Not yet wired into `CommandExecutor.process_text`.
- **Echo cancellation module** — `samsara/echo_cancel.py` uses WASAPI loopback
  capture + NLMS adaptive filter to subtract system audio from mic input.
  Windows-only, disabled by default.

### Changed
- **Wake word is now a boolean, not a mode** -- `wake_word_enabled` config flag
  replaces the old `wake_word` and `combined` capture modes.  Three capture modes
  remain (hold, toggle, continuous); wake word runs alongside any of them.
  - Tray menu shows "Wake Word" as a checkable item instead of two radio entries
  - Settings shows a checkbox beneath the capture-mode radios
  - `Ctrl+Alt+W` hotkey toggles `wake_word_enabled` on/off
  - Old configs with `mode='wake_word'` or `mode='combined'` auto-migrate to
    `mode='hold' + wake_word_enabled=true`
  - Snooze correctly saves/restores `wake_word_enabled` state
  - Tray tooltip shows combined state: "Hold + Wake", "Continuous", etc.
  - Listening indicator pill shows the same combined label
  - Tray icon chase animation runs while wake word listener is active
  - Icon stays animated after recording ends if wake word is still listening
  - Removed all dead 'wake_word'/'combined' mode references from code, tests, and docs
  - Updated README mode table, ARCHITECTURE.md state diagram, WAKE_WORD_GUIDE.md
- **Tray mic switching** — Now correctly stops and restarts all active audio
  streams (pre-buffer, wake word, continuous) on the new device. Previously
  only updated config without restarting streams, so the old mic kept recording.
  Uses closure-pattern callbacks to avoid pystray's 2-arg callback limitation.
- **Config save is now atomic** — Writes to `.json.tmp` first, rotates the
  existing config to `.json.bak`, then atomically promotes the temp file via
  `os.replace`. Prevents truncation/corruption if serialization fails mid-write.
- **Dual sample rate architecture** -- Capture at device native rate (44.1/48kHz),
  resample to 16kHz for Whisper. Fixes WASAPI "Invalid sample rate" errors.
  - All 5 stream sites updated to use `self.capture_rate`
  - `resample_audio()` via `np.interp` -- lightweight, no new dependencies
  - Reverted DirectSound workaround back to WASAPI (proper API, no duplicates)
  - `_detect_capture_rate()` queries device on init and mic switch
  - Wake Word Debug window also captures at native rate and resamples
- **Speech threshold default** raised from 0.01 to 0.03 RMS across all modes
  (config, continuous callback, wake word callback, debug window). The old
  default was below ambient noise floor for most environments, causing
  perpetual "Speaking" state that prevented silence detection from firing.
- **Toggle mode tray feedback** — `start_recording` sets tray icon to teal +
  tooltip to "RECORDING"; `stop_recording` restores idle state. Critical for
  toggle mode where there's no physical key-hold to indicate recording state.

### Fixed
- **Wake Word Debug performance** -- Reduced UI thread load by ~80%
  - Unified audio level meter + timer into single 4 Hz poll loop (was 10 Hz each)
  - Added change-detection guards to skip redundant widget reconfigs
  - Batched log textbox inserts (flush every 200ms instead of per-message)
- **Settings window performance** -- Eliminated save-time lag and faster open
  - Removed force-build of unvisited tabs on save; reads config directly instead
  - Microphone enumeration moved to background thread (window opens instantly)
- Replaced PyTorch CUDA detection with ctranslate2 native method
  - Eliminates 4.7 GB torch dependency
  - Uses `ctranslate2.get_supported_compute_types()` instead of `torch.cuda.is_available()`

### Fixed
- **Wake word substring false-positive** -- `wake_phrase in text` replaced with
  token-aware `match_wake_phrase()` in both dictation.py and wake_word_debug.py;
  "samsara-like" no longer triggers wake phrase "samsara"
- **Listening indicator vanishes behind taskbar** -- Periodically re-asserts topmost;
  positions inside the work area (excludes taskbar) instead of full screen
- **Listening indicator settings** -- Added enable/disable toggle and position dropdown
  to Settings > General; default position changed to top-right to avoid taskbar overlap
- **Wake word + hotkey contention** — Wake word transcription now pauses during hotkey recording
  - Eliminates 200-800ms GPU contention delay when pressing hotkey in combined mode
  - Wake word audio stream continues running (feeds pre-buffer) but skips transcription
  - Processing resumes automatically when hotkey recording ends

### Removed
- PyTorch, TensorFlow, Keras (not needed for faster-whisper)
- OpenCV, numba, librosa, pandas, scipy (unused dependencies)

---

## [0.9.0] - 2026-02-09

### Added
- **Alarm Reminders** — Persistent notifications with sound
  - Interval-based alarms (hydration, stretching, breaks)
  - Plays sound repeatedly until dismissed with hotkey (default: F11)
  - Configurable nag/repeat interval (default: 60 seconds)
  - Built-in sounds: alarm, chime, bell, gentle
  - Support for custom WAV/MP3 sound files
  - New "Alarms" tab in Settings

### Fixed
- Audio callback exception handling — prevents silent crashes during audio processing
- Thread safety for Whisper transcription — mutex lock prevents race conditions

---

## [0.8.0] - 2026-02-04

### Fixed
- **Critical: Clipboard preservation on 64-bit Windows**
  - Windows API handles were overflowing due to 32-bit integer casting
  - `save_clipboard()` now properly preserves all formats (images, files, HTML, etc.)
  - User clipboard no longer destroyed after every dictation

### Added
- Centralized `samsara/clipboard.py` module with shared lock

---

## [0.7.0] - 2026-01-30

### Added
- **Sound Theme System** — Four built-in themes
  - `cute` — Playful bloops (Nintendo/Duolingo style)
  - `warm` — Rich chords (OS boot sound vibes)
  - `zen` — Singing bowls and chimes
  - `chirpy` — Bright bird-like chirps
  - Theme selector in Settings → Sounds tab
  - Create custom themes by adding folders to `sounds/themes/`
- **Multi-format audio support** — MP3, OGG, FLAC, M4A (requires pydub + ffmpeg)

### Fixed
- Start sound not playing (winsound conflict with InputStream)
- Wake word response time — reduced silence detection threshold
- Speech cutoff after wake word
- Settings persistence — mode changes now take effect immediately
- Exit from system tray now works reliably

---

## [0.6.0] - 2026-01-23

### Added
- **Pause Word Support** — Say "hold on" during dictation to reset silence timer
  - Allows thinking mid-dictation without triggering timeout
  - Configurable phrase (pause, hold on, wait, or custom)
- **Dictation Auto-Finalization** — Text outputs after silence timeout
- Wake word debug window improvements (timer display, flow indicator, mode selector)

### Fixed
- Audio cue latency — persistent output stream eliminates device re-acquisition delay
- Clipboard preservation now handles all Windows formats (images, files, rich text)

---

## [0.5.0] - 2026-01-21

### Added
- **Expanded Wake Word System**
  - Configurable wake phrase (samsara, hey samsara, computer, jarvis, custom)
  - Three dictation modes: dictate (2s timeout), short dictate (1s), long dictate (waits for end word)
  - End word support (over, done, go, send, execute, etc.)
  - Cancel word support (cancel, abort, never mind, scratch that)
- Enhanced Settings UI with dropdowns for all wake word options

---

## [0.4.0] - 2026-01-20

### Added
- Voice Training button in Settings → General tab
- Profile Manager height increased to show all buttons

### Fixed
- App not fully closing on exit — all windows now properly destroyed
- Renamed "Dictionary Profiles" to "Voice Training Profiles" for clarity

---

## [0.3.0] - 2026-01-15

### Added
- Scrollable Settings tabs — content no longer cut off
- Model size dropdown shows disk space requirements
- Created Samsara-dev folder for active development

---

## [0.2.0] - 2026-01-11

### Added
- **Modular Package Structure** (`samsara/`)
  - `samsara.config` — Configuration management
  - `samsara.audio` — AudioCapture and AudioPlayer
  - `samsara.speech` — SpeechRecognizer and TextProcessor
  - `samsara.commands` — CommandExecutor
  - `samsara.ui` — UI components
- **Comprehensive Test Suite** — pytest-based with mocked dependencies
- **Dictation History Panel** — View, copy, clear recent transcriptions
- **20+ Punctuation Commands** — period, comma, question mark, parentheses, etc.
- **Cancel Recording Hotkey** — Escape to abort without transcribing
- **Sound Volume Slider** — 0-100% adjustment in Settings
- **Auto-Start with Windows** — Option in Settings → General
- **Auto-Capitalize Sentences** — Capitalizes after . ! ?
- **Number Formatting** — "twenty one" → "21"
- Voice Commands Manager in Settings with add/edit/delete/test
- Custom sound file support (WAV)

### Fixed
- Treeview headers invisible in dark mode
- Dictation history not persisting across restarts

---

## [0.1.0] - 2026-01-09

### Added
- **First Run Experience**
  - Splash screen with animated loading progress
  - Setup wizard (microphone, hotkey, model selection)
- **Launcher System**
  - `_launcher.vbs` for silent background launch
  - `install.bat` and `build.bat` scripts
- **Dictation Modes** — Hold, Toggle, Continuous, Wake Word
- **Voice Commands** — 41 predefined commands in `commands.json`
- **Voice Training Module**
  - Microphone level monitor
  - Custom vocabulary and corrections
  - Export/Import training data
- **Modern UI** — CustomTkinter dark theme with tabbed Settings

### Fixed
- Unicode emoji crashes on Windows console (replaced with ASCII)
- Settings/Voice Training buttons not visible

---

## [0.0.1] - 2026-01-08

### Added
- Initial release
- Python-based speech-to-text using faster-whisper
- GPU acceleration with CUDA
- Multiple Whisper model sizes (tiny to large-v3)
- System tray integration
- Global hotkey support
- JSON configuration
