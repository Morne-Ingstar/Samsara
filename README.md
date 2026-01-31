# 🔄 Samsara

### Voice Dictation & Control for Accessibility

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()

---

## 💬 From the Developer

> Hi, I'm Morne, and my hands fucking hurt.
>
> I've had HSD (Hypermobile Spectrum Disorder) for a decade. Using a mouse and keyboard hurts—all the time. But thanks to AI tools, I can now build software that reduces how much I need to type and click.
>
> I tried paid apps that do similar things, but they were fragmented, expensive, and didn't quite fit my needs. So I built Samsara—combining everything into one free, open-source tool.
>
> I'm making this public because I hope it helps others like me. If you know someone who could benefit, please pass it along. Feedback is always welcome.
>
> — Morne

---

## ✨ What is Samsara?

Samsara is a **fully offline** voice dictation and command system powered by OpenAI's Whisper. It's designed for people who need hands-free computing—whether due to chronic pain, limited mobility, RSI, or just wanting a faster workflow.

**Key highlights:**
- 🎤 **Speak, don't type** — Dictate text into any application
- 🔇 **100% offline** — Your voice never leaves your computer
- ⚡ **GPU accelerated** — Near-instant transcription with CUDA
- 🎮 **Voice commands** — Control your computer with 40+ built-in commands
- 🗣️ **Wake word mode** — Hands-free activation ("Hey Samsara...")
- 🎨 **Customizable** — Sounds, hotkeys, commands, and more

---

## 🚀 Quick Start

### 1. Install

```bash
git clone https://github.com/Morne-Ingstar/samsara.git
cd samsara
pip install -r requirements.txt
```

### 2. Run

**Windows:** Double-click `_launcher.vbs` (runs silently in background)

**macOS/Linux:** `python dictation.py`

### 3. Use

- Hold **Ctrl+Shift** and speak
- Release to transcribe
- Text appears at your cursor

That's it! A setup wizard will guide you through first-time configuration.

---

## 🎯 Features

### Speech-to-Text
| Feature | Description |
|---------|-------------|
| **Offline** | Uses Whisper locally—no internet, no cloud, no data collection |
| **Fast** | GPU acceleration with CUDA for sub-second transcription |
| **Accurate** | Multiple model sizes from fast (tiny) to precise (large-v3) |
| **Smart** | Auto-capitalizes sentences, formats numbers, preserves clipboard |

### Dictation Modes
| Mode | How it works |
|------|--------------|
| **Hold** | Hold hotkey to record, release to transcribe |
| **Toggle** | Press to start, press again to stop |
| **Continuous** | Always listening, transcribes when you pause |
| **Wake Word** | Say "Hey Samsara" to activate hands-free |

### Voice Commands
40+ built-in commands for hands-free control:

```
"new line"          → Enter key
"select all"        → Ctrl+A
"copy that"         → Ctrl+C
"paste"             → Ctrl+V
"undo"              → Ctrl+Z
"period"            → Inserts .
"question mark"     → Inserts ?
"open browser"      → Launches default browser
```

[See full command list →](Docs/VOICE_COMMANDS.md)

### Sound Themes 🎵
Four built-in audio themes for feedback sounds:

| Theme | Vibe |
|-------|------|
| **cute** | Playful bloops (Nintendo/Duolingo style) |
| **warm** | Rich chords (OS boot sound vibes) |
| **zen** | Singing bowls and chimes |
| **chirpy** | Bright bird-like chirps |

Switch themes in Settings → Sounds. Supports WAV, MP3, OGG, FLAC.

### Voice Training
- Add custom vocabulary (names, technical terms)
- Auto-correct common mistranscriptions
- Calibrate microphone sensitivity
- Import/export training profiles

---

## 💻 System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **OS** | Windows 10, macOS 10.15, Ubuntu 20.04 | Windows 11, macOS 13+, Ubuntu 22.04 |
| **Python** | 3.10 | 3.11+ |
| **RAM** | 4 GB | 8 GB+ |
| **GPU** | None (CPU works) | NVIDIA with 4GB+ VRAM |
| **Disk** | 2 GB | 10 GB (for larger models) |

### Model Sizes

