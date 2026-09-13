"""Execution policy -- the ONE choke point between "something decided to act"
and an actual side effect.

Every route that can cause an effect (exact/alias voice match, the ACTION2
grammar, a model-proposed ACTION/ACTION2, a macro, a scheduled repeat, a
Smart Actions tool call) builds an :class:`Invocation` and asks
:func:`authorize` immediately before doing anything. The answer is one of
three values:

    Allowed             -> perform the effect now
    NeedsConfirmation   -> stage it; a later "yes" re-enters authorize with
                           confirmed=True (and is still generation-checked)
    Denied              -> do nothing, say why

Policy table (see docs/EXECUTION_POLICY.md):

    risk      exact / grammar / macro / schedule    model / smart-action
    read, ui  Allowed                               Allowed (if on the model allow-list)
    write     Allowed                               NeedsConfirmation
    destructive, unknown  NeedsConfirmation         NeedsConfirmation
    any       generation != current -> Denied(stale) on every route
    model routes: tool id not on the allow-list -> Denied(not_allowed_for_model)
    any       args fail the command's param_schema -> Denied(invalid_args)

Risk comes from the registry, never from the caller: plugin ``risk_class``
metadata, the built-in command's *type + keys* (a key sequence that closes /
locks / quits is destructive, one that mutates text is write, navigation is
ui), ACTION2 verbs, and the Smart Actions tool tiers. Anything the registry
cannot describe is ``unknown`` -- and unknown is NOT safe.

Generation: the request identity is the app's ``_ava_cmd_generation`` counter
(the same one the Ava command-session waterfall already guards on --
ava_command_session.py's post-resolution check). A request captures it when it
is created; ``stop_all()`` bumps it. Anything that resolves under an older
generation is Denied(stale) here, whatever thread it is on.

Astra review 2026-09-12, section 1 items 1 and 3, and section 5 item 3.
"""
from __future__ import annotations

import logging
import string
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("Samsara.execution_policy")


class Route(str, Enum):
    EXACT = "exact"              # user spoke a registered phrase / alias
    GRAMMAR = "grammar"          # deterministic ACTION2 grammar (focus/open/close <x>)
    MODEL = "model"              # a model proposed the tool id (ACTION / ACTION2)
    MACRO = "macro"              # a macro step
    SMART_ACTION = "smart_action"  # a Smart Actions agent tool call
    SCHEDULE = "schedule"        # a repeating task the user confirmed once


# Routes where the tool id was chosen by a model, not spoken by the user.
MODEL_ROUTES = frozenset({Route.MODEL, Route.SMART_ACTION})
# Routes that count as "the user said exactly this".
USER_ROUTES = frozenset({Route.EXACT, Route.GRAMMAR, Route.MACRO, Route.SCHEDULE})

RISK_READ, RISK_UI, RISK_WRITE, RISK_DESTRUCTIVE, RISK_UNKNOWN = (
    "read", "ui", "write", "destructive", "unknown")


@dataclass(frozen=True)
class Invocation:
    """One requested effect, fully resolved: a canonical id + validated args."""
    command_id: str
    args: dict = field(default_factory=dict)
    route: Route = Route.EXACT
    generation: Optional[int] = None   # None = not generation-bound (legacy callers)
    prompt: str = ""                   # confirmation text a model / caller supplied
    source_text: str = ""              # the utterance that led here, for logs


@dataclass(frozen=True)
class Allowed:
    reason: str = "allowed"
    risk: str = RISK_UNKNOWN


@dataclass(frozen=True)
class NeedsConfirmation:
    prompt: str
    reason: str = "confirmation_required"
    risk: str = RISK_UNKNOWN


@dataclass(frozen=True)
class Denied:
    reason: str            # "stale" | "not_allowed_for_model" | "invalid_args" | "unknown_command" | "legacy_protocol"
    risk: str = RISK_UNKNOWN
    detail: str = ""


Decision = "Allowed | NeedsConfirmation | Denied"


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

