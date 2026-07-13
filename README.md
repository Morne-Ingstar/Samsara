# Samsara

### Voice-Controlled Computing for Accessibility

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-green.svg)](LICENSE)![Platform](https://img.shields.io/badge/platform-Windows-blue.svg)![Status: Beta](https://img.shields.io/badge/status-beta-yellow.svg)

> **The new Samsara site is live: [morneis.com/samsara](https://morneis.com/samsara)**
>
> Demo reel, screenshots, philosophy, accessibility framing, downloads, and roadmap — all over there. The README from here down is technical.

---

## What's New in v0.20.0

**The Voice Session** — the headline of this release. Activate once and the keyboard becomes optional: **Command** the computer, **Dictate** into anything, or talk to **Ava**, a local AI assistant on your own GPU — switching between them by voice, mid-thought. *"Scratch that"* undoes, misfire gates keep a cough from triggering anything, and a distinct audio cue marks every transition, so it works eyes-free too.

Plus a deep polish-and-hardening pass — weeks of fixing things that were quietly wrong:

- **Hallucination-free dictation** — Whisper no longer fabricates text during silence or short pauses; a rebuilt gate plus a regression test suite keep it that way.
- **Mic survives disconnects** — unplug a mic or lose a Bluetooth connection mid-session and Samsara reconnects automatically instead of going deaf until restart.
- **Onboarding redesigned** — the first-run wizard and tutorial share one consistent look now, with the invisible-button and clipped-text bugs fixed.
- **History window overhaul** — right-click a row to copy/delete, double-click to copy instantly, full date + time on every entry.
- **BYOK cloud AI is free** — no license gate on bring-your-own-key Ava / cloud features.
- **Startup hardening** — stale lock files, duplicate log lines, and a wizard-launch failure mode are all fixed.

---

## From the Developer

> I'm Morne, and my hands fucking hurt.
>
> I've had HSD for a decade — using a mouse and keyboard hurts, all the time. So I use these inflamed joints to build things that ease the load on someone else's. For the past year, that's been Samsara: an ongoing exploration of how accessible AI can actually make a computer, built by someone who needs the answer personally.
>
> It works for me every day, and it's still early — expect rough edges. Whether your hands hurt or not, I sincerely hope it helps you. And if it does, I'd love to hear from you.
>
> — Morne
>
> [Read more about the vision behind Samsara →](https://morneis.com/samsara)

---

## What Can It Do?

Samsara is a **fully offline** voice control system powered by Whisper. It runs as a Windows app with a main hub window, system tray integration, and hands-free control over your entire computer.

### Hands-Free Wake (flagship)

No hotkey, no hands. Bind a spoken phrase to an app and Samsara does the rest.

- **"Hey Claude"** → the Claude window focuses (restored if minimized) and a dictation session opens targeting it. Talk, pause to think, keep talking — the session survives silence and appends each utterance.
- **"over"** → submits. Only the *last* word you speak is checked, so "tell them to come over here" types normally and just "...and that's the plan. over." sends.
- **Per-target send policy** — Claude submits on "over"; agentic targets like Hermes leave the text staged so nothing fires without you.
- **Earcons** — audio cues for session-start and sent, so you know the state without looking.
- **Any mic** — the adaptive noise-floor gate means a quiet headset mic triggers wake words just as reliably as a desktop condenser.

Wake phrases, targets, and send behavior are all configurable. Built on custom OpenWakeWord models with a Whisper-transcript fallback.

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
- **Conversational memory** — Ava remembers the conversation. Follow up with "what about Germany?" and she knows you were asking about capitals.
- Responses are spoken aloud and also displayed in the history panel
- Uses whatever Ollama model you have installed locally
- Optional cloud mode (DeepSeek, OpenAI, Anthropic) with your own API key
- TTS is interruptible — start talking and Ava stops
- Say "ava forget" to clear the conversation and start fresh

Also reachable by voice in command mode: hold Right Ctrl and say "Hey Ava, [question]" or "Is it safe to [action]".

### Text-to-Speech

Samsara can speak. Uses EdgeTTS or Windows Natural voices — not the robotic pyttsx3 voices. The smart AudioCoordinator manages the audio: music ducks while Samsara speaks, the mic stays clean, and if you start talking mid-response, TTS stops immediately.

Enabled and configured in Settings → TTS tab. Off by default.

### Voice Commands

410+ built-in commands plus a plugin system. Say a command after your wake word (default: "Jarvis").

| Category | Examples |
|----------|----------|
| **Apps** | "open Chrome", "open Word", "open Spotify" |
| **Health** | "pain level 6", "took ibuprofen 400mg", "health summary", "how was my week" |
| **Reminders** | "remind me to stretch every 30 minutes", "list reminders", "cancel reminder" |
| **Alarms** | "complete alarm", "dismiss alarm", "read alarms" |
| **Macros** | "going dark" (mute + minimize + lock), "good morning" (mail + GitHub + music) |
| **Audio** | "switch to speakers", "use my headset" |
| **Browser** | "find tab GitHub", "search for ergonomic keyboards" |
| **Screen** | "record my screen", "record this window", "stop recording" |
| **Smart Home** | "lights red", "lights off", "light effect rainbow" |
| **Media** | "play", "pause", "next", "mute" — earbud-style transport on the current session |
| **Music** | "play me some music", "play moonlight", "volume down" |
| **3D Printing** | "printer status", "pause print", "cancel print", "printer light" |
| **Utilities** | "set a timer for 5 minutes", "search for a gif of dancing cat" |
| **Text** | "period", "new line", "select all", "undo" |
| **Scrolling** | "scroll up a little", "scroll up", "scroll up medium", "scroll up high", "scroll up fast" — plus down variants |
| **Text Selection** | "mark here", "select to here" — anchor-based selection across any scroll distance |
| **Repeat** | "again", "repeat" — re-fire the last command |
| **Volume** | "volume up", "volume down" — Core Audio API, no media keys |
| **Streaming** | "play on stremio", "stremio pause", "stremio fullscreen" |
| **Voice AI** | Hold Right Alt → speak to Ava (Ollama) → hear response |

### Smart Home & IoT

Samsara talks directly to hardware on your network:

- **Hyperion LED strips** — "lights red", "lights off", "light effect rainbow". 11 preset colors, 14 effect aliases with fuzzy matching against your Hyperion instance. Supports IPv4, IPv6, and hostnames.
- **FlashForge 3D printers** — "printer status" (temperatures, progress, state), "pause print", "resume print", "cancel print", "printer light". TCP M-code protocol, tested on AD5X.
- **Spotify** — earbud-style "play / pause / next / mute" plus a configurable song library ("play hurt", "play moonlight"). Opens tracks directly in the desktop app.

### Main Window

Samsara opens a hub window on launch with three views:

- **History** — searchable list of all dictations with timestamps, source apps, success/fail status, copy and retry buttons. Inline correction, session grouping, type/confidence indicators.
- **Dictionary** — unified corrections manager with three tabs: Vocabulary (Whisper hints), Corrections (phonetic wash rules), Wake Words (misrecognition fixes). Add, edit, delete from the UI — changes take effect immediately without restart.
- **Settings** — microphone, model, hotkeys, cleanup mode, streaming mode, TTS, all in one place.

Plus standalone overlays (PySide6 — always above all apps, DPI-aware, click-through):

- **Command Cheat Sheet** — floating always-on-top overlay listing every active command. Opacity slider, filterable by pack. Toggle from tray or by voice.
- **Show Numbers** — voice-driven clicking: an overlay numbers every interactive element on screen. Say the number to click it. Fully hands-free UI navigation.
- **Listening Indicator** — a pill that pulses and shows current mode (dictating / command / Ava / wake / streaming) at the corner of your screen.

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

Ships with 31 plugins including health tracking, voice reminders, alarm management, voice AI / Ava (Ollama + cloud LLM), smart home control, music and media transport, 3D printer integration, macros, audio switching, tab finder, web shortcuts, timer, GIF search, screen recording, scroll (5-speed + horizontal + page nav), text marker (deferred range selection), volume/mute (Core Audio API), window switcher (letter-based targeting), show numbers (hands-free clicking), Stremio, and more.

**Privacy note on the Task List plugin:** tasks you add by voice (`plugins/commands/tasks.py`) are stored locally only. As of v0.21.1 this plugin makes no network requests of any kind — an earlier undisclosed "sync to Arcana" network call has been removed entirely (see CHANGELOG.md).

---

## Getting Started

### Download for Windows

1. Go to the [latest release](https://github.com/Morne-Ingstar/Samsara/releases/latest)
2. Download **Samsara-Windows-\*.zip** (grab the CUDA pack too if you have an NVIDIA GPU — see the release notes)
3. Extract and run **Samsara.exe** — a setup wizard walks you through microphone selection and model download

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

### Configuring Wake Targets & Plugins

Hands-free wake targets and plugin settings live in `config.json`.

```json
{
  "wake_targets": [
    { "phrase": "hey claude", "target_process": "claude.exe", "send_policy": "enter" },
    { "phrase": "activate hermes", "target_process": "Hermes.exe", "send_policy": "stage_only" }
  ],
  "hyperion_host": "your-hyperion-ip-or-hostname",
  "hyperion_port": 19444,
  "flashforge_ip": "your-printer-ip",
  "music_volume": 30
}
```

`send_policy: "enter"` submits on the send word; `"stage_only"` leaves text staged for agentic targets that shouldn't auto-fire.

---

## Under the Hood

### Audio Pipeline

- **AudioCaptureEngine (ACE)** — a single PortAudio stream writes 16kHz int16 frames into a lock-free ring buffer. All consumers (wake word, VAD, dictation, streaming, debug) read from the same ring via independent cursors. No consumer can stall capture. Writer-dominant lossy design: a slow consumer only hurts itself.
- **Adaptive wake gate** — wake detection passes audio to Whisper when its energy rises above a rolling ambient noise floor (floor × ratio), not a fixed absolute threshold. This makes wake words fire reliably across mics of wildly different sensitivity — a quiet headset and a hot desktop condenser both work without tuning.
- **Silero VAD** — neural speech detection, ignores fan noise and background hum. Runs on raw mic signal, not AEC output.
- **Pre-buffer** — 1.5s rolling window is built into the ring. "Drain prebuffer on speech onset" is a cursor rewind, not a copy — structurally impossible to forget.
- **Echo cancellation** — frequency-domain AEC subtracts system audio from mic input. Dictate over music and Whisper still hears you.
- **Auto-calibration** — measures ambient noise on startup using IQR-based outlier rejection.
- **Auto-reconnect** — if audio dies after sleep/wake, Samsara detects it and reconnects automatically. No restart needed.

### Streaming Architecture

When streaming mode is enabled, dictation becomes real-time:

1. Hold the hotkey — audio capture starts, pre-buffer is skipped for faster response.
2. After 0.7 seconds, the first partial transcription appears (beam_size=1 for speed).
3. Every 1.0 seconds, Whisper re-transcribes the entire buffer from the start. New words appear, existing words may refine.
4. In direct-paste mode, each partial replaces the previous text in your focused app using Ctrl+A select-and-replace.
5. On release, a final pass runs with full beam search (beam_size=5) and Grammar-Lite cleanup. The polished result replaces everything.

First text appears in ~1 second. The overlay shows what's being transcribed even when direct-paste is off.

### Architecture

PySide6 UI with a main hub window (`samsara/ui/main_window_qt.py`). AudioCaptureEngine in `samsara/audio_engine/` — single PortAudio stream, lock-free ring buffer, independent consumer cursors. Dictation engine in `dictation.py` with an explicit mode state machine (`samsara/mode.py`) and the hands-free wake-session loop. Streaming engine in `samsara/streaming.py`. Ava voice AI with conversational memory (`samsara/ava_memory.py`). Config file-watch with three-way merge (`samsara/config_watch.py`). Correction pipeline: phonetic wash → wake corrections → grammar cleanup. All user corrections stored in `~/.samsara/` as JSON, hot-reloaded without restart.

```
samsara/
├── audio_engine/
│   ├── engine.py               # Single PortAudio stream, ring writer
│   ├── frame.py                # Frame definition, ring/prebuffer constants
│   ├── ring.py                 # Lock-free FrameBus + Reader cursors
│   ├── dictation_consumer.py   # Hold/toggle mode ring consumer
│   ├── continuous_consumer.py  # Continuous listening ring consumer
│   ├── wake_consumer.py        # Wake word + VAD policy loop
│   └── debug_recorder.py       # WAV dump consumer (opt-in)
├── mode.py                     # Mode enum + ModeStateMachine
├── wake_detector.py            # OpenWakeWord pre-filter (per-target models)
├── wake_word_matcher.py        # Transcript-level wake phrase matching
├── wake_corrections.py         # Wake word variant corrections
├── ava_memory.py               # Conversational memory (session-scoped)
├── config_watch.py             # File-watch + three-way merge
├── streaming.py                # Real-time streaming dictation engine
├── cleanup.py                  # Grammar-Lite post-processing
├── history.py                  # SQLite dictation history
├── health_store.py             # Pain/medication/symptom logging
├── phonetic_wash.py            # Fixes Whisper misrecognitions
├── command_registry.py         # Token-based longest-match resolver
├── tts/
│   ├── coordinator.py          # Audio ducking, interrupt, state machine
│   ├── edge_tts_engine.py      # Azure Neural voices via edge-tts
│   └── winrt_engine.py         # Windows native TTS
├── ui/
│   ├── main_window_qt.py       # Hub window (History/Dictionary/Settings)
│   ├── settings_qt.py          # All settings tabs (PySide6)
│   ├── first_run_wizard_qt.py  # Accessibility-path setup wizard
│   └── ...
plugins/commands/
├── health_tracker.py           # "pain level 6", "took ibuprofen"
├── alarm_commands.py           # "complete alarm", "dismiss alarm"
├── reminders.py                # "remind me to stretch every 30 min"
├── ask_ollama.py               # Voice AI / Ava (Ollama + cloud LLM)
├── hyperion_lights.py          # "lights red", "light effect rainbow"
├── window_switcher.py          # Letter-based window targeting + wake focus
├── show_numbers.py             # Hands-free overlay clicking
├── scroll.py                   # 5-speed + horizontal + page nav
├── music.py                    # Spotify + earbud-style media transport
├── volume.py                   # Core Audio API volume/mute
└── ...                         # 31 plugins total
```

### Tests

```bash
python -m pytest tests/ -v
```

---

## Roadmap

### Completed
- [x] **Hands-free multi-wakeword dictation** — "Hey Claude"/"Activate Hermes" → focus + dictate, "over" to send, per-target send policy, earcons
- [x] **Adaptive microphone gate** — noise-floor-relative wake detection that works across any mic
- [x] **AudioCaptureEngine** — single-stream lock-free ring buffer replacing 3 separate PortAudio streams
- [x] **Full PySide6 migration** — zero Tkinter/CustomTkinter in the codebase
- [x] **Ava conversational memory** — multi-turn, cloud + local Ollama, "ava forget" to reset
- [x] **Config file-watch** — edit config.json while running, three-way merge, .bak recovery
- [x] **Explicit mode state machine** — centralized mode transitions replacing scattered flag-sets
- [x] **First-run accessibility wizard** — chronic pain / privacy / power user / dictation paths
- [x] **Health tracking** — pain levels, medication, symptoms by voice. Local JSON, CSV export
- [x] **Medication dictionary** — 100+ medication names for speech recognition accuracy
- [x] **Voice reminders & alarms** — "remind me to stretch every 30 min", streaks, gamification
- [x] **Expanded scrolling** — scroll to top/bottom, page up/down, horizontal scroll
- [x] 410+ voice commands with 31-plugin system
- [x] Voice AI / Ava — local Ollama + optional cloud (DeepSeek/OpenAI/Anthropic)
- [x] Text-to-Speech with EdgeTTS / Windows Natural voices and smart audio ducking
- [x] Keyboard command mode (hold-to-talk walkie-talkie)
- [x] Earbud-style media transport ("play"/"pause"/"next"/"mute")
- [x] Show Numbers overlay for hands-free clicking
- [x] Window manager v2 — letter-based targeting, move between monitors, saved layouts
- [x] Streaming dictation with live overlay and direct-paste
- [x] Smart Actions webhook bridge with tiered consent
- [x] Command packs with per-user enable/disable
- [x] Deferred text selection, 5-speed scrolling, repeat/again
- [x] Core Audio API volume and mute
- [x] Smart home (Hyperion LED strips, FlashForge 3D printers, Spotify)
- [x] Dictation history with SQLite search and recovery
- [x] Echo cancellation, pre-buffer, auto-calibration, auto-reconnect

### Planned
- [ ] Streaming integration with hands-free wake sessions
- [ ] macOS support (platform abstraction layer designed, 7 port stages written)
- [ ] Ava Hemispheres — split-brain agent (cloud intelligence + local vision + Samsara actions)
- [ ] Symbolic targeting language for interaction compression
- [ ] Guided demo interactions for new users
- [ ] Speaker verification (local, via Resemblyzer)
- [ ] Per-app command profiles
- [ ] Voice-to-code pipeline (ARC review → Claude Code → confirm/reject)

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

AGPL-3.0 — genuinely open source, free for everyone, forever. The one rule:
any fork or service built on Samsara must keep its source open too, so no one
can ever take this away from the people it's built for. See [LICENSE](LICENSE).

## Support

Samsara is free forever — no paywalls, no feature gates, ever. If it's useful
to you, [sponsoring on GitHub](https://github.com/sponsors/Morne-Ingstar) or
[Ko-fi](https://ko-fi.com/morneingstar) helps fund the time spent building and
maintaining it. Entirely optional.

## Acknowledgments

- [OpenAI Whisper](https://github.com/openai/whisper) / [faster-whisper](https://github.com/guillaumekln/faster-whisper)
- [OpenWakeWord](https://github.com/dscripka/openWakeWord) for custom wake-phrase models
- [PySide6](https://doc.qt.io/qtforpython-6/) (Qt for Python)
- Designed with [Claude](https://anthropic.com), [ChatGPT](https://openai.com),
  and [Gemini](https://deepmind.google) through the
  [ARC](https://github.com/Morne-Ingstar/ARC) adversarial review process

---

<p align="center">
  <i>Named after the Buddhist concept of cyclical existence —
  Samsara helps break the cycle of repetitive strain.</i>
</p>
