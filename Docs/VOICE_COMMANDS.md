# Voice Commands Reference Guide

## System Control

| Command | Action | Description |
|---------|--------|-------------|
| "open chrome" | Launch Chrome | Opens Google Chrome browser |
| "open firefox" | Launch Firefox | Opens Firefox browser |
| "open edge" | Launch Edge | Opens Microsoft Edge browser |
| "close window" | Alt+F4 | Closes the active window |
| "minimize" | Win+Down | Minimizes current window |
| "maximize" | Win+Up | Maximizes current window |
| "show desktop" | Win+D | Shows the desktop |
| "task manager" | Ctrl+Shift+Esc | Opens Task Manager |

## Browser Control

| Command | Action | Description |
|---------|--------|-------------|
| "new tab" | Ctrl+T | Opens new browser tab |
| "close tab" | Ctrl+W | Closes current tab |
| "reopen tab" | Ctrl+Shift+T | Reopens last closed tab |
| "next tab" | Ctrl+Tab | Switch to next tab |
| "previous tab" | Ctrl+Shift+Tab | Switch to previous tab |
| "refresh page" | F5 | Refresh current page |
| "scroll down" | Page Down | Scroll down one page |
| "scroll up" | Page Up | Scroll up one page |
| "scroll to top" | Home | Jump to top of page |
| "scroll to bottom" | End | Jump to bottom of page |
| "go back" | Alt+Left | Browser back button |
| "go forward" | Alt+Right | Browser forward button |

## Text Editing

| Command | Action | Description |
|---------|--------|-------------|
| "select all" | Ctrl+A | Select all text |
| "copy" | Ctrl+C | Copy selection |
| "cut" | Ctrl+X | Cut selection |
| "paste" | Ctrl+V | Paste from clipboard |
| "undo" | Ctrl+Z | Undo last action |
| "redo" | Ctrl+Y | Redo last action |
| "save" | Ctrl+S | Save current file |
| "find" | Ctrl+F | Open find dialog |

## Gaming & Special Keys

| Command | Action | Description |
|---------|--------|-------------|
| "hold forward" | Press and hold W | Hold W key for continuous movement |
| "stop forward" | Release W | Release the W key |
| "hold shift" | Press and hold Shift | Hold Shift key |
| "release shift" | Release Shift | Release Shift key |
| "release all" | Release all keys | Release all currently held keys |
| "press e" | Press E | Single E key press |
| "press space" | Press Space | Single spacebar press |
| "submit" | Enter | Press Enter key |
| "escape" | Esc | Press Escape key |

## Mouse Control

| Command | Action | Description |
|---------|--------|-------------|
| "double click" | Double click | Double click mouse |
| "left click" | Single left click | Click left mouse button |
| "right click" | Single right click | Click right mouse button |

## Tips for Using Voice Commands

### Command Mode
- Commands work in **all three modes**: Hold, Toggle, and Continuous
- Voice commands are automatically detected and executed
- If a command is recognized, the text won't be typed/pasted

### Arc Raiders Gaming Tips
1. Say "hold forward" to auto-run
2. Use "release all" to stop all movement quickly
3. Combine with "press e" for quick interactions
4. "double click" for fast weapon switching

### Adding Custom Commands
Edit `commands.json` in the DictationApp folder to add your own commands:

```json
"my custom command": {
  "type": "hotkey",
  "keys": ["ctrl", "shift", "a"],
  "description": "My custom shortcut"
}
```

### Command Types
- **hotkey**: Key combinations (Ctrl+C, Alt+Tab, etc.)
- **press**: Single key press
- **key_down**: Hold a key down
- **key_up**: Release a held key
- **mouse**: Mouse clicks
- **launch**: Open programs

### Troubleshooting
- If a command isn't working, check the console output
- Commands are case-insensitive
- Partial matches work (saying extra words is OK)
- Use "release all" if keys get stuck
