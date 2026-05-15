# Samsara

### Voice-Controlled Computing for Accessibility

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)[![License: BSL-1.1](https://img.shields.io/badge/License-BSL--1.1-orange.svg)](LICENSE)![Platform](https://img.shields.io/badge/platform-Windows-blue.svg)![Status: Beta](https://img.shields.io/badge/status-beta-yellow.svg)

> **The new Samsara site is live: [morneis.com/samsara](https://morneis.com/samsara)**
>
> Demo reel, screenshots, philosophy, accessibility framing, downloads, and roadmap — all over there. The README from here down is technical.

**Recent updates:** Ava voice AI (hold Right Alt, talk to local Ollama, get spoken responses), Text-to-Speech with smart audio ducking, Show Numbers overlay for hands-free clicking, Command Cheat Sheet, Mouse 4 walkie-talkie command mode, window manager with saved layouts, Smart Actions webhook bridge, 320+ commands, deferred text selection, 5-speed scrolling, repeat/again, v0.9.9.

> *"Jarvis, open Chrome."*
>
> Local voice control via Whisper. ~300ms. No cloud, no internet, no typing.

---

## From the Developer

> I'm Morne, and my hands fucking hurt.
>
> I've had HSD (Hypermobile Spectrum Disorder) for a decade. Using a mouse and keyboard hurts — all the time. I tried paid apps, but they were fragmented, expensive, and didn't fit. So I built Samsara.
>
> This is early-stage software. It works for me daily, but expect rough edges. If you find it useful, I'd love to hear from you.
>
> — Morne
>
> [Read more about the vision behind Samsara →](https://morneis.com/samsara)

---

## What Can It Do?

Samsara is a **fully offline** voice control system powered by Whisper. It runs as a Windows app with a main hub window, system tray integration, and hands-free control over your entire computer.

### Dictation

- **Hold-to-dictate** — hold Ctrl+Shift, speak, release. Text appears wherever your cursor is. ~300ms latency with NVIDIA GPU.
- **Streaming dictation** — text appears in real-time as you speak. A floating overlay shows partial transcriptions that update every second, with a polished final paste on release. Direct-paste mode flows text straight into your focused app while you talk.
- **Continuous mode** — toggle on, talk freely, toggle off. For long dictation sessions.
- **Grammar-Lite cleanup** — automatic filler word removal, capitalization, and punctuation. Toggle between Clean and Verbatim modes.
- **Dictation history** — every transcription logged to a searchable SQLite database. Review, copy, retry failed attempts, track patterns.

### Voice AI — Ava

Hold Right Alt and speak. Ava sends your question to a local Ollama LLM and reads the answer back to you via TTS — no cloud, no API key, no typing. Fully offline.

- Ask anything: "Ava, what's the capital of Mongolia?"
- Get coding help: "Ava, what does this error mean?"
- Responses are spoken aloud and also displayed in the history panel
- Uses whatever Ollama model you have installed locally
- TTS is interruptible — start talking and Ava stops

Also reachable by voice in command mode: hold Right Ctrl and say "Hey Ava, [question]" or "Is it safe to [action]".

### Text-to-Speech

Samsara can speak. Uses EdgeTTS or Windows Natural voices — not the robotic pyttsx3 voices. The smart AudioCoordinator manages the audio: music ducks while Samsara speaks, the mic stays clean, and if you start talking mid-response, TTS stops immediately.

Enabled and configured in Settings → TTS tab. Off by default.

### Voice Commands

320+ built-in commands plus a plugin system. Say a command after your wake word (default: "Jarvis").

| Category | Examples |
|----------|----------|
| **Apps** | "open Chrome", "open Word", "open Spotify" |
| **Macros** | "going dark" (mute + minimize + lock), "good morning" (mail + GitHub + music) |
| **Audio** | "switch to speakers", "use my headset" |
| **Browser** | "find tab GitHub", "search for ergonomic keyboards" |
| **Screen** | "record my screen", "record this window", "stop recording" |
| **Smart Home** | "lights red", "lights off", "light effect rainbow" |
| **Music** | "play me some music", "play moonlight", "volume down" |
| **3D Printing** | "printer status", "pause print", "cancel print", "printer light" |
| **Utilities** | "set a timer for 5 minutes", "search for a gif of dancing cat" |
| **Text** | "period", "new line", "select all", "undo" |
| **Scrolling** | "scroll up a little", "scroll up", "scroll up medium", "scroll up high", "scroll up fast" — plus down variants |
| **Text Selection** | "mark here", "select to here" — anchor-based selection across any scroll distance |
| **Repeat** | "again", "repeat" — re-fire the last command |
| **Volume** | "volume up", "volume down", "mute" — Core Audio API, no media keys |
| **Streaming** | "play on stremio", "stremio pause", "stremio fullscreen" |
| **Voice AI** | Hold Right Alt → speak to Ava (Ollama) → hear response |

### Smart Home & IoT

Samsara talks directly to hardware on your network:

- **Hyperion LED strips** — "lights red", "lights off", "light effect rainbow". 11 preset colors, 14 effect aliases with fuzzy matching against your Hyperion instance. Supports IPv4, IPv6, and hostnames.
- **FlashForge 3D printers** — "printer status" (temperatures, progress, state), "pause print", "resume print", "cancel print", "printer light". TCP M-code protocol, tested on AD5X.
- **Spotify** — "play me some music", "play hurt", "volume up". Opens tracks directly in the desktop app with configurable song library.

### Main Window

Samsara opens a hub window on launch with three views:

- **History** — searchable list of all dictations with timestamps, source apps, success/fail status, copy and retry buttons. Phase 2: inline correction, session grouping, type/confidence indicators.
- **Dictionary** — unified corrections manager with three tabs: Vocabulary (Whisper hints), Corrections (phonetic wash rules), Wake Words (misrecognition fixes). Add, edit, delete from the UI — changes take effect immediately without restart.
- **Settings** — microphone, model, hotkeys, cleanup mode, streaming mode, TTS, all in one place.

Plus standalone overlays (Win32 layered windows — always above all apps, DPI-aware, per-pixel alpha):

- **Command Cheat Sheet** — floating always-on-top overlay listing every active command. Opacity slider, filterable by pack. Toggle from tray or by voice.
- **Show Numbers** — voice-driven clicking: an overlay numbers every interactive element on screen. Say the number to click it. Fully hands-free UI navigation.
- **Listening Indicator** — a pill that pulses and shows current mode (dictating / command / Ava / streaming) at the corner of your screen.

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

Ships with 18 plugins including smart home control, music playback, 3D printer integration, macros, audio switching, tab finder, web shortcuts, timer, GIF search, screen recording, voice AI / Ava (Ollama), scroll (5-speed mouse wheel), text marker (deferred range selection), volume/mute (Core Audio API), Stremio, and more.

---

## Getting Started

### Download for Windows

1. Go to the [latest release](https://github.com/Morne-Ingstar/Samsara/releases/latest)
2. Download **Samsara.exe**
3. Run it — a setup wizard walks you through microphone selection and model download

**NVIDIA GPU recommended** for ~300ms transcription. Works on CPU too, just slower.

### Run from Source

```bash
git clone https://github.com/Morne-Ingstar/Samsara.git
cd Samsara
pip install -r requirements.txt
python dictation.py
```

### Enabling CUDA (NVIDIA GPU)

If you have an NVIDIA GPU, Samsara will run dramatically faster on CUDA (~10x). The Settings → Advanced tab has a device dropdown for CUDA / CPU.

**The dropdown only offers CUDA if Samsara can find the CUDA runtime DLLs at startup.** When installing from source with a fresh environment, those DLLs (`cublas64_12.dll`, `cublasLt64_12.dll`) are not bundled with `ctranslate2` and Samsara will silently fall back to CPU.

Two ways to fix this:

**Option A — Copy from torch (if torch is installed):**

Torch bundles its own copy of cuBLAS. Copy the two DLLs into ctranslate2's folder:

```bash
copy "<env>\Lib\site-packages\torch\lib\cublas64_12.dll" "<env>\Lib\site-packages\ctranslate2\"
copy "<env>\Lib\site-packages\torch\lib\cublasLt64_12.dll" "<env>\Lib\site-packages\ctranslate2\"
```

**Option B — Install the CUDA Pack:**

Download `Samsara-CUDA-Pack-vX.X.X.zip` from the [GitHub releases page](https://github.com/Morne-Ingstar/Samsara/releases) and extract the DLLs into your `ctranslate2` site-packages folder.

Once the DLLs are in place, restart Samsara, open Settings → Advanced, and select **CUDA (NVIDIA GPU)** in the device dropdown. The startup log should show `Device: cuda, Compute: float16`.

### Configuring Plugins

Plugins are configured through `config.json`. For smart home and IoT plugins:

```json
{
  "hyperion_host": "your-hyperion-ip-or-hostname",
  "hyperion_port": 19444,
  "flashforge_ip": "your-printer-ip",
  "music_volume": 30
}
```

---

## Under the Hood

### Audio Pipeline

- **Silero VAD** — neural speech detection, ignores fan noise and background hum. Runs on raw mic signal, not AEC output.
- **Pre-buffer** — 1.5s rolling buffer captures audio before you press the hotkey. First words are never lost.
- **Echo cancellation** — frequency-domain AEC subtracts system audio from mic input. Dictate over music and Whisper still hears you.
- **Auto-calibration** — measures ambient noise on startup using IQR-based outlier rejection.
- **Auto-reconnect** — if audio dies after sleep/wake, Samsara detects it and reconnects automatically. No restart needed.
- **Single-stream fan-out** — wake word mode and continuous mode share one PortAudio InputStream. Eliminates a WASAPI device contention bug that previously silenced continuous mode entirely when both were active simultaneously.

### Streaming Architecture

When streaming mode is enabled, dictation becomes real-time:

1. Hold the hotkey — audio capture starts, pre-buffer is skipped for faster response.
2. After 0.7 seconds, the first partial transcription appears (beam_size=1 for speed).
3. Every 1.0 seconds, Whisper re-transcribes the entire buffer from the start. New words appear, existing words may refine.
4. In direct-paste mode, each partial replaces the previous text in your focused app using Ctrl+A select-and-replace.
5. On release, a final pass runs with full beam search (beam_size=5) and Grammar-Lite cleanup. The polished result replaces everything.

First text appears in ~1 second. The overlay shows what's being transcribed even when direct-paste is off.

### Architecture

Main hub window (`samsara/ui/main_window.py`) with reusable frame components. Dictation engine in `dictation.py`. Streaming engine in `samsara/streaming.py`. Correction pipeline: phonetic wash → wake corrections → grammar cleanup. All user corrections stored in `~/.samsara/` as JSON, hot-reloaded without restart.

All overlays (listening indicator, Show Numbers, Command Cheat Sheet) are Win32 layered windows, not Tkinter widgets — they stay above all apps, have correct DPI scaling, and support per-pixel alpha transparency.

```
samsara/
├── streaming.py            # Real-time streaming dictation engine
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
│   └── ...
plugins/commands/
├── hyperion_lights.py      # "lights red", "light effect rainbow"
├── flashforge_printer.py   # "printer status", "pause print"
├── music.py                # "play me some music", "volume down"
├── macros.py               # "going dark", "focus mode"
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
- [x] 320+ voice commands with plugin system
- [x] Voice AI / Ava — local Ollama integration, hold Right Alt to talk
- [x] Text-to-Speech with EdgeTTS / Windows Natural voices and smart audio ducking
- [x] Mouse 4 / keyboard command mode (hold-to-talk walkie-talkie, ghost-tap prevention)
- [x] Win32 layered window overlays (listening indicator, Show Numbers, Command Cheat Sheet)
- [x] Window manager v2 — move apps between monitors, saved layouts, lost window recovery
- [x] Smart Actions webhook bridge with tiered consent system
- [x] Command packs — named groups (core, browsers, AI, etc.) with per-user enable/disable UI
- [x] Deferred text selection — "mark here" / "select to here"
- [x] 5-speed scrolling via Win32 SendInput (works in Electron, browsers, all apps)
- [x] Repeat / again — re-fire last safe command
- [x] Core Audio API volume and mute (no media keys, no pycaw)
- [x] Stremio voice control via AutoHotkey v1
- [x] Per-app keyboard shortcuts (app_overrides — different keys per app)
- [x] History panel phase 2 — inline correction, session grouping, search, type/confidence
- [x] Streaming dictation with live overlay and direct-paste
- [x] Smart home control (Hyperion LED strips)
- [x] 3D printer control (FlashForge AD5X)
- [x] Spotify music playback by voice
- [x] Multi-step macros (going dark, focus mode, good morning)
- [x] Main hub window (History, Dictionary, Settings)
- [x] Dictation history with SQLite search and recovery
- [x] Unified dictionary UI (vocabulary, corrections, wake words)
- [x] Grammar-Lite cleanup (filler removal, capitalization)
- [x] Audio auto-reconnect after sleep/wake
- [x] Silero VAD speech detection (raw mic signal)
- [x] Echo cancellation, pre-buffer, auto-calibration
- [x] Single-stream fan-out (wake word + continuous share one PortAudio stream)
- [x] Screen recording to GIF by voice
- [x] Audio device switching, browser tab search
- [x] Phonetic wash + wake word corrections
- [x] First-run wizard, splash screen, profiles
- [x] v0.9.9 release

### Planned
- [ ] Speaker verification (local, via Resemblyzer)
- [ ] Music-reactive lighting
- [ ] Voice Training panel (calibration, test phrases, corrections)
- [ ] End-word / cancel-word dictation protocol
- [ ] Voice-to-code pipeline (ARC review → Claude Code → confirm/reject)
- [ ] Edit-to-learn corrections (edit history → auto-suggest rules)
- [ ] Snippets / text expansions
- [ ] Per-app command profiles
- [ ] Mobile companion app (phone as wireless mic)
- [ ] Cross-platform support
- [ ] Eye-tracking integration

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
