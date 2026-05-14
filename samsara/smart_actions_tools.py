"""Tool call dispatcher for Smart Actions Phase 2.

Tiered consent model:
  Tier 1 (TIER_AUTO):           execute silently, no prompt
  Tier 2 (TIER_SETUP):          confirm once per exact scope, then remember
  Tier 3 (TIER_ALWAYS_CONFIRM): always show a confirmation dialog

SECURITY CRITICAL:
  - Tier is determined SOLELY by local TOOL_TIERS. Any 'tier' field in the
    agent response is SILENTLY IGNORED (_get_tier() never reads tool_call).
  - File paths are canonicalized via Path.resolve() and checked against
    allowed_directories BEFORE any write, blocking path traversal attacks.
  - Tier 2 approvals are scoped to the exact path/URL, not a broad category.
    Approving URL A does NOT approve URL B.

Five-layer fallback hierarchy ensures the user's thought is never lost even
when the agent endpoint or the brain dump file are unavailable.
"""

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---- Tier constants ---------------------------------------------------------

TIER_AUTO           = 1
TIER_SETUP          = 2
TIER_ALWAYS_CONFIRM = 3

TOOL_TIERS: Dict[str, int] = {
    'paste_text':           TIER_AUTO,
    'append_to_brain_dump': TIER_AUTO,
    'show_notification':    TIER_AUTO,
    'append_to_file':       TIER_AUTO,          # scope-restricted via _check_scope
    'webhook_trigger':      TIER_SETUP,
    'calendar_create':      TIER_SETUP,
    'email_draft':          TIER_SETUP,
    'send_email':           TIER_ALWAYS_CONFIRM,
    'delete_file':          TIER_ALWAYS_CONFIRM,
    'run_shell_command':    TIER_ALWAYS_CONFIRM,
}


