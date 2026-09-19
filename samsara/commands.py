"""
Samsara Commands Module

Handles voice command loading, matching, and execution.
"""

import difflib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import command_scope as _command_scope
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


# ---------------------------------------------------------------------------
# The menu Ava is offered (queue 107 / Astra F1)
#
# One executable set, one menu source: ava_menu() below returns canonical ids
# that execute_canonical() will accept right now -- builtin AND plugin, in an
# enabled pack, live in the current scope, and callable on the route asking.
# It is bounded by RELEVANCE to the utterance and a character budget, never by
# an alphabetical slice: the old `sorted(names)[:100]` cut the menu at "mute
# tab", so every command from "n" onward -- "volume up", "submit" -- was
# invisible to Ava while still being a real command the user could speak.
# ---------------------------------------------------------------------------

#: Budget for the {COMMAND_LIST} substitution. The default enabled packs
#: produce 271 AI-visible commands / ~3.8k characters, so an ordinary install
#: is offered ALL of them; only a user who enables every pack loses the least
#: relevant tail (and never a whole letter range).
AVA_MENU_MAX_CHARS = 4500
AVA_MENU_MAX_COMMANDS = 400


def menu_score(query: str, candidate: str) -> float:
    """Dependency-free relevance of one phrase to the utterance (rapidfuzz is
    not installed here). Moved from ava_command_session._fuzzy_score so the
    conversation menu and the command-session shortlist rank identically."""
    q = (query or "").lower().strip()
    c = (candidate or "").lower().strip()
    if not q or not c:
        return 0.0
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    overlap = len(q_tokens & c_tokens) / max(len(q_tokens), len(c_tokens), 1)
    seq_ratio = difflib.SequenceMatcher(None, q, c).ratio()
    return 0.5 * overlap + 0.5 * seq_ratio


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
            # Queue 68: scoped commands are evaluated against the foreground
            # window and active tags at match time (samsara.command_scope).
            matcher.set_context_provider(_command_scope.capture_context)
            self._matcher = matcher
            _plugin_commands.set_shared_matcher(matcher)
            return matcher

    def reload_commands(self) -> None:
        """Reload editable commands and rebuild runtime dispatch immediately."""
        self.load_commands()
        self.rebuild_matcher()

    def _repair_plugin_matcher_drift(self, text: str, context=None):
        """Rebuild once when a loaded, enabled exact plugin phrase is absent."""
        clean = re.sub(r'[^\w\s]', '', (text or '').lower().strip())
        plugin_entry = _plugin_commands._REGISTRY.get(clean)
        if plugin_entry is None:
            return None, ''
        app_config = getattr(self._app, 'config', {}) if self._app is not None else {}
        if plugin_entry.get('pack', 'core') not in get_enabled_packs(app_config):
            return None, ''
        if self._matcher.out_of_scope_for(text, context) is not None:
            # Registered and enabled, just not live here (queue 68): not drift.
            # Without this, every out-of-scope phrase would rebuild the matcher.
            return None, ''

        # Another thread may already have repaired the matcher. Retry before
        # rebuilding, then publish one new immutable matcher under the lock.
        with self._matcher_lock:
            entry, remainder = self._matcher.match(text, context)
            if entry is not None:
                return entry, remainder
            logger.warning(
                "[REGISTRY] Loaded plugin phrase %r missing from matcher; rebuilding",
                clean,
            )
            self.rebuild_matcher()
            return self._matcher.match(text, context)

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

    # ── The executable set (queue 107) ──────────────────────────────────────

    def _registry_entry(self, command_id: str):
        """The CommandEntry for a canonical phrase OR an alias, whatever its
        pack or scope says (that is a separate question -- see
        command_availability). None for ids with no registry row at all
        ("key:...", "action2:...", a fabricated name)."""
        cid = (command_id or "").strip().lower()
        matcher = getattr(self, '_matcher', None)
        if not cid or matcher is None:
            return None
        # command_registry.py belongs to another queue; _entries (canonical
        # phrases AND aliases -> entry) is read, never mutated, from here.
        return getattr(matcher, '_entries', {}).get(cid)

    def _ai_visible(self, entry) -> bool:
        """Whether a registry row may appear in Ava's menu. Built-ins keep
        their commands.json flag: CommandMatcher.load_builtins does not thread
        ai_visible through, so entry.ai_visible is always True for them."""
        if entry.source == 'builtin':
            return bool((self.commands.get(entry.phrase) or {}).get('ai_visible', True))
        return bool(entry.ai_visible)

    #: Queue 126/A. Whole utterance only -- normalised, then compared for
    #: EQUALITY. A sentence that merely contains one of these is prose.
    _COMMAND_MODE_ON = ("command mode on", "command mode enable", "enable command mode")
    _COMMAND_MODE_OFF = ("command mode off", "command mode disable", "disable command mode")

    #: A reminder utterance BEGINS with its trigger. "Tell Sarah to remind me
    #: in 5 minutes to check the build" is a sentence about a reminder.
    _REMINDER_OPENERS = ("remind me ", "set a reminder", "set reminder")
    _REMINDER_NUMERIC = re.compile(r"^\d+ minute reminder\b")

    @staticmethod
    def _whole_utterance(text_lower: str) -> str:
        """The utterance with trailing sentence punctuation dropped, so
        "Command mode off." still matches and "...command mode off and..."
        still does not."""
        return text_lower.strip().rstrip(".!?,;: ").strip()

    def _is_whole_reminder(self, whole: str) -> bool:
        if any(whole.startswith(opener) for opener in self._REMINDER_OPENERS):
            return True
        return bool(self._REMINDER_NUMERIC.match(whole))

    def _authorize_session_control(self, cid, args, text, app):
        return execution_policy.authorize(
            execution_policy.Invocation(
                cid, args, Route.EXACT,
                execution_policy.current_generation(app), "", text),
            app=app, executor=self)

    @staticmethod
    def _session_control_refusal(decision, name, text):
        """A refused session control is REJECTED, never a miss: the utterance
        was claimed, so it must not also be delivered as dictation."""
        reason = getattr(decision, "reason", None) or "refused"
        logger.info("[CMD] session control %r refused: %s", name, reason)
        return DispatchResult(DispatchState.REJECTED, name, name,
                              {'reason': reason, 'text': text})

    def _session_control_effect(self, text: str, text_lower: str, app):
        """The three ex-preprocessing effects, each now an authorized
        invocation. Returns a DispatchResult when one claimed the utterance,
        else None.

        Authorization STRICTLY precedes the effect: nothing below mutates
        anything until authorize() has returned Allowed."""
        whole = self._whole_utterance(text_lower)

        toggles = ((self._COMMAND_MODE_ON, True, "session:command_mode_on", "command_mode_on"),
                   (self._COMMAND_MODE_OFF, False, "session:command_mode_off", "command_mode_off"))
        for phrases, enable, cid, name in toggles:
            if whole not in phrases:
                continue
            decision = self._authorize_session_control(cid, {}, text, app)
            if not isinstance(decision, execution_policy.Allowed):
                return self._session_control_refusal(decision, name, text)
            if app:
                app.command_matching_enabled = enable
                with app._config_lock:
                    app.config.setdefault('command_mode', {})['command_matching_enabled'] = enable
                    app.save_config()
            print("[OK] Command mode ENABLED" if enable else "[OFF] Command mode DISABLED")
            return DispatchResult(DispatchState.COMPLETED, name, name)

        # Reminders. parse_remind_command is an re.search, so it matches
        # inside prose too; the whole-utterance rule is applied here rather
        # than in notifications.py, which is not this prompt's to change.
        if app and hasattr(app, 'notification_manager'):
            parsed = app.notification_manager.parse_remind_command(whole)
            if parsed and self._is_whole_reminder(whole):
                minutes, task = parsed
                message = task if task else "Time's up!"
                args = {"minutes": int(minutes), "message": message}
                decision = self._authorize_session_control("session:reminder", args, text, app)
                if not isinstance(decision, execution_policy.Allowed):
                    return self._session_control_refusal(decision, "reminder", text)
                app.notification_manager.add_quick_reminder(minutes, message)
                print(f"[OK] Reminder set for {minutes} minutes: {message}")
                app.play_sound("success")
                name = f"reminder_{minutes}min"
                return DispatchResult(DispatchState.COMPLETED, name, name)
        return None

    def command_availability(self, command_id: str, context=None):
        """None when `command_id` may run HERE, else (reason, detail).

        Pack membership and scope are matcher filters, which only the SPOKEN
        path passes through (Astra F2): a model proposal, a scheduled repeat
        and a confirmed callback reach the effect without ever being matched.
        execution_policy.authorize() calls this so every route meets the same
        restriction, and the refusal is emitted like any other Denied.
        """
        entry = self._registry_entry(command_id)
        if entry is None:
            return None
        matcher = self._matcher
        if not matcher._pack_enabled(entry.pack):
            return ("pack_disabled", entry.pack)
        if entry.scope is None:
            return None
        if context is None:
            context = matcher.current_context()
        live, why = _command_scope.scope_live(entry.scope, context)
        if live:
            return None
        return ("out_of_scope", why or entry.scope.describe())

    def executable_entries(self, context=None) -> list:
        """Every registry entry that could run right now: enabled pack, live
        scope. The set execute_canonical() accepts, deduplicated by command."""
        matcher = getattr(self, '_matcher', None)
        if matcher is None:
            return []
        if context is None:
            context = matcher.current_context()
        out, seen = [], set()
        for entry in getattr(matcher, '_sorted', []):
            if id(entry) in seen:
                continue
            seen.add(id(entry))
            if not matcher._pack_enabled(entry.pack):
                continue
            if entry.scope is not None and not matcher.is_live(entry, context):
                continue
            out.append(entry)
        return out

    def ava_menu(self, utterance: str = "", *, limit: "int | None" = None,
                 max_chars: int = AVA_MENU_MAX_CHARS, app: Any = None,
                 route: Route = Route.MODEL, context=None) -> list:
        """THE menu source for both Ava paths (queue 107).

        Every name returned is one execute_canonical() will accept on `route`
        -- decided by asking execution_policy.authorize(quiet=True), the same
        function the effect boundary asks, so the menu cannot drift from what
        Ava may actually run. Registered, AI-visible, in an enabled pack, live
        in this scope, and not refused by the policy.

        Ordered by RELEVANCE to `utterance` (menu_score, canonical phrase or
        best alias), alphabetical within equal scores, then bounded by
        `limit` and by `max_chars` of the comma-joined list. Never an
        alphabetical slice.
        """
        effective_app = app if app is not None else self._app
        generation = execution_policy.current_generation(effective_app)
        scored = []
        for entry in self.executable_entries(context):
            if not self._ai_visible(entry):
                continue
            decision = execution_policy.authorize(
                Invocation(entry.phrase, {}, route, generation, "", ""),
                app=effective_app, executor=self, quiet=True)
            if isinstance(decision, execution_policy.Denied):
                continue
            best = menu_score(utterance, entry.phrase)
            for alias in entry.aliases:
                best = max(best, menu_score(utterance, alias))
            scored.append((-best, entry.phrase))
        scored.sort()
        names, used = [], 0
        cap = AVA_MENU_MAX_COMMANDS if limit is None else max(0, int(limit))
        for _neg, phrase in scored:
            if len(names) >= cap:
                break
            cost = len(phrase) + (2 if names else 0)
            if max_chars and used + cost > max_chars:
                if limit is None:
                    break
                continue
            names.append(phrase)
            used += cost
        return names

    # ── The one execution API (queue 107) ───────────────────────────────────

    def execute_canonical(self, command_id: str, app_instance: Any = None, *,
                          route: Route = Route.EXACT, generation: "int | None" = None,
                          args: "dict | None" = None, source_text: str = "",
                          confirmed: bool = False) -> DispatchResult:
        """Execute ONE canonical command id (builtin or plugin) and report what
        really happened.

        The single entry every route uses -- spoken match, model ACTION,
        scheduled repeat, confirmed callback -- so no caller can report a hit
        the executor refused (Astra F1). An alias resolves to its canonical
        phrase. Returns a DispatchResult:

            COMPLETED  the effect ran
            QUEUED     accepted: staged for "yes", or an async handler
            REJECTED   refused before running -- detail['reason'] says why
                       (unknown_command, pack_disabled, out_of_scope, stale,
                       not_allowed_for_model, unvalidated, ...)
            FAILED     attempted and failed or raised
            MISS       a plugin handler declined this utterance (only the
                       spoken path may then treat the words as dictation)
        """
        effective_app = app_instance if app_instance is not None else self._app
        cid = (command_id or "").strip().lower()
        entry = self._registry_entry(cid)
        canonical = entry.phrase if entry is not None else cid
        builtin = self.commands.get(canonical)
        if generation is None and route is Route.EXACT and not confirmed:
            # A direct exact invocation (cheat sheet, settings Test, a spoken
            # phrase) is created by this call: capture its generation now.
            # Every other route must carry the one captured when its request
            # was made -- None there is Denied(stale) at the choke point.
            generation = execution_policy.capture_generation(effective_app)

        # THE choke point. Nothing below touches the keyboard/mouse/apps until
        # authorize() says Allowed; it also checks pack and scope through
        # command_availability(), on every route.
        inv = Invocation(canonical, dict(args or {}), route, generation, "", source_text)
        decision = execution_policy.authorize(inv, app=effective_app, executor=self,
                                              confirmed=confirmed)
        if isinstance(decision, execution_policy.Denied):
            logger.info("[EXEC] %r refused: %s (%s)", canonical, decision.reason,
                        getattr(decision, 'detail', '') or '')
            return DispatchResult(DispatchState.REJECTED, canonical, canonical,
                                  {'reason': decision.reason,
                                   'detail': getattr(decision, 'detail', '') or ''})
        if isinstance(decision, execution_policy.NeedsConfirmation):
            self._stage_confirmation(effective_app, inv, decision)
            return DispatchResult(DispatchState.QUEUED, canonical, canonical,
                                  {'awaiting_confirmation': True, 'prompt': decision.prompt})

        if entry is not None and entry.source == 'plugin' and entry.handler is not None:
            remainder = str((args or {}).get('remainder', '') or '')
            logger.info("[PLUGIN] Executing: %s", canonical)
            try:
                state = adapt_handler_return(entry.handler(effective_app, remainder))
            except Exception as e:
                logger.exception("[ERROR] Plugin '%s' failed", canonical)
                return DispatchResult(DispatchState.FAILED, canonical, canonical, {'error': str(e)})
            if state is DispatchState.MISS:
                # The handler declined (documented `return False`): not this
                # command after all.
                return DispatchResult(DispatchState.MISS, source_text or canonical, canonical,
                                      {'declined_by': canonical})
            if state in (DispatchState.COMPLETED, DispatchState.QUEUED):
                self._matcher.record_execution(entry)
            return DispatchResult(state, canonical, canonical)

        if builtin is None:
            # Authorized (a registry row exists) but nothing here can run it.
            logger.error("[EXEC] %r has no builtin entry and no plugin handler", canonical)
            return DispatchResult(DispatchState.FAILED, canonical, canonical,
                                  {'reason': 'no_handler'})
        handler = get_handler(builtin.get('type'))
        if handler is None:
            print(f"[WARN] Unknown command type: {builtin.get('type')}")
            return DispatchResult(DispatchState.FAILED, canonical, canonical,
                                  {'reason': 'unknown_type'})
        try:
            success = handler.execute(builtin, self._build_context(app_instance))
        except Exception as e:
            print(f"[ERROR] Command execution error: {e}")
            return DispatchResult(DispatchState.FAILED, canonical, canonical, {'error': str(e)})
        if not success:
            return DispatchResult(DispatchState.FAILED, canonical, canonical)
        print(f"[OK] Executed: {canonical}")
        if entry is not None:
            self._matcher.record_execution(entry)
        return DispatchResult(DispatchState.COMPLETED, canonical, canonical)

    def execute_command(self, command_name: str, app_instance: Any = None, *,
                        route: Route = Route.EXACT, generation: "int | None" = None,
                        prompt: str = "", confirmed: bool = False,
                        args: "dict | None" = None, source_text: str = "") -> bool:
        """Execute a command by name. True ONLY when the effect actually ran.

        The boolean face of execute_canonical() for callers that only need
        "did it run" (the cheat sheet, the settings Test button, the
        scheduler). `prompt` is accepted and ignored: the confirmation
        question is always a local template, never caller or model wording.
        """
        return self.execute_canonical(
            command_name, app_instance, route=route, generation=generation,
            args=args, source_text=source_text, confirmed=confirmed,
        ).state is DispatchState.COMPLETED

    def _stage_confirmation(self, app, inv: Invocation, decision) -> None:
        """Park a NeedsConfirmation in the shared pending slot and ask. "yes"
        (ask_ollama.handle_ava_confirm) re-enters execute_canonical with
        confirmed=True -- which re-authorizes, so a pack switched off or a
        scope left while the question was open refuses the effect."""
        targets, target_probe, foreground_bound = execution_policy.bind_staged_target(inv, self)

        def _approve(op):
            self.execute_canonical(inv.command_id, app, route=inv.route, generation=inv.generation,
                                   confirmed=True, args=inv.args, source_text=inv.source_text)

        def _reject(op):
            if foreground_bound and getattr(op, "target_change", None):
                speak = getattr(app, "audio_coordinator", None)
                if speak is not None:
                    speak.speak("That window changed, so I didn't do it", category="confirmation")

        stage_target = {} if foreground_bound else {"targets": targets, "target_probe": target_probe}
        if foreground_bound:
            stage_target = {"foreground_target": targets, "foreground_probe": target_probe}
        execution_policy.stage_pending(app, inv, decision.prompt, on_approve=_approve,
                                       on_reject=_reject, record_type="action", **stage_target)
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
        context = self._matcher.current_context()
        entry, _remainder = self._matcher.match(text, context)
        if entry is None:
            entry, _remainder = self._repair_plugin_matcher_drift(text, context)
        return entry.phrase if entry is not None else None

    def find_exact_command(self, text: str) -> Optional[str]:
        """Return a command only when it consumes the complete utterance."""
        context = self._matcher.current_context()
        entry, remainder = self._matcher.match(text, context)
        if entry is None:
            entry, remainder = self._repair_plugin_matcher_drift(text, context)
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
        if generation is None:
            # This call is where a spoken exact request is created; bind it to
            # the generation current NOW (a caller holding a capture-time
            # generation -- wake FIFO, session entry -- passes it instead).
            generation = execution_policy.capture_generation(effective_app)

        # THE pending question has priority: a complete-utterance "yes" / "no"
        # / "wait" answers it (single use, deadline, generation and target
        # checked in execution_policy). A yes that is only part of a sentence,
        # or quoted, is not an answer and never reaches the confirm handler.
        if execution_policy.pending_operation() is not None:
            answer = execution_policy.answer_pending(effective_app, text)
            if answer is not None:
                state = DispatchState.COMPLETED if answer in ("approved", "rejected", "extended") \
                    else DispatchState.REJECTED
                return DispatchResult(state, "pending_reply", "pending_reply", {'answer': answer})
        if execution_policy.mentions_confirmation(text):
            return DispatchResult.miss(text, {'reason': 'not a complete confirmation'})

        text_lower = text.lower().strip()

        # Queue 126/A. These three used to run their effect HERE, off a
        # SUBSTRING test, before the matcher and before authorize() had been
        # called even once. A dictated paragraph containing "command mode
        # off" turned command matching off and wrote config.json; one
        # containing "remind me in 5 minutes to ..." scheduled a reminder out
        # of the rest of the sentence. Both reproduced end to end in the
        # queue 126 report, with authorize called zero times.
        #
        # Two things were wrong and both are fixed:
        #   * substring, where every other session control is WHOLE UTTERANCE
        #     (queue 84 -- "Have it stop listening to you, or something like
        #     that" once destroyed a 477-character draft exactly this way);
        #   * the effect ran before anything authorized it.
        # They keep the property that mattered -- "always processed,
        # regardless of mode state" -- because the command_matching_enabled
        # gate below still comes after them.
        session_effect = self._session_control_effect(text, text_lower, effective_app)
        if session_effect is not None:
            return session_effect

        # Gate on command_matching_enabled — bypassed by wake word mode via force_commands
        if not force_commands:
            if effective_app and not effective_app.command_matching_enabled:
                return DispatchResult.miss(text)

        # Phonetic wash for matching only; original text is returned on fallthrough
        # so free-form dictation output is never silently rewritten.
        match_text = apply_phonetic_wash(text)
        # One scope context for the whole utterance (queue 68): every match
        # below sees the same foreground app and tags.
        context = self._matcher.current_context()
        entry, remainder = self._matcher.match(match_text, context)
        if entry is None:
            entry, remainder = self._repair_plugin_matcher_drift(match_text, context)
        if entry is None:
            disabled_pack = self._matcher.disabled_pack_for(match_text)
            if disabled_pack:
                # Exactly a disabled pack's phrase: say the pack is off rather
                # than run a shorter command with the rest as its argument.
                print(f"[CMD] '{text}' belongs to the disabled pack '{disabled_pack}'")
                return DispatchResult.miss(text, {'reason': 'pack_disabled', 'pack': disabled_pack})
            out_of_scope = self._matcher.out_of_scope_for(match_text, context)
            if out_of_scope is not None:
                # Exactly a scoped command's phrase, not live here: a miss, and
                # the log says which command and why (never silent).
                scoped_entry, why = out_of_scope
                logger.info(f"[SCOPE] {scoped_entry.phrase!r} is {scoped_entry.scope.describe()} "
                            f"({why}) -- not a candidate for this utterance")
                return DispatchResult.miss(text, {'reason': 'out_of_scope', 'phrase': scoped_entry.phrase,
                                                  'scope': scoped_entry.scope.to_json(), 'why': why})
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
        remainder = self._original_remainder(text, match_text, entry, remainder, context)

        # Command mode debounce: suppress rapid re-execution of flagged commands
        in_cmd_mode = getattr(effective_app, 'command_mode_active', False)
        if in_cmd_mode and self._matcher.should_suppress(entry):
            print(f"[CMD] Debounce: '{entry.phrase}' still in cooldown")
            return DispatchResult(DispatchState.REJECTED, entry.phrase, entry.phrase,
                                  {'reason': 'debounce'})

        # ONE execution API for every route (queue 107): the spoken path hands
        # the matched canonical phrase to execute_canonical exactly as a model
        # proposal or a scheduled repeat does, and reports what it returns. A
        # claimed-but-failed command stays claimed -- only a plugin's own
        # decline (MISS) gives the utterance back as dictation.
        result = self.execute_canonical(
            entry.phrase, effective_app, route=route, generation=generation,
            args=({'remainder': remainder} if entry.source == 'plugin' else None),
            source_text=text)
        if result.state is DispatchState.MISS:
            return DispatchResult.miss(text, dict(result.detail))
        return result

    def _original_remainder(self, text: str, match_text: str, entry, washed_remainder: str,
                            context=None) -> str:
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
        detail = self._matcher.match_detail(text, context)
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
