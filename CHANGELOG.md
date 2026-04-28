# Changelog

All notable changes to Samsara are documented here.

## \[0.9.7\] - 2026-04-27

### Added

- **Hyperion LED strip control** — voice-controlled ambient lighting via Hyperion JSON API. "Jarvis, lights red", "lights effect rainbow", "lights off". Supports hostname, IPv4, and IPv6. 11 preset colors, 14 effect aliases with fuzzy matching.
## \[0.9.6\] - 2026-04-26

### Added

- **Main hub window** — opens on launch with sidebar navigation (History, Dictionary, Settings). Closing minimizes to tray. Double-click tray icon to reopen.
- **Dictation history** — SQLite database logging every transcription with timestamp, source app (via win32gui), raw text, cleaned text, duration, mode, and success/fail status. Searchable, with copy/retry/delete.
- **Unified dictionary UI** — three-tab corrections manager (Vocabulary, Corrections, Wake Words) in the main window. Add, edit, delete corrections from the UI. User corrections stored in `~/.samsara/` as JSON, hot-reloaded without restart. Hardcoded defaults are read-only; user overrides merge on top.
- **Grammar-Lite cleanup** — post-Whisper processing removes filler words (um, uh, like), fixes capitalization, adds missing punctuation. Two modes: Clean (default) and Verbatim. Raw transcript always preserved in history.
- **Audio auto-reconnect** — stream health monitor detects dead PortAudio streams after sleep/wake, auto-reconnects with exponential backoff (max 5 retries). Windows toast notification on reconnect.
- **Timer plugin** — "set a timer for 5 minutes" with natural language parsing, background thread, Windows notification on completion.
- **GIF search plugin** — "search for a gif of dancing cat" opens Giphy.
- **Screen recording plugin** — "record my screen" / "record this window" captures screen to GIF using mss (DXGI). Persistent red REC indicator, 30-second safety cap, active window capture via ctypes.
- **Demo commands** — "show me my portfolio" (fake catastrophic stock dashboard), "print me a gun" (FlashForge AD5X integration for promo video).
- **Edit-to-Learn hook** — placeholder in dictionary UI for future auto-correction learning from history edits.

### Changed

- Voice Training window refactored into reusable CTkFrame components (HistoryFrame, DictionaryFrame) shared between main window and standalone windows.
- `on_dictation_complete` callback updates main window status bar directly (no polling for last transcription).

### Fixed

- **Hold-to-dictate VAD bypass** — faster-whisper's internal VAD was stripping 80% of speech during explicit hotkey recordings. Now disabled for hold-to-dictate and long dictation modes.
- **Min audio guard** — Whisper hallucinations ("Thank you", "Subtitles by Amara") on sub-0.5s clips. Audio shorter than 0.51s is now silently discarded.
- **No ghost typing** — unrecognized wake word commands no longer paste garbage into focused apps. No-match goes silently back to sleep.

## \[0.9.5\] - 2026-04-24

### Added

- **BSL-1.1 license** — replaces MIT. Free for non-commercial use, converts to MIT April 2030.
- **Silero VAD v5 fixed** — now receives correct 512-sample chunks at 16kHz (was getting 1600 samples). Returns True if ANY 512-sample window contains speech.
- **Raw mic pipeline** — VAD runs on raw microphone signal, not AEC output. AEC was amplifying noise 10,000x instead of cancelling. Speech buffer stores raw mic audio for Whisper.
- **Wake word corrections** — "charvus", "jervice", "jervis", "service" → "jarvis".
- **\[HEAR\] debug trace** — every transcription logs what Whisper heard or why it returned empty.
- **Buffer cap** — 7-second speech buffer cap with stuck buffer detection. Prevents infinite accumulation from ambient noise fooling VAD.

### Changed

- Old MIT releases (v0.9.0–v0.9.4) deleted from GitHub.
- README badge updated to BSL-1.1.

## \[0.9.4\] - 2026-04-21

### Added

