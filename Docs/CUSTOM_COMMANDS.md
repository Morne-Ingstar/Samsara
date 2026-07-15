# Adding Custom Voice Commands

## Use the Commands Page

For ordinary shortcuts, use **Settings → Commands → Add Command**. This avoids
JSON syntax mistakes and makes the change available immediately.

The editor supports these common command types:

- **Hotkey** — send a combination such as `Ctrl+Shift+S`.
- **Launch** — open an application or file.
- **Press / Hold / Release** — operate one key.
- **Text** — insert fixed text.
- **Macro** — run several supported keyboard or typing steps in order.

Use the **Test** button on the selected row. **Reload** rereads externally
edited built-in commands and rebuilds the command matcher without restarting
Samsara. Command-pack changes still require a restart.

## JSON Examples

Advanced users can edit the `commands.json` shipped beside the application
code. Back it up first; application upgrades can replace this file. Close
Samsara while editing it, or use **Reload** on the Commands page afterward.

The file has one top-level `commands` object:

```json
{
  "commands": {
    "open discord": {
      "type": "launch",
      "target": "C:\\Users\\YourName\\AppData\\Local\\Discord\\Update.exe",
      "description": "Open Discord",
      "pack": "core"
    },
    "emoji picker": {
      "type": "hotkey",
      "keys": ["win", "."],
      "description": "Open the Windows emoji picker",
      "pack": "core"
    },
    "press escape": {
      "type": "press",
      "key": "esc",
      "description": "Press Escape",
      "pack": "core"
    }
  }
}
```

Supported JSON command types are `hotkey`, `launch`, `press`, `key_down`,
`key_up`, `release_all`, `mouse`, `text`, `macro`, and trusted app `method`
commands. Prefer the UI or a plugin over adding a `method` command; it calls an
existing `DictationApp` method and is not a general scripting interface.

### Macro Example

Macros use `type: "macro"`. Their supported step actions are `hotkey`, `press`,
and `type`, with an optional `delay_after` in milliseconds:

```json
"save and close": {
  "type": "macro",
  "steps": [
    {"action": "hotkey", "keys": ["ctrl", "s"], "delay_after": 150},
    {"action": "hotkey", "keys": ["alt", "f4"]}
  ],
  "description": "Save, then close the active window",
  "pack": "core"
}
```

There is no `sequence` command type. Do not modify `dictation.py` to add one;
the command dispatcher lives in `samsara/commands.py` and
`samsara/handlers.py`, and Python extensions belong in the plugin system.

## Python Command Plugins

For dynamic behavior, create a focused plugin under `plugins/commands/`:

```python
from samsara.plugin_commands import command


@command(
    "my command",
    aliases=["do the thing"],
    description="Describe the effect",
    pack="core",
)
def my_command(app, remainder="", **kwargs):
    # Use remainder for words spoken after the registered phrase.
    return True
```

Plugins are Python code with the same permissions as Samsara. Only install or
write plugins you trust. Restart Samsara after adding or changing a plugin so
plugin discovery runs cleanly.

## Command Design Tips

- Use natural, distinct phrases that Whisper recognizes reliably.
- Avoid phrases that overlap common dictation. In HANDS FREE, an enabled exact
  command utterance runs as a command; say `literal <phrase>` to dictate it.
- Put optional commands in an appropriate pack and enable only the packs you
  use.
- Use full executable paths for Launch commands when Windows cannot resolve a
  program name.
- Test destructive shortcuts in a disposable window first.

Common key names include `ctrl`, `shift`, `alt`, `win`, `enter`, `esc`,
`space`, `tab`, `backspace`, `delete`, arrow keys, `home`, `end`, `pageup`,
`pagedown`, and `f1` through `f12`.
