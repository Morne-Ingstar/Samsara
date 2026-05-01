"""
Samsara Commands Module

Handles voice command loading, matching, and execution.
"""

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import plugin_commands as _plugin_commands
from .command_registry import CommandMatcher
from .handlers import CommandContext, get_handler
from .phonetic_wash import apply_phonetic_wash

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
except Exception:
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

        # Build the unified matcher. Both executors (this one and the one in
        # dictation.py) use this class so matching semantics stay consistent
        # between tests and runtime.
        self._matcher = CommandMatcher()
        self._matcher.load_builtins(self.commands)
        self._matcher.load_plugins(_plugin_commands._REGISTRY)
        self._matcher.freeze()
        self._matcher.detect_collisions()
        _plugin_commands.set_shared_matcher(self._matcher)

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

    def _build_context(self) -> CommandContext:
        """Build a CommandContext pointing at this executor's live state."""
        return CommandContext(
            keyboard_controller=self.keyboard_controller,
            mouse_controller=self.mouse_controller,
            held_keys=self.held_keys,
            key_map=self.KEY_MAP,
            app=self._app,
        )

    def execute_command(self, command_name: str) -> bool:
        """Execute a voice command by name via the handler registry."""
        if command_name not in self.commands:
            return False

        cmd = self.commands[command_name]
        cmd_type = cmd.get('type')
        handler = get_handler(cmd_type)
        if handler is None:
            print(f"[WARN] Unknown command type: {cmd_type}")
            return False

        try:
            success = handler.execute(cmd, self._build_context())
            if success:
                print(f"[OK] Executed: {command_name}")
            return success
        except Exception as e:
            print(f"[ERROR] Command execution error: {e}")
            return False

    def find_command(self, text: str) -> Optional[str]:
        """Return the canonical phrase of the best matching command, or None."""
        entry, _remainder = self._matcher.match(text)
        return entry.phrase if entry is not None else None

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

        # Wash known mis-transcriptions for matching only. When nothing matches
        # the ORIGINAL text is returned so free-form dictation isn't rewritten.
        match_text = apply_phonetic_wash(text)
        entry, remainder = self._matcher.match(match_text)
        if entry is None:
            return text, False

        if entry.source == 'plugin':
            print(f"[PLUGIN] Executing: {entry.phrase}")
            try:
                success = bool(entry.handler(self._app, remainder))
            except Exception as e:
                print(f"[ERROR] Plugin '{entry.phrase}' failed: {e}")
                success = False
            return entry.phrase, success

        # All built-in types (hotkey/press/.../text/macro/method) now route
        # through execute_command -> handler registry.
        success = self.execute_command(entry.phrase)
        return entry.phrase, success

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
