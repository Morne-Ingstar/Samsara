# Voice Commands Reference

Samsara has 100+ built-in commands and a plugin system for custom commands.
Say your wake word (default: "Jarvis") followed by any command below.

## Plugins (Dynamic Commands)

These commands accept variable input after the trigger phrase.

| Plugin | Trigger | Example | What it does |
|--------|---------|---------|-------------|
| **Tab Finder** | "where is [X]" | "where is GitHub" | Focuses browser, searches tabs, switches to match |
| **Web Shortcuts** | "go to [X]" | "go to YouTube" | Opens configured URL bookmark |
| **Web Search** | "search for [X]" | "search for cat toys" | Google search |
| **Audio Switch** | "switch to [X]" | "switch to speakers" | Changes default audio output device |
| **Quick Ask** | "ask [model] [X]" | "ask Claude what is a decorator" | Sends question to AI via ARC |
| **Macros** | see below | "going dark" | Multi-step workflows |

### Macros

| Command | Aliases | Actions |
|---------|---------|---------|
| "going dark" | "goodnight", "shut it down", "end of day" | Mute → minimize all → lock screen |
| "focus mode" | "time to work", "let's work" | 20% volume → clear desktop → open VS Code |
| "morning routine" | "good morning", "start my day" | 50% volume → open mail/GitHub/daily sites |
| "break time" | "take a break", "stretch break" | Pause media → lock screen |
| "presentation mode" | "demo mode" | 80% volume → maximize window |
| "clear my desk" | "hide everything", "clean desktop" | Minimize all windows |

### Plugin Aliases

| Phrase | Also works as |
|--------|--------------|
| "go to" | "browse to", "pull up", "show me" |
| "search for" | "look up", "google" |
| "find tab" | "find the tab", "switch to tab", "where is", "find my" |
| "switch to" | "use", "switch audio to" |
| "switch mic to" | "switch microphone to", "use mic", "use microphone" |

## Built-in Commands

### System Control

| Command | Action |
|---------|--------|
| "take a screenshot" / "screenshot" | Win+Shift+S (snip tool) |
| "lock screen" / "lock computer" | Win+L |
| "show desktop" | Win+D |
| "task manager" | Ctrl+Shift+Esc |
| "open settings" | Win+I |
| "open terminal" | Win+X (power user menu) |
| "notifications" | Win+N |
| "clipboard history" | Win+V |
| "emoji" | Win+. (emoji picker) |

### Window Management

| Command | Action |
|---------|--------|
| "snap left" | Win+Left |
| "snap right" | Win+Right |
| "maximize" | Win+Up |
| "minimize" | Win+Down |
| "close window" | Alt+F4 |
| "switch window" / "switch app" | Alt+Tab |
| "full screen" | F11 |

### Apps

| Command | Action |
|---------|--------|
| "open chrome" | Launch Chrome |
| "open firefox" | Launch Firefox |
| "open edge" | Launch Edge |
| "open notepad" | Launch Notepad |
| "open calculator" | Launch Calculator |
| "open file explorer" / "open files" | Win+E |

### Browser

| Command | Action |
|---------|--------|
| "new tab" | Ctrl+T |
| "close tab" | Ctrl+W |
| "reopen tab" | Ctrl+Shift+T |
| "next tab" | Ctrl+Tab |
| "previous tab" | Ctrl+Shift+Tab |
| "refresh page" | F5 |
| "go back" | Alt+Left |
| "go forward" | Alt+Right |
| "scroll down" / "scroll up" | Page Down / Page Up |
| "scroll to top" / "scroll to bottom" | Home / End |
| "zoom in" / "zoom out" / "reset zoom" | Ctrl+/- / Ctrl+0 |

### Media

| Command | Action |
|---------|--------|
| "volume up" / "volume down" | System volume |
| "mute" | Toggle mute |
| "play pause" / "pause music" | Media play/pause |
| "next song" / "next track" | Next track |
| "previous track" | Previous track |

### Text Editing

| Command | Action |
|---------|--------|
| "select all" | Ctrl+A |
| "copy" / "cut" / "paste" | Ctrl+C / X / V |
| "undo" / "redo" | Ctrl+Z / Y |
| "save" | Ctrl+S |
| "find" | Ctrl+F |
| "print" | Ctrl+P |
| "bold" / "italic" / "underline" | Ctrl+B / I / U |
| "select word left" / "select word right" | Ctrl+Shift+Left/Right |
| "delete word" | Ctrl+Backspace |
| "delete line" | Home → Shift+End → Delete |
| "backspace" | Backspace |
| "new line" / "submit" | Enter |
| "new paragraph" | Double Enter |
| "scratch that" / "undo that" | Undo last dictation |

### Punctuation

| Command | Output |
|---------|--------|
| "period" / "full stop" | . |
| "comma" | , |
| "question mark" | ? |
| "exclamation mark" | ! |
| "colon" / "semicolon" | : / ; |
| "apostrophe" / "quote" | ' / " |
| "open/close parenthesis" | ( / ) |
| "open/close bracket" | [ / ] |
| "hyphen" / "dash" / "ellipsis" | - / — / ... |

### Mouse & Keys

| Command | Action |
|---------|--------|
| "left click" / "right click" / "double click" | Mouse clicks |
| "press e" / "press space" | Single key press |
| "hold forward" / "stop forward" | Hold/release W key |
| "hold shift" / "release shift" | Hold/release Shift |
| "release all" | Release all held keys |
| "escape" | Esc key |

## Adding Custom Commands

### Via commands.json
```json
"my command": {
  "type": "hotkey",
  "keys": ["ctrl", "shift", "a"],
  "description": "My custom shortcut"
}
```

### Via Plugin (recommended)
Create `plugins/commands/my_plugin.py`:
```python
from samsara.plugin_commands import command

@command("my command", aliases=["do the thing"])
def handler(app, remainder):
    # remainder = text after the trigger phrase
    return True
```

Command types: `hotkey`, `launch`, `press`, `text`, `macro`, `method`, `plugin`.
