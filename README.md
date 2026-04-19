# <img src="logo.png" width="28" height="28"> Samsara

### Voice-Controlled Computing for Accessibility

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-blue.svg)]()
[![Status](https://img.shields.io/badge/status-early%20dev-orange.svg)]()

<p align="center">
  <img src="demo.gif" alt="Samsara Demo" width="800">
</p>

> *"Jarvis, going dark."*
>
> Samsara mutes your system audio, minimizes every window, and locks your screen — all from one voice command.
>
> No mouse. No keyboard. No menus.

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

## What Can It Do?

Samsara is a **fully offline** voice command system powered by OpenAI's Whisper.
It runs as a Windows system tray app, listens for your wake word, and controls
your entire computer — hands-free.

### 🎙️ The Basics
- **Hold-to-dictate** — hold Ctrl+Shift, speak, release. Text appears wherever your cursor is. ~300ms latency with NVIDIA GPU.
- **Wake word** — say **"Jarvis"** (or your custom phrase) to activate commands without touching anything.
- **100+ voice commands** — apps, browser, media, window management, text editing, all customizable.

### ⚡ The Impressive Stuff

**Multi-step macros** — single voice commands that chain complex actions:
| You say | What happens |
|---------|-------------|
| *"Jarvis, going dark"* | Mutes audio → minimizes all windows → locks screen |
| *"Jarvis, focus mode"* | Sets volume to 20% → clears desktop → opens VS Code |
| *"Jarvis, good morning"* | Sets volume to 50% → opens your mail, GitHub, and daily sites |
| *"Jarvis, presentation mode"* | Volume to 80% → maximizes current window |
| *"Jarvis, break time"* | Pauses media → locks screen |

**Audio device switching** — bypass the Windows sound menu entirely:
| You say | What happens |
|---------|-------------|
| *"Jarvis, switch to speakers"* | Instantly changes default audio output |
| *"Jarvis, use my headset"* | Switches to headset — zero UI, zero clicks |

Device names are configurable aliases in `config.json`, so you map your own friendly names to exact Windows device names.

**Browser tab finder** — stop scrolling through 30 tiny favicons:
| You say | What happens |
|---------|-------------|
| *"Jarvis, where is GitHub"* | Focuses your browser, opens tab search, finds and switches to the GitHub tab |
| *"Jarvis, where is YouTube"* | Same — works across multiple Chrome/Edge windows |

**Web shortcuts** — skip bookmarks, skip typing URLs:
| You say | What happens |
|---------|-------------|
| *"Jarvis, go to my orders"* | Opens your Amazon order history directly |
| *"Jarvis, search for ergonomic keyboards"* | Google search, instantly |

All shortcut URLs are configurable — add your own sites in `config.json`.

### 🔮 Coming Soon

**Voice-to-code pipeline** — say *"Jarvis, improve error handling in the audio module"* and:
1. Three AIs design the solution through adversarial review ([ARC](https://github.com/Morne-Ingstar/ARC))
2. Claude Code implements it against your codebase automatically
3. A final AI audit rates confidence on the actual changes
4. You say *"Jarvis, confirm"* to commit or *"Jarvis, reject"* to revert

All hands-free. All local. Built on the [JARVIS Pipeline architecture](https://github.com/Morne-Ingstar/ARC).

**Also in progress:** undo last dictation by voice, true pause/resume for long dictation, command chaining, echo-stripping for Whisper hallucinations.

### 🔌 Plugin System

Drop a Python file in `plugins/commands/` and it becomes a voice command. No config changes, no restart required. Ships with 6 plugins out of the box:

| Plugin | Commands |
|--------|----------|
| **Audio Switch** | "switch to speakers", "use headset" — dynamic via config aliases |
| **Web Shortcuts** | "go to YouTube", "search for cat toys" — configurable URL bookmarks |
| **Tab Finder** | "where is GitHub" — focuses browser + searches tabs |
| **Macros** | "going dark", "focus mode", "good morning" — multi-step workflows |
| **Greeting** | "greet me" — demo plugin showing how to build your own |

Write your own in under 10 lines:
```python
from samsara.plugin_commands import command

@command("my custom thing", aliases=["do the thing"])
def my_command(app, remainder):
    # remainder = whatever the user said after the trigger phrase
    import webbrowser
    webbrowser.open("https://example.com")
    return True
```

---

## Getting Started

### Option 1: Download for Windows (easiest)

1. Go to the [latest release](https://github.com/Morne-Ingstar/Samsara/releases/latest)
2. Download **Samsara-Windows-v0.9.2.7z**
3. Extract the folder anywhere (e.g. `C:\Samsara`)
4. Double-click **Samsara.exe**
5. A setup wizard walks you through picking your microphone and downloading the AI model

That's it. No Python, no command line, no configuration files.

**NVIDIA GPU recommended** — Samsara uses your graphics card to transcribe speech almost instantly (~300ms). It works on CPU too, just slower.

### Option 2: Run from source (for developers)

```bash
git clone https://github.com/Morne-Ingstar/Samsara.git
cd Samsara
pip install -r requirements.txt
python dictation.py
```

### How to use it

Once running, Samsara sits in your **system tray** (bottom-right, near the clock).

**Hold-to-dictate:** Hold **Ctrl+Shift**, speak, release — your words appear wherever your cursor is.

**Wake word:** Say **"Jarvis"** followed by any command. Right-click the tray icon to switch modes, change mic, or open settings.

---

## Under the Hood

### Dictation (4-State Machine)

| State | Trigger | Behavior |
|-------|---------|----------|
| **Asleep** | Default | Wake word listener active, all other audio discarded |
| **Command Window** | Wake word detected | 3-second window for action verbs, then back to Asleep |
| **Quick Dictation** | Hotkey or "type..." | 1.0s silence timeout, auto-transcribes and pastes |
| **Long Dictation** | "dictate..." | No timeout — say "over" to finish, "pause"/"resume" to suspend |

### Audio Pipeline
- **Pre-buffer** — 1.5s rolling buffer captures audio *before* you press the hotkey. First words are never lost.
- **Dual sample rate** — captures at device native rate (48kHz WASAPI), resamples to 16kHz for Whisper
- **Auto-calibration** — measures ambient noise on startup, sets speech threshold using IQR-based outlier rejection
- **Echo cancellation** — frequency-domain block NLMS with FFT overlap-save, subtracts system audio from mic input

### Voice Command Types
| Type | Example | What it does |
|------|---------|-------------|
| `hotkey` | "screenshot" → Win+Shift+S | Sends key combinations |
| `launch` | "open chrome" → chrome.exe | Launches applications |
| `macro` | "going dark" → mute+minimize+lock | Chains multiple actions |
| `text` | "period" → `.` | Inserts characters |
| `method` | "scratch that" → undo_last_dictation() | Calls app functions |
| `plugin` | "where is GitHub" → tab_finder | Runs plugin handlers |

### Accessibility Extras
Alarm reminders with streak tracking, key macros for tap-combos, clipboard preservation around every paste, listening indicator overlay (pulses teal when active), sound themes (cute/warm/zen/chirpy), and snooze from the tray.

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **OS** | Windows 10 | Windows 11 |
| **Python** | 3.10 | 3.11+ |
| **RAM** | 4 GB | 8 GB+ |
| **GPU** | None (CPU works) | NVIDIA with 4GB+ VRAM |

---

## For Developers

### Architecture

`dictation.py` (~3,600 lines) contains `DictationApp` and `CommandExecutor`. UI is fully extracted into `samsara/ui/`. Plugins live in `plugins/commands/`. The codebase was modularized through a structured multi-AI review process ([ARC](https://github.com/Morne-Ingstar/ARC)).

```
samsara/
├── calibration.py          # Auto-calibrate speech threshold (IQR outlier rejection)
├── clipboard.py            # Win32 clipboard save/restore
├── command_parser.py       # Wake word intent parsing
├── constants.py            # Extracted magic numbers
├── echo_cancel.py          # Frequency-domain AEC (FFT block NLMS + WASAPI loopback)
├── wake_word_matcher.py    # Token-aware wake phrase matching
├── commands.py             # Command loading and execution
├── plugin_commands.py      # Plugin command registry and @command decorator
├── ui/                     # All UI (settings, wizard, debug, indicator)
plugins/commands/
├── audio_switch.py         # "switch to speakers" — NirCmd-based
├── web_shortcuts.py        # "go to youtube" — config-driven bookmarks
├── tab_finder.py           # "where is github" — browser tab search
├── macros.py               # "going dark" — multi-step workflows
```

### Running Tests

```bash
python -m pytest tests/ -v
```

### Default Hotkeys

| Hotkey | Action |
|--------|--------|
| `Ctrl+Shift` | Hold to dictate (Quick Dictation) |
| `Ctrl+Alt+D` | Toggle Long Dictation (continuous) |
| `Ctrl+Alt+W` | Toggle wake word mode |
| `Escape` | Cancel current recording |

All hotkeys configurable in Settings.

---

## Roadmap

- [x] 100+ voice commands with plugin system
- [x] Multi-step macros ("going dark", "focus mode", "good morning")
- [x] Audio device switching via voice
- [x] Browser tab search by voice
- [x] Web shortcuts and Google search by voice
- [x] 4-state dictation model (Asleep/Command/Quick/Long)
- [x] Auto-calibrating speech threshold (IQR-based)
- [x] Frequency-domain echo cancellation
- [x] Pre-buffer captures 1.5s before hotkey press
- [x] Listening indicator overlay with wake word feedback
- [ ] Voice-to-code pipeline (JARVIS — ARC review → Claude Code → confirm/reject)
- [ ] Undo last dictation by voice
- [ ] True pause/resume for Long Dictation
- [ ] Echo-stripping for Whisper hallucinations
- [ ] Command chaining ("select all copy")
- [ ] Application-specific command profiles
- [ ] Spelling mode ("spell c-a-t" → "cat")

---

## License

MIT License — free for personal and commercial use.

If Samsara saves your wrists or speeds up your workflow, consider
[supporting the project](https://morneis.com) — built through chronic
pain, kept free for accessibility.

## Acknowledgments

- [OpenAI Whisper](https://github.com/openai/whisper) — speech recognition
- [faster-whisper](https://github.com/guillaumekln/faster-whisper) — CTranslate2 implementation
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — modern UI
- [NirCmd](https://www.nirsoft.net/utils/nircmd.html) — audio device switching
- Designed with [Claude](https://anthropic.com), [ChatGPT](https://openai.com), and [Gemini](https://deepmind.google) through the [ARC](https://github.com/Morne-Ingstar/ARC) adversarial review process

---

<p align="center">
  <i>Named after the Buddhist concept of cyclical existence —
  Samsara helps break the cycle of repetitive strain.</i>
</p>
