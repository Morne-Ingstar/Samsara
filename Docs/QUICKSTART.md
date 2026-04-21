# Quick Start

## Download & Run (Windows)

1. Go to the [latest release](https://github.com/Morne-Ingstar/Samsara/releases/latest)
2. Download **Samsara-Windows-v0.9.4.7z**
3. Extract anywhere, double-click **Samsara.exe**
4. The setup wizard picks your mic and downloads the AI model

NVIDIA GPU recommended (~300ms transcription). Works on CPU too, just slower.

## From Source

```bash
git clone https://github.com/Morne-Ingstar/Samsara.git
cd Samsara
pip install -r requirements.txt
python dictation.py
```

## Your First Commands

Once running, Samsara sits in your system tray (bottom-right, near the clock).

**Hold-to-dictate:** Hold Ctrl+Shift, speak, release. Text appears at your cursor.

**Voice commands:** Say "Jarvis" followed by a command:

| Try saying | What happens |
|------------|-------------|
| "Jarvis, open Chrome" | Launches Chrome |
| "Jarvis, take a screenshot" | Opens snip tool |
| "Jarvis, snap left" | Snaps window to left half |
| "Jarvis, where is GitHub" | Finds and switches to your GitHub tab |
| "Jarvis, go to YouTube" | Opens YouTube in your browser |
| "Jarvis, going dark" | Mutes + minimizes all + locks screen |
| "Jarvis, volume up" | Raises system volume |
| "Jarvis, dictate" | Starts hands-free dictation (say "over" to finish) |
| "Jarvis, scratch that" | Undoes the last dictation |

## Default Hotkeys

| Hotkey | Action |
|--------|--------|
| Ctrl+Shift | Hold to dictate |
| Ctrl+Alt+D | Toggle long dictation |
| Ctrl+Alt+W | Toggle wake word mode |
| Escape | Cancel current recording |

All hotkeys are configurable in Settings.

## Next Steps

- Right-click the tray icon to explore settings
- Edit `config.json` to add web shortcuts and audio device aliases
- Drop Python files in `plugins/commands/` to create custom voice commands
- See [VOICE_COMMANDS.md](VOICE_COMMANDS.md) for the full command reference
