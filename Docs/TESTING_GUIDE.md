# Testing Your Voice Commands

## Step-by-Step Test Guide

### 1. Basic Startup Test
- [ ] Run dictation.py
- [ ] App appears in system tray
- [ ] Console shows "Loaded XX voice commands"
- [ ] Console shows "Model loaded!"

### 2. Simple Dictation Test
- [ ] Hold Ctrl+Shift
- [ ] Say "Hello world"
- [ ] Release keys
- [ ] Text should paste where cursor is

### 3. Voice Command Test - System
Try these commands one at a time:

- [ ] "open chrome" (or firefox/edge)
- [ ] "minimize"
- [ ] "maximize"
- [ ] "show desktop"

### 4. Voice Command Test - Browser
(With browser open)

- [ ] "new tab"
- [ ] "scroll down"
- [ ] "scroll up"
- [ ] "scroll to top"
- [ ] "close tab"

### 5. Voice Command Test - Text
(In a text editor or this document)

- [ ] "select all"
- [ ] "copy"
- [ ] "paste"
- [ ] "undo"

### 6. Voice Command Test - Gaming Keys
- [ ] "hold forward" (should hold W key)
- [ ] "release all" (should release W)
- [ ] "press e"
- [ ] "press space"

### 7. Mouse Commands
- [ ] "double click"
- [ ] "left click"
- [ ] "right click"

### 8. Microphone Switching
- [ ] Right-click system tray icon
- [ ] Hover over microphone menu
- [ ] See list of microphones
- [ ] Switch to different mic (if available)
- [ ] Verify checkmark moves

### 9. Settings Test
- [ ] Right-click tray → Settings
- [ ] Change a setting
- [ ] Click Save
- [ ] Reopen Settings (shouldn't crash)

### 10. Continuous Mode Test
- [ ] Press Ctrl+Alt+D
- [ ] Speak naturally with pauses
- [ ] Text should auto-transcribe after pauses
- [ ] Try a voice command during continuous mode
- [ ] Press Ctrl+Alt+D to stop

## Expected Console Output

```
Dictation app starting...
Mode: hold
Hotkey: [ctrl+shift]
Using model: base
Loading Whisper model...
✓ Loaded 44 voice commands
Model loaded! (cpu)
Ready for dictation.
```

## When Running Commands:

```
✓ Executed: new tab
✓ Pressed: e
✓ Holding: w
✓ Released: w
✓ Mouse double click
✓ Launching: chrome.exe
```

## Troubleshooting

### "Command not recognized"
- Check spelling in commands.json
- Try exact phrase from VOICE_COMMANDS.md
- Check console for "Loaded XX commands"

### "No audio recorded"
- Check microphone selection
- Verify Windows permissions
- Test mic in Windows sound settings

### Commands execute but keys get stuck
- Say "release all"
- Check that you're using key_up for held keys

### Model loading very slow
- Normal on first run (downloads model)
- Subsequent runs should be fast
- Try smaller model ("tiny" or "base")

## All Tests Passed?

You're ready to:
- ✅ Use voice control for daily tasks
- ✅ Game with Arc Raiders hands-free
- ✅ Add custom commands
- ✅ Share your experience

## Found a Bug?

Document:
1. What command you said
2. What you expected
3. What actually happened
4. Console output

Save to: bug_reports.txt
