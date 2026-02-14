# Create Macro Chains

Use this prompt to create complex multi-step command sequences.

---

## Prompt Template

Copy everything below the line and paste it into your AI chat:

---

I need help creating macro chains for Samsara voice commands. A macro chain executes multiple actions in sequence with optional delays.

### Macro Chain Structure

```json
{
  "trigger phrase": {
    "type": "macro",
    "steps": [
      { "action": "hotkey", "keys": ["ctrl", "a"], "delay_after": 100 },
      { "action": "hotkey", "keys": ["ctrl", "c"], "delay_after": 100 },
      { "action": "launch", "target": "notepad.exe", "delay_after": 500 },
      { "action": "hotkey", "keys": ["ctrl", "v"] }
    ],
    "description": "What this macro does"
  }
}
```

### Step Types

| Action | Fields | Example |
|--------|--------|---------|
| `hotkey` | `keys` (array) | `{"action": "hotkey", "keys": ["ctrl", "s"]}` |
| `press` | `key` | `{"action": "press", "key": "enter"}` |
| `text` | `text` | `{"action": "text", "text": "Hello world"}` |
| `launch` | `target` | `{"action": "launch", "target": "notepad.exe"}` |
| `wait` | `ms` | `{"action": "wait", "ms": 1000}` |

### Timing

- `delay_after`: milliseconds to wait after this step (default: 50)
- Use longer delays after launching apps (500-1000ms)
- Use short delays between keystrokes (50-100ms)

### Example Macros

**"Save and close":**
```json
{
  "save and close": {
    "type": "macro",
    "steps": [
      { "action": "hotkey", "keys": ["ctrl", "s"], "delay_after": 200 },
      { "action": "hotkey", "keys": ["alt", "f4"] }
    ],
    "description": "Save current file and close window"
  }
}
```

**"New email":**
```json
{
  "new email": {
    "type": "macro",
    "steps": [
      { "action": "launch", "target": "outlook.exe", "delay_after": 1000 },
      { "action": "hotkey", "keys": ["ctrl", "n"] }
    ],
    "description": "Open Outlook and start new email"
  }
}
```

**"Format code":**
```json
{
  "format code": {
    "type": "macro",
    "steps": [
      { "action": "hotkey", "keys": ["ctrl", "a"], "delay_after": 50 },
      { "action": "hotkey", "keys": ["ctrl", "shift", "f"] }
    ],
    "description": "Select all and format in VS Code"
  }
}
```

**"Copy to notes":**
```json
{
  "copy to notes": {
    "type": "macro",
    "steps": [
      { "action": "hotkey", "keys": ["ctrl", "c"], "delay_after": 100 },
      { "action": "hotkey", "keys": ["alt", "tab"], "delay_after": 200 },
      { "action": "hotkey", "keys": ["ctrl", "end"], "delay_after": 100 },
      { "action": "press", "key": "enter", "delay_after": 50 },
      { "action": "hotkey", "keys": ["ctrl", "v"] }
    ],
    "description": "Copy selection, switch to notes, paste at end"
  }
}
```

### My Request

[DESCRIBE THE WORKFLOW YOU WANT TO AUTOMATE]

Examples:
- "Select all text, copy it, open a new browser tab, and paste into the search bar"
- "Take a screenshot, open Paint, paste, and save"
- "In Premiere Pro: mark in point, move forward 5 seconds, mark out point, cut"

---

## How to Use

1. Copy the JSON macro
2. Add to your `commands.json` file
3. Reload commands in Samsara
4. Say the trigger phrase to execute

## Tips

- Test each step individually first
- Add longer delays for slow applications
- Use `wait` steps for apps that need loading time
- Keep trigger phrases distinct from simple commands
