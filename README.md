# ![](logo.png) Samsara

### Voice-Controlled Computing for Accessibility

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)[![License: BSL-1.1](https://img.shields.io/badge/License-BSL--1.1-orange.svg)](LICENSE)![Platform](https://img.shields.io/badge/platform-Windows-blue.svg)

![Samsara Demo](demo.gif)

> *"Jarvis, open Chrome."*
>
> Samsara hears the wake word, transcribes locally via Whisper in \~300ms, and launches Chrome. No cloud, no internet, no typing.

---

## From the Developer

> I'm Morne, and my hands fucking hurt.
>
> I've had HSD (Hypermobile Spectrum Disorder) for a decade. Using a mouse and keyboard hurts — all the time. I tried paid apps, but they were fragmented, expensive, and didn't fit. So I built Samsara.
>
> This is early-stage software. It works for me daily, but expect rough edges. If you find it useful, I'd love to hear from you.
>
> — Morne

---

## What Can It Do?

Samsara is a **fully offline** voice control system powered by Whisper. It runs as a Windows app with a main hub window, system tray integration, and hands-free control over your entire computer.

### Dictation

- **Hold-to-dictate** — hold Ctrl+Shift, speak, release. Text appears wherever your cursor is. \~300ms latency with NVIDIA GPU.
- **Continuous mode** — toggle on, talk freely, toggle off. For long dictation sessions.
- **Grammar-Lite cleanup** — automatic filler word removal, capitalization, and punctuation. Toggle between Clean and Verbatim modes.
- **Dictation history** — every transcription logged to a searchable SQLite database. Review, copy, retry failed attempts, track patterns.

### Voice Commands

120+ built-in commands plus a plugin system. Say your wake word (default: "Jarvis", fully customizable) followed by any command.

CategoryExamples**Apps**"open Chrome", "open Word", "open Spotify"**Macros**"going dark" (mute + minimize + lock), "good morning" (mail + GitHub + music)**Audio**"switch to speakers", "use my headset"**Browser**"find tab GitHub", "search for ergonomic keyboards"**Screen**"record my screen", "record this window", "stop recording"**Utilities**"set a timer for 5 minutes", "search for a gif of dancing cat"**Text**"period", "new line", "select all", "undo"

### Main Window

Samsara opens a hub window on launch with three views:

- **History** — searchable list of all dictations with timestamps, source apps, success/fail status, copy and retry buttons
- **Dictionary** — unified corrections manager with three tabs: Vocabulary (Whisper hints), Corrections (phonetic wash rules), Wake Words (misrecognition fixes). Add, edit, delete from the UI — changes take effect immediately without restart.
- **Settings** — microphone, model, hotkeys, cleanup mode, all in one place

Closing the window minimizes to tray. Double-click the tray icon to reopen.

### Plugin System

Drop a Python file in `plugins/commands/` and it becomes a voice command:

```python
from samsara.plugin_commands import command

@command("my custom thing", aliases=["do the thing"])
def my_command(app, remainder):
    import webbrowser
    webbrowser.open("https://example.com")
    return True
```

Ships with 9 plugins: macros, audio switching, tab finder, web shortcuts, timer, GIF search, screen recording, quick ask (ARC integration), and a demo greeting.

---

## Getting Started

### Download for Windows

1. Go to the [latest release](https://github.com/Morne-Ingstar/Samsara/releases/latest)
2. Download **Samsara.exe**
3. Run it — a setup wizard walks you through microphone selection and model download

**NVIDIA GPU recommended** for \~300ms transcription. Works on CPU too, just slower.

### Run from Source

```bash
git clone https://github.com/Morne-Ingstar/Samsara.git
cd Samsara
pip install -r requirements.txt
python dictation.py
```

---

## Under the Hood

### Audio Pipeline

- **Silero VAD** — neural speech detection, ignores fan noise and background hum. Runs on raw mic signal, not AEC output.
- **Pre-buffer** — 1.5s rolling buffer captures audio before you press the hotkey. First words are never lost.
- **Echo cancellation** — frequency-domain AEC subtracts system audio from mic input. Whisper receives clean speech.
- **Auto-calibration** — measures ambient noise on startup using IQR-based outlier rejection.
- **Auto-reconnect** — if audio dies after sleep/wake, Samsara detects it and reconnects automatically. No restart needed.

### Architecture

Main hub window (`samsara/ui/main_window.py`) with reusable frame components. Dictation engine in `dictation.py`. Correction pipeline: phonetic wash → wake corrections → grammar cleanup. All user corrections stored in `~/.samsara/` as JSON, hot-reloaded without restart.

```
samsara/
├── cleanup.py              # Grammar-Lite post-processing
├── history.py              # SQLite dictation history
├── phonetic_wash.py        # Fixes Whisper misrecognitions
├── wake_corrections.py     # Wake word variant corrections
├── command_registry.py     # Token-based longest-match resolver
├── echo_cancel.py          # Frequency-domain AEC
├── ui/
│   ├── main_window.py      # Hub window (History/Dictionary/Settings)
│   ├── history_frame.py    # Searchable dictation history
│   ├── dictionary_frame.py # Unified corrections manager
│   ├── settings_window.py  # Configuration
│   └── ...
plugins/commands/
├── macros.py               # "going dark", "focus mode", "good morning"
├── audio_switch.py         # "switch to speakers"
├── tab_finder.py           # "find tab GitHub"
├── timer.py                # "set a timer for 5 minutes"
├── screen_gif.py           # "record my screen"
├── gif_search.py           # "search for a gif of cats"
└── ...
```

### Tests

```bash
python -m pytest tests/ -v
```

---

## Roadmap

### Completed
- [x] 120+ voice commands with plugin system
- [x] Multi-step macros (going dark, focus mode, good morning)
- [x] Main hub window (History, Dictionary, Settings)
- [x] Dictation history with SQLite search and recovery
- [x] Unified dictionary UI (vocabulary, corrections, wake words)
- [x] Grammar-Lite cleanup (filler removal, capitalization)
- [x] Audio auto-reconnect after sleep/wake
- [x] Silero VAD speech detection (raw mic signal)
- [x] Screen recording to GIF by voice
- [x] Timer with natural language duration
- [x] Audio device switching, browser tab search
- [x] Phonetic wash + wake word corrections
- [x] Echo cancellation, pre-buffer, auto-calibration
- [x] First-run wizard, splash screen, profiles

### Planned
- [ ] Voice-to-code pipeline (ARC review → Claude Code → confirm/reject)
- [ ] Edit-to-learn corrections (edit history → auto-suggest rules)
- [ ] Snippets / text expansions
- [ ] Per-app command profiles
- [ ] Mobile companion app (phone as wireless mic)

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Windows 10 | Windows 11 |
| Python | 3.10 | 3.11+ |
| RAM | 4 GB | 8 GB+ |
| GPU | None (CPU works) | NVIDIA 4GB+ VRAM |

---

## License

BSL-1.1 (Business Source License) — free for all non-commercial use.
Converts to MIT on April 23, 2030. See [LICENSE](LICENSE) for details.

If Samsara saves your wrists, consider
[supporting the project](https://ko-fi.com/morneingstar) — built through
chronic pain, kept free for accessibility.

## Acknowledgments

- [OpenAI Whisper](https://github.com/openai/whisper) / [faster-whisper](https://github.com/guillaumekln/faster-whisper)
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)
- Designed with [Claude](https://anthropic.com), [ChatGPT](https://openai.com),
  and [Gemini](https://deepmind.google) through the
  [ARC](https://github.com/Morne-Ingstar/ARC) adversarial review process

---

<p align="center">
  <i>Named after the Buddhist concept of cyclical existence —
  Samsara helps break the cycle of repetitive strain.</i>
</p>
