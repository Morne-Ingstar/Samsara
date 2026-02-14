# Create Custom Voice Commands

Use this prompt with any AI (Claude, ChatGPT, etc.) to generate custom voice commands for Samsara.

---

## Prompt Template

Copy everything below the line and paste it into your AI chat:

---

I need help creating custom voice commands for Samsara, a voice dictation app. Generate valid JSON entries for `commands.json`.

### Command Types

| Type | Purpose | Required Fields |
|------|---------|-----------------|
| `hotkey` | Keyboard shortcuts | `keys` (array of key names) |
| `launch` | Open applications | `target` (executable name or path) |
| `press` | Single key press | `key` (key name) |
| `text` | Insert text | `text` (string to insert) |
| `key_down` | Hold key down | `key` (key name) |
| `key_up` | Release held key | `key` (key name) |
| `release_all` | Release all keys | (no extra fields) |
| `mouse` | Mouse actions | `action` (click/double_click), optional `button` |

### Valid Key Names
Modifiers: `ctrl`, `alt`, `shift`, `win`
Special: `enter`, `esc`, `tab`, `space`, `backspace`, `delete`
Arrows: `up`, `down`, `left`, `right`
Function: `f1` through `f12`
Letters: `a` through `z`
Numbers: `0` through `9`

### Example Commands

```json
{
  "open spotify": {
    "type": "launch",
    "target": "spotify.exe",
    "description": "Open Spotify"
  },
  "screenshot": {
    "type": "hotkey",
    "keys": ["win", "shift", "s"],
    "description": "Take screenshot with Snipping Tool"
  },
  "my email": {
    "type": "text",
    "text": "myemail@example.com",
    "description": "Insert my email address"
  },
  "hold crouch": {
    "type": "key_down",
    "key": "ctrl",
    "description": "Hold crouch key for gaming"
  }
}
```

### My Request

[DESCRIBE WHAT YOU WANT HERE]

Examples:
- "I want to say 'dev tools' and have it open Chrome DevTools (F12)"
- "Create commands for video editing: play/pause, skip forward 5 sec, skip back 5 sec"
- "I need a command to type my signature block"

---

## How to Use the Output

1. Copy the JSON the AI generates
2. Open Samsara Settings → Commands tab
3. Click "Edit JSON" or manually add entries
4. Save and reload commands

## Tips

- Keep trigger phrases short and distinct
- Avoid phrases that sound like common words
- Test with "Execute" button before saving
- Group related commands (all video controls, all text snippets, etc.)