# Built-in key sequences. Classification is by EFFECT of the keys, not by the
# command's name, so a renamed or aliased command cannot dodge it.
_DESTRUCTIVE_HOTKEYS = {
    ("alt", "f4"),                 # close window
    ("ctrl", "w"), ("ctrl", "f4"), ("ctrl", "shift", "w"),   # close tab / window
    ("ctrl", "q"), ("alt", "q"),   # quit app
    ("win", "l"),                  # lock
    ("ctrl", "alt", "delete"), ("ctrl", "alt", "del"),
    ("shift", "delete"),           # permanent delete
}
_DESTRUCTIVE_NORMALIZED = {tuple(sorted(d[:-1])) + (d[-1],) for d in _DESTRUCTIVE_HOTKEYS}
_WRITE_BASE_KEYS = {"enter", "return", "delete", "del", "backspace", "space", "insert"}
_WRITE_CTRL_KEYS = {"x", "v", "z", "y", "s", "d", "k", "n", "o", "p", "backspace", "delete", "enter"}
_SHIFTED_KEY_IS_SELECTION = {"left", "right", "up", "down", "home", "end", "page_up", "page_down"}

# Method-type built-ins: explicit, because a method name says nothing about its effect.
_METHOD_RISK = {
    "undo_last_dictation": RISK_WRITE,
    "start_recording": RISK_UI,
    "cancel_recording": RISK_UI,
    "show_tutorial": RISK_UI,
    "show_cheat_sheet": RISK_UI,
    "hide_cheat_sheet": RISK_UI,
    "repeat_last_command": RISK_UNKNOWN,   # repeats whatever ran last -- cannot be classified statically
    "open_log_file": RISK_UI,
}

_ACTION2_RISK = {"focus": RISK_UI, "open": RISK_UI, "close": RISK_DESTRUCTIVE}

_PLUGIN_RISK_CLASS = {
    "safe": RISK_UI,          # the registry's own statement (02a will flip *unset* to unknown)
    "reversible": RISK_WRITE,
    "destructive": RISK_DESTRUCTIVE,
}


def _norm_keys(keys) -> tuple:
    if isinstance(keys, str):
        keys = keys.replace("+", " ").split()
    return tuple(str(k).lower().strip() for k in (keys or []))


def classify_keys(keys) -> str:
    """Risk of pressing a key combination."""
    ks = _norm_keys(keys)
    if not ks:
        return RISK_UNKNOWN
    mods = {k for k in ks if k in ("ctrl", "alt", "shift", "win", "cmd", "meta")}
    base = [k for k in ks if k not in mods]
    if tuple(sorted(mods)) + tuple(base) in _DESTRUCTIVE_NORMALIZED:
        return RISK_DESTRUCTIVE
    if not base:
        return RISK_UI          # a bare modifier press/hold
    key = base[-1]
    if "ctrl" in mods and key in _WRITE_CTRL_KEYS:
        return RISK_WRITE
    if "shift" in mods and key in _SHIFTED_KEY_IS_SELECTION:
        return RISK_UI          # extend selection
    if key in _WRITE_BASE_KEYS:
        return RISK_WRITE
    if len(key) == 1 and not mods:
        return RISK_WRITE       # types a character
    if len(key) == 1 and mods == {"shift"}:
        return RISK_WRITE
    return RISK_UI


def classify_builtin(cmd: dict) -> str:
    """Risk of one commands.json entry, from its type and keys."""
    ctype = (cmd or {}).get("type")
    if ctype in ("hotkey", "key_down", "key_up"):
        return classify_keys(cmd.get("keys") or cmd.get("key"))
    if ctype == "press":
        return classify_keys([cmd.get("key")])
    if ctype == "release_all":
        return RISK_UI
    if ctype == "text":
        return RISK_WRITE
    if ctype == "launch":
        return RISK_UI
    if ctype == "mouse":
        return RISK_WRITE       # a click can submit or activate anything
    if ctype == "method":
        return _METHOD_RISK.get(str(cmd.get("method")), RISK_UNKNOWN)
    if ctype == "macro":
        worst = RISK_UI
        for step in cmd.get("steps") or []:
            action = step.get("action")
            if action == "hotkey":
                r = classify_keys(step.get("keys"))
            elif action == "press":
                r = classify_keys([step.get("key")])
            elif action == "type":
                r = RISK_WRITE
            else:
                r = RISK_UNKNOWN
            worst = _worse(worst, r)
        return worst
    return RISK_UNKNOWN


_RISK_ORDER = {RISK_READ: 0, RISK_UI: 1, RISK_WRITE: 2, RISK_DESTRUCTIVE: 3, RISK_UNKNOWN: 4}


def _worse(a: str, b: str) -> str:
    return a if _RISK_ORDER.get(a, 4) >= _RISK_ORDER.get(b, 4) else b


