# Accessibility Tuning

Use this prompt to customize Samsara for specific accessibility needs.

---

## Prompt Template

Copy everything below the line and paste it into your AI chat:

---

I need help customizing Samsara (a voice dictation app) for my accessibility needs. Please suggest config changes and voice command additions.

### Samsara's Accessibility Features

**Timing Controls:**
- `silence_threshold`: How long to wait before ending recording (0.5-5.0 seconds)
- `wake_detection_silence`: Silence before processing wake word (0.5-3.0 seconds)  
- `wake_command_timeout`: How long to listen after wake word (5-60 seconds)

**Input Options:**
- Hotkey hold mode (hold to record, release to transcribe)
- Hotkey toggle mode (press to start, press to stop)
- Continuous mode (always listening, auto-transcribes on pause)
- Wake word mode (say trigger phrase, then command)

**Audio Settings:**
- Microphone selection
- Input sensitivity calibration
- Feedback sounds (can be disabled)

### Config Structure (config.json)

```json
{
  "silence_threshold": 2.0,
  "auto_capitalize": true,
  "format_numbers": true,
  "mode": "combined",
  "wake_word_config": {
    "wake_phrase": "hey samsara",
    "dictate_timeout": 2.0,
    "short_dictate_timeout": 1.0,
    "long_dictate_timeout": 60.0,
    "end_word_enabled": true,
    "end_word": "done",
    "pause_word_enabled": true,
    "pause_word": "hold on"
  }
}
```

### My Accessibility Needs

**Physical:**
[Describe any motor/physical considerations]
- Example: "I have tremor, so holding keys is difficult"
- Example: "Limited hand mobility, need minimal key presses"

**Speech:**
[Describe any speech considerations]
- Example: "I speak slowly with pauses"
- Example: "I have a stutter"
- Example: "Accent that Whisper struggles with"

**Cognitive:**
[Describe any cognitive considerations]
- Example: "I need longer timeouts to formulate thoughts"
- Example: "Simpler command phrases"

**Environment:**
[Describe your setup]
- Example: "Noisy environment with background sounds"
- Example: "I use a specific microphone model"

**Current frustrations:**
[What's not working well?]

---

## Example Customizations

### For Slow Speech / Long Pauses
```json
{
  "silence_threshold": 4.0,
  "wake_word_config": {
    "dictate_timeout": 5.0,
    "pause_word_enabled": true,
    "pause_word": "thinking"
  }
}
```

### For Limited Mobility (Avoid Holding Keys)
```json
{
  "mode": "toggle",
  "dictation_hotkey": ["f9"]
}
```
Plus voice commands for common shortcuts:
```json
{
  "click": { "type": "mouse", "action": "click" },
  "scroll down": { "type": "hotkey", "keys": ["pagedown"] },
  "scroll up": { "type": "hotkey", "keys": ["pageup"] }
}
```

### For Stuttering
```json
{
  "silence_threshold": 3.0,
  "wake_word_config": {
    "short_dictate_timeout": 3.0
  }
}
```
Consider using longer, distinct wake phrases to avoid false triggers.

### For Noisy Environments
- Use a directional or noise-canceling microphone
- Increase model size for better accuracy (small → medium)
- Use push-to-talk (hold mode) instead of wake word

---

## How to Apply Changes

1. Open `config.json` in a text editor
2. Apply recommended changes
3. Save and restart Samsara
4. Test and adjust as needed

## Tips

- Make one change at a time and test
- Use Voice Training → Calibration to optimize for your voice
- Add corrections for words you're consistently misheard on
- The pause word ("hold on", "thinking") resets the silence timer
