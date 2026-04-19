"""
Samsara Commands Module

Handles voice command loading, matching, and execution.
"""

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import platform as plat
from . import plugin_commands as _plugin_commands
from .clipboard import clipboard_lock as _clipboard_lock, save_clipboard as _save_clipboard_win32, restore_clipboard as _restore_clipboard_win32

# Optional dependencies - may not be available in test environments
try:
    from pynput.keyboard import Key, Controller as KeyboardController
    from pynput.mouse import Button, Controller as MouseController
    HAS_PYNPUT = True
except ImportError:
    # Create mock classes for testing
    class Key:
        ctrl = 'ctrl'
        shift = 'shift'
        alt = 'alt'
        cmd = 'cmd'
        enter = 'enter'
        esc = 'esc'
        space = 'space'
        tab = 'tab'
        backspace = 'backspace'
        delete = 'delete'
        home = 'home'
        end = 'end'
        page_up = 'page_up'
        page_down = 'page_down'
        up = 'up'
        down = 'down'
        left = 'left'
        right = 'right'
        f1 = 'f1'
        f2 = 'f2'
        f3 = 'f3'
        f4 = 'f4'
        f5 = 'f5'
        f6 = 'f6'
        f7 = 'f7'
        f8 = 'f8'
        f9 = 'f9'
        f10 = 'f10'
        f11 = 'f11'
        f12 = 'f12'

    class Button:
        left = 'left'
        right = 'right'

    class KeyboardController:
        def press(self, key): pass
        def release(self, key): pass

    class MouseController:
        def click(self, button, count=1): pass

    HAS_PYNPUT = False

try:
    import pyperclip
    import pyautogui
    HAS_CLIPBOARD = True
except ImportError:
    pyperclip = None
    pyautogui = None
    HAS_CLIPBOARD = False