def _plugin_entry(command_id: str):
    try:
        from samsara import plugin_commands  # noqa: PLC0415
        return plugin_commands._REGISTRY.get(command_id)
    except Exception:
        return None


def classify(command_id: str, *, executor=None, app=None, declared_only: bool = False) -> tuple[str, bool, dict]:
    """Return (risk, reversible, param_schema) for a canonical command id.

    Resolution order: ACTION2 verb ids, Smart Actions tool ids, the
    executor's built-in table, the plugin registry. Unknown ids are
    ('unknown', False, {}) -- never safe.

    Plugin entries (02a registry adapter): the registry keeps two views of
    risk. The flat ``risk_class`` is the operational value dispatch has
    always used (author-declared, else the historical default 'safe');
    ``metadata['risk_class']`` is the AUTHOR'S statement and is 'unknown'
    when nothing was declared. ``declared_only=True`` reads the latter, and
    is what the model routes use -- so a plugin nobody classified is
    'unknown' to a model (not on the allow-list) while the user's own
    exact phrase keeps working as it always has.
    """
    cid = (command_id or "").strip().lower()
    if cid.startswith("key:"):
        # A raw key press with no registry entry (the scheduler's KEY form).
        return classify_keys(cid[4:]), False, {}
    if cid.startswith("action2:"):
        return _ACTION2_RISK.get(cid[8:], RISK_UNKNOWN), False, {}
    if cid.startswith("smart_action:"):
        try:
            from samsara.smart_actions_tools import (  # noqa: PLC0415
                TIER_ALWAYS_CONFIRM, TIER_AUTO, TIER_SETUP, TOOL_TIERS)
            tier = TOOL_TIERS.get(cid[13:])
            risk = {TIER_AUTO: RISK_UI, TIER_SETUP: RISK_WRITE, TIER_ALWAYS_CONFIRM: RISK_DESTRUCTIVE}.get(tier, RISK_UNKNOWN)
            return risk, False, {}
        except Exception:
            return RISK_UNKNOWN, False, {}
    ex = executor if executor is not None else getattr(app, "command_executor", None)
    builtin = None
    if ex is not None:
        table = getattr(ex, "commands", None)
        if isinstance(table, dict):
            builtin = table.get(cid)
    if builtin is not None:
        return classify_builtin(builtin), False, {}
    entry = _plugin_entry(cid)
    if entry is not None:
        declared = str((entry.get("metadata") or {}).get("risk_class", "") or "").lower()
        # The flat key is what dispatch has always used; an entry without it
        # (hand-built, pre-registry) gets the registry's own default, 'safe'.
        flat = str(entry.get("risk_class") or "safe").lower()
        if declared_only:
            risk = _PLUGIN_RISK_CLASS.get(declared, RISK_UNKNOWN)
        else:
            risk = _PLUGIN_RISK_CLASS.get(declared, None) or _PLUGIN_RISK_CLASS.get(flat, RISK_UNKNOWN)
        return risk, bool(entry.get("reversible", False)), dict(entry.get("param_schema") or {})
    return RISK_UNKNOWN, False, {}


def command_exists(command_id: str, *, executor=None, app=None) -> bool:
    cid = (command_id or "").strip().lower()
    if cid.startswith("key:"):
        return bool(_norm_keys(cid[4:]))
    if cid.startswith("action2:"):
        return cid[8:] in _ACTION2_RISK
    if cid.startswith("smart_action:"):
        try:
            from samsara.smart_actions_tools import TOOL_TIERS  # noqa: PLC0415
            return cid[13:] in TOOL_TIERS
        except Exception:
            return False
    ex = executor if executor is not None else getattr(app, "command_executor", None)
    table = getattr(ex, "commands", None)
    if isinstance(table, dict) and cid in table:
        return True
    return _plugin_entry(cid) is not None


# ---------------------------------------------------------------------------
# Model allow-list
# ---------------------------------------------------------------------------

# Tool ids a model may propose even though they are write/destructive (they
# always go through confirmation). Everything read/ui in the registry is
# allowed implicitly. Config key: execution_policy.model_tool_allowlist
# replaces the whole computed list; execution_policy.model_tool_extra adds.
DEFAULT_MODEL_EXTRA_ALLOWLIST = frozenset({
    "close window", "close tab", "enter", "submit", "new line",
    "delete selection", "delete word", "delete next word", "delete line",
    "cut", "paste", "undo", "redo", "select all",
    "action2:close",
    "smart_action:webhook_trigger", "smart_action:calendar_create", "smart_action:email_draft",
    "smart_action:send_email", "smart_action:delete_file", "smart_action:run_shell_command",
    "smart_action:paste_text", "smart_action:append_to_file",
})


