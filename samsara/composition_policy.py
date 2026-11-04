"""1031e: composition carries source authority, never reusable consent.

Only locally captured snapshots live here. Tool arguments cannot choose a
route, trust a target, or turn a previous approval into another operation.
"""
from __future__ import annotations

import copy
import os
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

_active = ContextVar("execution_invocation", default=None)
PLACEHOLDER_TOOLS = frozenset({
    "calendar_create", "email_draft", "send_email", "delete_file", "run_shell_command",
})
_NOTE_SUFFIXES = {".txt", ".md", ".rst", ".log"}
_CONTROL_NAMES = {"agents.md", "claude.md", "skill.md", "requirements.txt",
                  "cmakelists.txt", "config.txt", "settings.txt", "commands.txt"}
_PROTECTED_PARTS = {".git", ".codex", ".claude", ".agents", ".config", ".ssh",
                    ".aws", "startup", "plugins", "scripts", "config", "configuration",
                    "settings", "shared", "public", "onedrive", "dropbox", "google drive",
                    "appdata", "windows", "system32", "program files", "programdata"}


def network_drive(path):
    if os.name != "nt":
        return False
    import ctypes
    query = ctypes.windll.kernel32.GetDriveTypeW
    query.argtypes = [ctypes.c_wchar_p]
    query.restype = ctypes.c_uint
    return query(path.anchor) in {0, 1, 4}  # Unknown/unavailable/mapped network.


def inherited_request(route, generation):
    """A nested dispatcher cannot promote model/deferred work to exact voice."""
    from samsara import execution_policy as ep
    parent = _active.get()
    if parent is not None:
        generation = parent.generation
        if parent.route in ep.MODEL_ROUTES:
            route = parent.route
    return route, generation


def run_bound(inv, run):
    token = _active.set(inv)
    try:
        return run()
    finally:
        _active.reset(token)


def snapshot_call(call):
    if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
        raise ValueError("invalid tool call")
    args = call.get("args", {})
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    return call["tool"], copy.deepcopy(args)


