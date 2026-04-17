# 🔄 Samsara

### Voice Dictation & Control for Accessibility

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-blue.svg)]()
[![Status](https://img.shields.io/badge/status-early%20dev-orange.svg)]()

---

## From the Developer

> Hi, I'm Morne, and my hands fucking hurt.
>
> I've had HSD (Hypermobile Spectrum Disorder) for a decade. Using a mouse and keyboard hurts — all the time. But thanks to AI tools, I can now build software that reduces how much I need to type and click.
>
> I tried paid apps that do similar things, but they were fragmented, expensive, and didn't quite fit my needs. So I built Samsara — combining everything into one free, open-source tool.
>
> This is early-stage software. It works for me daily, but expect rough edges, half-finished features, and occasional breakage. If you find it useful or want to help improve it, I'd love to hear from you.
>
> — Morne

---

## What is Samsara?

Samsara is a **fully offline** voice dictation and command system powered by OpenAI's Whisper. It runs as a Windows system tray app that records audio, transcribes it locally using your GPU, and types the result into whatever application is focused.

Built for accessibility first — specifically for someone with chronic hand pain who needs to minimize keyboard and mouse use.

**What it does well right now:**
- Hold-to-dictate with near-instant CUDA transcription (~300ms for most utterances)
- 40+ voice commands (copy, paste, undo, open apps, etc.)
- Wake word activation ("Jarvis, dictate...")
- Pre-buffer captures 1.5s of audio *before* you press the hotkey, so first words aren't lost
- Custom vocabulary and correction dictionaries
- Sound themes, alarms, and break reminders

**What's rough / in progress:**
- Settings and debug windows use CustomTkinter and can be sluggish (scrolling, resizing)
- Wake word mode works but needs tuning per-environment (speech threshold, correction map)
- Toggle and continuous modes are functional but less tested than hold-to-dictate
- Cross-platform support exists in theory but Windows is the only tested target
- The codebase is a ~6,200-line monolith (`dictation.py`) with a partially-completed modular refactor

---

## Quick Start

### From Source (recommended for now)

```bash
git clone https://github.com/Morne-Ingstar/Samsara.git
cd Samsara
pip install -r requirements.txt
python dictation.py
```

A first-run wizard will guide you through microphone selection, model download, and hotkey configuration.

### Windows Installer

A standalone Windows build (no Python required) is available on the [Releases page](https://github.com/Morne-Ingstar/Samsara/releases). Note: the installer may lag behind the dev branch.

### Usage

- Hold **Ctrl+Shift** and speak, release to transcribe
- Right-click the tray icon to switch modes, change mic, snooze, or open settings
- Say your wake word (default: "Jarvis") followed by a command for hands-free control

---

## Features

### Dictation Modes

| Mode | How it works |
|------|-------------|
| **Hold** (default) | Hold hotkey to record, release to transcribe. Most reliable. |
| **Toggle** | Tap hotkey to start recording, tap again to stop. Tray icon shows recording state. |
| **Continuous** | Always listening, auto-transcribes on speech pauses. |

**Wake Word** is a separate toggle (not a mode) that works alongside any capture mode above. Enable it via the tray menu checkbox, `Ctrl+Alt+W` hotkey, or Settings. When enabled, say your wake phrase (e.g. "Jarvis") to activate hands-free commands and dictation while still using hotkeys for on-demand recording.

All modes and wake word are switchable instantly from the tray right-click menu.

### Wake Word System

- Configurable wake phrase (Jarvis, Samsara, Computer, or custom)
- Token-aware matching prevents false triggers on similar words ("samsara-like" won't fire)
- Dictation sub-modes: "dictate" (auto-timeout), "short dictate" (quick), "long dictate" (waits for end word)
- End word ("over"), cancel word ("cancel"), pause word ("pause") support
- Wake word correction map for known Whisper misrecognitions (add entries as you find them)
- Full debug/observability window with trace pipeline, evaluation panel, and decision timeline

### Voice Commands

40+ built-in commands loaded from `commands.json`:

```
"new line"          → Enter key
"select all"        → Ctrl+A
"copy that"         → Ctrl+C
"paste"             → Ctrl+V
"undo"              → Ctrl+Z
"open chrome"       → Launches Chrome
```

Command types: `hotkey` (key combos), `launch` (start programs), `macro` (scripted multi-step sequences).

Custom commands can be added via Settings → Commands tab or by editing `commands.json` directly.

### Audio Pre-Buffer

A rolling 1.5-second audio buffer runs in the background during hold/toggle modes. When you press the hotkey, the buffer's contents are prepended to your recording — so the first words you speak are never lost to startup latency.

### Accessibility Extras

- **Alarm reminders** — interval-based nag-until-dismissed alarms (hydration, stretch, break) with streak tracking
- **Key macros** — tap-combos and toggle-hold for accessibility (e.g. triple-tap W to auto-hold W)
- **Toast notifications** — timed reminders via Windows toast
- **Echo cancellation** — optional WASAPI loopback filter to prevent transcribing system audio (experimental)
- **Clipboard preservation** — saves and restores your clipboard around every paste operation
- **Listening indicator** — always-on-top pill overlay showing current mode, pulses teal when recording

### Sound Themes

Four built-in themes: `cute` (playful bloops), `warm` (rich chords), `zen` (singing bowls), `chirpy` (bright chirps). Custom themes supported — add a folder under `sounds/themes/`. Supports WAV, MP3, OGG, FLAC.

### Snooze

Temporarily pause all listening from the tray menu (5 min / 15 min / 30 min / 1 hour / until resumed). Active streams are stopped and auto-restored when snooze expires.

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **OS** | Windows 10 | Windows 11 |
| **Python** | 3.10 | 3.11+ |
| **RAM** | 4 GB | 8 GB+ |
| **GPU** | None (CPU works) | NVIDIA with 4GB+ VRAM |
| **Disk** | 2 GB | 10 GB (for larger models) |

### Whisper Model Sizes

| Model | Speed | Accuracy | VRAM | Disk |
|-------|-------|----------|------|------|
| tiny | Fast | Fair | ~1 GB | ~75 MB |
| base | Fast | Good | ~1 GB | ~150 MB |
| small | Moderate | Good | ~2 GB | ~500 MB |
| medium | Slow | Very good | ~5 GB | ~1.5 GB |
| large-v3 | Slowest | Best | ~10 GB | ~3 GB |

---

## Default Hotkeys

| Hotkey | Action |
|--------|--------|
| `Ctrl+Shift` | Hold to dictate (default mode) |
| `Ctrl+Alt+D` | Toggle continuous mode |
| `Ctrl+Alt+W` | Toggle wake word mode |
| `Ctrl+Alt+C` | Command-only recording (no text output) |
| `Escape` | Cancel current recording |
| `F7` | Complete alarm (gets streak credit) |
| `F8` | Dismiss alarm (no credit) |

All hotkeys are configurable in Settings.

---

## For Developers

### Architecture Overview

`dictation.py` (~6,200 lines) is the monolith that runs the app. It contains six classes: `SplashScreen`, `FirstRunWizard`, `CommandExecutor`, `SettingsWindow`, `HistoryWindow`, and `DictationApp` (the brain — owns all state, streams, timers, tray, and windows).

Supporting modules live under `samsara/`:

```
samsara/
├── wake_word_matcher.py      # Token-aware wake phrase matching (shared)
├── wake_corrections.py       # Whisper misrecognition correction map
├── clipboard.py              # Win32 clipboard save/restore for paste operations
├── key_macros.py             # Tap-combos and toggle-hold for accessibility
├── notifications.py          # Windows toast reminders
├── alarms.py                 # Nag-until-dismissed alarms with streak tracking
├── echo_cancel.py            # WASAPI loopback + NLMS adaptive filter
├── profiles.py               # Dictionary/command profile import-export
├── plugin_commands.py        # Plugin-based command system (scaffold, not yet wired)
└── ui/
    ├── wake_word_debug.py    # Wake word debug window with trace pipeline
    ├── listening_indicator.py # Always-on-top mode/listening overlay
    ├── profile_manager_ui.py # Profile manager window
    └── splash.py             # Splash screen (stale — duplicates class in dictation.py)
```

**Important caveat:** some modules under `samsara/` are from a partial refactor that was never completed. `samsara/commands.py`, `audio.py`, `config.py`, `platform.py`, `speech.py` are **stale** — only the test suite imports them. The canonical implementations live in `dictation.py`. When in doubt, `dictation.py` is the source of truth.

### Config and Commands

User configuration lives in `config.json` (auto-created on first run, not committed). Voice commands are defined in `commands.json`. Both are documented in detail in the architecture reference (`docs/` or ask for the latest version).

Config saves use atomic writes (temp file + `os.replace`) with automatic `.bak` rotation, so a crash mid-save never corrupts the config file.

### Threading Model

The main thread runs tkinter. Background threads handle: Whisper transcription (guarded by `self.model_lock`), audio stream callbacks (PortAudio), pystray tray icon, pynput keyboard listener, alarm/notification schedulers, and echo cancellation capture. Cross-thread UI updates use `self.root.after(0, callable)`.

### Running Tests

```bash
cd Samsara
pip install -r requirements-test.txt
python -m pytest tests/ -v
```

The wake word matcher has its own focused test suite:
```bash
python -m pytest tests/test_wake_word_matcher.py -v
```

### Contributing

Contributions welcome. The most impactful areas right now:

1. **Settings UI performance** — the CustomTkinter windows are sluggish; needs either lazy tab loading, widget caching, or a different UI approach
2. **Wake word robustness** — building the correction map, improving silence detection, possibly adding fuzzy matching
3. **Completing the modular refactor** — extracting classes from `dictation.py` into the `samsara/` package
4. **Cross-platform testing** — macOS and Linux are untested

Please fork the repository, create a feature branch, and submit a pull request. For bugs or feature requests, [open an issue](https://github.com/Morne-Ingstar/Samsara/issues).

---

## Troubleshooting

**App won't start?**
- Run `python dictation.py` from a terminal to see errors (console is hidden by default)
- Check Python 3.10+ is installed
- Run `pip install -r requirements.txt`

**No transcription / first words cut off?**
- Check microphone selection in Settings or tray menu
- Test mic in Voice Training → Calibration
- Ensure Whisper model is loaded (check tray tooltip)
- Pre-buffer should capture first words — if not, check `[PRE]` lines in console

**Wake word not triggering?**
- Open Wake Word Debug from the tray menu
- Check the speech threshold — if the level meter stays green (speaking) continuously, your threshold is too low for your environment
- Look at the Decision Timeline — does it show the right "Heard:" text? If Whisper is misrendering your wake word, add the variant to `samsara/wake_corrections.py`

**Commands not working?**
- Enable command mode in Settings (or say "command mode on")
- Commands are case-insensitive
- In wake word mode, commands always work regardless of the command-mode toggle

**GPU not detected?**
- Samsara uses `ctranslate2` for CUDA detection (no PyTorch required)
- Verify with `nvidia-smi` that your GPU is visible
- Falls back to CPU automatically if CUDA isn't available

---

## Roadmap

- [x] Alarm reminders with persistent sound notifications
- [x] Standalone Windows EXE (no Python needed)
- [x] Wake word debug/observability window
- [x] Tray-based mode switching (no settings dialog needed)
- [x] Listening state indicator overlay
- [x] Snooze listening from tray
- [x] Token-aware wake word matching (prevents false triggers)
- [x] Atomic config saves with backup rotation
- [ ] Plugin system for custom commands
- [ ] Auto-calibrate speech threshold on startup
- [ ] Undo last dictation
- [ ] Command chaining ("select all copy")
- [ ] Application-specific command profiles
- [ ] Spelling mode ("spell c-a-t" → "cat")
- [ ] Usage statistics dashboard

---

## License

MIT License — free for personal and commercial use.

---

## Acknowledgments

- [OpenAI Whisper](https://github.com/openai/whisper) — Speech recognition model
- [faster-whisper](https://github.com/guillaumekln/faster-whisper) — Optimized CTranslate2 implementation
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — Modern UI framework
- Built with assistance from [Claude](https://anthropic.com) by Anthropic and [ChatGPT](https://openai.com) by OpenAI

---

<p align="center">
  <i>Named after the Buddhist concept of cyclical existence — Samsara helps break the cycle of repetitive strain.</i>
</p>
