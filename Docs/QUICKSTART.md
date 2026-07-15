# Quick Start

## Download & Run (Windows)

1. Go to the [latest release](https://github.com/Morne-Ingstar/Samsara/releases/latest)
2. Download **Samsara-Windows-v0.22.0.zip**
3. Extract anywhere, double-click **Samsara.exe**
4. The setup wizard picks your mic and downloads the AI model

The standard download is the verified CPU build. It works without an NVIDIA
GPU. For faster transcription on a compatible NVIDIA GPU, follow the
[verified CUDA add-on instructions](CUDA.md).

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

**Hands Free:** Enable it in Settings, then speak normally. Samsara buffers a
complete thought across pauses. Say **"end"** as a sole utterance to paste the
thought; HANDS FREE stays active for the next one. Exact enabled commands such
as "scroll down" or "show numbers" work without leaving the session.

**Wake-word commands:** Enable wake words, then say "Jarvis" followed by a
command:

| Try saying | What happens |
|------------|-------------|
| "Jarvis, open Chrome" | Launches Chrome |
| "Jarvis, take a screenshot" | Opens snip tool |
| "Jarvis, snap left" | Snaps window to left half |
| "Jarvis, where is GitHub" | Finds and switches to your GitHub tab |
| "Jarvis, go to YouTube" | Opens YouTube in your browser |
| "Jarvis, going dark" | Mutes + minimizes all + locks screen |
| "Jarvis, volume up" | Raises system volume |
| "Jarvis, scratch that" | Undoes the last dictation |

## Default Hotkeys

| Hotkey | Action |
|--------|--------|
| Ctrl+Shift | Hold to dictate |
| Configurable | Toggle HANDS FREE |
| Configurable | Toggle wake-word listening |
| Configurable | Commit the current HANDS FREE thought |

The setup wizard and Settings show the actual bindings in use; defaults can
vary by input device and first-run choices.

## Next Steps

- Right-click the tray icon to explore settings
- Use Settings to configure devices, modes, wake words, and command packs
- Drop Python files in `plugins/commands/` to create custom voice commands
- Open Quick Reference from the tray for the commands enabled on your machine
