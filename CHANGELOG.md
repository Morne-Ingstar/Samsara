# Changelog

All notable changes to Samsara are documented here.

## [Unreleased]

### Fixed

- **Tasks are local-only** — removed the obsolete Arcana task-sync setting,
  network delivery code, and config migration. Voice-added tasks remain on the
  local machine with no task-sync pathway.

## [0.21.0] - 2026-07-10

The trust release. The theme is dictation you can rely on: an adversarial
review series audited the transcription pipeline end to end (six audits,
independent AI reviewers, findings verified against the code), and the fixes
shipped here. Also: spoken formatting, a live Quick Reference, a plain-English
health readout, and CI-built releases as the first step toward signed
downloads.

### Fixed

- **"You know" no longer vanishes from dictation** — an overzealous filler
  cleanup rule deleted the phrase "you know" from every dictation, anywhere
  in the sentence, whether it was filler or not. It's now comma-anchored
  like the other filler rules: "It's, you know, complicated" still cleans
  up; "you know what I mean" survives intact.
- **A voice-only escape that cannot be blocked** — abort words now use
  word-boundary matching ("report" can no longer accidentally trigger
  "abort"), work from every session lane, and are structurally guaranteed
  to bypass all quality gates: a user with degraded audio can always end a
  stuck session by voice.
- **Wake listener goes fully deaf during hotkey dictation** — previously it
  kept processing your speech in parallel (its suppression only applied
  before speech began), corrupting the shared voice-detection state and
  double-transcribing. One recording, one listener, always.
- **Clipboard preservation hardened** — copied *files* (Explorer) and
  transparent images (CF_DIBV5) now survive a dictation paste; restore is
  atomic (a failure can no longer leave your clipboard empty); a copy you
  make mid-paste is never clobbered (sequence-number guard); a hung
  clipboard owner can't freeze Samsara.
- **Scratch-that can't delete text in the wrong app** — destructive undo
  keystrokes now refuse to fire if the focused window changed since the
  text was dictated, instead of deleting content in whatever app happens
  to be focused.
- **Prefix lane switches are transactional** — "dictate: hello world"
  reverts cleanly (audibly) if delivery fails instead of stranding you in
  the new mode with nothing delivered, and payload spacing survives
  verbatim.