def _policy_cfg(app) -> dict:
    try:
        cfg = getattr(app, "config", None) or {}
        return dict(cfg.get("execution_policy", {}) or {})
    except Exception:
        return {}


def model_may_call(command_id: str, *, app=None, executor=None, risk: Optional[str] = None) -> bool:
    cid = (command_id or "").strip().lower()
    cfg = _policy_cfg(app)
    explicit = cfg.get("model_tool_allowlist")
    if isinstance(explicit, (list, tuple, set)):
        return cid in {str(x).lower() for x in explicit}
    if risk is None:
        risk, _rev, _schema = classify(cid, executor=executor, app=app, declared_only=True)
    if risk in (RISK_READ, RISK_UI) and command_exists(cid, executor=executor, app=app):
        return True
    extra = set(DEFAULT_MODEL_EXTRA_ALLOWLIST)
    extra.update(str(x).lower() for x in (cfg.get("model_tool_extra") or []))
    return cid in extra


# ---------------------------------------------------------------------------
# Args validation (plugin param_schema)
# ---------------------------------------------------------------------------

def validate_args(schema: dict, args: dict) -> Optional[str]:
    """Return an error string if args violate the schema, else None.

    Free-text ``remainder`` is never validated (it is what the user said);
    only keys the schema names are checked.
    """
    if not schema:
        return None
    args = args or {}
    for name, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        if name not in args:
            if spec.get("required"):
                return f"missing required argument '{name}'"
            continue
        value = args[name]
        typ = spec.get("type")
        try:
            if typ == "int":
                value = int(value)
            elif typ == "float":
                value = float(value)
            elif typ == "bool" and not isinstance(value, bool):
                return f"argument '{name}' must be a bool"
            elif typ == "str" and not isinstance(value, str):
                return f"argument '{name}' must be a string"
        except (TypeError, ValueError):
            return f"argument '{name}' must be a {typ}"
        if "min" in spec and value < spec["min"]:
            return f"argument '{name}'={value} below minimum {spec['min']}"
        if "max" in spec and value > spec["max"]:
            return f"argument '{name}'={value} above maximum {spec['max']}"
        choices = spec.get("choices")
        if choices and value not in choices:
            return f"argument '{name}'={value!r} not one of {list(choices)}"
    return None


# ---------------------------------------------------------------------------
# Generation (request identity / cancellation)
# ---------------------------------------------------------------------------

_GEN_ATTR = "_ava_cmd_generation"


def current_generation(app) -> int:
    """The app's request generation; 0 for anything that has none (fakes,
    Mock apps, the executor's own None app)."""
    gen = getattr(app, _GEN_ATTR, 0)
    return gen if isinstance(gen, int) and not isinstance(gen, bool) else 0


def is_current(app, generation: Optional[int]) -> bool:
    return generation is None or generation == current_generation(app)


def bump_generation(app, reason: str = "") -> int:
    """Invalidate every request captured before now. Uses the app's own
    lock when it has one; falls back to a plain increment for fakes."""
    lock = getattr(app, "_ava_cmd_mode_lock", None)
    if lock is not None and hasattr(lock, "acquire"):
        with lock:
            setattr(app, _GEN_ATTR, current_generation(app) + 1)
            gen = current_generation(app)
    else:
        setattr(app, _GEN_ATTR, current_generation(app) + 1)
        gen = current_generation(app)
    logger.info("[POLICY] generation -> %d (%s)", gen, reason or "bump")
    return gen


# Exact whole-utterance stop words for the Ava lanes. "nevermind"/"never
# mind" are deliberately NOT here: they stay the pending-only cancel they
# already are (dictation._PENDING_CANCEL_UTTERANCES) and otherwise remain
# ordinary prose for the model.
STOP_UTTERANCES = frozenset({
    "stop", "cancel", "cancel that", "stop it", "stop that",
    "ava stop", "ava cancel", "go to sleep",
})


