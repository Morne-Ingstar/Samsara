# Adding Your Own Voice Commands

## Simple Command Examples

### Open Your Favorite Programs

Add to `commands.json`:

```json
"open discord": {
  "type": "launch",
  "target": "C:\\Users\\YourName\\AppData\\Local\\Discord\\Update.exe --processStart Discord.exe",
  "description": "Open Discord"
},

"open spotify": {
  "type": "launch",
  "target": "spotify.exe",
  "description": "Open Spotify"
},

"open visual studio code": {
  "type": "launch",
  "target": "code",
  "description": "Open VS Code"
},

"open arcana": {
  "type": "launch",
  "target": "chrome.exe http://your-arcana-url",
  "description": "Open The Arcana"
}
```

### Custom Hotkeys

```json
"screenshot": {
  "type": "hotkey",
  "keys": ["win", "shift", "s"],
  "description": "Take screenshot"
},

"emoji picker": {
  "type": "hotkey",
  "keys": ["win", "."],
  "description": "Open emoji picker"
},

"lock screen": {
  "type": "hotkey",
  "keys": ["win", "l"],
  "description": "Lock computer"
}
```

### Gaming - More Arc Raiders Commands

```json
"hold run": {
  "type": "key_down",
  "key": "shift",
  "description": "Hold shift to run"
},

"stop running": {
  "type": "key_up",
  "key": "shift",
  "description": "Release shift"
},

"crouch": {
  "type": "key_down",
  "key": "ctrl",
  "description": "Hold crouch"
},

"stand up": {
  "type": "key_up",
  "key": "ctrl",
  "description": "Release crouch"
},

"reload": {
  "type": "press",
  "key": "r",
  "description": "Reload weapon"
},

"inventory": {
  "type": "press",
  "key": "i",
  "description": "Open inventory"
}
```

### Text Shortcuts

```json
"new line": {
  "type": "press",
  "key": "enter",
  "description": "Press Enter"
},

"delete line": {
  "type": "hotkey",
  "keys": ["ctrl", "shift", "k"],
  "description": "Delete current line (VS Code)"
},

"comment out": {
  "type": "hotkey",
  "keys": ["ctrl", "/"],
  "description": "Toggle comment"
}
```

## Advanced: Command Sequences

Want "save and close"? You'll need to add a new command type.

Here's how (advanced users):

### In dictation.py, add to CommandExecutor:

```python
elif cmd_type == 'sequence':
    # Execute multiple commands in order
    steps = cmd.get('steps', [])
    for step in steps:
        step_type = step.get('action')
        if step_type == 'hotkey':
            keys = [self.get_key(k) for k in step['keys']]
            for key in keys[:-1]:
                self.keyboard_controller.press(key)
            self.keyboard_controller.press(keys[-1])
            self.keyboard_controller.release(keys[-1])
            for key in reversed(keys[:-1]):
                self.keyboard_controller.release(key)
        elif step_type == 'wait':
            time.sleep(step.get('ms', 100) / 1000)
    print(f"✓ Executed sequence: {command_name}")
    return True
```

### Then in commands.json:

```json
"save and close": {
  "type": "sequence",
  "steps": [
    {
      "action": "hotkey",
      "keys": ["ctrl", "s"]
    },
    {
      "action": "wait",
      "ms": 100
    },
    {
      "action": "hotkey",
      "keys": ["alt", "f4"]
    }
  ],
  "description": "Save then close window"
}
```

## Tips for Great Commands

### 1. Keep Names Natural
✅ Good: "new tab", "close window", "scroll down"
❌ Bad: "nt", "cw", "sd"

You'll remember natural language better!

### 2. Avoid Similar Sounding Commands
❌ Avoid: "new tab" and "new tape" (sound the same)
✅ Better: "new tab" and "open tab"

### 3. Test Each Command
After adding a command:
1. Save commands.json
2. Restart dictation app
3. Test the new command
4. Check console for errors

### 4. Use Descriptions
Always add good descriptions - helps you remember what each command does!

### 5. Group Similar Commands
Keep gaming commands together, browser commands together, etc.
Makes the JSON easier to navigate.

## Common Keys Reference

### Modifier Keys
- `ctrl` - Control
- `shift` - Shift  
- `alt` - Alt
- `win` - Windows key

### Special Keys
- `enter` - Enter
- `esc` - Escape
- `space` - Spacebar
- `tab` - Tab
- `backspace` - Backspace
- `delete` - Delete

### Navigation
- `up`, `down`, `left`, `right` - Arrow keys
- `home`, `end` - Home/End
- `pageup`, `pagedown` - Page Up/Down

### Function Keys
- `f1` through `f12` - Function keys

### Regular Keys
- Single letters: `a`, `b`, `c`, etc.
- Numbers: `1`, `2`, `3`, etc.

## Reload Commands Without Restart

Currently you need to restart the app to reload commands.json.

**Want hot reload?** (Advanced)

Add this to DictationApp class:
```python
def reload_commands(self):
    """Reload commands without restart"""
    self.command_executor.load_commands()
    print("✓ Commands reloaded")
```

Then add to system tray menu:
```python
pystray.MenuItem("🔄 Reload Commands", lambda: self.reload_commands())
```

## Need Help?

**Common Issues:**

1. **Command not executing**
   - Check spelling in commands.json
   - Verify JSON syntax (use jsonlint.com)
   - Check console for errors

2. **Program not launching**
   - Use full path to .exe
   - Check if program name is correct
   - Try running from command line first

3. **Keys not working**
   - Verify key names (see Common Keys Reference)
   - Test with `press` type first
   - Check if app accepts that hotkey

**Still stuck?** 
- Check BUILD_SUMMARY.md for architecture
- See README.md for more examples
- Test with pre-configured commands first

Happy commanding! 🎤✨
