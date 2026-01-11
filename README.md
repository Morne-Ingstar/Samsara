# Samsara Voice Dictation

A Python-based speech-to-text and voice command application designed for accessibility. Uses OpenAI's Whisper model (via faster-whisper) for accurate offline transcription with GPU acceleration.

## Features

- **Multiple Dictation Modes**
  - Hold mode: Hold hotkey to record, release to transcribe
  - Toggle mode: Press to start/stop recording
  - Continuous mode: Auto-transcribes on silence pauses
  - Wake word mode: Trigger with a phrase like "hey claude"

- **Voice Commands**: 40+ built-in commands for navigation, text editing, and punctuation
- **Voice Training**: Custom vocabulary, corrections dictionary, and recognition calibration
- **GPU Acceleration**: CUDA support for fast transcription
- **System Tray**: Runs in background with easy access to settings

## Requirements

- Python 3.10+
- CUDA-capable GPU (recommended) or CPU
- Windows 10/11

## Installation

1. Clone this repository
2. Run `install.bat` or manually install dependencies:
   ```
   pip install -r requirements.txt
   ```

## Usage

Launch the application using `_launcher.vbs` for silent background operation.

On first run, a setup wizard will guide you through:
- Microphone selection
- Hotkey configuration
- Model size selection

### Default Hotkeys

- **Ctrl+Shift**: Hold to dictate (in hold mode)
- **Ctrl+Alt+D**: Toggle continuous mode
- **Ctrl+Alt+W**: Toggle wake word mode

## Configuration

Settings are stored in `config.json`. Access the Settings window from the system tray.

## Model Sizes

| Model | Speed | Accuracy | VRAM |
|-------|-------|----------|------|
| tiny | Fastest | Basic | ~1GB |
| base | Fast | Good | ~1GB |
| small | Medium | Better | ~2GB |
| medium | Slow | Very Good | ~5GB |
| large-v3 | Slowest | Best | ~10GB |

## License

MIT License
