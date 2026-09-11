# Wake Word System Enhancement - Implementation Handoff

## Overview

Enhance Samsara's wake word functionality to support multiple dictation modes with configurable end words, cancel words, and pause words. This makes wake word mode more practical for real-world use.

## The Problem

Currently, wake word mode uses silence timeout to determine when speech ends. This is clunky because:
- Users have to wait for timeout even when done speaking
- Fast workflows (filling multiple fields) are slow
- No way to cancel mid-dictation
- One-size-fits-all timeout doesn't work for both quick entries and long dictation

## The Solution

A command protocol structure:
```
[Wake Word] + [Command] + [Content] + [End Word]
"Samsara"  + "dictate" + "hello world" + "over"
```

### Three Dictation Modes

| Mode | Trigger Phrase | Behavior |
|------|----------------|----------|
| **dictate** | "samsara dictate" | Default silence timeout (~2 sec) |
| **short dictate** | "samsara short dictate" | Short timeout (~1 sec) for quick entries |
| **long dictate** | "samsara long dictate" | Waits for end word, 60 sec backup timeout |

### Optional Control Words (all can be disabled)

- **End word** (e.g., "over") - Immediately ends dictation and transcribes
- **Cancel word** (e.g., "cancel") - Aborts without transcribing
- **Pause word** (e.g., "pause") - Resets silence timer (for thinking mid-dictation)

## Config Structure (Already Implemented)

The new nested config structure has been added to `load_config()` in dictation.py:

```json
{
  "wake_word_config": {
    "enabled": true,
    "phrase": "samsara",
    "phrase_options": ["samsara", "hey samsara", "computer", "hey computer", "jarvis", "hey jarvis"],
    
    "end_word": {
      "enabled": true,
      "phrase": "over",
      "phrase_options": ["over", "done", "go", "send", "execute", "that's all", "end dictation"]
    },
    
    "cancel_word": {
      "enabled": false,
      "phrase": "cancel",
      "phrase_options": ["cancel", "abort", "never mind", "scratch that"]
    },
    
    "pause_word": {
      "enabled": false,
      "phrase": "pause",
      "phrase_options": ["pause", "hold on", "wait"]
    },
    
    "modes": {
      "dictate": {
        "silence_timeout": 2.0,
        "require_end_word": false
      },
      "short_dictate": {
        "silence_timeout": 1.0,
        "require_end_word": false
      },
      "long_dictate": {
        "silence_timeout": 60.0,
        "require_end_word": true
      }
    },
    
    "audio": {
      "speech_threshold": 0.01,
      "min_speech_duration": 0.3
    },
    
    "feedback": {
      "play_sound_on_wake": true,
      "play_sound_on_end": true
    }
  }
}
```

Migration function `_migrate_wake_word_config()` handles old flat config format.

## State Variables (Already Added)

In `DictationApp.__init__()`:
```python
self.dictation_mode = None  # None, 'dictate', 'short_dictate', 'long_dictate'
self.dictation_buffer = []  # Audio buffer for dictation content
self.dictation_start_time = None  # When dictation started
```

## Implementation Tasks

### 1. Update `process_wake_word_buffer()` 

Current flow:
1. Transcribe audio
2. Check for wake word
3. If found, extract command after wake word
4. Execute command or dictate

New flow:
1. Transcribe audio
2. If NOT in dictation mode:
   - Check for wake word
   - If found, parse command: "dictate", "short dictate", "long dictate", or other voice command
   - If dictation command, enter dictation mode with appropriate settings
   - If other command, execute it
3. If IN dictation mode:
   - Check for end word (if enabled) - strip it and finish
   - Check for cancel word (if enabled) - abort without output
   - Check for pause word (if enabled) - reset silence timer
   - Otherwise, accumulate to dictation buffer and continue

### 2. Update `wake_word_audio_callback()`

- Use dynamic silence timeout based on current `self.dictation_mode`
- In long_dictate mode with require_end_word=True, only end on end word detection (use backup timeout)

### 3. Add Helper Methods

