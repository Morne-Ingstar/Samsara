"""
Samsara Commands Module

Handles voice command loading, matching, and execution.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import plugin_commands as _plugin_commands
from .command_packs import get_enabled_packs
from .command_registry import CommandMatcher
from .handlers import CommandContext, get_handler
from .phonetic_wash import apply_phonetic_wash

from samsara.log import get_logger

logger = get_logger(__name__)

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


class CommandExecutor:
    """Executes voice commands — hotkeys, launches, key holds, etc.

    This is the single authoritative implementation.  dictation.py imports it;
    tests validate it directly.  The class carries the full production feature
    set: command debounce, reminder parsing, force_commands bypass for wake
    word mode, and Smart Actions routing.

    Args:
        commands_path: Path to commands.json (defaults to repo root).
        app:          DictationApp instance stored as self._app.  Passed to
                      plugin handlers and used for state reads/writes.
        plugins_dir:  Plugin directory to scan (defaults to plugins/commands/).
    """

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
    ) -> None:
        if commands_path is None:
            commands_path = Path(__file__).parent.parent / "commands.json"
        self.commands_path = Path(commands_path)
        self._app = app
        self.commands: Dict[str, Dict[str, Any]] = {}
        self.held_keys: Dict[str, Any] = {}
        self.keyboard_controller = KeyboardController()
        self.mouse_controller = MouseController()
        self.load_commands()

        if plugins_dir is None:
            plugins_dir = Path(__file__).parent.parent / "plugins" / "commands"
        try:
            _plugin_commands.load_plugins(plugins_dir)
        except Exception as e:
            print(f"[PLUGINS] Failed to load plugins: {e}")
        unique = len({id(entry) for entry in _plugin_commands._REGISTRY.values()})
        print(f"[PLUGINS] Loaded {unique} plugin commands")

        self._matcher = CommandMatcher()
        app_config = getattr(app, 'config', {}) if app is not None else {}
        enabled_packs = get_enabled_packs(app_config)
        self._matcher.set_enabled_packs(enabled_packs)
        self._matcher.load_builtins(self.commands)
        self._matcher.load_plugins(_plugin_commands._REGISTRY)
        self._matcher.freeze()
        self._matcher.detect_collisions()
        _plugin_commands.set_shared_matcher(self._matcher)

    # ── Command file I/O ────────────────────────────────────────────────────────

    def load_commands(self) -> None:
        try:
            with open(self.commands_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.commands = data.get('commands', {})
            print(f"[OK] Loaded {len(self.commands)} voice commands")
        except Exception as e:
            print(f"[WARN] Could not load commands: {e}")
            self.commands = {}

    def save_commands(self) -> None:
        # commands.json lives in the app root, which is monitored by the
        # config file watcher.  os.replace requires FILE_SHARE_DELETE on
        # all open handles — Python's open() never sets that flag, so a
        # rename would fail with PermissionError while the watcher runs.
        # Mirror save_config's workaround: serialize to a .tmp, read it
        # back as a string, then overwrite the live file in a single write
        # call (open('w') uses FILE_SHARE_READ|FILE_SHARE_WRITE, which
        # succeeds even while the watcher holds a read handle).
        tmp_path = str(self.commands_path) + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump({'commands': self.commands}, f, indent=2)
            tmp_text = open(tmp_path, 'r', encoding='utf-8').read()
            with open(self.commands_path, 'w', encoding='utf-8') as f:
                f.write(tmp_text)
        except Exception as e:
            print(f"[ERROR] Could not save commands: {e}")
        finally:
            try:
                os.remove(tmp_path)
            except OSError as e:
                logger.debug(f"save_commands: {e}")

    def get_command(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the command dict for *name*, or None if not found."""
        return self.commands.get(name.lower())

    def list_commands(self) -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of the full commands dict."""
        return self.commands.copy()

    # ── Key helpers ─────────────────────────────────────────────────────────────

    def get_key(self, key_str: str) -> Any:
        key_lower = key_str.lower()
        if key_lower in self.KEY_MAP:
            return self.KEY_MAP[key_lower]
        return key_str.lower() if len(key_str) == 1 else key_str

    # ── Dispatch core ───────────────────────────────────────────────────────────

    def _build_context(self, app_instance: Any = None) -> CommandContext:
        """Build a CommandContext for the handler registry.

        Falls back to self._app when app_instance is not given so callers
        that don't have a per-call app reference (e.g. Ava's execute_command
        call) still get a fully-populated context.
        """
        effective_app = app_instance if app_instance is not None else self._app
        return CommandContext(
            keyboard_controller=self.keyboard_controller,
            mouse_controller=self.mouse_controller,
            held_keys=self.held_keys,
            key_map=self.KEY_MAP,
            app=effective_app,
        )

    def execute_command(self, command_name: str, app_instance: Any = None) -> bool:
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
            success = handler.execute(cmd, self._build_context(app_instance))
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
        app_instance: Any = None,
        force_commands: bool = False,
    ) -> Tuple[Optional[str], bool]:
        """Process transcribed text — execute a command or return text for dictation.

        Args:
            text:          Transcribed text.
            app_instance:  DictationApp passed per-call.  When None, self._app
                           is used as a fallback so tests that provide app at
                           construction time work without re-passing it.
            force_commands: Skip the command_matching_enabled gate.  Used by wake
                           word mode where commands always execute.

        Returns:
            (result, was_command) where result is the matched command phrase,
            the processed text, or None on empty input.
        """
        if not text:
            return None, False

        effective_app = app_instance if app_instance is not None else self._app
        text_lower = text.lower().strip()

        # Command mode toggle — always processed, regardless of mode state
        if ("command mode on" in text_lower
                or "command mode enable" in text_lower
                or "enable command mode" in text_lower):
            if effective_app:
                effective_app.command_matching_enabled = True
                with effective_app._config_lock:
                    effective_app.config.setdefault('command_mode', {})['command_matching_enabled'] = True
                    effective_app.save_config()
            print("[OK] Command mode ENABLED")
            return "command_mode_on", True

        if ("command mode off" in text_lower
                or "command mode disable" in text_lower
                or "disable command mode" in text_lower):
            if effective_app:
                effective_app.command_matching_enabled = False
                with effective_app._config_lock:
                    effective_app.config.setdefault('command_mode', {})['command_matching_enabled'] = False
                    effective_app.save_config()
            print("[OFF] Command mode DISABLED")
            return "command_mode_off", True

        # Reminder commands — always work regardless of command mode
        if effective_app and hasattr(effective_app, 'notification_manager'):
            reminder_result = effective_app.notification_manager.parse_remind_command(text)
            if reminder_result:
                minutes, task = reminder_result
                message = task if task else "Time's up!"
                effective_app.notification_manager.add_quick_reminder(minutes, message)
                print(f"[OK] Reminder set for {minutes} minutes: {message}")
                effective_app.play_sound("success")
                return f"reminder_{minutes}min", True

        # Gate on command_matching_enabled — bypassed by wake word mode via force_commands
        if not force_commands:
            if effective_app and not effective_app.command_matching_enabled:
                return text, False

        # Phonetic wash for matching only; original text is returned on fallthrough
        # so free-form dictation output is never silently rewritten.
        match_text = apply_phonetic_wash(text)
        entry, remainder = self._matcher.match(match_text)
        if entry is None:
            # Smart Actions routing-verb fallback (e.g. "ask Spotify for jazz").
            # Routing verbs are not @command entries — they live here so they
            # only trigger when no real command matched.
            if self._app is not None and self._is_routing_verb(text):
                sa_cfg = getattr(self._app, 'config', {}).get('smart_actions', {})
                if sa_cfg.get('enabled', False):
                    if self._try_smart_actions_route(text):
                        return text, True
            return text, False

        # Command mode debounce: suppress rapid re-execution of flagged commands
        in_cmd_mode = getattr(effective_app, 'command_mode_active', False)
        if in_cmd_mode and self._matcher.should_suppress(entry):
            print(f"[CMD] Debounce: '{entry.phrase}' still in cooldown")
            return entry.phrase, False

        if entry.source == 'plugin':
            print(f"[PLUGIN] Executing: {entry.phrase}")
            try:
                success = bool(entry.handler(effective_app, remainder))
            except Exception as e:
                print(f"[ERROR] Plugin '{entry.phrase}' failed: {e}")
                success = False
            if success:
                self._matcher.record_execution(entry)
            return entry.phrase, success

        # Built-in command types route through execute_command -> handler registry
        success = self.execute_command(entry.phrase, app_instance=effective_app)
        if success:
            self._matcher.record_execution(entry)
        return entry.phrase, success

    # ── Smart Actions routing ───────────────────────────────────────────────────

    def _is_routing_verb(self, text: str) -> bool:
        sa_cfg = getattr(self._app, 'config', {}).get('smart_actions', {})
        verbs = set(sa_cfg.get('routing_verbs', ['ask', 'plan', 'summarize']))
        first = text.strip().split()[0].lower() if text.strip() else ''
        return first in verbs

    def _try_smart_actions_route(self, text: str) -> bool:
        verb = text.strip().split()[0].lower() if text.strip() else ''
        try:
            from plugins.commands.smart_actions import _do_agent_route
            _do_agent_route(self._app, text, verb)
            return True
        except Exception as e:
            print(f"[SMART ACTIONS] Routing failed: {e}")
            return False
