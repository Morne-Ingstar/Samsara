# Wake Word Mode - Hands-Free Operation

## What is Wake Word Mode?

Wake Word Mode allows completely hands-free operation of your dictation app. The app is always listening for your custom wake word (default: "hey claude"), and when it hears it, it processes your command or dictation.

**Perfect for:**
- True hands-free computing
- Gaming while keeping both hands free
- Accessibility - zero keyboard interaction needed
- Multitasking (cooking, cleaning, working)

---

## How It Works

### 1. Activate Wake Word Mode
Press **Ctrl+Alt+W** (or your custom hotkey)

Console shows: `👂 Wake word mode ACTIVE - say 'hey claude' to give commands`

### 2. Say Your Wake Word
Say **"hey claude"** (or your custom wake word)

Console shows: `🎤 Wake word detected: 'hey claude'`

### 3. Give Your Command or Dictation

**Option A: Command in one phrase**
```
"Hey Claude, new tab"           → Opens new tab
"Hey Claude, scroll down"       → Scrolls down
"Hey Claude, hold forward"      → Holds W key
```

**Option B: Wake word, then command**
```
You: "Hey Claude"
App: 👂 Listening for command...
You: "open Chrome"
App: ✓ Launching: chrome.exe
```

### 4. Deactivate When Done
Press **Ctrl+Alt+W** again to stop listening

Console shows: `🔇 Wake word mode STOPPED`

---

## Wake Word vs Other Modes

| Mode | Activation | Best For |
|------|------------|----------|
| **Hold** | Hold hotkey while speaking | Quick bursts, precise control |
| **Toggle** | Press once to start/stop | Longer dictation sessions |
| **Continuous** | Auto-transcribes on pauses | Extended hands-free periods |
| **Wake Word** | Always listening for wake word | True hands-free operation |

---

## Customizing Your Wake Word

### Change the Wake Word
1. Right-click tray icon → Settings
2. Find "Wake Word Mode Settings"
3. Change "Wake word phrase" (e.g., "computer", "assistant", "hey jarvis")
4. Click Save

**Tips for choosing a wake word:**
- 2-3 syllables works best
- Avoid common words you say often
- Make it distinct from normal conversation
- Test that Whisper recognizes it clearly

**Good wake words:**
- "hey claude"
- "hey computer"
- "okay assistant"
- "hey jarvis"
- "activate"

**Avoid:**
- Single words like "hi" or "hey"
- Common phrases like "you know"
- Words that sound like commands

### Change the Hotkey
1. Right-click tray icon → Settings
2. Find "Toggle wake word:" under Keyboard Shortcuts
3. Click "Set"
4. Press your desired key combination
5. Click Save

### Adjust Timeout
Default: 5 seconds

If you say the wake word but no command, the app waits 5 seconds before timing out.

To change:
1. Settings → Wake Word Mode Settings
2. Adjust "Command timeout (seconds)"
3. Range: 3-10 seconds

---

## Examples

### Gaming (Arc Raiders)
```
"Hey Claude, hold forward"      → Auto-run
"Hey Claude, press E"          → Interact
"Hey Claude, double click"     → Quick action
"Hey Claude, release all"      → Stop all movement
```

### Web Browsing
```
"Hey Claude, new tab"
"Hey Claude, scroll down"
"Hey Claude, close tab"
"Hey Claude, refresh page"
```

### Dictation
```
You: "Hey Claude"
App: 👂 Listening for command...
You: "This is a test message"
App: ✅ [pastes text]
```

### Combined Use
```
"Hey Claude, open Chrome"      → Opens Chrome
"Hey Claude, new tab"          → New tab
"Hey Claude"                   → Listening...
"google.com"                   → Types and pastes
```

---

## How to Use With Commands

Wake Word Mode works with ALL 44+ voice commands!

**System Control:**
```
"Hey Claude, minimize"
"Hey Claude, show desktop"
"Hey Claude, task manager"
```

**Text Editing:**
```
"Hey Claude, copy"
"Hey Claude, paste"  
"Hey Claude, save"
```

**Custom Commands:**
Add your own in commands.json and use with wake word!