def is_stop_utterance(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    normalized = normalized.strip(string.punctuation + " ")
    return normalized in STOP_UTTERANCES


def stop_all(app, reason: str = "stop", *, chip: bool = True, bump: bool = True) -> dict:
    """The stop path that does not wait on understanding (Astra section 5.3).

    Bumps the generation FIRST (so any worker that is mid-model-call or
    mid-resolution is stale before we touch anything else), then clears every
    place a request can be waiting: the staged confirmation, the AVA-session
    queue, the D3 waterfall queue, the scheduler. Drafts (staged dictation)
    are deliberately untouched. Returns what was cleared, for feedback.
    """
    # bump=False is for a caller that has ALREADY bumped under its own lock
    # (exit_ava_command_session) and only needs the clearing half.
    gen = bump_generation(app, reason) if bump else current_generation(app)
    cleared = {"generation": gen, "pending": False, "queued": 0, "in_flight": False, "schedule": False}

    # Staged confirmation (Ava pending action, incl. a Smart Actions dialog wait).
    try:
        from plugins.commands import ask_ollama  # noqa: PLC0415
        cleared["pending"] = ask_ollama.get_pending_action() is not None
        ask_ollama.clear_pending_action()
        cleared["schedule"] = ask_ollama._scheduled_task is not None
        ask_ollama._stop_schedule()
    except Exception as exc:  # plugin missing in a stripped build
        logger.debug("[POLICY] stop_all: ask_ollama state not cleared: %s", exc)

    # AVA session-mode queue (generation-bound items; drop them all now).
    q = getattr(app, "_ava_session_dispatch_queue", None)
    lock = getattr(app, "_ava_session_dispatch_lock", None)
    if q is not None:
        try:
            if lock is not None:
                with lock:
                    cleared["queued"] += len(q)
                    q.clear()
                    cleared["in_flight"] = bool(getattr(app, "_ava_session_request_in_flight", False))
            else:
                cleared["queued"] += len(q)
                q.clear()
        except Exception as exc:
            logger.debug("[POLICY] stop_all: AVA queue not cleared: %s", exc)

    # D3 waterfall queue: items carry their generation and are now stale;
    # drain the not-yet-started ones so they do not even reach the resolver.
    try:
        from samsara import ava_command_session  # noqa: PLC0415
        cleared["queued"] += ava_command_session.drain_stale(app)
    except Exception as exc:
        logger.debug("[POLICY] stop_all: waterfall queue not drained: %s", exc)

    if chip:
        _chip(app, "Stopped", "warning")
    logger.info("[POLICY] stop_all(%s): %s", reason, cleared)
    return cleared


# ---------------------------------------------------------------------------
# The choke point
# ---------------------------------------------------------------------------

def _chip(app, label: str, kind: str) -> None:
    show = getattr(app, "_show_outcome_chip", None)
    if show is None:
        return
    try:
        show(label, kind)
    except Exception as exc:
        logger.debug("[POLICY] chip failed: %s", exc)


def _emit(app, inv: Invocation, decision) -> None:
    cid = inv.command_id
    if isinstance(decision, Allowed):
        logger.info("[POLICY] ALLOW %s route=%s risk=%s gen=%s", cid, inv.route.value, decision.risk, inv.generation)
        return  # the executor's own outcome chip covers the success case
    if isinstance(decision, NeedsConfirmation):
        logger.info("[POLICY] CONFIRM %s route=%s risk=%s gen=%s", cid, inv.route.value, decision.risk, inv.generation)
        _chip(app, f"Confirm: {cid}", "pending")
        return
    logger.warning("[POLICY] DENY %s route=%s risk=%s gen=%s reason=%s %s",
                   cid, inv.route.value, decision.risk, inv.generation, decision.reason, decision.detail)
    _chip(app, f"Blocked: {cid}" if decision.reason != "stale" else "Cancelled", "error" if decision.reason != "stale" else "warning")


def authorize(inv: Invocation, *, app=None, executor=None, confirmed: bool = False):
    """Decide whether `inv` may cause its effect right now.

    ``confirmed=True`` means the user already answered a NeedsConfirmation
    for this exact invocation; the generation, allow-list and args checks
    still apply (a confirmation can be stale too).
    """
    cid = (inv.command_id or "").strip().lower()

    # 1. Request identity: anything from a superseded request is dead.
    if not is_current(app, inv.generation):
        d = Denied("stale", detail=f"generation {inv.generation} != {current_generation(app)}")
        _emit(app, inv, d)
        return d

    # 2. The id must resolve to something the registry knows.
    if not command_exists(cid, executor=executor, app=app):
        d = Denied("unknown_command", detail=cid)
        _emit(app, inv, d)
        return d

    # Model routes see only what the registry DECLARES (undeclared = unknown).
    risk, _reversible, schema = classify(cid, executor=executor, app=app,
                                         declared_only=inv.route in MODEL_ROUTES)

    # 3. Arguments must satisfy the command's own schema.
    err = validate_args(schema, inv.args)
    if err:
        d = Denied("invalid_args", risk=risk, detail=err)
        _emit(app, inv, d)
        return d

    # 4. A model may only ever name tools on the allow-list.
    if inv.route in MODEL_ROUTES and not model_may_call(cid, app=app, executor=executor, risk=risk):
        d = Denied("not_allowed_for_model", risk=risk, detail=cid)
        _emit(app, inv, d)
        return d

    # 5. Already confirmed by the user for this invocation.
    if confirmed:
        a = Allowed("confirmed", risk=risk)
        _emit(app, inv, a)
        return a

    # 6. The policy table.
    if risk in (RISK_READ, RISK_UI):
        a = Allowed("low_risk", risk=risk)
    elif risk == RISK_WRITE and inv.route in USER_ROUTES:
        a = Allowed("write_user_route", risk=risk)
    else:
        a = NeedsConfirmation(inv.prompt or f"{cid.capitalize()}?", risk=risk)
    _emit(app, inv, a)
    return a


# ---------------------------------------------------------------------------
# Pending confirmation record (shared by voice yes/no and the Smart Actions
# dialog). Stored in ask_ollama's pending slot so "yes"/"ava cancel"/
# "scratch that" keep resolving ONE object.
# ---------------------------------------------------------------------------

CONFIRM_TTL_S = 30.0


class PendingOperation:
    """A staged invocation waiting for the user's answer.

    ``approve()``/``reject()`` are idempotent and thread-safe; ``wait()``
    lets a blocking consumer (the Smart Actions dispatcher) sit on it while
    voice or a dialog resolves it.
    """

    def __init__(self, inv: Invocation, prompt: str, on_approve=None, on_reject=None):
        self.invocation = inv
        self.prompt = prompt
        self.generation = inv.generation
        self.expires = time.time() + CONFIRM_TTL_S
        self._on_approve = on_approve
        self._on_reject = on_reject
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.approved: Optional[bool] = None
        self.always: bool = False

    def resolve(self, approved: bool, *, always: bool = False) -> bool:
        with self._lock:
            if self.approved is not None:
                return False
            self.approved = bool(approved)
            self.always = bool(always)
        self._event.set()
        cb = self._on_approve if approved else self._on_reject
        if cb is not None:
            try:
                cb(self)
            except Exception as exc:
                logger.exception("[POLICY] pending %s callback failed: %s", "approve" if approved else "reject", exc)
        return True

    def approve(self, *, always: bool = False) -> bool:
        return self.resolve(True, always=always)

    def reject(self) -> bool:
        return self.resolve(False)

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)

    def is_expired(self) -> bool:
        return time.time() > self.expires


