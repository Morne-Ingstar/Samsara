# Samsara Voice Dictation

A powerful Python-based speech-to-text and voice command application designed for accessibility. Uses OpenAI's Whisper model (via faster-whisper) for accurate, fully offline transcription with GPU acceleration.

Samsara is designed for people who need hands-free computing - whether due to chronic pain, limited mobility, or simply wanting a more efficient workflow.

## Features

### Speech-to-Text
- **Offline transcription** using OpenAI's Whisper model - no internet required
- **GPU acceleration** with CUDA for near-instant transcription
- **Multiple model sizes** from tiny (fastest) to large-v3 (most accurate)
- **Auto-paste** transcribed text directly into any application
- **Voice Activity Detection (VAD)** for cleaner transcriptions

### Dictation Modes
- **Hold Mode**: Hold your hotkey to record, release to transcribe
- **Toggle Mode**: Press hotkey to start recording, press again to stop
- **Continuous Mode**: Always listening, auto-transcribes when you pause speaking
- **Wake Word Mode**: Hands-free activation with a trigger phrase (e.g., "hey samsara")

See [Docs/WAKE_WORD_GUIDE.md](Docs/WAKE_WORD_GUIDE.md) for detailed mode information.

### Voice Commands
- **40+ built-in commands** for navigation, text editing, and system control
- **Command types**: Hotkeys, app launchers, key press/hold, mouse clicks
- **Customizable** - add, edit, or remove commands through the Settings UI
- **Gaming support** - hold/release keys for continuous movement

See [Docs/VOICE_COMMANDS.md](Docs/VOICE_COMMANDS.md) for the full command list.
See [Docs/CUSTOM_COMMANDS.md](Docs/CUSTOM_COMMANDS.md) for adding your own commands.

### Voice Training
- **Custom vocabulary** - add technical terms, names, or jargon that Whisper often misrecognizes
- **Corrections dictionary** - auto-replace common transcription errors
- **Microphone calibration** - visual level monitor and test phrases
- **Initial prompt customization** - bias Whisper toward your domain

See [Docs/VOICE_TRAINING_FEATURE.md](Docs/VOICE_TRAINING_FEATURE.md) for details.

### Modern Settings Interface
- **Tabbed settings window** with CustomTkinter dark theme
- **General**: Microphone selection, model size, basic options
- **Hotkeys & Modes**: Configure all keyboard shortcuts and recording modes
- **Commands**: View, search, add, edit, delete, and test voice commands
- **Sounds**: Customize audio feedback with your own WAV files
- **Advanced**: Fine-tune continuous mode, wake word settings

### Audio Feedback
- **Customizable sounds** for recording start, stop, success, and error
- **WAV file support** - use your own sound files
- **Quick response** - sounds play immediately on hotkey press

### System Integration
- **System tray** icon with quick access to all features
- **Background operation** - runs silently without console window
- **Auto-start** option available
- **First-run wizard** for easy setup

## Requirements

- **Python 3.10+**
- **Windows 10/11**
- **CUDA-capable GPU** (recommended) or CPU-only mode
- **~2-10GB disk space** for Whisper models (downloaded on first use)

### GPU Requirements by Model

| Model | Speed | Accuracy | VRAM Required |
|-------|-------|----------|---------------|
| tiny | Fastest | Basic | ~1GB |
| base | Fast | Good | ~1GB |
| small | Medium | Better | ~2GB |
| medium | Slow | Very Good | ~5GB |
| large-v3 | Slowest | Best | ~10GB |

## Installation

### 1. Clone the Repository
```bash
git clone https://github.com/YOUR_USERNAME/samsara.git
cd samsara
```

### 2. Install Dependencies
Run the install script:
```bash
install.bat
```

Or manually with pip:
```bash
pip install -r requirements.txt
```

### 3. First Run
Launch using the VBS launcher for silent background operation:
```
_launcher.vbs
```

On first run, a setup wizard will guide you through:
1. Welcome screen
2. Microphone selection and testing
3. Hotkey configuration
4. Model size selection

## Usage

### Launching the App

**Recommended**: Double-click `_launcher.vbs` for silent background operation (no console window).

You can create a shortcut to `_launcher.vbs` on your desktop or add it to your startup folder.