class CommandExecutor:
    """Executes voice commands - hotkeys, launches, key holds, etc."""

    KEY_MAP = {
        'ctrl': Key.ctrl,
        'shift': Key.shift,
        'alt': Key.alt,
        'win': Key.cmd,
        'enter': Key.enter,
        'esc': Key.esc,
        'space': Key.space,
        'tab': Key.tab,
        'backspace': Key.backspace,
        'delete': Key.delete,
        'home': Key.home,
        'end': Key.end,
        'pageup': Key.page_up,
        'pagedown': Key.page_down,
        'up': Key.up,
        'down': Key.down,
        'left': Key.left,
        'right': Key.right,
        'f1': Key.f1, 'f2': Key.f2, 'f3': Key.f3, 'f4': Key.f4,
        'f5': Key.f5, 'f6': Key.f6, 'f7': Key.f7, 'f8': Key.f8,
        'f9': Key.f9, 'f10': Key.f10, 'f11': Key.f11, 'f12': Key.f12,
    }

    def __init__(
        self,
        commands_path: Optional[Path] = None,
        app: Any = None,
        plugins_dir: Optional[Path] = None,
    ):
        """
        Initialize command executor.

        Args:
            commands_path: Path to commands.json file
            app: DictationApp instance passed to plugin handlers
            plugins_dir: Directory to scan for plugin command modules
        """
        if commands_path is None:
            commands_path = Path(__file__).parent.parent / "commands.json"
        self.commands_path = Path(commands_path)

        self.commands: Dict[str, Dict[str, Any]] = {}
        self.held_keys: Dict[str, Any] = {}

        self.keyboard_controller = KeyboardController()
        self.mouse_controller = MouseController()

        self._on_command_mode_change: Optional[Callable[[bool], None]] = None
        self._app = app

        self.load_commands()

        if plugins_dir is None:
            plugins_dir = Path(__file__).parent.parent / "plugins" / "commands"
        try:
            _plugin_commands.load_plugins(plugins_dir)
        except Exception as e:
            print(f"[PLUGINS] Failed to load plugins: {e}")
        unique = len({id(entry) for entry in _plugin_commands._REGISTRY.values()})
        print(f"[PLUGINS] Loaded {unique} plugin commands")

    def set_command_mode_callback(
        self,
        callback: Callable[[bool], None],
    ) -> None:
        """
        Set callback for command mode changes.

        Args:
            callback: Function called with True/False when mode changes
        """
        self._on_command_mode_change = callback

    def load_commands(self) -> None:
        """Load commands from JSON file."""
        try:
            with open(self.commands_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.commands = data.get('commands', {})
            print(f"[OK] Loaded {len(self.commands)} voice commands")
        except Exception as e:
            print(f"[WARN] Could not load commands: {e}")
            self.commands = {}

    def save_commands(self) -> None:
        """Save commands to JSON file."""
        try:
            with open(self.commands_path, 'w', encoding='utf-8') as f:
                json.dump({'commands': self.commands}, f, indent=2)
        except Exception as e:
            print(f"[ERROR] Could not save commands: {e}")

    def get_key(self, key_str: str) -> Any:
        """
        Convert key string to pynput Key object.

        Args:
            key_str: Key name string

        Returns:
            pynput Key object or character
        """
        key_lower = key_str.lower()
        if key_lower in self.KEY_MAP:
            return self.KEY_MAP[key_lower]
        return key_str.lower() if len(key_str) == 1 else key_str

    def execute_command(self, command_name: str) -> bool:
        """
        Execute a voice command by name.

        Args:
            command_name: Name of command to execute

        Returns:
            True if command executed successfully
        """
        if command_name not in self.commands:
            return False

        cmd = self.commands[command_name]
        cmd_type = cmd.get('type')

        try:
            if cmd_type == 'hotkey':
                return self._execute_hotkey(cmd)

            elif cmd_type == 'press':
                return self._execute_press(cmd)

            elif cmd_type == 'key_down':
                return self._execute_key_down(cmd)

            elif cmd_type == 'key_up':
                return self._execute_key_up(cmd)

            elif cmd_type == 'release_all':
                return self._execute_release_all()

            elif cmd_type == 'mouse':
                return self._execute_mouse(cmd)

            elif cmd_type == 'launch':
                return self._execute_launch(cmd)

            elif cmd_type == 'text':
                return self._execute_text(cmd)

            else:
                print(f"[WARN] Unknown command type: {cmd_type}")
                return False

        except Exception as e:
            print(f"[ERROR] Command execution error: {e}")
            return False

    def _execute_hotkey(self, cmd: Dict[str, Any]) -> bool:
        """Execute a hotkey combination."""
        keys = [self.get_key(k) for k in cmd['keys']]

        for key in keys[:-1]:
            self.keyboard_controller.press(key)

        self.keyboard_controller.press(keys[-1])
        self.keyboard_controller.release(keys[-1])

        for key in reversed(keys[:-1]):
            self.keyboard_controller.release(key)

        return True

    def _execute_press(self, cmd: Dict[str, Any]) -> bool:
        """Execute a single key press."""
        key = self.get_key(cmd['key'])
        self.keyboard_controller.press(key)
        self.keyboard_controller.release(key)
        return True

    def _execute_key_down(self, cmd: Dict[str, Any]) -> bool:
        """Hold a key down."""
        key = self.get_key(cmd['key'])
        self.keyboard_controller.press(key)
        self.held_keys[cmd['key']] = key
        return True

    def _execute_key_up(self, cmd: Dict[str, Any]) -> bool:
        """Release a held key."""
        key_str = cmd['key']
        if key_str in self.held_keys:
            self.keyboard_controller.release(self.held_keys[key_str])
            del self.held_keys[key_str]
        return True

    def _execute_release_all(self) -> bool:
        """Release all held keys."""
        for key in self.held_keys.values():
            self.keyboard_controller.release(key)
        count = len(self.held_keys)
        self.held_keys.clear()
        print(f"[OK] Released {count} held keys")
        return True

    def _execute_mouse(self, cmd: Dict[str, Any]) -> bool:
        """Execute a mouse action."""
        action = cmd.get('action')

        if action == 'click':
            button = Button.left if cmd.get('button') == 'left' else Button.right
            self.mouse_controller.click(button)
        elif action == 'double_click':
            self.mouse_controller.click(Button.left, 2)

        return True

    def _execute_launch(self, cmd: Dict[str, Any]) -> bool:
        """Launch an application."""
        target = cmd['target']
        success = plat.launch_application(target)
        if success:
            print(f"[OK] Launching: {target}")
        else:
            print(f"[ERROR] Failed to launch: {target}")
        return success

    def _execute_text(self, cmd: Dict[str, Any]) -> bool:
        """Insert text via clipboard while preserving original clipboard content."""
        if not HAS_CLIPBOARD:
            print("[ERROR] Clipboard not available")
            return False

        text_to_insert = cmd.get('text', '')
        if text_to_insert:
            with _clipboard_lock:
                saved = _save_clipboard_win32()
                try:
                    pyperclip.copy(text_to_insert)
                    time.sleep(0.02)
                    pyautogui.hotkey('ctrl', 'v')
                    time.sleep(0.4)  # Wait for paste to complete
                except Exception as e:
                    print(f"[ERROR] Paste failed: {e}")
                finally:
                    _restore_clipboard_win32(saved)

        return True

    def find_command(self, text: str) -> Optional[str]:
        """
        Check if transcribed text matches a command.

        Args:
            text: Transcribed text

        Returns:
            Command name if found, None otherwise
        """
        text_lower = text.lower().strip()

        # Exact match first
        if text_lower in self.commands:
            return text_lower

        # Check for partial matches using word boundaries.
        # Pad with spaces so boundary checks work at start/end of string.
        padded = f" {text_lower} "
        for cmd_name in self.commands:
            if f" {cmd_name} " in padded:
                return cmd_name

        # Plugin commands — lower priority than built-ins, so only consulted
        # after commands.json has no match.
        plugin_entry, _remainder = _plugin_commands.find_command(text)
        if plugin_entry is not None:
            return plugin_entry['phrase']

        return None

    def process_text(
        self,
        text: str,
        command_mode_enabled: bool = True,
        on_mode_change: Optional[Callable[[bool], None]] = None,
    ) -> Tuple[str, bool]:
        """
        Process transcribed text - check for command or return for dictation.

        Args:
            text: Transcribed text
            command_mode_enabled: Whether command mode is active
            on_mode_change: Callback for command mode toggle

        Returns:
            Tuple of (result_text, was_command)
        """
        if not text:
            return "", False

        text_lower = text.lower().strip()
        callback = on_mode_change or self._on_command_mode_change

        # Check for command mode toggle commands
        if any(phrase in text_lower for phrase in [
            "command mode on", "command mode enable", "enable command mode"
        ]):
            if callback:
                callback(True)
            print("[OK] Command mode ENABLED")
            return "command_mode_on", True

        if any(phrase in text_lower for phrase in [
            "command mode off", "command mode disable", "disable command mode"
        ]):
            if callback:
                callback(False)
            print("[OFF] Command mode DISABLED")
            return "command_mode_off", True

        # If command mode is disabled, return text for dictation
        if not command_mode_enabled:
            return text, False

        # Try to find and execute a command
        command = self.find_command(text)
        if command:
            if command in self.commands:
                success = self.execute_command(command)
            else:
                print(f"[PLUGIN] Executing: {command}")
                _phrase, success = _plugin_commands.execute_command(text, app=self._app)
            return command, success

        # Not a command, return text for dictation
        return text, False

    def add_command(
        self,
        name: str,
        command_type: str,
        **kwargs: Any,
    ) -> None:
        """
        Add a new command.

        Args:
            name: Command name (trigger phrase)
            command_type: Type of command
            **kwargs: Command parameters
        """
        self.commands[name.lower()] = {
            'type': command_type,
            **kwargs,
        }

    def remove_command(self, name: str) -> bool:
        """
        Remove a command.

        Args:
            name: Command name to remove

        Returns:
            True if command was removed
        """
        if name.lower() in self.commands:
            del self.commands[name.lower()]
            return True
        return False

    def get_command(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get a command by name.

        Args:
            name: Command name

        Returns:
            Command dict or None
        """
        return self.commands.get(name.lower())

    def list_commands(self) -> Dict[str, Dict[str, Any]]:
        """Get all commands."""
        return self.commands.copy()