---

## Pro Tips

### 1. Speak Naturally
The wake word detection is forgiving. You can say:
- "Hey Claude new tab" (all at once)
- "Hey Claude... uh... new tab" (with pauses)
- "Hey Claude, could you new tab" (extra words are filtered)

### 2. Clear Commands
After the wake word, be clear and concise:
- ✅ "Hey Claude, scroll down"
- ❌ "Hey Claude, um, I want to like scroll down or whatever"

### 3. Use Timeout Wisely
- **Short timeout (3s)**: For rapid-fire commands
- **Long timeout (10s)**: For thinking before dictating

### 4. Combine with Other Modes
You can switch between wake word mode and other modes:
- Ctrl+Alt+W → Wake word on/off
- Ctrl+Alt+D → Continuous on/off
- Ctrl+Shift → Hold mode (still works!)

### 5. Background Noise
Wake word mode works best in quiet environments. 
If false triggers occur:
- Choose a more unique wake word
- Speak the wake word more clearly
- Move mic closer to you

---

## Troubleshooting

### Wake Word Not Detected
- **Speak clearly**: Enunciate the wake word
- **Check mic**: Make sure correct mic is selected
- **Test recognition**: Try different wake words
- **Volume**: Speak at normal volume, not whisper

### False Triggers
- **Change wake word**: Use something more unique
- **Check background noise**: TV/music can trigger it
- **Adjust threshold**: (future feature)

### Command After Wake Word Not Working
- **Wait for confirmation**: Look for "👂 Listening for command..."
- **Speak clearly**: After wake word, speak command clearly
- **Check timeout**: May have timed out (5s default)
- **Try combined**: Say wake word + command in one phrase

### High CPU Usage
Wake word mode is always listening, which uses CPU.
- This is normal behavior
- Uses same resources as Continuous mode
- Stop mode when not needed (Ctrl+Alt+W)

---

## Technical Details

### How It Works Internally
1. **Always Listening**: Continuous audio stream at 16kHz
2. **Speech Detection**: RMS energy threshold (like Continuous mode)
3. **Buffer Collection**: Records speech segments
4. **Transcription**: Sends to Whisper when silence detected
5. **Wake Word Check**: Looks for wake word in transcription
6. **Command Execution**: If wake word found, processes rest as command

### Performance
- **CPU**: Similar to Continuous mode
- **Latency**: ~1-2 seconds from wake word to execution
- **Accuracy**: Depends on Whisper model (base recommended)
- **Memory**: Buffers are cleared after each transcription

### Privacy
- All processing is local
- No data sent to cloud
- Audio not saved to disk
- Wake word detection happens offline

---

## Comparison with Commercial Assistants

| Feature | Wake Word Mode | Alexa/Siri/Google |
|---------|---------------|-------------------|
| **Privacy** | 100% local | Cloud-based |
| **Customization** | Fully customizable | Limited |
| **Commands** | All your commands | Pre-defined only |
| **Dictation** | Full text dictation | Limited |
| **Offline** | Works offline | Requires internet |
| **Speed** | 1-2 seconds | 0.5-1 seconds |
| **Accuracy** | Whisper AI (excellent) | Very good |

---

## Future Enhancements (Ideas)

- [ ] Multiple wake words
- [ ] Wake word confidence threshold
- [ ] Visual feedback when listening
- [ ] Wake word + specific command shortcuts
- [ ] Beep/sound when wake word detected
- [ ] Different wake words for different actions

---

## Getting Started

### Quick Test:
1. **Activate**: Press Ctrl+Alt+W
2. **Wait**: See "👂 Wake word mode ACTIVE"
3. **Say**: "Hey Claude, new tab"
4. **Observe**: New browser tab opens!

### For Arc Raiders:
1. **Activate**: Ctrl+Alt+W before starting game
2. **Play**: Keep both hands on mouse
3. **Command**: "Hey Claude, hold forward" to auto-run
4. **Stop**: "Hey Claude, release all" to stop

---

**Wake word mode makes the dictation app truly hands-free!**

Try it out and customize it to your needs. 🎤✨