- **Phonetic wash** — post-transcription correction layer. Fixes common Whisper misrecognitions ("open crow" → "open chrome", "you two" → "YouTube").
- **Command registry** — token-based longest-match resolver with frozen phrase table. Handles prefix overlaps deterministically.
- **6 plugins shipped** — macros, audio switching, tab finder, web shortcuts, quick ask (ARC IPC), example greeting.
- **Ko-fi integration** — support link in README and license section.
- **Test suite** — 347/347 tests passing across 8 test files.

### Changed

- Command matching upgraded from substring to token-aware longest-match.
- README rewritten with architecture diagram and feature matrix.

## \[0.9.3\] - 2026-04-21

### Added

- **Wake word debug window** — structured trace events, evaluation panel, decision timeline, token-aware wake phrase matcher.
- **Snooze from tray** — pause all listening for 5/15/30/60 minutes or until manually resumed.
- **Listening indicator** — borderless always-on-top pill showing current mode, pulses teal during capture.

### Fixed

- Wake word substring match bug — "samsara-like" no longer falsely triggers on wake phrase "samsara". Now uses token-bounded matching.

## \[0.9.2\] - 2026-04-18

### Added

- **4-state dictation model** — Replaces fragmented dictate/short/long modes with clean state machine: Asleep → Command Window → Quick Dictation → Long Dictation. Designed through ARC (multi-AI review process).
- **Auto-calibrate speech threshold** — Measures ambient noise on startup (1.5s), sets threshold using IQR-based outlier rejection. Floor of 0.0005 (not 0.01). Re-calibrates on mic switch. Configurable via Settings (Auto/Manual toggle).
- **Frequency-domain echo cancellation** — Replaced sample-by-sample NLMS with block FFT-based adaptive filter. 4096 taps at 16kHz = 256ms echo path. Fully vectorized (no Python for-loops in signal path). Diagnostic logging.
- **Command parser module** — `samsara/command_parser.py` extracts wake word command parsing into pure, testable functions. `parse_wake_command()` returns structured intent dicts (type/name/content/raw). Handles dictation keywords, filler stripping, Whisper punctuation, colon/dash separators, and joined tokens. 32 tests.
- **Wake word observability** -- Debug window upgraded to structured trace pipeline
  - Wake Word Evaluation panel shows match decision (type, index, YES/NO) per utterance
  - Decision Timeline groups all pipeline stages per utterance in a scrollable view
  - Export Timeline button writes trace to `docs/wake_word_trace_YYYYMMDD_HHMMSS.txt`
  - Main app pipeline events appear in debug window when open (optional trace callback)
  - New shared module: `samsara/wake_word_matcher.py` -- token-aware phrase matching
- **Snooze listening** -- Tray submenu to temporarily pause all listening for 5/15/30/60 min or indefinitely, then auto-resume; hotkeys are ignored while snoozed
  - Active streams (continuous, wake word) are stopped and restored on resume
  - Tray tooltip shows snooze state and resume time
  - Alarm hotkeys still work while snoozed