| Model | Speed | Accuracy | VRAM | Disk |
|-------|-------|----------|------|------|
| tiny | ⚡⚡⚡⚡ | ★★☆☆ | ~1 GB | ~75 MB |
| base | ⚡⚡⚡ | ★★★☆ | ~1 GB | ~150 MB |
| small | ⚡⚡ | ★★★★ | ~2 GB | ~500 MB |
| medium | ⚡ | ★★★★☆ | ~5 GB | ~1.5 GB |
| large-v3 | 🐢 | ★★★★★ | ~10 GB | ~3 GB |

---

## 📦 Installation

### Windows

```batch
git clone https://github.com/Morne-Ingstar/samsara.git
cd samsara
install.bat
```

Or manually:
```batch
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### macOS

```bash
brew install portaudio
git clone https://github.com/Morne-Ingstar/samsara.git
cd samsara
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Linux (Ubuntu/Debian)

```bash
sudo apt-get install python3-dev portaudio19-dev
git clone https://github.com/Morne-Ingstar/samsara.git
cd samsara
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Optional: MP3 Support

For MP3/OGG/FLAC sound files:
```bash
pip install pydub
# Also install ffmpeg:
# Windows: choco install ffmpeg  (or download from ffmpeg.org)
# macOS: brew install ffmpeg
# Linux: sudo apt install ffmpeg
```

---

## ⌨️ Default Hotkeys

| Hotkey | Action |
|--------|--------|
| `Ctrl+Shift` | Hold to dictate |
| `Ctrl+Alt+D` | Toggle continuous mode |
| `Ctrl+Alt+W` | Toggle wake word mode |
| `Escape` | Cancel current recording |

All hotkeys are customizable in Settings.

---

## 🗂️ Project Structure

```
samsara/
├── dictation.py          # Main application
├── voice_training.py     # Training module
├── commands.json         # Voice command definitions
├── config.json           # User settings (auto-created)
├── requirements.txt      # Dependencies
├── _launcher.vbs         # Silent Windows launcher
├── sounds/               # Audio feedback files
│   └── themes/           # Sound theme folders
├── samsara/              # Modular components
│   ├── config.py
│   ├── audio.py
│   ├── speech.py
│   └── commands.py
└── Docs/                 # Documentation
```

---

## 📚 Documentation

| Guide | Description |
|-------|-------------|
| [Quick Start](Docs/QUICKSTART.md) | Get up and running in 5 minutes |
| [Voice Commands](Docs/VOICE_COMMANDS.md) | Full command reference |
| [Custom Commands](Docs/CUSTOM_COMMANDS.md) | Create your own commands |
| [Wake Word Guide](Docs/WAKE_WORD_GUIDE.md) | Hands-free activation setup |
| [Voice Training](Docs/VOICE_TRAINING_FEATURE.md) | Improve recognition accuracy |

---

## 🐛 Troubleshooting

**App won't start?**
- Run `python dictation.py` directly to see errors
- Check Python 3.10+ is installed
- Run `pip install -r requirements.txt`

**No transcription?**
- Check microphone in Settings
- Test mic in Voice Training → Calibration
- Ensure Whisper model is loaded (check tray tooltip)

**Commands not working?**
- Enable command mode in Settings
- Commands are case-insensitive
- Check Commands tab for available commands

**GPU not detected?**
- Install [CUDA Toolkit](https://developer.nvidia.com/cuda-toolkit)
- Verify with `nvidia-smi`
- App falls back to CPU automatically

---

## 🗺️ Roadmap

- [ ] Toast notifications on transcription
- [ ] Undo last dictation
- [ ] Command chaining ("select all copy")
- [ ] Application-specific commands
- [ ] Spelling mode ("spell c-a-t" → "cat")
- [ ] Usage statistics dashboard
- [ ] Plugin system for extensions

---

## 🤝 Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

For bugs or feature requests, [open an issue](https://github.com/Morne-Ingstar/samsara/issues).

---

## 📄 License

MIT License — free for personal and commercial use.

---

## 🙏 Acknowledgments

- [OpenAI Whisper](https://github.com/openai/whisper) — Speech recognition model
- [faster-whisper](https://github.com/guillaumekln/faster-whisper) — Optimized implementation
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) — Modern UI framework
- Built with assistance from [Claude](https://anthropic.com) by Anthropic

---

<p align="center">
  <i>Named after the Buddhist concept of cyclical existence — Samsara helps break the cycle of repetitive strain.</i>
</p>
