# Changelog

All notable changes to Samsara will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Macro command example** — "jump five times" demonstrates chained/repeated actions
- **Standalone Windows EXE distribution** — No Python installation required
  - Single-folder executable built with PyInstaller
  - Reduced from 5.8 GB to 1.9 GB (1.07 GB compressed)
  - Whisper models download on first run (not bundled)

### Changed
- Replaced PyTorch CUDA detection with ctranslate2 native method
  - Eliminates 4.7 GB torch dependency
  - Uses `ctranslate2.get_supported_compute_types()` instead of `torch.cuda.is_available()`

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
