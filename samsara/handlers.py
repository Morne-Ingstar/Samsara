"""
Command handler protocol and implementations.

Each command type (hotkey, press, launch, text, macro, etc.) has a handler
class with an execute(cmd, ctx) method. Dispatch becomes a single dict
lookup against _HANDLER_REGISTRY instead of an if/elif chain duplicated
across executors. Adding a new command type is now: write one class, add
one entry to the registry.

Usage:
    handler = get_handler(cmd.get('type'))
    if handler is not None:
        handler.execute(cmd, ctx)
"""

import logging
import subprocess
import sys
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CommandContext:
    """Execution context passed to every handler.

    Provides access to shared resources (keyboard/mouse controllers,
    held-key dict, key-name map, app instance) without handlers needing
    to import or construct them. `held_keys` is intentionally a reference
    -- handlers mutate it in place so KeyDownHandler/KeyUpHandler see the
    same dict as the executor.
    """

    def __init__(self, keyboard_controller=None, mouse_controller=None,
                 held_keys=None, key_map=None, app=None):
        self.keyboard = keyboard_controller
        self.mouse = mouse_controller
        self.held_keys = held_keys if held_keys is not None else {}
        self.key_map = key_map or {}
        self.app = app

    def get_key(self, key_str):
        """Convert key string to pynput Key object (or bare char)."""
        key_lower = key_str.lower()
        if key_lower in self.key_map:
            return self.key_map[key_lower]
        return key_str.lower() if len(key_str) == 1 else key_str


class CommandHandler:
    """Base class for command handlers."""

    def execute(self, cmd: Dict[str, Any], ctx: CommandContext) -> bool:
        """Execute a command.

        Args:
            cmd: Command definition dict from commands.json
            ctx: Execution context with controllers and state

        Returns:
            True if command executed successfully
        """
        raise NotImplementedError


class HotkeyHandler(CommandHandler):
    """Execute a key combination (e.g. Ctrl+C, Alt+F4)."""

    def execute(self, cmd, ctx):
        keys = [ctx.get_key(k) for k in cmd['keys']]
        for key in keys[:-1]:
            ctx.keyboard.press(key)
        ctx.keyboard.press(keys[-1])
        ctx.keyboard.release(keys[-1])
        for key in reversed(keys[:-1]):
            ctx.keyboard.release(key)
        return True


class PressHandler(CommandHandler):
    """Press and release a single key."""

    def execute(self, cmd, ctx):
        key = ctx.get_key(cmd['key'])
        ctx.keyboard.press(key)
        ctx.keyboard.release(key)
        return True


class KeyDownHandler(CommandHandler):
    """Hold a key down until released."""

    def execute(self, cmd, ctx):
        key = ctx.get_key(cmd['key'])
        ctx.keyboard.press(key)
        ctx.held_keys[cmd['key']] = key
        return True


class KeyUpHandler(CommandHandler):
    """Release a held key."""

    def execute(self, cmd, ctx):
        key_str = cmd['key']
        if key_str in ctx.held_keys:
            ctx.keyboard.release(ctx.held_keys[key_str])
            del ctx.held_keys[key_str]
        return True


class ReleaseAllHandler(CommandHandler):
    """Release every currently-held key."""

    def execute(self, cmd, ctx):
        for key in ctx.held_keys.values():
            ctx.keyboard.release(key)
        count = len(ctx.held_keys)
        ctx.held_keys.clear()
        logger.info(f"Released {count} held keys")
        return True


class MouseHandler(CommandHandler):
    """Execute mouse clicks."""

    def execute(self, cmd, ctx):
        # Import lazily so test environments without pynput don't fail at
        # module load (the executors already have mock Button/Controller
        # classes for that case; we just need to pick the right Button).
        try:
            from pynput.mouse import Button
        except ImportError:
            class Button:  # local shim, same string API as the mock executor uses
                left = 'left'
                right = 'right'

        action = cmd.get('action')
        if action == 'click':
            button = Button.left if cmd.get('button') == 'left' else Button.right
            ctx.mouse.click(button)
        elif action == 'double_click':
            ctx.mouse.click(Button.left, 2)
        return True