- **Listening state indicator overlay** -- Small always-on-top pill window shows current mode and pulses teal (#00CED1) while audio is actively captured
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
- **Tray mode switching** — Switch between all 5 modes (Hold, Toggle, Wake Word, Combined, Continuous) directly from tray right-click menu with radio checkmarks; no settings dialog needed. Central `apply_mode()` handles all state transitions.
- **Wake word correction map** — `samsara/wake_corrections.py` provides a token-level substitution map for known Whisper misrecognitions (e.g. "charvis" → "jarvis"). Applied before matching in both main app and debug window. Trace pipeline shows both RAW and CORRECTED text for easy pattern discovery.
- **Plugin command system** (scaffold) — `samsara/plugin_commands.py` with `@command` decorator, global registry, alias support, and auto-loader for `plugins/commands/*.py`. Not yet wired into `CommandExecutor.process_text`.
- **Echo cancellation module** — `samsara/echo_cancel.py` frequency-domain block NLMS with FFT overlap-save. WASAPI loopback capture for reference signal. Windows-only, disabled by default. Periodic diagnostic logging.
- **Constants module** — `samsara/constants.py` extracts magic numbers (sample rates, thresholds, timing values) from [dictation.py](http://dictation.py)
- **Pipeline tests** — `tests/test_pipeline.py` end-to-end simulation using real modules (wake_word_matcher, wake_corrections, command_parser). 21 tests.
- **Calibration tests** — `tests/test_calibration.py` validates IQR outlier rejection and threshold calculation

### Changed

- **UI extraction** — SettingsWindow, FirstRunWizard, HistoryWindow, SplashScreen extracted from [dictation.py](http://dictation.py) into `samsara/ui/`. [dictation.py](http://dictation.py) reduced from 6,555 to 3,592 lines.
- **Settings performance** — Lazy tab loading + generator-based staged building. Only builds the visible tab; others build on first click.
- **Thread-safe buffer** — `buffer_lock` added around all `speech_buffer` access to prevent race conditions between PortAudio callbacks and transcription.
- **Clipboard error logging** — Replaced 4 silent `except: pass` patterns with `_log_error()` calls for diagnosability.
- **Clipboard delay** — Reduced from `sleep(0.4)` × 3 = 1.2s per paste to configurable `sleep(0.05)` × 3 = 0.15s.
- **Stale module cleanup** — Moved deprecated [audio.py](http://audio.py), [config.py](http://config.py), [speech.py](http://speech.py)to `samsara/_stale/`.
- **Wake word is now a boolean, not a mode** -- `wake_word_enabled` config flag replaces the old `wake_word` and `combined` capture modes. Three capture modes remain (hold, toggle, continuous); wake word runs alongside any of them.
  - Tray menu shows "Wake Word" as a checkable item instead of two radio entries
  - Settings shows a checkbox beneath the capture-mode radios
  - `Ctrl+Alt+W` hotkey toggles `wake_word_enabled` on/off
  - Old configs with `mode='wake_word'` or `mode='combined'` auto-migrate to `mode='hold' + wake_word_enabled=true`
  - Snooze correctly saves/restores `wake_word_enabled` state
  - Tray tooltip shows combined state: "Hold + Wake", "Continuous", etc.
  - Listening indicator pill shows the same combined label
  - Tray icon chase animation runs while wake word listener is active
  - Icon stays animated after recording ends if wake word is still listening
  - Removed all dead 'wake_word'/'combined' mode references from code, tests, and docs
  - Updated README mode table, [ARCHITECTURE.md](http://ARCHITECTURE.md) state diagram, WAKE_WORD_GUIDE.md
- **Tray mic switching** — Now correctly stops and restarts all active audio streams (pre-buffer, wake word, continuous) on the new device. Previously only updated config without restarting streams, so the old mic kept recording. Uses closure-pattern callbacks to avoid pystray's 2-arg callback limitation.
- **Config save is now atomic** — Writes to `.json.tmp` first, rotates the existing config to `.json.bak`, then atomically promotes the temp file via `os.replace`. Prevents truncation/corruption if serialization fails mid-write.
- **Dual sample rate architecture** -- Capture at device native rate (44.1/48kHz), resample to 16kHz for Whisper. Fixes WASAPI "Invalid sample rate" errors.
  - All 5 stream sites updated to use `self.capture_rate`
  - `resample_audio()` via `np.interp` -- lightweight, no new dependencies
  - Reverted DirectSound workaround back to WASAPI (proper API, no duplicates)
  - `_detect_capture_rate()` queries device on init and mic switch
  - Wake Word Debug window also captures at native rate and resamples
- **Speech threshold default** raised from 0.01 to 0.03 RMS across all modes (config, continuous callback, wake word callback, debug window). The old default was below ambient noise floor for most environments, causing perpetual "Speaking" state that prevented silence detection from firing.
- **Toggle mode tray feedback** — `start_recording` sets tray icon to teal + tooltip to "RECORDING"; `stop_recording` restores idle state. Critical for toggle mode where there's no physical key-hold to indicate recording state.

### Fixed

- **Wake Word Debug performance** -- Reduced UI thread load by \~80%
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

- **Wake word substring false-positive** -- `wake_phrase in text` replaced with token-aware `match_wake_phrase()` in both [dictation.py](http://dictation.py) and wake_word_debug.py; "samsara-like" no longer triggers wake phrase "samsara"
- **Listening indicator vanishes behind taskbar** -- Periodically re-asserts topmost; positions inside the work area (excludes taskbar) instead of full screen
- **Listening indicator settings** -- Added enable/disable toggle and position dropdown to Settings &gt; General; default position changed to top-right to avoid taskbar overlap
- **Wake word + hotkey contention** — Wake word transcription now pauses during hotkey recording
  - Eliminates 200-800ms GPU contention delay when pressing hotkey in combined mode
  - Wake word audio stream continues running (feeds pre-buffer) but skips transcription
  - Processing resumes automatically when hotkey recording ends

### Removed

- PyTorch, TensorFlow, Keras (not needed for faster-whisper)
- OpenCV, numba, librosa, pandas, scipy (unused dependencies)

### Changed

- ARC refactored: registry router, dead code removal, running guard, persistent config, collapsible UI.

## \[0.9.1\] - 2026-04-18

### Added

- **ARC created** — Adversarial Reasoning Chain, multi-AI orchestration tool. Builder/Challenger/Auditor pipeline with Claude, GPT, and Gemini.
- **Tray quick-switch** — mode submenu in system tray with radio buttons.
- **Echo cancellation** — frequency-domain AEC with loopback capture.
- **Auto-calibration** — ambient noise measurement with IQR-based outlier rejection.
- **4-state dictation model** — hold, toggle, wake word, continuous modes.

### Changed

- Architecture docs created ([ARCHITECTURE.md](http://ARCHITECTURE.md)).
- Tray icon redesigned (teal theme).

## \[0.9.0\] - 2026-02-10

### Added

- Initial release. Hold-to-dictate with Whisper, system tray, basic voice commands, voice training module, configurable hotkeys, CUDA support.

---

## \[0.9.0\] - 2026-02-09

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

## \[0.8.0\] - 2026-02-04

### Fixed

- **Critical: Clipboard preservation on 64-bit Windows**
  - Windows API handles were overflowing due to 32-bit integer casting
  - `save_clipboard()` now properly preserves all formats (images, files, HTML, etc.)
  - User clipboard no longer destroyed after every dictation

### Added

- Centralized `samsara/clipboard.py` module with shared lock

---

## \[0.7.0\] - 2026-01-30

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

## \[0.6.0\] - 2026-01-23

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

## \[0.5.0\] - 2026-01-21

### Added

- **Expanded Wake Word System**
  - Configurable wake phrase (samsara, hey samsara, computer, jarvis, custom)
  - Three dictation modes: dictate (2s timeout), short dictate (1s), long dictate (waits for end word)
  - End word support (over, done, go, send, execute, etc.)
  - Cancel word support (cancel, abort, never mind, scratch that)
- Enhanced Settings UI with dropdowns for all wake word options

---

## \[0.4.0\] - 2026-01-20

### Added

- Voice Training button in Settings → General tab
- Profile Manager height increased to show all buttons

### Fixed

- App not fully closing on exit — all windows now properly destroyed
- Renamed "Dictionary Profiles" to "Voice Training Profiles" for clarity

---

## \[0.3.0\] - 2026-01-15

### Added

- Scrollable Settings tabs — content no longer cut off
- Model size dropdown shows disk space requirements
- Created Samsara-dev folder for active development

---

## \[0.2.0\] - 2026-01-11

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

## \[0.1.0\] - 2026-01-09

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

## \[0.0.1\] - 2026-01-08

### Added

- Initial release
- Python-based speech-to-text using faster-whisper
- GPU acceleration with CUDA
- Multiple Whisper model sizes (tiny to large-v3)
- System tray integration
- Global hotkey support
- JSON configuration