def append_target(path, allowed_directories):
    """Directory permission admits personal notes, not code/config/shared data."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("No path provided")
    raw = Path(path).expanduser()
    if ("\x00" in path or path.startswith(("\\\\", "//")) or ":" in raw.name
            or network_drive(raw.absolute())):
        raise ValueError("Executable, configuration or shared append target")
    target = raw.resolve()
    roots = [Path(root).expanduser().resolve() for root in allowed_directories]
    scopes = [root for root in roots if target.is_relative_to(root)]
    if not scopes:
        raise ValueError("Path is outside allowed directories")
    scope = max(scopes, key=lambda root: len(root.parts))
    names = {part.casefold() for part in (*raw.parts, *target.parts)}
    if (target.suffix.casefold() not in _NOTE_SUFFIXES
            or target.name.casefold() in _CONTROL_NAMES
            or any(part.startswith(".") for part in target.parts if part not in {".", ".."})
            or names & _PROTECTED_PARTS
            or any(part.startswith("onedrive") for part in names)
            or any((parent / ".git").exists() for parent in target.parents if parent.is_relative_to(scope))
            or (target.exists() and (not target.is_file() or target.stat().st_nlink > 1))):
        raise ValueError("Executable, configuration or shared append target")
    return target


def webhook_scope(url, domains):
    if not isinstance(url, str) or not url:
        return False, "No URL provided"
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or any(c.isspace() for c in url)):
            return False, "Invalid webhook URL"
        _ = parsed.port
        for domain in domains:
            allowed = urlsplit(domain if "://" in domain else "https://" + domain).hostname
            if allowed and (host == allowed or host.endswith("." + allowed)):
                return True, "OK"
    except (TypeError, ValueError):
        return False, "Invalid webhook URL"
    return False, "URL not in allowed_domains"


def paste_target(app):
    """Reuse MCP safety refusals, then bind a verified editor/composer control."""
    from samsara.agent_server import _foreground_target
    from samsara.effect_policy import resolve_submit_target
    identity, reason = _foreground_target()
    if reason:
        raise ValueError(reason)
    target = resolve_submit_target(app)
    if (target.kind not in {"editor", "ai"} or not target.hwnd
            or not target.control or not target.revision
            or target.hwnd != identity.get("hwnd") or target.process != identity.get("exe")):
        raise ValueError("Verified editable paste target unavailable")
    return identity, target


def tool_binding(dispatcher, name, args):
    if name == "paste_text":
        return paste_target(dispatcher.app)
    if name in {"append_to_file", "append_to_brain_dump"}:
        path = args.get("path")
        if name == "append_to_brain_dump":
            from plugins.commands.smart_actions import get_config, resolve_brain_dump_path
            path = str(resolve_brain_dump_path(get_config(dispatcher.app).get("brain_dump_path")))
        target = append_target(path, dispatcher.allowed_directories)
        # Canonical path + file identity/revision prevent a swapped link/file
        # from inheriting an approval. The file body is never retained here.
        stat = target.stat() if target.exists() else None
        return str(target), (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) if stat else None
    return None


def tool_effect_allowed(dispatcher, name, args):
    from samsara import execution_policy as ep
    inv = _active.get()
    try:
        return (inv is not None and inv.command_id == "smart_action:" + name
                and ep.is_fresh(dispatcher.app, inv.generation)
                and ep.argument_hash(inv.command_id, inv.args) == ep.argument_hash(inv.command_id, args)
                and inv.effect_target == (id(dispatcher), tool_binding(dispatcher, name, args))
                and dispatcher._check_scope(name, args)[0])
    except (OSError, ValueError):
        return False


def repeat_target(app, executor, wrapper):
    from samsara.effect_policy import input_kind
    name, saved = getattr(app, "_last_command_name", None), getattr(app, "_last_command", None)
    command = executor.commands.get(name, {}) if isinstance(name, str) else {}
    if (not saved or not command or command != saved or name == wrapper
            or command.get("method") == "repeat_last_command"):
        raise ValueError("nothing to repeat or repeat target changed")
    steps = command.get("steps", []) if command.get("type") == "macro" else [command]
    if any(input_kind(dict(step, type=step.get("action", step.get("type")))) == "submit"
           for step in steps):
        raise ValueError("repeat requires an explicit send request")
    return name


def scheduled_invocation(app, task):
    from samsara import execution_policy as ep
    command = task.get("command") or ("key:" + task["key"] if task.get("key") else "")
    return ep.Invocation(command, route=ep.Route.SCHEDULE, generation=task.get("generation"))


def run_macro(inv, app, executor, run):
    """Expand a frozen plan, asking for each consequential step at its real target."""
    from samsara import execution_policy as ep, effect_policy as effects
    from samsara.command_registry import DispatchResult, DispatchState
    plan = copy.deepcopy(executor.commands[inv.command_id])
    fingerprint = ep.argument_hash(inv.command_id, plan)
    steps = plan.get("steps") or []
    def destination(target):
        return (target.kind, target.hwnd, target.process, target.control,
                target.account, target.conversation, target.recipient)

    expected_destination = destination(effects.resolve_submit_target(app))

    def fresh():
        return (ep.is_fresh(app, inv.generation) and fingerprint == ep.argument_hash(
            inv.command_id, executor.commands.get(inv.command_id)))

    def result(state, reason):
        return DispatchResult(state, inv.command_id, inv.command_id, {"reason": reason})

    def advance(index):
        nonlocal expected_destination
        while index < len(steps):
            if not fresh():
                return result(DispatchState.REJECTED, "macro plan or generation changed")
            if destination(effects.resolve_submit_target(app)) != expected_destination:
                return result(DispatchState.REJECTED, "macro destination changed")
            step = copy.deepcopy(steps[index])
            action = step.get("action")
            if action not in {"press", "hotkey", "type"}:
                return result(DispatchState.REJECTED, "unsupported macro step")
            child = dict(step, type="text" if action == "type" else action)

            class StepExecutor:
                commands = dict(executor.commands, **{inv.command_id: child})

                def __getattr__(self, name):
                    return getattr(executor, name)

            view = StepExecutor()
            child_inv = ep.Invocation(inv.command_id, route=inv.route, generation=inv.generation)
            child_inv = effects.bind_input_invocation(child_inv, app, view)
            typing_target = None
            if action == "type":
                if any(ord(char) < 32 for char in step.get("text", "")):
                    return result(DispatchState.REJECTED, "macro text contains control keys")
                try:
                    typing_target = paste_target(app)
                except (OSError, ValueError) as exc:
                    return result(DispatchState.REJECTED, str(exc))
            refusal = effects.input_refusal(child_inv, app, view)
            if refusal:
                return result(DispatchState.REJECTED, refusal)
            decision = ep.authorize(child_inv, app=app, executor=view)
            if isinstance(decision, ep.Denied):
                return result(DispatchState.REJECTED, decision.reason)
            if effects.handling_for(child_inv, app=app, executor=view) == effects.PRIVATE_SEND:
                # A stored macro is not a fresh user-authored private message.
                decision = ep.NeedsConfirmation("Send this macro message?", risk=decision.risk)

            def eligible(bound=child_inv, typed=typing_target, selected=view):
                try:
                    return (fresh() and (bound.effect_target is None or
                            bound.effect_target == effects.resolve_submit_target(app))
                            and (typed is None or typed == paste_target(app))
                            and not effects.input_refusal(bound, app, selected))
                except (OSError, ValueError):
                    return False

            def commit(confirmed=False, bound=child_inv, selected=view,
                       item=step, check=eligible):
                nonlocal expected_destination
                if not check() or not isinstance(ep.authorize(
                        bound, app=app, executor=selected, confirmed=confirmed), ep.Allowed):
                    return result(DispatchState.REJECTED, "macro step target changed")
                outcome = effects.dispatch_input(bound, app, selected,
                    lambda: run_bound(bound, lambda: run(dict(plan, steps=[item]))),
                    confirmed=confirmed)
                keys = ep._norm_keys(item.get("keys"))
                if outcome is True and keys in {("alt", "tab"), ("win", "d"), ("ctrl", "tab")}:
                    expected_destination = destination(effects.resolve_submit_target(app))
                return outcome

            if isinstance(decision, ep.NeedsConfirmation):
                next_index = index + 1

                def approved(op, execute=commit, next_step=next_index):
                    outcome = execute(confirmed=True)
                    if outcome is True:
                        advance(next_step)

                target = child_inv.effect_target
                label = getattr(target, "label", "the focused control")
                keys = step.get("keys") or step.get("key") or "text"
                # PLACEHOLDER COPY (1031e)
                prompt = f"Run macro step {index + 1}: {keys} in {ep._template_value(label)}?"
                ep.stage_pending(app, child_inv, prompt, on_approve=approved,
                    refusal_guard=lambda check=eligible: None if check() else "macro plan or target changed")
                ep.listen_for_confirmation(app, prompt)
                return result(DispatchState.QUEUED, "awaiting macro step confirmation")
            outcome = commit()
            if isinstance(outcome, DispatchResult):
                return outcome
            if not outcome:
                return result(DispatchState.FAILED, "macro step failed")
            index += 1
        return result(DispatchState.COMPLETED if steps else DispatchState.FAILED,
                      "macro completed" if steps else "empty macro")

    return advance(0)