class ToolDispatcher:
    """Dispatch tool calls from agent responses under the tiered consent model."""

    def __init__(self, app, config: dict):
        self.app = app
        self._config = config
        self.allowed_directories: List[str] = [
            str(Path(d).expanduser())
            for d in config.get('allowed_directories', ['~/Documents'])
        ]
        self.allowed_domains: List[str] = list(config.get('allowed_domains', []))
        # key = "tool|scope", value = True
        self._approvals: Dict[str, bool] = dict(config.get('tier2_approvals', {}))

        # Thinking pulse
        self._thinking_stop = threading.Event()

        # Emergency SQLite buffer (fallback layer 2)
        self._emergency_db: Optional[sqlite3.Connection] = None
        self._emergency_db_lock = threading.Lock()
        self._init_emergency_db()

    # ---- Emergency DB --------------------------------------------------------

    def _init_emergency_db(self):
        try:
            db_dir = Path(os.environ.get('APPDATA', Path.home())) / 'Samsara'
            db_dir.mkdir(parents=True, exist_ok=True)
            path = db_dir / 'emergency_buffer.db'
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS buffer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    text TEXT NOT NULL,
                    reason TEXT
                )
            """)
            conn.commit()
            self._emergency_db = conn
        except Exception as e:
            logger.error("[TOOLS] Could not init emergency DB: %s", e)

    def _emergency_db_write(self, text: str, reason: str):
        if self._emergency_db is None:
            raise RuntimeError("Emergency DB not available")
        from datetime import datetime
        with self._emergency_db_lock:
            self._emergency_db.execute(
                "INSERT INTO buffer (timestamp, text, reason) VALUES (?, ?, ?)",
                (datetime.now().isoformat(), text, reason))
            self._emergency_db.commit()

    # ---- Thinking pulse (kill-switch via threading.Event) --------------------

    def _start_thinking_pulse(self):
        """Start a 1Hz earcon pulse that signals agent is processing."""
        self._thinking_stop.clear()

        def _pulse():
            from plugins.commands.smart_actions import (
                EARCON_THINKING_PULSE, _play_earcon, get_config)
            smart_cfg = get_config(self.app)
            # wait() returns True immediately when event is set -- no ghost threads
            while not self._thinking_stop.wait(1.0):
                _play_earcon(self.app, EARCON_THINKING_PULSE, smart_cfg)

        threading.Thread(target=_pulse, daemon=True,
                         name="sa-thinking-pulse").start()

    def _stop_thinking_pulse(self):
        """Kill the pulse. Thread exits within 1s (next wait() returns True)."""
        self._thinking_stop.set()

    # ---- Five-layer fallback hierarchy --------------------------------------

    def _fallback_save(self, text: str, reason: str) -> str:
        """Ensure the user's thought is NEVER silently lost.

        Tries each layer in order and returns the name of the layer that worked.
        """
        from plugins.commands.smart_actions import (
            EARCON_FALLBACK, EARCON_ERROR, _play_earcon, get_config,
            resolve_brain_dump_path, append_entry)
        smart_cfg = get_config(self.app)

        # Layer 1: Brain dump file
        try:
            path = resolve_brain_dump_path(smart_cfg.get('brain_dump_path'))
            ok = append_entry(path, f"[{reason}] {text}")
            if ok:
                _play_earcon(self.app, EARCON_FALLBACK, smart_cfg)
                logger.info("[FALLBACK] Saved to brain dump (%s)", reason)
                return "saved_to_brain_dump"
        except Exception as e:
            logger.error("[FALLBACK] Brain dump write failed: %s", e)

        # Layer 2: Emergency SQLite buffer
        try:
            self._emergency_db_write(text, reason)
            _play_earcon(self.app, EARCON_FALLBACK, smart_cfg)
            logger.warning("[FALLBACK] Saved to emergency DB (%s)", reason)
            return "saved_to_emergency_db"
        except Exception as e:
            logger.error("[FALLBACK] Emergency DB write failed: %s", e)

        # Layer 3: Clipboard
        try:
            import pyperclip
            pyperclip.copy(text)
            _play_earcon(self.app, EARCON_ERROR, smart_cfg)
            logger.warning("[FALLBACK] Copied to clipboard (%s)", reason)
            return "saved_to_clipboard"
        except Exception as e:
            logger.error("[FALLBACK] Clipboard write failed: %s", e)

        # Layer 4: Visible modal (non-blocking, stays on top)
        try:
            self._show_recovery_modal(text)
            logger.warning("[FALLBACK] Shown in recovery modal (%s)", reason)
            return "shown_in_modal"
        except Exception as e:
            logger.error("[FALLBACK] Recovery modal failed: %s", e)

        # Layer 5: Console (absolute last resort)
        print(f"[SMART ACTIONS EMERGENCY] UNSAVED TEXT: {text}")
        return "printed_to_console"

    def _show_recovery_modal(self, text: str):
        root = getattr(self.app, 'root', None)
        if root is None:
            raise RuntimeError("No Tk root")

        def _make():
            import tkinter as tk
            top = tk.Toplevel(root)
            top.title("Smart Actions — Unsaved Text")
            top.attributes("-topmost", True)
            top.geometry("480x200")
            top.resizable(True, True)

            tk.Label(top, text="The following text could not be saved:",
                     anchor='w').pack(fill='x', padx=12, pady=(12, 4))
            txt = tk.Text(top, height=4, wrap='word')
            txt.insert('1.0', text)
            txt.configure(state='disabled')
            txt.pack(fill='both', expand=True, padx=12, pady=(0, 4))

            def _copy_close():
                try:
                    import pyperclip
                    pyperclip.copy(text)
                except Exception:
                    pass
                top.destroy()

            tk.Button(top, text="Copy to Clipboard",
                      command=_copy_close).pack(pady=8)

        root.after(0, _make)

    # ---- Dispatch ------------------------------------------------------------

    def dispatch(self, tool_call: dict) -> Dict[str, Any]:
        """Execute a tool call dict from an agent response.

        SECURITY: tier is read from local TOOL_TIERS only. Any 'tier' key in
        tool_call is ignored here and in _get_tier().
        """
        tool_name = tool_call.get('tool', '')
        args = tool_call.get('args', {})

        if not tool_name:
            return {'success': False, 'result': 'Missing tool name'}

        # SECURITY: local tier only — never read from tool_call
        tier = self._get_tier(tool_name)

        # Scope check before consent (fail fast, no UI shown)
        allowed, scope_reason = self._check_scope(tool_name, args)
        if not allowed:
            logger.warning("[TOOLS] Scope rejected %s: %s", tool_name, scope_reason)
            return {'success': False, 'result': f'Scope check failed: {scope_reason}'}

        if tier == TIER_AUTO:
            return self._execute(tool_name, args)

        if tier == TIER_SETUP:
            approval_key = self._build_approval_key(tool_name, args)
            if self._approvals.get(approval_key):
                return self._execute(tool_name, args)
            approved, always = self._request_confirmation(tool_call, allow_always=True)
            if not approved:
                return {'success': False, 'result': 'User rejected'}
            if always:
                self._store_approval(approval_key)
            return self._execute(tool_name, args)

        # Tier 3: always confirm — no "always allow"
        approved, _ = self._request_confirmation(tool_call, allow_always=False)
        if not approved:
            return {'success': False, 'result': 'User rejected'}
        return self._execute(tool_name, args)

    def _get_tier(self, tool_name: str) -> int:
        """ALWAYS use local TOOL_TIERS. Never read tier from the tool_call."""
        return TOOL_TIERS.get(tool_name, TIER_ALWAYS_CONFIRM)

    # ---- Scope checking ------------------------------------------------------

    def _check_scope(self, tool_name: str, args: dict) -> Tuple[bool, str]:
        if tool_name == 'append_to_file':
            path_str = args.get('path', '')
            if not path_str:
                return False, "No path provided"
            target = Path(path_str).expanduser().resolve()
            for allowed_dir in self.allowed_directories:
                allowed_resolved = Path(allowed_dir).expanduser().resolve()
                try:
                    target.relative_to(allowed_resolved)
                    return True, "OK"
                except ValueError:
                    continue
            return False, f"Path {target} is outside allowed directories"

        if tool_name == 'webhook_trigger':
            url = args.get('url', '')
            if not url:
                return False, "No URL provided"
            if not self.allowed_domains:
                return False, "No allowed_domains configured"
            for domain in self.allowed_domains:
                if url.startswith(domain):
                    return True, "OK"
            return False, f"URL not in allowed_domains"

        return True, "OK"

    def _build_approval_key(self, tool_name: str, args: dict) -> str:
        """Exact-scope approval key. Approving key A never approves key B."""
        if tool_name == 'append_to_file':
            path_str = args.get('path', '')
            resolved_dir = str(Path(path_str).expanduser().resolve().parent)
            return f"append_to_file|{resolved_dir}"
        if tool_name == 'webhook_trigger':
            url = args.get('url', '')
            return f"webhook_trigger|{url}"
        if tool_name == 'calendar_create':
            cal_id = args.get('calendar_id', 'default')
            return f"calendar_create|{cal_id}"
        return f"{tool_name}|*"

    def _store_approval(self, key: str):
        self._approvals[key] = True
        try:
            if self.app and hasattr(self.app, 'config'):
                sa = self.app.config.setdefault('smart_actions', {})
                sa.setdefault('tier2_approvals', {})[key] = True
                if hasattr(self.app, 'save_config'):
                    self.app.save_config()
        except Exception as e:
            logger.error("[TOOLS] Could not persist approval: %s", e)

    # ---- Confirmation UI -----------------------------------------------------

    def _request_confirmation(self, tool_call: dict,
                               allow_always: bool = False) -> Tuple[bool, bool]:
        """Show a blocking dialog. Returns (approved, always_allow)."""
        from plugins.commands.smart_actions import (
            EARCON_CONFIRM_REQUIRED, _play_earcon, get_config)
        _play_earcon(self.app, EARCON_CONFIRM_REQUIRED, get_config(self.app))

        tool_name = tool_call.get('tool', '')
        args = tool_call.get('args', {})
        desc = self._describe_tool_call(tool_name, args)
        try:
            return self._tk_confirm_dialog(desc, allow_always)
        except Exception as e:
            logger.error("[TOOLS] Confirmation dialog failed: %s — rejecting", e)
            return False, False

    def _tk_confirm_dialog(self, description: str,
                           allow_always: bool) -> Tuple[bool, bool]:
        import tkinter as tk
        root = getattr(self.app, 'root', None)
        result: Dict[str, Any] = {'approved': False, 'always': False}
        done = threading.Event()

        def _make():
            top = tk.Toplevel(root)
            top.title("Smart Actions")
            top.attributes("-topmost", True)
            top.resizable(False, False)
            if root:
                top.grab_set()

            tk.Label(top, text="The agent wants to:",
                     font=('Segoe UI', 10),
                     anchor='w').pack(anchor='w', padx=16, pady=(16, 4))
            tk.Label(top, text=description,
                     font=('Segoe UI', 10, 'bold'),
                     wraplength=400, anchor='w',
                     justify='left').pack(anchor='w', padx=24, pady=(0, 16))

            row = tk.Frame(top)
            row.pack(pady=(0, 14))

            def _approve():
                result['approved'] = True
                top.destroy()
                done.set()

            def _reject():
                top.destroy()
                done.set()

            def _always():
                result['approved'] = True
                result['always'] = True
                top.destroy()
                done.set()

            tk.Button(row, text="Approve",
                      width=12, command=_approve).pack(side='left', padx=4)
            tk.Button(row, text="Reject",
                      width=12, command=_reject).pack(side='left', padx=4)
            if allow_always:
                tk.Button(row, text="Always allow this",
                          width=16, command=_always).pack(side='left', padx=4)

            top.protocol("WM_DELETE_WINDOW", _reject)

        if root is not None:
            root.after(0, _make)
        done.wait(timeout=120)
        return result['approved'], result['always']

    @staticmethod
    def _describe_tool_call(tool_name: str, args: dict) -> str:
        if tool_name == 'append_to_file':
            return f"append_to_file: {args.get('path', '?')}"
        if tool_name == 'webhook_trigger':
            return f"webhook_trigger: {args.get('url', '?')}"
        if tool_name == 'send_email':
            return f"send_email to: {args.get('to', '?')}"
        if tool_name == 'delete_file':
            return f"delete_file: {args.get('path', '?')}"
        if tool_name == 'run_shell_command':
            return f"run_shell_command: {args.get('command', '?')}"
        return f"{tool_name}: {args}"

    # ---- Tool implementations ------------------------------------------------

    def _execute(self, tool_name: str, args: dict) -> Dict[str, Any]:
        dispatch_table = {
            'paste_text':           self._tool_paste_text,
            'append_to_brain_dump': self._tool_append_to_brain_dump,
            'show_notification':    self._tool_show_notification,
            'append_to_file':       self._tool_append_to_file,
            'webhook_trigger':      self._tool_webhook_trigger,
            'calendar_create':      self._tool_placeholder,
            'email_draft':          self._tool_placeholder,
            'send_email':           self._tool_placeholder,
            'delete_file':          self._tool_placeholder,
            'run_shell_command':    self._tool_placeholder,
        }
        fn = dispatch_table.get(tool_name, self._tool_unknown)
        try:
            return fn(args)
        except Exception as e:
            logger.exception("[TOOLS] %s raised: %s", tool_name, e)
            return {'success': False, 'result': str(e)}

    def _tool_paste_text(self, args: dict) -> Dict[str, Any]:
        text = args.get('text', '')
        try:
            import pyperclip
            import pyautogui
            pyperclip.copy(text)
            pyautogui.hotkey('ctrl', 'v')
            return {'success': True, 'result': None}
        except Exception as e:
            return {'success': False, 'result': str(e)}

    def _tool_append_to_brain_dump(self, args: dict) -> Dict[str, Any]:
        from plugins.commands.smart_actions import (
            get_config, resolve_brain_dump_path, append_entry)
        content = args.get('content', args.get('text', ''))
        smart_cfg = get_config(self.app)
        path = resolve_brain_dump_path(smart_cfg.get('brain_dump_path'))
        ok = append_entry(path, content)
        return {'success': ok, 'result': None}

    def _tool_show_notification(self, args: dict) -> Dict[str, Any]:
        title = args.get('title', 'Samsara')
        message = args.get('message', args.get('text', ''))
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40 | 0x1000)
        except Exception:
            print(f"[NOTIFY] {title}: {message}")
        return {'success': True, 'result': None}

    def _tool_append_to_file(self, args: dict) -> Dict[str, Any]:
        path_str = args.get('path', '')
        content = args.get('content', args.get('text', ''))
        target = Path(path_str).expanduser().resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, 'a', encoding='utf-8') as f:
                f.write(content)
                if content and not content.endswith('\n'):
                    f.write('\n')
            return {'success': True, 'result': None}
        except Exception as e:
            return {'success': False, 'result': str(e)}

    def _tool_webhook_trigger(self, args: dict) -> Dict[str, Any]:
        import json as _json
        import urllib.request as _req
        url = args.get('url', '')
        payload = args.get('payload', {})
        try:
            body = _json.dumps(payload).encode('utf-8')
            r = _req.Request(url, data=body,
                             headers={'Content-Type': 'application/json'},
                             method='POST')
            with _req.urlopen(r, timeout=10) as resp:
                return {'success': True, 'result': resp.read().decode('utf-8')[:500]}
        except Exception as e:
            return {'success': False, 'result': str(e)}

    def _tool_placeholder(self, args: dict) -> Dict[str, Any]:
        return {'success': False, 'result': 'not yet implemented — Phase 3'}

    def _tool_unknown(self, args: dict) -> Dict[str, Any]:
        return {'success': False, 'result': 'unknown tool'}