- **Audio-engine correctness pass** — COM handle leak in the output-device
  watcher fixed (was leaking every 2 seconds for the app's lifetime); the
  device-death sound no longer plays from inside the dying audio callback
  (a deadlock risk exactly when a Bluetooth mic drops); shutdown gives every
  thread a fair share of the timeout.
- **Smart Corrections hardening (8 fixes)** — tighter change budget (a
  correction can alter at most 15% of your words), structural artifacts
  (think-tags, code fences) cause the correction to be rejected rather than
  "cleaned," language handling no longer force-translates, cloud fallback
  reports outages accurately, and timeouts are signaled structurally instead
  of by string-matching.
- **Multilingual correctness** — hallucination blacklist now fires only when
  a junk phrase dominates the transcript (mentioning "amara.org" in a real
  sentence no longer deletes your sentence), corrections handle mixed-script
  vocabulary correctly, and Unicode normalization makes visually identical
  text compare equal.
- **Stress Test Wizard actually captures dictation** — it previously watched
  the clipboard and failed every step; it now routes through the real
  dictation pipeline, reads your actual hotkey from config for its
  instructions, and scores long utterances on the complete result.

### Added

- **Spoken formatting tokens** — say "new line", "new paragraph", "tab", or
  "bullet" while dictating and get real formatting, applied after
  corrections so nothing mangles it. Collision-guarded ("open a new tab"
  stays literal). Toggleable in config.
- **Quick Reference window** — a tray-menu cheat sheet of your hotkeys, wake
  phrases, send word, lane-switch phrases, and formatting tokens — every
  value read live from your current settings, never hardcoded.
- **Plain-English dictation health verdict** — the Diagnostics panel now
  opens with a sentence a human can act on ("Audio is clear but the model is
  struggling — try a larger model or add problem words to your vocabulary")
  computed from your recent dictations, instead of only a wall of numbers.
- **Tray menu reorganized** — daily actions stay one click away; occasional
  tools grouped under Tools; debug surfaces under Developer. Nothing removed.
- **CI-built releases** — every release is now built by a public GitHub
  Actions workflow from tagged source. This is the prerequisite for signed
  releases (SignPath), coming next.
- **WER benchmark** — opt-in collection of your own dictation samples plus an
  offline accuracy sweep across Whisper models, so "which model hears *me*
  best" is a measurement, not a guess.

### Changed

- **Homegrown echo cancellation disabled by default** — it converged to only
  3–8% cancellation and review concluded it likely added artifacts. It will
  be replaced by an industry implementation (WebRTC AEC3 / OS-level) in a
  future release; the code remains for opt-in.
- **Relicensed to AGPL-3.0** (from BSL) — qualifies Samsara for free
  open-source code signing and grant eligibility.

## [0.20.0] - 2026-07-02

A polish-and-hardening release. No single headline feature — this is weeks of
fixing things that were quietly wrong: dictation hallucinating during silence,
Samsara going deaf when a mic disconnected, onboarding screens with invisible
buttons and clipped text, and a startup path that could lock you out with a
stale lock file. The version jump from 0.12.x to 0.20.0 reflects the scope of
this cleanup, not one single new headline feature.

### Fixed

- **Dictation hallucination during silence** — Whisper no longer fabricates
  text during near-silent holds or short pauses. Replaced leaky heuristics
  with causal fixes (native no-speech/confidence thresholds, per-press state
  reset so one hallucination couldn't poison the next, a speech-presence
  gate on short buffers, and a brief fade on the recording buffer to kill
  mechanical hotkey-click artifacts), and locked the tuned parameters in
  with a regression test suite.
- **Microphone disconnect/reconnect no longer breaks dictation** — unplugging
  a mic or a Bluetooth headset dropping out used to leave Samsara deaf until
  restart. Audio input now survives a device disconnect and resumes
  automatically on reconnect.
- **Invisible onboarding buttons** — the first-run wizard's and tutorial's
  primary buttons ("Next", "Let's go", "Start using Samsara") could render
  as dark text on a dark background, effectively invisible, on a fresh
  install.
- **Clipped onboarding text and buttons** — several wizard/tutorial pages
  had fixed-size boxes sized for an older, smaller font; labels and button
  text could get cut off. Sizing now follows actual text metrics instead of
  hardcoded pixel widths.
- **Stray borders around plain text** — some onboarding and tutorial text
  was rendering with a faint input-box-like outline around it, a Qt
  stylesheet quirk fixed at the source.
- **Startup could get stuck behind a stale lock file** — if Samsara was
  force-killed (crash, Task Manager, a bad update), the next launch could
  refuse to start and report "already running" even though nothing was.
  Samsara now checks whether the process that actually owns the lock is
  still alive and still Samsara before refusing to start, and cleans up
  after a dead one automatically.
- **Duplicate log lines on startup** — some boots logged every line twice
  because of a root-logger setup race; logging now initializes exactly
  once.
- **First-run wizard could leave the app stuck** on a failed launch instead
  of continuing with defaults, and could write its config somewhere that
  vanished on the next update. Both fixed.
- Windowed build no longer crashes on launch when Windows redirects
  stdout/stderr to nothing.
- Config migration no longer breaks on older configs missing the `modes`
  key.

### Changed

- **History window overhaul** — right-click a row for Copy/Delete, or
  double-click to copy instantly. Timestamps now show full date and time
  instead of just time, and the window's look was brought in line with the
  rest of the app.
- **Onboarding redesigned** — the first-run wizard and tutorial now share
  one consistent visual design across every screen. The tutorial's
  interactive Ava step was removed to keep the walkthrough focused on the
  core loop (Ava and "show numbers" are pointed to from the wrap-up
  instead), and wake-word instructions only appear when wake word is
  actually turned on.
- **Splash screen** — the old progress bar is now a spinning, brand-red
  segmented wheel matching Samsara's own icon.
- **Ava and session modes reworked** — Command, Dictate, and Ava now run
  through one unified state machine instead of scattered mode flags, with
  a dedicated Ava lane and a "substance gate" that skips agent calls for
  coughs and stray syllables that aren't real requests.
- **BYOK cloud AI is free** — the license gate on bring-your-own-key cloud
  AI has been removed.
- **Settings reorganized** — mode activation (Command/Dictate/Ava) now
  lives in one consolidated Modes tab instead of being spread across
  several.
- **Earbud-style media controls** — play/pause/next/mute now route through
  Windows' native media transport controls, working reliably across
  Spotify, browser tabs, and other media apps, with per-app Spotify muting.

### Under the hood

- **Thread registry** — every background thread and timer now spawns
  through a single tracked registry instead of bare
  `threading.Thread`/`Timer` calls, so shutdown can account for everything
  instead of leaking daemon threads.
- **Fail-loud pass** — swallowed exceptions across the codebase now log
  instead of silently vanishing; stray debug `print()`s route through the
  real logger.
- **Centralized per-user data directory** — config, logs, and every
  per-user file now resolve through one function, with an environment
  override for isolated testing.
- **Frozen-build smoke harness** — one command builds the release EXE and
  runs it through boot, first-run-wizard, and clean-shutdown checks against
  an isolated profile, catching the class of bug that only shows up in a
  frozen build.

---

## [0.12.0] - 2026-06-30

Hands-free multi-wakeword dictation and microphone-agnostic wake detection.

### Added

- **Multi-wakeword hands-free dictation** — say a named wake phrase ("Hey Claude", "Activate Hermes") to focus that app's window and start dictating into it, fully hands-free. Wake phrases are configurable per target (process-name targeting, restores minimized windows). Custom OpenWakeWord models with Whisper-transcript fallback.
- **Open-ended wake sessions** — a wake-triggered session survives silence (does not end on a short pause), appending each spoken utterance into the target. Ends on inactivity timeout or a spoken send word.
- **Send terminator** — end an utterance with "over"/"send" to submit; only the final spoken word is checked, so the word mid-sentence is typed normally. Per-target send policy: Claude submits on send; agentic targets (Hermes) leave text staged.
- **Earcons** — session-start and sent audio cues for hands-free feedback.
- **Adaptive microphone gate** — wake detection now measures speech relative to a rolling ambient noise floor instead of a fixed energy threshold, so low-output mics (headsets, USB mics with AGC) work without manual tuning. Includes a mic-calibration helper.

### Fixed

- Wake-session inactivity timer no longer races in-flight transcription (last utterance could be dropped).
- Wake corrections no longer self-corrupt on repeated canonical phrases.

---

## [0.9.9] - 2026-05-15

Focused on accessibility, voice-control surface area, and reliability. Adds a floating command cheat sheet, monitor-to-monitor window moves, a phonetic-collision audit, a voice-driven semantic clicking overlay, and an echo-cancellation calibration tool. Also fixes several silent failures around config persistence, CUDA fallback, and Whisper model selection that were degrading user experience without obvious symptoms.

### Added

- **Floating command cheat sheet** — semi-transparent always-on-top window listing your voice commands. Resizable, draggable, click-through-friendly. Real-time filter bar, manual pinning (★/☆) so you can keep your most-used commands at the top, and click-to-execute fires the same command path as voice. 300ms teal flash before execution gives visual feedback. Position, size, opacity, and pinned commands persist to `command_palette.json`. Trigger via tray menu "Command Reference" or voice: "show commands" / "hide commands".
- **Show numbers overlay (experimental)** — voice-driven semantic clicking. "Jarvis, show numbers" labels every clickable element in the foreground window with a number; "Jarvis, click 7" performs a left-click on element 7. Supports "click thirty seven", "click 7 twice" (double), "click 7 right". Uses Windows UI Automation tree, foreground-window scope only. Auto-dismisses on click, hide command, foreground change, or 30s inactivity.
- **AEC calibration harness** — "Calibrate Echo Cancellation" in the tray menu plays a 50ms log-chirp through speakers, records the mic, and cross-correlates to measure speaker-to-mic latency. Reports a confidence-scored lag in milliseconds for tuning `latency_ms` in the echo cancellation config. Replaces guessing with measurement.
- **Phonetic collision audit tool** (`tools/phonetic_audit.py`) — uses CMU pronouncing dict to find command pairs that sound similar. One-shot report identifies homophones, near-collisions, and overlaps with common English speech. Run before any command-naming changes.
- **Command routing test harness** (`tools/test_commands.py`) — validates every registered command (builtins + plugins) can resolve to a handler. Catches broken hotkey keys, missing methods, dead plugin references, and stale paths before users hit them. Run before each release.
- **AI command pack** — open Claude / ChatGPT / Gemini / Perplexity / Midjourney / Anthropic / Hugging Face / GitHub, plus shortcuts to Claude desktop, projects, settings, and new chat. 18 commands total.
- **Accessibility command pack** — Magnifier (zoom in/out), Narrator (start/stop), high contrast, color filters, live captions, mono audio, on-screen keyboard, sticky keys, mouse keys, eye control, big cursor, accessibility settings, focus mode / do not disturb, immersive reader, night light, dark mode, and more. 44 commands total.
- **Window manager voice commands** — "send to far right", "send to far left", "send to middle monitor", "send to main monitor" for multi-monitor setups. "send maximized right" / "send maximized left" handle maximized windows automatically. Standard "move to next monitor" / "move to right monitor" for single-step moves.
- **Steam navigation commands** — "steam library", "steam friends", "steam store", "steam downloads" via `steam://` protocol URIs.
- **Twitch commands** — "open twitch", "open twitch directory", "open twitch following".
- **Obsidian command pack** — 27 commands for new note, quick switcher, palette, search, replace, graph view, daily note, indent/unindent, and more.
- **Per-monitor DPI awareness** — Samsara now calls `SetProcessDpiAwareness(2)` at startup. Fixes click-coordinate offset issues on 150% scaling and HiDPI displays.
- **Bluetooth mic refresh** — mic list refreshes when the tray menu opens. New mics (especially BT earbuds) appear without restarting Samsara. Name-based reconciliation handles unstable PortAudio device indices across reconnects. Skips refresh during active capture.
- **Whisper `.en` model variants in Settings** — `tiny.en`, `base.en`, `small.en`, `medium.en` now appear in the model dropdown. `small.en` is the recommended sweet spot for English speakers on GPU.
- **Continuous mode** has its own speech threshold (`continuous_speech_threshold`, default 0.03), independent of wake word threshold which is intentionally higher to avoid false activations.

### Changed

- **Splash screen stays visible until the model has actually loaded.** Previously closed after 3 seconds even though Whisper could still be loading for another 20-60s, leaving users wondering why dictation wasn't working. Now reflects real readiness.
- **Settings window opens at 920×700** (was 700×700). With the additional packs and tabs added this release, the old size squished tabs together.
- **Media commands route through SMTC plugin** — "pause music", "play music", "next track", "previous track" now use Windows SMTC (System Media Transport Controls) via the `media_keys` plugin. Replaces broken pynput `playpause` / `nexttrack` / `prevtrack` virtual-key strings that never actually worked. Reliable across Spotify, browser tabs, Stremio, and any other media app.
- **Phonetic-aware command aliases** — `remove file` added as the preferred trigger for file deletion (was `delete file`, phonetically too close to `delete line`). Old phrase kept as alias.
- **Streaming dictation direct-paste** uses validated Ctrl+Z + Ctrl+V cycle (ARC-reviewed, empirically tested). Word-boundary selection replaced.
- **Tray menu "Command Reference"** toggle opens / hides the cheat sheet.

### Fixed

- **Config save no longer wipes external edits.** `save_config()` now deep-merges in-memory config over the on-disk file before writing. Previously, editing `config.json` externally while Samsara was running would silently lose those changes when Samsara exited and overwrote the file with its in-memory state. This was the root cause of the recurring "command packs disappear after restart" pattern.
- **CUDA detection works correctly.** The Settings dropdown previously hid the CUDA option when `cublas64_12.dll` wasn't findable, which made it look like CUDA support was missing. Added README documentation and detection guidance. Samsara also no longer silently falls back to CPU at model load when the user explicitly selected CUDA — a clear log line surfaces what happened.
- **Command mode (Right Ctrl)** now actually executes commands. A wrong-object `hasattr` guard in `CommandExecutor.process_text` was blocking every execution path. ARC tribunal review identified the root cause.
- **Click-to-execute in cheat sheet** uses the same code path as wake-word command mode (`force_commands=True`). Previously called a non-existent keyword argument and silently no-op'd.
- **VAD/Silero error spam** — TorchScript exceptions from Silero VAD are now rate-limited (one log per 30s), the model state is reset on error to attempt recovery, and after 50 consecutive failures Samsara disables VAD for the session and runs on RMS-only speech detection. Previously dumped a 30-line traceback on every audio chunk during a state-corruption episode.
- **Continuous mode** now reads from `continuous_speech_threshold` instead of `wake_word_config.audio.speech_threshold`. The wake threshold (typically 0.15) was too high for normal-volume speech, so continuous mode captured nothing.
- **Settings model dropdown** displays the currently-selected `.en` variant correctly. Previously fell back to showing `base (~150 MB)` for any `.en` model because the display map didn't include them.
- **Splash screen** is now closed by the model-load worker thread on completion, not pre-emptively closed by the main thread before the worker even starts.
- **Continuous mode toggle wiring fix** — Right Ctrl + voice commands now properly enter command mode regardless of starting state.

### Known issues

- Some commands may still trigger Whisper's `no_speech_threshold` filter (default 0.6) and be silently discarded on borderline audio. Working as designed, but worth knowing if a transcribed phrase mysteriously fails to appear.
- The show numbers overlay currently uses Tkinter with `-transparentcolor` which can darken the entire screen on some Windows configurations instead of showing crisp labels. A Win32 layered-window rewrite is planned for the next release.
- VAD/Silero state corruption is suppressed in the logs but the underlying torch issue remains. Doesn't affect functionality (RMS fallback works) but if you see persistent `[VAD] inference error` lines, the dictation pipeline is still operating correctly.

---

## [0.9.8] - 2026-05-01

This release bundles all work since v0.9.5: the new main hub window, dictation history, streaming dictation, and four new plugins (Hyperion lights, FlashForge 3D printer, Spotify music, screen recording / GIF search). Also includes substantial reliability fixes for hold-to-dictate, wake word recognition, and audio recovery after sleep/wake.

### Added

- **Streaming dictation** — real-time voice-to-text with live overlay. Hold the streaming hotkey (CapsLock) and watch text appear and update as you speak, with a polished final paste on release. Two modes: overlay-only (safe) and direct-paste (text flows into the focused app in real time). Configurable via `streaming_mode` and `streaming_direct_paste` in config.
- **CapsLock streaming hotkey** — suppressed at the OS level so it never toggles caps while Samsara is running. atexit handler guarantees the hook is released even on crash, so users never end up with a stuck CapsLock key.
- **Main hub window** — opens on launch with sidebar navigation (History, Dictionary, Settings). Closing minimizes to tray. Double-click tray icon to reopen.
- **Dictation history** — SQLite database logging every transcription with timestamp, source app, raw text, cleaned text, duration, mode, and success/fail status. Searchable, with copy/retry/delete.
- **Unified dictionary UI** — three-tab corrections manager (Vocabulary, Corrections, Wake Words) in the main window. Add, edit, delete corrections from the UI. User corrections stored in `~/.samsara/` as JSON, hot-reloaded without restart. Hardcoded defaults are read-only; user overrides merge on top.
- **Grammar-Lite cleanup** — post-Whisper processing removes filler words (um, uh, like), fixes capitalization, adds missing punctuation. Two modes: Clean (default) and Verbatim. Raw transcript always preserved in history.
- **Hyperion LED strip control** — voice-controlled ambient lighting via Hyperion JSON API. "Jarvis, lights red", "lights effect rainbow", "lights off". Supports hostname, IPv4, and IPv6. 11 preset colors, 14 effect aliases with fuzzy matching.
- **FlashForge 3D printer control** — voice commands for printer status, pause/resume/cancel, chamber light toggle, file listing. TCP M-code protocol on port 8899. Tested on AD5X firmware v1.2.3.
- **Spotify music playback** — "Jarvis, play me some music" opens tracks in the Spotify desktop app. Pre-configured library, user-configurable music_library, volume control, Spotify search fallback.
- **Timer plugin** — "set a timer for 5 minutes" with natural language parsing, background thread, Windows notification on completion.
- **GIF search plugin** — "search for a gif of dancing cat" opens Giphy.
- **Screen recording plugin** — "record my screen" / "record this window" captures screen to GIF using mss (DXGI). Persistent red REC indicator, 30-second safety cap, active window capture via ctypes.
- **Audio auto-reconnect** — stream health monitor detects dead PortAudio streams after sleep/wake, auto-reconnects with exponential backoff (max 5 retries). Windows toast notification on reconnect.
- **Wake word corrections** — harvest, charge us, charge, driver's, drivers added to Jarvis recognition.
- **Music command mishearing aliases** — Whisper sometimes hears "place of music" or "plays some music" instead of "play some music". All common variants now route to the same handler.

### Changed

- Streaming first-chunk latency reduced from 1.3s to 1.0s (0.7s first chunk + 0.3s transcription).
- Streaming update interval tightened from 1.5s to 1.0s between partials.
- Partial transcriptions use beam_size=1 (greedy, fast), final pass uses beam_size=5 (full beam search).
- Pre-buffer skipped in streaming mode to eliminate 1.5s of unnecessary initial latency.
- Direct-paste cycle uses Ctrl+Z + Ctrl+V instead of select-and-replace by word boundary. Works reliably across Notepad, browser fields, and Obsidian. Tunable via `UNDO_SETTLE_S` and `PASTE_SETTLE_S` constants.
- History tab performance: callback-based updates replace 5-second polling, pagination limits initial load to 50 entries.
- Main window UI restyled with blue-teal tinted dark theme.
- Voice Training window refactored into reusable CTkFrame components (HistoryFrame, DictionaryFrame) shared between main window and standalone windows.

### Fixed

- **Hold-to-dictate VAD bypass** — faster-whisper's internal VAD was stripping 80% of speech during explicit hotkey recordings. Now disabled for hold-to-dictate and long dictation modes.
- **Min audio guard** — Whisper hallucinations ("Thank you", "Subtitles by Amara") on sub-0.5s clips. Audio shorter than 0.51s is now silently discarded.
- **No ghost typing** — unrecognized wake word commands no longer paste garbage into focused apps. No-match goes silently back to sleep.
- **History tab close** no longer spams TclError from stale widget redraws.
- **Tk shutdown-race filter** installed on the root window. Suppresses benign "invalid command name" / "application has been destroyed" TclErrors that fire from CTk widget `<Configure>` handlers during teardown. Real exceptions still surface.
- **Quiet third-party loggers** — torio, torchaudio, urllib3, huggingface_hub pinned to WARNING. voice_training.py raises root logger to DEBUG at import, which was producing FFmpeg traceback spam on every probe.
- **Streaming module imports** — pyautogui and pyperclip moved to top-level imports. Inline imports in the hot path were an anti-pattern.

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