def stage_pending(app, inv: Invocation, prompt: str, *, on_approve=None, on_reject=None,
                  extra: Optional[dict] = None, record_type: str = "invocation") -> PendingOperation:
    """Put one PendingOperation into the shared pending slot (replacing any
    older one, which is rejected first so a blocked waiter wakes up).

    ``record_type`` keeps the slot's existing vocabulary ("action" for a
    command id, "action2" for a verb+argument, "invocation" for anything
    else) so scratch-that / cancel / tests keep reading it; every record
    carries ``op`` and "yes" resolves that, whatever the type says.
    """
    op = PendingOperation(inv, prompt, on_approve=on_approve, on_reject=on_reject)
    record = {
        "type": record_type,
        "op": op,
        "confirm_text": prompt,
        "generation": inv.generation,
        "expires": op.expires,
        "original_text": inv.source_text,
        "command": inv.command_id,
    }
    if extra:
        record.update(extra)
    try:
        from plugins.commands import ask_ollama  # noqa: PLC0415
        with ask_ollama._pending_action_lock:
            old = ask_ollama._pending_action
            ask_ollama._pending_action = record
        if isinstance(old, dict) and isinstance(old.get("op"), PendingOperation):
            old["op"].reject()
    except Exception as exc:
        logger.debug("[POLICY] stage_pending: pending slot unavailable: %s", exc)
    return op
