# Troubleshoot Samsara Issues

Use this prompt to get AI help diagnosing and fixing Samsara problems.

---

## Prompt Template

Copy everything below the line and paste it into your AI chat:

---

I need help troubleshooting an issue with Samsara, a voice dictation app for Windows. Here's the context:

### About Samsara
- Python-based voice-to-text using faster-whisper (Whisper AI)
- Supports GPU (CUDA) and CPU inference
- Has hotkey dictation, wake word mode, and voice commands
- Config stored in `config.json`, commands in `commands.json`

### Common Issue Categories

1. **App won't start** - crashes on launch, missing DLLs
2. **No transcription** - records but doesn't produce text
3. **Wrong transcription** - text is garbled or inaccurate
4. **Commands not working** - voice commands don't trigger
5. **Audio issues** - wrong mic, no recording, feedback sounds missing
6. **Performance** - slow transcription, high CPU/GPU usage
7. **Hotkeys not working** - key combinations don't trigger recording

### My Issue

**What's happening:**
[Describe the problem]

**What I expected:**
[What should happen]

**When it started:**
[After update? After changing settings? Always?]

**Error messages (if any):**
```
[Paste any error messages here]
```

**My config.json:**
```json
[Paste relevant parts of your config]
```

**My system:**
- Windows version:
- GPU (if applicable):
- Running as EXE or Python:

---

## Common Fixes Reference

### App Crashes on Start
- Missing CUDA DLLs → Install CUDA Toolkit or use CPU mode
- Corrupted config → Delete `config.json` and restart
- Port conflict → Another Samsara instance running

### No Transcription Output
- Check microphone in Settings → Audio
- Run Voice Training calibration
- Try a different Whisper model size

### Commands Not Triggering
- Enable command mode in Settings
- Check exact phrase in Commands tab
- Commands are case-insensitive but must match exactly

### Slow Performance
- Use smaller model (tiny/base) for faster response
- Enable GPU acceleration if you have NVIDIA
- Close other GPU-intensive applications

---

## How to Get Your Config

```
Location: C:\Users\[You]\Projects\Samsara\config.json
Or in the EXE folder: Samsara\config.json
```

Only share relevant sections - remove any sensitive paths or API keys.
