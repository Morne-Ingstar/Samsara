"""1031a: local effects select handling; authorization remains execution_policy's job.

No model-supplied argument can declare a destination trustworthy. Desktop
adapters and optional local messaging integrations supply bound snapshots.
Snapshots retain hashes, never composer bodies. An unknown commit asks once.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Handling:
    tier: str
    mode: str
    wait_s: float = 0.0


IMMEDIATE = Handling("T0", "immediate")
LOCAL_EDIT = Handling("T1", "undoable")  # Mode does not promise a universal undo.
PRIVATE_SEND = Handling("T2", "short_send_grace", 1.0)
CONFIRM = Handling("T3", "explicit_confirm")


@dataclass(frozen=True)
class SubmitTarget:
    """A local adapter's exact destination/composer revision, not a trust flag.

    DM adapters must identify an account, conversation and individual recipient
    with stable provider IDs. Display names and executables cannot grant T2.
    The current tree has no dedicated DM adapter; unresolved chats ask once.
    """
    kind: str = "unknown"
    hwnd: int = 0
    process: str = ""
    control: str = ""
    revision: str = ""
    account: str = ""
    conversation: str = ""
    recipient: str = ""
    label: str = "the focused control"
    user_authored: bool = True
    preview: str = field(default="", repr=False)


def text_hash(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def send_handling(target, *, route="exact"):
    if target.kind == "ai":
        return IMMEDIATE
    if target.kind == "editor":
        return LOCAL_EDIT
    if (target.kind == "known_person" and target.user_authored
            and route in {"exact", "grammar"}
            and target.account and target.conversation and target.recipient):
        return PRIVATE_SEND
    return CONFIRM  # email, public, money, shell and unresolved commits


def raw_key_command(keys):
    from samsara.execution_policy import _norm_keys
    return {"type": "hotkey", "keys": list(_norm_keys(keys))}


def input_kind(command):
    from samsara.execution_policy import _norm_keys, _DESTRUCTIVE_NORMALIZED
    kind = command.get("type")
    if kind == "macro":
        return "macro"
    if kind not in {"press", "hotkey", "key_down"}:
        return None
    keys = _norm_keys(command.get("keys") or command.get("key"))
    if not keys:
        return None
    mods, base = set(keys[:-1]), keys[-1]
    normalized = tuple(sorted(mods)) + (base,)
    if normalized in _DESTRUCTIVE_NORMALIZED:
        return "close" if base not in {"delete", "del"} else "delete"
    # Ctrl+Delete edits the next word; it does not activate a Send/Delete
    # control. Keep model confirmation and foreground binding in policy.
    if base in {"delete", "del"} and mods == {"ctrl"}:
        return None
    if base in {"enter", "return", "space"} and not (mods - {"ctrl", "shift"}):
        return "submit"
    if base in {"delete", "del"}:
        return "delete"
    return None


def command_input(command_id, app=None, executor=None):
    if command_id.startswith("key:"):
        return raw_key_command(command_id[4:])
    ex = executor if executor is not None else getattr(app, "command_executor", None)
    command = (getattr(ex, "commands", None) or {}).get(command_id, {})
    if command.get("type") == "hotkey" and command.get("app_overrides"):
        from samsara.handlers import _resolve_app_override
        keys, skip = _resolve_app_override(command)
        return {} if skip else dict(command, keys=keys)
    return command


def resolve_submit_target(app=None):
    """Read actual foreground/control evidence. Never infer a browser from its title."""
    from samsara import execution_policy as ep
    identity = ep.foreground_window_identity() or {}
    hwnd, process = identity.get("hwnd", 0), identity.get("process", "")
    local = getattr(app, "_resolve_submit_target", None)
    if callable(local):
        try:
            target = local()
        except Exception:
            target = None
        if (isinstance(target, SubmitTarget) and target.hwnd and target.revision
                and target.hwnd == hwnd and target.process == process):
            return target
        return SubmitTarget(hwnd=hwnd, process=process)
    try:
        if process in {"claude.exe", "chatgpt.exe"}:
            if process == "claude.exe":
                from plugins.commands.message_claude import ClaudeUiaAdapter as Adapter
            else:
                from plugins.commands.message_chatgpt import ChatGPTUiaAdapter as Adapter
            adapter = Adapter()
            window = adapter.resolve_window()
            composer = adapter.find_composer(hwnd)
            if window and window[0] == hwnd and composer is not None:
                import uiautomation as auto
                focused = auto.GetFocusedControl()
                if focused is None or focused.GetRuntimeId() != composer.GetRuntimeId():
                    return SubmitTarget(hwnd=hwnd, process=process)
                return SubmitTarget("ai", hwnd, process, str(composer.GetRuntimeId()),
                                    text_hash(adapter.read_composer(composer)),
                                    conversation=str(window[2]), label=window[1])
        import uiautomation as auto
        focused = auto.GetFocusedControl()
        if focused is None or focused.ProcessId != identity.get("pid"):
            return SubmitTarget(hwnd=hwnd, process=process)
        control = str(focused.GetRuntimeId())
        pattern = focused.GetValuePattern()
        body = pattern.Value if pattern else focused.GetTextPattern().DocumentRange.GetText(-1)
        kind = "email" if process in {"outlook.exe", "olk.exe", "thunderbird.exe"} else "unknown"
        if (process in {"notepad.exe", "winword.exe", "wordpad.exe"}
                and focused.ControlTypeName in {"EditControl", "DocumentControl"}
                and not focused.IsPassword):
            kind = "editor"
        # All unrecognised apps remain unknown; no title-only AI/DM exemption.
        # Bind the surrounding recipient/subject controls as well as the body.
        # No UI text is persisted, logged or interpreted as a command.
        root = auto.ControlFromHandle(hwnd)
        context = []
        pending = [(root, 0)]
        while pending and len(context) < 250:
            item, depth = pending.pop()
            context.append((item.ControlTypeName, item.AutomationId, item.Name))
            if depth < 6:
                pending.extend((child, depth + 1) for child in item.GetChildren())
        revision = text_hash((body, identity.get("title_hash"), context))
        return SubmitTarget(kind, hwnd, process, control, revision, label=process or "the focused control")
    except Exception:
        return SubmitTarget(hwnd=hwnd, process=process, label=process or "the focused control")


def bind_input_invocation(inv, app, executor=None):
    command = command_input(inv.command_id, app, executor)
    if input_kind(command):
        return replace(inv, effect_target=resolve_submit_target(app))
    return inv


def input_refusal(inv, app=None, executor=None):
    """A question cannot grant permission to submit an unreadable composer."""
    target = inv.effect_target
    command = command_input(inv.command_id, app, executor)
    kind = input_kind(command)
    requires_composer = kind in {"submit", "delete"} or (kind == "macro" and any(
        input_kind(dict(step, type=step.get("action"))) in {"submit", "delete"}
        for step in command.get("steps") or []))
    if (target is not None and requires_composer
            and not (target.hwnd and target.control and target.revision)):
        return "composer unavailable"
    return None


# These are registry identities (including aliases via the registry's phrase),
# not matches against a user's dictated body or arbitrary command name.
_AI_MODULES = {"message_chatgpt", "message_claude", "ask_agent", "quick_ask"}
_IMMEDIATE_MODULES = {"flashforge_printer", "screen_gif", "workflow_capture", "voice_memo"}
_PROTECTED = {
    "close", "interact close", "close stremio", "restart samsara", "window close",
    "clear health log", "undo health log", "cancel reminder", "cancel all reminders",
    "remove task", "clear completed", "delete layout", "remove layout",
    "wipe drive", "send message", "delete permanently", "purchase", "shutdown", "restart",
}
_EFFECTS = {"email", "email_sent", "money", "purchase", "payment", "publish",
            "public_post", "file_delete", "data_delete", "unsaved_close",
            "software_install", "system_change", "security_change", "webhook", "shell"}


def handling_for(inv, *, app=None, executor=None):
    from samsara import execution_policy as ep
    command = command_input(inv.command_id, app, executor)
    kind = input_kind(command)
    if kind == "macro":
        modes = []
        for step in command.get("steps") or []:
            child = dict(step, type=step.get("action"))
            step_kind = input_kind(child)
            modes.append(CONFIRM if step_kind == "close" else
                         send_handling(inv.effect_target or resolve_submit_target(app), route=inv.route)
                         if step_kind else LOCAL_EDIT)
        return max(modes, key=lambda h: int(h.tier[1:]), default=IMMEDIATE)
    if kind == "close":
        return CONFIRM  # No claim that reopening recovers unsaved documents.
    if kind in {"submit", "delete"}:
        target = inv.effect_target or resolve_submit_target(app)
        if kind == "delete":
            keys = ep._norm_keys(command.get("keys") or command.get("key"))
            destructive = tuple(sorted(keys[:-1])) + (keys[-1],) in ep._DESTRUCTIVE_NORMALIZED
            return LOCAL_EDIT if target.kind == "editor" and not destructive else CONFIRM
        # Shift+Enter is a newline in verified AI composers; it is not a send.
        keys = ep._norm_keys(command.get("keys") or command.get("key"))
        if "shift" in keys and "ctrl" not in keys and target.kind in {"ai", "editor"}:
            return LOCAL_EDIT
        return send_handling(target, route=inv.route)
    entry = ep._plugin_entry(inv.command_id) or {}
    phrase = entry.get("phrase", inv.command_id)
    module = str(entry.get("source", "")).rsplit(".", 1)[-1]
    if module in _AI_MODULES or module in _IMMEDIATE_MODULES:
        return IMMEDIATE
    # The DICTATE lane owns its recoverable clear. Direct command-mode clear
    # bypasses that recovery path (DictationApp.clear_dictation_draft); keep its question.
    if command.get("method") == "clear_dictation_draft":
        return CONFIRM
    effects = (entry.get("metadata") or {}).get("side_effects") or entry.get("side_effects") or []
    effects = set(effects) if isinstance(effects, (list, tuple, set)) else set()
    if (phrase in _PROTECTED or inv.command_id == "action2:close"
            or inv.command_id in {"smart_action:send_email", "smart_action:delete_file",
                                  "smart_action:run_shell_command", "smart_action:webhook_trigger"}
            or effects & _EFFECTS):
        return CONFIRM
    # Unrated/destructive metadata alone adds no wait to an ordinary user command.
    risk, _, _ = ep.classify(inv.command_id, app=app, executor=executor)
    return LOCAL_EDIT if risk == ep.RISK_WRITE else IMMEDIATE


def stage_input_confirmation(app, inv, executor, prompt):
    from samsara import execution_policy as ep
    import copy
    inv = replace(inv, args=copy.deepcopy(inv.args))
    expected_hash = ep.argument_hash(inv.command_id, inv.args)

    def approve(op):
        # Exact snapshot is checked again in the callback, not merely before resolution.
        if (inv.effect_target != resolve_submit_target(app)
                or ep.argument_hash(inv.command_id, inv.args) != expected_hash):
            return
        executor.execute_canonical(inv.command_id, app, args=inv.args, route=inv.route,
                                   generation=inv.generation, source_text=inv.source_text, confirmed=True)

    target = inv.effect_target
    targets, target_probe, foreground_bound = ep.bind_staged_target(inv, executor)
    binding = ({"foreground_target": targets, "foreground_probe": target_probe}
               if foreground_bound else {"targets": targets, "target_probe": target_probe})
    ep.stage_pending(app, inv, prompt, on_approve=approve,
                     record_type="action", **binding,
                     refusal_guard=lambda: None if (target == resolve_submit_target(app)
                         and ep.argument_hash(inv.command_id, inv.args) == expected_hash)
                     else "composer, arguments or destination changed")
    ep.listen_for_confirmation(app, prompt)


def dispatch_input(inv, app, executor, run, *, confirmed=False, timer_factory=None):
    from samsara import execution_policy as ep
    from samsara.command_registry import DispatchResult, DispatchState
    target = inv.effect_target
    if target is None:
        return run()
    if target != resolve_submit_target(app) or not ep.is_fresh(app, inv.generation):
        return DispatchResult(DispatchState.REJECTED, inv.command_id, inv.command_id,
                              {"reason": "composer or destination changed"})
    command = command_input(inv.command_id, app, executor)
    if input_kind(command) == "macro":
        for step in command.get("steps") or []:
            current = resolve_submit_target(app)
            # A macro may change editor text, but cannot transfer approval to
            # another control, conversation or account between its steps.
            same_destination = replace(current, revision=target.revision, preview=target.preview) == target
            if not same_destination:
                return DispatchResult(DispatchState.REJECTED, inv.command_id, inv.command_id,
                                      {"reason": "macro destination changed"})
            child = dict(step, type=step.get("action"))
            if (input_kind(child) in {"close", "delete", "submit"}
                    and send_handling(current, route=inv.route) == CONFIRM
                    and current != target):
                return DispatchResult(DispatchState.REJECTED, inv.command_id, inv.command_id,
                                      {"reason": "macro commit context changed"})
            if not isinstance(ep.authorize(inv, app=app, executor=executor, confirmed=confirmed), ep.Allowed):
                return False
            if not run(dict(command, steps=[step])):
                return False
        return bool(command.get("steps"))
    handling = handling_for(inv, app=app, executor=executor)
    if handling == PRIVATE_SEND and not confirmed:
        # Feedback is started before the 1 s clock. No confirmation question.
        preview = " ".join(target.preview.split()[:8])
        ep._chip(app, f"Sending to {target.label}: {preview}; say cancel or send now", "pending")  # PLACEHOLDER COPY (1031a)

        def commit():
            if (target == resolve_submit_target(app)
                    and isinstance(ep.authorize(inv, app=app, executor=executor), ep.Allowed)):
                run()

        op = ep.stage_cancel_window(app, inv, target.label, 1.0, commit,
                                    bind_foreground=False, timer_factory=timer_factory)
        op._refusal_guard = lambda: None if target == resolve_submit_target(app) else "composer or destination changed"
        return DispatchResult(DispatchState.QUEUED, inv.command_id, inv.command_id,
                              {"send_grace_s": 1.0, "op_id": op.op_id})
    return run()


def submit_wake_profile(app, reply_context=None):
    """The net-zero dictation.py seam; asynchronous sends own their completion."""
    from samsara import execution_policy as ep
    from samsara.command_registry import DispatchState
    if getattr(app, "_wake_session_mode", "focus_dictate") != "focus_dictate":
        return False
    inv = ep.Invocation("key:return", generation=ep.capture_generation(app))
    inv = bind_input_invocation(inv, app)
    if input_refusal(inv, app):
        return False
    target = inv.effect_target
    body = getattr(app, "_wake_session_delivered_text", "")
    expected_body = text_hash(body)
    # Latch each capture-session/body/target before an attempt, even if Return raises.
    session = getattr(app, "_wake_session_started_at", None)
    receipt = (inv.generation, session, expected_body)
    attempts = getattr(app, "_policy_submit_attempts", None)
    if attempts is None or getattr(app, "_policy_submit_session", None) != (inv.generation, session):
        attempts = app._policy_submit_attempts = set()
        app._policy_submit_session = (inv.generation, session)
    if receipt in attempts:
        return False

    def eligible():
        return (target == resolve_submit_target(app)
                and text_hash(getattr(app, "_wake_session_delivered_text", "")) == expected_body
                and ep.is_fresh(app, inv.generation)
                and getattr(app, "_wake_session_started_at", None) == session
                and getattr(app, "_wake_session_mode", "focus_dictate") == "focus_dictate"
                and getattr(app, "_wake_session_target_hwnd", None) == target.hwnd
                and getattr(app, "_wake_session_target_process", "") == target.process
                and not getattr(app, "_wake_session_delivery_failed", False)
                and bool(getattr(app, "config", {}).get("auto_paste"))
                and not getattr(app, "wake_dictation_buffer", []) and bool(body.strip()))

    def run():
        if receipt in attempts or not eligible():
            return False
        attempts.add(receipt)
        import pyautogui
        pyautogui.press("return")
        app._wake_failed_draft = ""
        app.play_sound("success")
        if deferred[0]:
            app.play_sound("text_sent")
            app._emit_wake_trace({"stage": "utterance_end", "result": "wake_session_sent"})
            app._end_wake_session()
            if reply_context is not None:
                from samsara.agent_channel import claude_reply, chatgpt_reply
                reader = chatgpt_reply if target.process == "chatgpt.exe" else claude_reply
                reader.start_after_send(app, reply_context)
        return True

    deferred = [False]
    if not eligible():
        return False
    decision = ep.authorize(inv, app=app)
    if isinstance(decision, ep.NeedsConfirmation):
        deferred[0] = True

        def approve(op):
            if eligible() and isinstance(ep.authorize(inv, app=app, confirmed=True), ep.Allowed):
                run()

        ep.stage_pending(app, inv, decision.prompt, on_approve=approve,
                         refusal_guard=lambda: None if eligible() else "composer or destination changed")
        ep.listen_for_confirmation(app, decision.prompt)
        return False
    if not isinstance(decision, ep.Allowed):
        return False
    deferred[0] = send_handling(target) == PRIVATE_SEND
    result = dispatch_input(inv, app, None, run)
    return result is True  # QUEUED means caller must preserve this wake session.