**Alternative launchers**:
- `samsara_launcher.py` - GUI launcher with options
- Direct Python: `pythonw dictation.py` (silent) or `python dictation.py` (with console)

### Default Hotkeys

| Hotkey | Action |
|--------|--------|
| **Ctrl+Shift** | Hold to dictate (in hold mode) |
| **Ctrl+Alt+D** | Toggle continuous mode |
| **Ctrl+Alt+W** | Toggle wake word mode |

All hotkeys are customizable in Settings.

### System Tray

Right-click the Samsara icon in the system tray to:
- Switch microphones
- View current mode and model
- Open Settings
- Open Voice Training
- View logs
- Exit the application

### Quick Start

1. Launch Samsara using `_launcher.vbs`
2. Hold **Ctrl+Shift** and speak
3. Release to transcribe - text appears at your cursor
4. Say commands like "new line", "select all", "copy", "paste"

See [Docs/QUICKSTART.md](Docs/QUICKSTART.md) for a detailed getting started guide.

## Configuration

### Settings File
Configuration is stored in `config.json`. You can edit this directly or use the Settings window.

### Key Settings
- `microphone` - Selected input device ID
- `model_size` - Whisper model (tiny/base/small/medium/large-v3)
- `mode` - Recording mode (hold/toggle/continuous/wake_word)
- `hotkey` - Main recording hotkey
- `language` - Transcription language (default: en)
- `audio_feedback` - Enable/disable sounds

### Custom Sounds
Place WAV files in the `sounds/` folder:
- `start.wav` - Recording started
- `stop.wav` - Recording stopped
- `success.wav` - Transcription complete
- `error.wav` - Error occurred

## File Structure

```
Samsara/
    dictation.py          # Main application
    voice_training.py     # Voice training module
    commands.json         # Voice command definitions
    config.json           # User settings (created on first run)
    requirements.txt      # Python dependencies
    _launcher.vbs         # Silent launcher (recommended)
    samsara_launcher.py   # GUI launcher
    install.bat           # Dependency installer
    sounds/               # Audio feedback files
    Docs/                 # Documentation
```

See [Docs/FILE_GUIDE.md](Docs/FILE_GUIDE.md) for detailed file descriptions.

## Documentation

| Document | Description |
|----------|-------------|
| [QUICKSTART.md](Docs/QUICKSTART.md) | Getting started guide |
| [VOICE_COMMANDS.md](Docs/VOICE_COMMANDS.md) | Full list of voice commands |
| [CUSTOM_COMMANDS.md](Docs/CUSTOM_COMMANDS.md) | How to add your own commands |
| [VOICE_TRAINING_FEATURE.md](Docs/VOICE_TRAINING_FEATURE.md) | Voice training and calibration |
| [WAKE_WORD_GUIDE.md](Docs/WAKE_WORD_GUIDE.md) | Wake word and modes guide |
| [MICROPHONE_FEATURE.md](Docs/MICROPHONE_FEATURE.md) | Microphone selection details |
| [FILE_GUIDE.md](Docs/FILE_GUIDE.md) | File structure reference |

## Troubleshooting

### App won't start
- Check Python is installed and in PATH
- Run `install.bat` to ensure dependencies are installed
- Try running `python dictation.py` directly to see error messages

### No transcription / silence
- Check microphone is selected correctly in Settings
- Test microphone in Voice Training > Calibration
- Ensure model is loaded (check system tray tooltip)

### Commands not working
- Verify command mode is enabled in Settings
- Check the command exists in Commands tab
- Commands are case-insensitive

### GPU not detected
- Install CUDA toolkit matching your GPU
- Check `nvidia-smi` works in command prompt
- App will fall back to CPU if GPU unavailable

## License

MIT License - see LICENSE file for details.

## Contributing

Contributions welcome! Please open an issue or pull request.

## Acknowledgments

- [OpenAI Whisper](https://github.com/openai/whisper) - Speech recognition model
- [faster-whisper](https://github.com/guillaumekln/faster-whisper) - Optimized Whisper implementation
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) - Modern UI framework
- [pynput](https://github.com/moses-palmer/pynput) - Keyboard/mouse control
