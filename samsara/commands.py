"""
Samsara Commands Module

Handles voice command loading, matching, and execution.
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import plugin_commands as _plugin_commands
from .command_packs import get_enabled_packs
from .command_registry import (
    CommandMatcher,
    DispatchResult,
    DispatchState,
    adapt_handler_return,
    argument_text,
    view_tokens,
)
from .handlers import CommandContext, get_handler
from . import execution_policy
from .execution_policy import Invocation, Route
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

    This is the single authoritative implementation and the production
    executor: DictationApp constructs it and every voice-command lane
    dispatches through process_text().  The class carries the full production
    feature set: command debounce, reminder parsing, force_commands bypass for
    wake word mode, and Smart Actions routing.

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
        self._matcher_lock = threading.RLock()
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

        self.rebuild_matcher()

        # Plugin background services (e.g. ask_ollama's health monitor) start
        # here, once, for a real app -- never as an import side effect.
        if app is not None:
            _plugin_commands.start_plugin_services(app)

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

    def rebuild_matcher(self) -> CommandMatcher:
        """Rebuild the authoritative builtin+plugin matcher atomically.

        Plugin discovery and commands.json reloads can happen independently.
        Replacing the frozen matcher as one unit prevents a successfully loaded
        plugin from remaining invisible to runtime dispatch.
        """
        with self._matcher_lock:
            matcher = CommandMatcher()
            app_config = getattr(self._app, 'config', {}) if self._app is not None else {}
            matcher.set_enabled_packs(get_enabled_packs(app_config))
            matcher.load_builtins(self.commands)
            matcher.load_plugins(_plugin_commands._REGISTRY)
            matcher.freeze()
            matcher.detect_collisions()
            self._matcher = matcher
            _plugin_commands.set_shared_matcher(matcher)
            return matcher

    def reload_commands(self) -> None:
        """Reload editable commands and rebuild runtime dispatch immediately."""
        self.load_commands()
        self.rebuild_matcher()

    def _repair_plugin_matcher_drift(self, text: str):
        """Rebuild once when a loaded, enabled exact plugin phrase is absent."""
        clean = re.sub(r'[^\w\s]', '', (text or '').lower().strip())
        plugin_entry = _plugin_commands._REGISTRY.get(clean)
        if plugin_entry is None:
            return None, ''
        app_config = getattr(self._app, 'config', {}) if self._app is not None else {}
        if plugin_entry.get('pack', 'core') not in get_enabled_packs(app_config):
            return None, ''

        # Another thread may already have repaired the matcher. Retry before
        # rebuilding, then publish one new immutable matcher under the lock.
        with self._matcher_lock:
            entry, remainder = self._matcher.match(text)
            if entry is not None:
                return entry, remainder
            logger.warning(
                "[REGISTRY] Loaded plugin phrase %r missing from matcher; rebuilding",
                clean,
            )
            self.rebuild_matcher()
            return self._matcher.match(text)

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

    def execute_command(self, command_name: str, app_instance: Any = None, *,
                        route: Route = Route.EXACT, generation: "int | None" = None,
                        prompt: str = "", confirmed: bool = False,
                        args: "dict | None" = None, source_text: str = "") -> bool:
        """Execute a voice command by name via the handler registry.

        THE built-in choke point (samsara.execution_policy): nothing below
        touches the keyboard/mouse/apps until authorize() says Allowed.
        NeedsConfirmation stages the invocation (a later "yes" re-enters
        here with confirmed=True); Denied executes nothing. Returns True
        only when the effect actually ran.
        """
        if command_name not in self.commands:
            return False
        effective_app = app_instance if app_instance is not None else self._app
        inv = Invocation(command_name, dict(args or {}), route, generation, prompt, source_text)
        decision = execution_policy.authorize(inv, app=effective_app, executor=self, confirmed=confirmed)
        if isinstance(decision, execution_policy.Denied):
            return False
        if isinstance(decision, execution_policy.NeedsConfirmation):
            self._stage_confirmation(effective_app, inv, decision)
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

    def _stage_confirmation(self, app, inv: Invocation, decision) -> None:
        """Park a NeedsConfirmation in the shared pending slot and ask. "yes"
        (ask_ollama.handle_ava_confirm) re-enters execute_command with
        confirmed=True; "ava cancel"/stop rejects it."""
        def _approve(op):
            self.execute_command(inv.command_id, app, route=inv.route, generation=inv.generation,
                                 prompt=inv.prompt, confirmed=True, args=inv.args,
                                 source_text=inv.source_text)
        execution_policy.stage_pending(app, inv, decision.prompt, on_approve=_approve,
                                       record_type="action")
        self._speak_confirmation(app, decision.prompt)

    def _speak_confirmation(self, app, prompt: str) -> None:
        text = f"{prompt} -- say yes to confirm, or say ava cancel."
        speak = getattr(app, 'audio_coordinator', None)
        try:
            if speak is not None:
                speak.speak(text, category="confirmation")
            else:
                print(f"[POLICY] {text}")
        except Exception as e:
            print(f"[POLICY] confirmation prompt failed: {e}")

    def find_command(self, text: str) -> Optional[str]:
        """Return the canonical phrase of the best matching command, or None."""
        entry, _remainder = self._matcher.match(text)
        if entry is None:
            entry, _remainder = self._repair_plugin_matcher_drift(text)
        return entry.phrase if entry is not None else None

    def find_exact_command(self, text: str) -> Optional[str]:
        """Return a command only when it consumes the complete utterance."""
        entry, remainder = self._matcher.match(text)
        if entry is None:
            entry, remainder = self._repair_plugin_matcher_drift(text)
        if entry is None or remainder:
            return None
        return entry.phrase

    def process_text(
        self,
        text: str,
        app_instance: Any = None,
        force_commands: bool = False,
        *,
        route: Route = Route.EXACT,
        generation: "int | None" = None,
    ) -> DispatchResult:
        """Process transcribed text — execute a command or return text for dictation.

        Args:
            text:          Transcribed text.
            app_instance:  DictationApp passed per-call.  When None, self._app
                           is used as a fallback so tests that provide app at
                           construction time work without re-passing it.
            force_commands: Skip the command_matching_enabled gate.  Used by wake
                           word mode where commands always execute.

        Returns:
            DispatchResult. Unpacks as (result, was_command): result is the
            matched command phrase, the unprocessed text, or None on empty
            input; was_command is True for EVERY state except MISS. A command
            that was matched but failed, was rejected (debounce, policy) or
            is only queued is still a command -- callers must never re-offer
            it as dictation or as a new model request. Read .state for the
            outcome.
        """
        if not text:
            return DispatchResult.miss(None)

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
            return DispatchResult(DispatchState.COMPLETED, "command_mode_on", "command_mode_on")

        if ("command mode off" in text_lower
                or "command mode disable" in text_lower
                or "disable command mode" in text_lower):
            if effective_app:
                effective_app.command_matching_enabled = False
                with effective_app._config_lock:
                    effective_app.config.setdefault('command_mode', {})['command_matching_enabled'] = False
                    effective_app.save_config()
            print("[OFF] Command mode DISABLED")
            return DispatchResult(DispatchState.COMPLETED, "command_mode_off", "command_mode_off")

        # Reminder commands — always work regardless of command mode
        if effective_app and hasattr(effective_app, 'notification_manager'):
            reminder_result = effective_app.notification_manager.parse_remind_command(text)
            if reminder_result:
                minutes, task = reminder_result
                message = task if task else "Time's up!"
                effective_app.notification_manager.add_quick_reminder(minutes, message)
                print(f"[OK] Reminder set for {minutes} minutes: {message}")
                effective_app.play_sound("success")
                name = f"reminder_{minutes}min"
                return DispatchResult(DispatchState.COMPLETED, name, name)

        # Gate on command_matching_enabled — bypassed by wake word mode via force_commands
        if not force_commands:
            if effective_app and not effective_app.command_matching_enabled:
                return DispatchResult.miss(text)

        # Phonetic wash for matching only; original text is returned on fallthrough
        # so free-form dictation output is never silently rewritten.
        match_text = apply_phonetic_wash(text)
        entry, remainder = self._matcher.match(match_text)
        if entry is None:
            entry, remainder = self._repair_plugin_matcher_drift(match_text)
        if entry is None:
            # Smart Actions routing-verb fallback (e.g. "ask Spotify for jazz").
            # Routing verbs are not @command entries — they live here so they
            # only trigger when no real command matched.
            if self._app is not None and self._is_routing_verb(text):
                sa_cfg = getattr(self._app, 'config', {}).get('smart_actions', {})
                if sa_cfg.get('enabled', False):
                    if self._try_smart_actions_route(text):
                        return DispatchResult(DispatchState.QUEUED, text, None,
                                              {'route': 'smart_actions'})
            return DispatchResult.miss(text)
        # The wash lowercases and scrubs punctuation for matching; the command
        # argument must come from what the user actually said.
        remainder = self._original_remainder(text, match_text, entry, remainder)

        # Command mode debounce: suppress rapid re-execution of flagged commands
        in_cmd_mode = getattr(effective_app, 'command_mode_active', False)
        if in_cmd_mode and self._matcher.should_suppress(entry):
            print(f"[CMD] Debounce: '{entry.phrase}' still in cooldown")
            return DispatchResult(DispatchState.REJECTED, entry.phrase, entry.phrase,
                                  {'reason': 'debounce'})

        if entry.source == 'plugin':
            # Plugin choke point: same policy, same pending slot as built-ins.
            inv = Invocation(entry.phrase, {'remainder': remainder}, route, generation, source_text=text)
            decision = execution_policy.authorize(inv, app=effective_app, executor=self)
            if isinstance(decision, execution_policy.Denied):
                # Matched, but not executed. Still "a command" so no caller
                # re-interprets the utterance as dictation or a model request.
                return DispatchResult(DispatchState.REJECTED, entry.phrase, entry.phrase,
                                      {'reason': 'policy'})
            if isinstance(decision, execution_policy.NeedsConfirmation):
                def _approve(op, _entry=entry, _rem=remainder, _app=effective_app):
                    try:
                        _entry.handler(_app, _rem)
                    except Exception as e:
                        print(f"[ERROR] Plugin '{_entry.phrase}' failed: {e}")
                execution_policy.stage_pending(effective_app, inv, decision.prompt, on_approve=_approve,
                                               record_type="action")
                self._speak_confirmation(effective_app, decision.prompt)
                return DispatchResult(DispatchState.QUEUED, entry.phrase, entry.phrase,
                                      {'awaiting_confirmation': True})
            print(f"[PLUGIN] Executing: {entry.phrase}")
            try:
                state = adapt_handler_return(entry.handler(effective_app, remainder))
            except Exception as e:
                print(f"[ERROR] Plugin '{entry.phrase}' failed: {e}")
                return DispatchResult(DispatchState.FAILED, entry.phrase, entry.phrase,
                                      {'error': str(e)})
            if state is DispatchState.MISS:
                # The handler declined (documented `return False`): not this
                # command after all, so the utterance is the caller's again.
                return DispatchResult.miss(text, {'declined_by': entry.phrase})
            if state in (DispatchState.COMPLETED, DispatchState.QUEUED):
                self._matcher.record_execution(entry)
            return DispatchResult(state, entry.phrase, entry.phrase)

        # Built-in command types route through execute_command -> handler
        # registry (the choke point lives there). Built-ins never decline, so
        # False is a failed (or policy-held) command, not dictation.
        success = self.execute_command(entry.phrase, app_instance=effective_app,
                                       route=route, generation=generation, source_text=text)
        if success:
            self._matcher.record_execution(entry)
            return DispatchResult(DispatchState.COMPLETED, entry.phrase, entry.phrase)
        return DispatchResult(DispatchState.FAILED, entry.phrase, entry.phrase)

    def _original_remainder(self, text: str, match_text: str, entry, washed_remainder: str) -> str:
        """Map a match made on the phonetically washed text back to the
        argument span of the ORIGINAL utterance.

        The wash may rewrite the command phrase itself ("fine tab" -> "find
        tab"), so the original is re-matched first; failing that, the shortest
        original prefix whose wash reproduces exactly the matched phrase
        tokens marks where the argument begins. When neither maps cleanly the
        washed remainder is kept (the pre-contract behaviour) rather than
        guessing at a split.
        """
        if not washed_remainder or match_text == text:
            return washed_remainder
        detail = self._matcher.match_detail(text)
        if detail is not None and detail.entry is entry:
            return detail.remainder

        washed = [tok for tok, _s, _e in view_tokens(match_text)]
        consumed = len(washed) - len(view_tokens(washed_remainder))
        if consumed <= 0:
            return washed_remainder
        phrase = washed[:consumed]
        for _norm, _start, end in view_tokens(text)[:consumed + 3]:
            prefix = [tok for tok, _s, _e in view_tokens(apply_phonetic_wash(text[:end]) or '')]
            if prefix == phrase:
                return argument_text(text, end)
            if len(prefix) > consumed:
                break
        return washed_remainder

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