class LaunchHandler(CommandHandler):
    """Launch an application."""

    def execute(self, cmd, ctx):
        target = cmd['target']
        try:
            if sys.platform == 'win32':
                subprocess.Popen(f'start "" "{target}"', shell=True)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', target])
            else:
                subprocess.Popen(['xdg-open', target])
            logger.info(f"Launching: {target}")
            return True
        except Exception as e:
            logger.error(f"Failed to launch {target}: {e}")
            return False


class TextHandler(CommandHandler):
    """Insert text via clipboard (preserving original clipboard)."""

    def execute(self, cmd, ctx):
        try:
            import pyperclip
            import pyautogui
            from samsara.clipboard import (
                clipboard_lock, save_clipboard, restore_clipboard,
            )
        except Exception:
            logger.error("Clipboard/pyautogui not available")
            return False

        text_to_insert = cmd.get('text', '')
        if text_to_insert:
            with clipboard_lock:
                saved = save_clipboard()
                try:
                    pyperclip.copy(text_to_insert)
                    time.sleep(0.02)
                    pyautogui.hotkey('ctrl', 'v')
                    time.sleep(0.4)
                except Exception as e:
                    logger.error(f"Paste failed: {e}")
                finally:
                    restore_clipboard(saved)
        return True


class MacroHandler(CommandHandler):
    """Execute a multi-step macro.

    Steps are a list of dicts, each with an "action" plus action-specific
    keys. Optional "delay_after" (ms) between steps (default 50 for
    reliability -- many apps drop keystrokes that arrive faster).

    Supported actions: hotkey, press, type.
    """

    def execute(self, cmd, ctx):
        steps = cmd.get('steps', [])
        if not steps:
            return False

        for step in steps:
            action = step.get('action')

            if action == 'hotkey':
                keys = [ctx.get_key(k) for k in step['keys']]
                for key in keys[:-1]:
                    ctx.keyboard.press(key)
                ctx.keyboard.press(keys[-1])
                ctx.keyboard.release(keys[-1])
                for key in reversed(keys[:-1]):
                    ctx.keyboard.release(key)

            elif action == 'press':
                key = ctx.get_key(step['key'])
                ctx.keyboard.press(key)
                ctx.keyboard.release(key)

            elif action == 'type':
                try:
                    import pyautogui
                except ImportError:
                    logger.error("pyautogui not available for 'type' macro step")
                    continue
                pyautogui.typewrite(step.get('text', ''), interval=0.02)

            else:
                logger.warning(f"Unknown macro action: {action!r}")
                continue

            delay_ms = step.get('delay_after', 50)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)

        return True


class MethodHandler(CommandHandler):
    """Call a named method on the DictationApp instance.

    Used for commands that need app-level state access (undo, mode toggles)
    rather than raw keystrokes. The method name is in cmd['method']; the app
    is pulled from ctx. Silent no-op with a warning if the app lacks the
    method or isn't attached (keeps tests happy without a real DictationApp).
    """

    def execute(self, cmd, ctx):
        method_name = cmd.get('method')
        if ctx.app and method_name and hasattr(ctx.app, method_name):
            try:
                getattr(ctx.app, method_name)()
                return True
            except Exception as e:
                logger.error(f"Method '{method_name}' failed: {e}")
                return False
        logger.warning(f"Method '{method_name}' not found on app")
        return False


# Registry of all built-in handler types. Extend by adding a class + an entry.
_HANDLER_REGISTRY: Dict[str, CommandHandler] = {
    'hotkey': HotkeyHandler(),
    'press': PressHandler(),
    'key_down': KeyDownHandler(),
    'key_up': KeyUpHandler(),
    'release_all': ReleaseAllHandler(),
    'mouse': MouseHandler(),
    'launch': LaunchHandler(),
    'text': TextHandler(),
    'macro': MacroHandler(),
    'method': MethodHandler(),
}


def get_handler(cmd_type: str) -> Optional[CommandHandler]:
    """Look up a handler by command type string."""
    return _HANDLER_REGISTRY.get(cmd_type)


def build_handler_registry() -> Dict[str, CommandHandler]:
    """Return a shallow copy of the handler registry."""
    return dict(_HANDLER_REGISTRY)