```python
def _start_wake_dictation(self, mode):
    """Enter dictation mode after wake word + dictate command"""
    self.dictation_mode = mode
    self.dictation_buffer = []
    self.dictation_start_time = time.time()
    
    # Get timeout for this mode
    modes = self.config.get('wake_word_config', {}).get('modes', {})
    mode_config = modes.get(mode, modes.get('dictate', {}))
    self._dictation_silence_timeout = mode_config.get('silence_timeout', 2.0)
    self._dictation_require_end_word = mode_config.get('require_end_word', False)
    
    print(f"[DICTATE] {mode} mode - speak now...")

def _finish_wake_dictation(self, text):
    """Finish dictation and output text"""
    # Strip end word if present
    end_config = self.config.get('wake_word_config', {}).get('end_word', {})
    if end_config.get('enabled', False):
        end_phrase = end_config.get('phrase', 'over').lower()
        if text.lower().rstrip().endswith(end_phrase):
            text = text[:text.lower().rfind(end_phrase)].rstrip()
    
    # Process and output
    text = self.process_transcription(text)
    if self.config['add_trailing_space']:
        text = text + " "
    
    if self.config['auto_paste']:
        pyperclip.copy(text)
        time.sleep(0.05)
        pyautogui.hotkey('ctrl', 'v')
    
    self._reset_wake_dictation()

def _cancel_wake_dictation(self):
    """Cancel dictation without output"""
    print("[CANCEL] Dictation cancelled")
    self.play_sound("error")  # Or a cancel-specific sound
    self._reset_wake_dictation()

def _reset_wake_dictation(self):
    """Reset dictation state"""
    self.dictation_mode = None
    self.dictation_buffer = []
    self.dictation_start_time = None
    self._dictation_silence_timeout = None
    self._dictation_require_end_word = False
```

### 4. Update Settings UI (Advanced Tab)

Replace current simple wake word settings with new comprehensive UI:

```
┌─ Wake Word Settings ─────────────────────────────────────────┐
│                                                              │
│  ☑ Enable wake word                                          │
│                                                              │
│  Wake phrase:    [samsara         ▼]  ☐ Custom: [________]  │
│                                                              │
│  ☑ End phrase:   [over            ▼]  ☐ Custom: [________]  │
│                                                              │
│  ☐ Cancel phrase:[cancel          ▼]  ☐ Custom: [________]  │
│                                                              │
│  ☐ Pause phrase: [pause           ▼]  ☐ Custom: [________]  │
│                                                              │
├─ Dictation Mode Timeouts ────────────────────────────────────┤
│                                                              │
│  "dictate" timeout:        [2.0 ] seconds                   │
│  "short dictate" timeout:  [1.0 ] seconds                   │
│  "long dictate" timeout:   [60.0] seconds (backup)          │
│                                                              │
│  [Test Wake Word...]                                         │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

Location: `SettingsWindow` class in dictation.py, Advanced tab section

### 5. Update Wake Word Debug Window

File: `samsara/ui/wake_word_debug.py`

Add:
- Mode selector dropdown (test each mode: dictate, short dictate, long dictate)
- End word detection indicator
- Show flow: "Wake word → Command recognized → [Recording...] → End word/Timeout → Transcribed"
- Timer showing time until timeout
- Test end word, cancel word, pause word functionality

### 6. Backwards Compatibility

- Old config with flat `wake_word` and `wake_word_timeout` should migrate automatically (migration function exists)
- Old behavior (just say wake word + command) should still work
- "samsara [any voice command]" still executes voice commands from commands.json

## File Locations

- Main app: `C:\Users\Morne\Projects\Samsara-dev\dictation.py`
- Wake word debug UI: `C:\Users\Morne\Projects\Samsara-dev\samsara\ui\wake_word_debug.py`
- Voice commands: `C:\Users\Morne\Projects\Samsara-dev\commands.json`
- Config: `C:\Users\Morne\Projects\Samsara-dev\config.json`

## Testing Checklist

- [ ] "Samsara dictate [text] over" - transcribes text without "over"
- [ ] "Samsara short dictate [text]" - faster timeout
- [ ] "Samsara long dictate [text] over" - waits for end word
- [ ] "Samsara [voice command]" - still executes commands
- [ ] Cancel word stops dictation without output (when enabled)
- [ ] Pause word resets timer (when enabled)
- [ ] Custom phrases work for all word types
- [ ] Settings UI saves/loads all new config options
- [ ] Debug window shows all states and modes
- [ ] Old configs migrate correctly

## Notes

- End word detection happens post-transcription (strip from end of text)
- Future enhancement: Real-time end word detection for more responsiveness (Phase 2)
- All control words are optional - users can disable any they don't want
- Default setup: wake word + end word enabled, cancel/pause disabled
