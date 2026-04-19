# <img src="logo.png" width="28" height="28"> Samsara

### Voice Dictation & Control for Accessibility

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-blue.svg)]()
[![Status](https://img.shields.io/badge/status-early%20dev-orange.svg)]()

<p align="center">
  <img src="demo.gif" alt="Samsara Demo — saying 'Jarvis, open Chrome' launches Chrome hands-free" width="800">
  <br>
  <em>Say "Jarvis, open Chrome" → Samsara hears the wake word, transcribes the command locally via Whisper, and launches Chrome. No cloud, no internet, no typing.</em>
</p>

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

Samsara is a **fully offline** voice dictation and command system powered by
OpenAI's Whisper. It runs as a Windows system tray app that records audio,
transcribes it locally using your GPU, and types the result into whatever
application is focused.

Built for accessibility first — specifically for someone with chronic hand
pain who needs to minimize keyboard and mouse use.

**What it does well right now:**
- Hold-to-dictate with near-instant CUDA transcription (~300ms)
- 4-state dictation model: Asleep → Command Window → Quick/Long Dictation
- 40+ voice commands (copy, paste, undo, open apps, etc.)
- Wake word activation ("Jarvis, dictate...")
- Pre-buffer captures 1.5s before hotkey press — first words never lost
- Auto-calibrating speech threshold with IQR outlier rejection
- Frequency-domain echo cancellation (WASAPI loopback + FFT-based filter)
- Custom vocabulary and correction dictionaries

**What's rough / in progress:**
- Wake word mode works but needs tuning per-environment
- Cross-platform support exists in theory but Windows is the only tested target
- Echo cancellation is functional but may need parameter tuning for different rooms

---

## Quick Start

### From Source (recommended for now)

```bash
git clone https://github.com/Morne-Ingstar/Samsara.git
cd Samsara
pip install -r requirements.txt
python dictation.py
```

A first-run wizard will guide you through microphone selection, model
download, and hotkey configuration.

### Usage

- Hold **Ctrl+Shift** and speak, release to transcribe
- Right-click tray icon to switch modes, change mic, snooze, or open settings
- Say your wake word (default: "Jarvis") followed by a command

---

## Features

### Dictation Model (4-State Machine)

Samsara uses a clean 4-state model designed through a multi-AI review
process ([ARC](https://github.com/Morne-Ingstar/ARC)):

| State | Trigger | Behavior |
|-------|---------|----------|
| **Asleep** | Default | Wake word listener active, all other audio discarded |
| **Command Window** | Wake word detected | 3-second window for action verbs, then back to Asleep |
| **Quick Dictation** | Hotkey or "type..." | 1.0s silence timeout, auto-transcribes and pastes |
| **Long Dictation** | Continuous hotkey or "dictate..." | No timeout — requires "over"/"done" to finish, supports pause/resume |

**Wake Word** works alongside any capture mode. Say your wake phrase
("Jarvis") to activate hands-free commands and dictation.

### Wake Word System

- Configurable wake phrase (Jarvis, Samsara, Computer, or custom)
- Token-aware matching prevents false triggers on similar words
- End words ("over", "done") — terminates Long Dictation
- Cancel words ("cancel", "abort") — discards current dictation
- Pause/resume words — suspends Long Dictation without losing buffer
- Wake word correction map for known Whisper misrecognitions
- Debug window with trace pipeline, evaluation panel, and decision timeline

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

Types: `hotkey` (key combos), `launch` (programs), `macro` (multi-step).
Custom commands via Settings → Commands tab or `commands.json` directly.

### Audio Pipeline

- **Pre-buffer** — 1.5s rolling buffer captures audio before hotkey press
- **Dual sample rate** — captures at device native rate (48kHz WASAPI), resamples to 16kHz for Whisper
- **Auto-calibration** — measures ambient noise on startup (1.5s), sets speech threshold using IQR-based outlier rejection. Floor of 0.0005 (not 0.01 — that caused false triggers in quiet rooms)
- **Echo cancellation** — frequency-domain block NLMS with FFT overlap-save. Subtracts system audio from mic input. 4096 taps at 16kHz = 256ms echo path coverage

### Accessibility Extras

- **Alarm reminders** — nag-until-dismissed with streak tracking
- **Key macros** — tap-combos and toggle-hold for accessibility
- **Toast notifications** — timed Windows toast reminders
- **Clipboard preservation** — saves/restores clipboard around every paste
- **Listening indicator** — always-on-top pill overlay, pulses teal when recording
- **Sound themes** — cute, warm, zen, chirpy (custom themes supported)
- **Snooze** — pause all listening from tray (5 min to 1 hour)

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **OS** | Windows 10 | Windows 11 |
| **Python** | 3.10 | 3.11+ |
| **RAM** | 4 GB | 8 GB+ |
| **GPU** | None (CPU works) | NVIDIA with 4GB+ VRAM |
| **Disk** | 2 GB | 10 GB (for larger models) |

---

## For Developers

### Architecture

The codebase has been modularized through a structured review process.
`dictation.py` (~3,600 lines) contains `DictationApp` (the core engine)
and `CommandExecutor`. All UI classes have been extracted:

```
samsara/
├── calibration.py            # Auto-calibrate speech threshold (IQR outlier rejection)
├── clipboard.py              # Win32 clipboard save/restore with error logging
├── command_parser.py         # Wake word intent parsing (type/dictate/command routing)
├── constants.py              # Extracted magic numbers (sample rates, thresholds, timing)
├── echo_cancel.py            # Frequency-domain AEC (FFT block NLMS + WASAPI loopback)
├── wake_word_matcher.py      # Token-aware wake phrase matching
├── wake_corrections.py       # Whisper misrecognition correction map
├── commands.py               # Command definitions and loading
├── key_macros.py             # Tap-combos and toggle-hold
├── notifications.py          # Windows toast reminders
├── alarms.py                 # Nag-until-dismissed alarms with streaks
├── profiles.py               # Dictionary/command profile import-export
├── plugin_commands.py        # Plugin command system (scaffold)
├── ui/
│   ├── settings_window.py    # Settings UI (lazy tabs + staged building)
│   ├── first_run_wizard.py   # First-run setup wizard
│   ├── history_window.py     # Dictation history viewer
│   ├── splash.py             # Splash screen with animated dots
│   ├── wake_word_debug.py    # Wake word debug/trace window
│   ├── listening_indicator.py # Always-on-top mode overlay
│   └── profile_manager_ui.py # Profile manager window
├── _stale/                   # Deprecated modules (kept for reference)
│   ├── audio.py
│   ├── config.py
│   └── speech.py
```

### Key Engineering Decisions

- **Thread safety**: `buffer_lock` around all `speech_buffer` access
  (PortAudio callbacks vs transcription thread)
- **Atomic config saves**: writes to `.tmp`, rotates to `.bak`, promotes
  via `os.replace()` — crash-safe
- **WASAPI-first audio**: captures at device native rate, resamples to
  16kHz for Whisper (dual sample rate architecture)
- **Settings performance**: lazy tab loading + generator-based staged
  building prevents UI freeze on settings open
- **Echo cancellation**: frequency-domain block NLMS (replaced original
  sample-by-sample Python loop that was too slow for real-time)

### Running Tests

```bash
python -m pytest tests/ -v
```

Tests cover: wake word matching, command parsing, pipeline integration,
calibration logic.

---

## Default Hotkeys

| Hotkey | Action |
|--------|--------|
| `Ctrl+Shift` | Hold to dictate (Quick Dictation) |
| `Ctrl+Alt+D` | Toggle Long Dictation (continuous) |
| `Ctrl+Alt+W` | Toggle wake word mode |
| `Escape` | Cancel current recording |

All hotkeys configurable in Settings.

---

## Roadmap

- [x] Hold/Toggle/Continuous capture modes
- [x] Wake word activation with token-aware matching
- [x] 40+ voice commands with custom command support
- [x] Audio pre-buffer (1.5s before hotkey)
- [x] Alarm reminders with streak tracking
- [x] Sound themes and snooze
- [x] Listening indicator overlay
- [x] Atomic config saves with backup rotation
- [x] WASAPI dual sample rate architecture
- [x] Thread-safe buffer locking
- [x] UI extraction (dictation.py halved from 6,555 to 3,592 lines)
- [x] Settings lazy loading + staged building
- [x] Constants extraction (no more magic numbers)
- [x] Command parser with intent routing
- [x] Auto-calibrate speech threshold (IQR-based)
- [x] 4-state dictation model (Asleep/Command/Quick/Long)
- [x] Frequency-domain echo cancellation
- [x] Stale module cleanup
- [ ] Plugin system for custom commands
- [ ] Undo last dictation
- [ ] Command chaining ("select all copy")
- [ ] Application-specific command profiles
- [ ] Spelling mode ("spell c-a-t" → "cat")
- [ ] Local LLM post-processing for corrections

---

## License

MIT License — free for personal and commercial use.

## Acknowledgments

- [OpenAI Whisper](https://github.com/openai/whisper) — Speech recognition
- [faster-whisper](https://github.com/guillaumekln/faster-whisper) — CTranslate2 implementation
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — Modern UI
- Designed with assistance from [Claude](https://anthropic.com),
  [ChatGPT](https://openai.com), and [Gemini](https://deepmind.google)
  through the [ARC](https://github.com/Morne-Ingstar/ARC) review process

---

<p align="center">
  <i>Named after the Buddhist concept of cyclical existence —
  Samsara helps break the cycle of repetitive strain.</i>
</p>
