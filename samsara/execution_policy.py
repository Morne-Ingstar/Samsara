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

Policy table (see Docs/EXECUTION_POLICY.md):

    risk      exact / grammar / macro / schedule    model / smart-action
    read, ui  Allowed                               Allowed (if on the model allow-list)
    write     Allowed                               NeedsConfirmation
    destructive, undoable Allowed on exact only     NeedsConfirmation
    destructive, irreversible / unknown NeedsConfirmation on every route
    any       generation != current -> Denied(stale) on every route
    model routes: tool id not on the allow-list -> Denied(not_allowed_for_model)
    any       args fail the command's param_schema -> Denied(invalid_args)

Risk comes from the registry, never from the caller: plugin ``risk_class``
metadata, the built-in command's *type + keys* (a key sequence that closes /
quits is destructive, one that mutates text is write, locking/navigation is
ui), ACTION2 verbs, and the Smart Actions tool tiers. Anything the registry
cannot describe is ``unknown`` -- and unknown is NOT safe.

Generation: the request identity is the app's ``_ava_cmd_generation`` counter
(the same one the Ava command-session waterfall already guards on --
ava_command_session.py's post-resolution check). A request captures it when it
is created -- at capture time, never when a model finishes -- and
``stop_all()`` bumps it. A request with NO generation, or any generation other
than the current one, is Denied(stale) here, whatever thread it is on.

Confirmation text is built from LOCAL templates and resolved argument values
only (:func:`confirmation_prompt`). A model may name a tool; it may not phrase
the question the user is asked.

Arguments: a schema that is absent or empty is *undeclared*, never "takes no
arguments". Undeclared -> unavailable to model routes (Denied "unvalidated");
on user routes only the user's own spoken ``remainder`` may ride along. Where
a schema exists, extra keys, wrong types and over-long strings are rejected.

Pending confirmation: ONE record at a time (:class:`PendingOperation`), with
operation id, generation, argument hash, bound target versions and a 30 s
monotonic deadline. A new proposal supersedes and visibly cancels the old
one; "yes" is accepted only as a complete utterance while the record is live
(:func:`classify_reply` / :func:`answer_pending`); "wait" extends it once.

Astra review 2026-09-12, section 1 items 1 and 3, and section 5 item 3;
02_conversational_architecture.md sections 1, 5 and 7 step 1.
"""
from __future__ import annotations

import hashlib
import json
import logging
import copy
import re
import string
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Optional

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
    # The generation captured when the request was CREATED (capture_generation
    # at the wake FIFO / session entry / utterance dispatch). None is not
    # "unbound": authorize() denies it as stale.
    generation: Optional[int] = None
    # Retained for call-site compatibility only. NEVER shown to the user: the
    # confirmation text comes from confirmation_prompt() (local templates).
    prompt: str = ""
    source_text: str = ""              # the utterance that led here, for logs


@dataclass(frozen=True)
class Allowed:
    reason: str = "allowed"
    risk: str = RISK_UNKNOWN
    hint: str = ""


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
    ("ctrl", "alt", "delete"), ("ctrl", "alt", "del"),
    ("shift", "delete"),           # permanent delete
}
_DESTRUCTIVE_NORMALIZED = {tuple(sorted(d[:-1])) + (d[-1],) for d in _DESTRUCTIVE_HOTKEYS}
# Effect table: close can be reopened; quit/delete remain irreversible by default.
_UNDOABLE_HOTKEYS = {("alt", "f4"), ("ctrl", "w"), ("ctrl", "f4"),
                     ("ctrl", "shift", "w")}
_SAFE_UNKNOWN_VERBS = {"lock computer": RISK_UI, "lock screen": RISK_UI}


def _irreversible(entry: dict, default: bool = True) -> bool:
    """Only an explicit reversible declaration overrides the conservative default."""
    metadata = entry.get("metadata") or {}
    for source in (metadata, entry):
        for key in ("reversibility", "reversible"):
            if key in source:
                value = source[key]
                return not (value is True or value in ("reversible", "undoable"))
        if "irreversible" in source:
            return source["irreversible"] is not False
    return default
_WRITE_BASE_KEYS = {"enter", "return", "delete", "del", "backspace", "space", "insert"}
_WRITE_CTRL_KEYS = {"x", "v", "z", "y", "s", "d", "k", "n", "o", "p", "backspace", "delete", "enter"}
_SHIFTED_KEY_IS_SELECTION = {"left", "right", "up", "down", "home", "end", "page_up", "page_down"}

# Method-type built-ins: explicit, because a method name says nothing about its effect.
_METHOD_RISK = {
    "undo_last_dictation": RISK_WRITE,
    # Queue 80: discards the whole staged draft; the text cannot be recovered.
    "clear_dictation_draft": RISK_DESTRUCTIVE,
    "start_recording": RISK_UI,
    "cancel_recording": RISK_UI,
    "show_tutorial": RISK_UI,
    "show_cheat_sheet": RISK_UI,
    "hide_cheat_sheet": RISK_UI,
    "repeat_last_command": RISK_UNKNOWN,   # repeats whatever ran last -- cannot be classified statically
    "open_log_file": RISK_UI,
}

_ACTION2_RISK = {"focus": RISK_UI, "open": RISK_UI, "close": RISK_DESTRUCTIVE}

#: Queue 126/A. Three effects used to run in CommandExecutor.process_text
#: BEFORE anything was authorized, off a SUBSTRING match: a dictated
#: paragraph containing "command mode off" turned command matching off and
#: wrote config.json, and one containing "remind me in 5 minutes to ..."
#: scheduled a reminder from the rest of the sentence. Reproduced end to end
#: in the queue 126 report; authorize() was called zero times in all three.
#:
#: They are real effects, so they get real identities and go through the same
#: chokepoint as everything else. They are not registry commands (checked:
#: none of the four phrases resolves), so the ids are synthetic and declared
#: here. RISK_WRITE, deliberately: a user route runs them without a question,
#: exactly as before, while a MODEL naming one has to be confirmed.
SESSION_CONTROL_IDS = {
    "session:command_mode_on": RISK_WRITE,
    "session:command_mode_off": RISK_WRITE,
    "session:reminder": RISK_WRITE,
}

#: Queue 126/B. A synthetic id -> the REGISTRY command whose pack and scope
#: govern the same effect.
#:
#: Without this the restriction binds to whether an id happens to resolve
#: rather than to what the effect does: with window-management switched off,
#: authorize denied the registered `open` with pack_disabled and ALLOWED
#: `action2:open` on the same target in the same breath (reproduced in the
#: queue 126 report). The pack is a statement about the effect; a second way
#: of naming the effect must not escape it.
SYNTHETIC_GOVERNORS = {
    "action2:open": "open",
    "action2:focus": "focus",
    "action2:close": "close",
}


def governing_command_id(command_id: str) -> str:
    """The id whose pack/scope restriction applies to this effect.

    For a registry id, itself. For a synthetic id that stands in for a
    registered command, that command -- so "refused when spoken" and
    "refused when a model proposes it" cannot disagree."""
    cid = (command_id or "").strip().lower()
    return SYNTHETIC_GOVERNORS.get(cid, cid)

_PLUGIN_RISK_CLASS = {
    "read": RISK_READ,
    "ui": RISK_UI,
    "write": RISK_WRITE,
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


_CATALOG_RISKS = frozenset({RISK_READ, RISK_UI, RISK_WRITE, RISK_DESTRUCTIVE})
_catalog_index: Optional[tuple] = None
_catalog_index_lock = threading.Lock()


def clear_catalog_risk_cache() -> None:
    """Forget the loaded commands_catalog.json ratings (tests, a regenerated catalog)."""
    global _catalog_index
    with _catalog_index_lock:
        _catalog_index = None


def _catalog_risk_index() -> tuple:
    """({canonical_id: risk}, {(plugin, alias): risk}) from commands_catalog.json,
    loaded once. Empty when the file is absent or invalid; frozen-smoke treats
    absence from a packaged build as a release failure."""
    global _catalog_index
    with _catalog_index_lock:
        if _catalog_index is None:
            by_id, by_alias = {}, {}
            try:
                from samsara.command_catalog import load_catalog_json  # noqa: PLC0415
                for record in load_catalog_json() or []:
                    risk = record.get("risk")
                    if record.get("kind") != "plugin" or risk not in _CATALOG_RISKS:
                        continue
                    by_id[record.get("canonical_id")] = risk
                    for alias in record.get("aliases") or []:
                        by_alias.setdefault((record.get("plugin"), alias), risk)
            except Exception as exc:
                logger.debug("[POLICY] commands_catalog.json unavailable for risk fallback: %s", exc)
            _catalog_index = (by_id, by_alias)
        return _catalog_index


def catalog_risk(entry: dict, command_id: str = "") -> Optional[str]:
    """The commands_catalog.json risk for a plugin registry entry, or None.

    Keyed by the catalog's canonical id (module stem + slug of the entry's
    primary phrase), falling back to the spoken alias within the same plugin.
    Used only when the command declares no risk_class itself."""
    try:
        from samsara.command_catalog import normalize_phrase, slug  # noqa: PLC0415
    except Exception:
        return None
    by_id, by_alias = _catalog_risk_index()
    plugin = str(entry.get("source") or "").rsplit(".", 1)[-1]
    phrase = normalize_phrase(entry.get("phrase") or command_id)
    risk = by_id.get(f"{plugin}.{slug(phrase)}")
    if risk is None:
        risk = by_alias.get((plugin, normalize_phrase(command_id)))
    return risk


def _plugin_entry(command_id: str):
    try:
        from samsara import plugin_commands  # noqa: PLC0415
        return plugin_commands._REGISTRY.get(command_id)
    except Exception:
        return None


#: The explicit, locally-declared "takes no arguments" schema. Identity
#: matters: an empty dict from a registry is UNDECLARED, this object is not.
NO_ARGS_SCHEMA = MappingProxyType({})

#: ACTION2 verbs carry exactly one target name.
#: The session controls take at most a free-text body (the reminder's).
_SESSION_CONTROL_SCHEMA = MappingProxyType({
    "minutes": {"type": "int", "required": False},
    "message": {"type": "str", "required": False},
})

_ACTION2_SCHEMA = MappingProxyType({
    "target": {"type": "str", "required": True, "max_len": 120},
})

#: Argument schemas for Smart Actions tools, keyed by tool name. Policy-owned
#: and deliberately EMPTY: no tool has a reviewed schema yet, so every Smart
#: Actions tool call is Denied("unvalidated") until one is added here.
SMART_ACTION_SCHEMAS: dict = {}

#: Default upper bound for a schema'd string argument without its own max_len.
DEFAULT_MAX_STR_LEN = 500


def _declared_schema(entry: dict) -> Optional[dict]:
    """A plugin's param_schema, or None when absent, empty or 'unknown'."""
    metadata = entry.get("metadata")
    if isinstance(metadata, dict) and "param_schema" in metadata:
        schema = metadata.get("param_schema")
    else:
        schema = entry.get("param_schema")
    return dict(schema) if isinstance(schema, dict) and schema else None


def classify(command_id: str, *, executor=None, app=None, declared_only: bool = False) -> tuple:
    """Return (risk, reversible, param_schema) for a canonical command id.

    Resolution order: ACTION2 verb ids, Smart Actions tool ids, the
    executor's built-in table, the plugin registry. Unknown ids are
    ('unknown', False, None) -- never safe.

    param_schema is None when the command has no declared schema (see module
    docstring), NO_ARGS_SCHEMA for commands whose type takes no arguments
    (commands.json built-ins, raw keys), else the declared mapping.

    All routes use declared metadata when available. The legacy flat default
    'safe' cannot make an undeclared plugin safe. When a plugin declares no
    risk and ``declared_only`` is False (user routes), the command's rating in
    commands_catalog.json is used (:func:`catalog_risk`); ``declared_only``
    (model routes) keeps such a command ``unknown``.
    """
    cid = (command_id or "").strip().lower()
    if cid in SESSION_CONTROL_IDS:
        # Reversible: say it again, or cancel the reminder.
        return SESSION_CONTROL_IDS[cid], True, _SESSION_CONTROL_SCHEMA
    if cid.startswith("key:"):
        # A raw key press with no registry entry (the scheduler's KEY form).
        return classify_keys(cid[4:]), False, NO_ARGS_SCHEMA
    if cid.startswith("action2:"):
        return _ACTION2_RISK.get(cid[8:], RISK_UNKNOWN), False, _ACTION2_SCHEMA
    if cid.startswith("smart_action:"):
        schema = SMART_ACTION_SCHEMAS.get(cid[13:])
        schema = dict(schema) if isinstance(schema, dict) and schema else None
        try:
            from samsara.smart_actions_tools import (  # noqa: PLC0415
                TIER_ALWAYS_CONFIRM, TIER_AUTO, TIER_SETUP, TOOL_TIERS)
            tier = TOOL_TIERS.get(cid[13:])
            risk = {TIER_AUTO: RISK_UI, TIER_SETUP: RISK_WRITE, TIER_ALWAYS_CONFIRM: RISK_DESTRUCTIVE}.get(tier, RISK_UNKNOWN)
            return risk, False, schema
        except Exception:
            return RISK_UNKNOWN, False, schema
    ex = executor if executor is not None else getattr(app, "command_executor", None)
    builtin = None
    if ex is not None:
        table = getattr(ex, "commands", None)
        if isinstance(table, dict):
            builtin = table.get(cid)
    if builtin is not None:
        keys = _norm_keys(builtin.get("keys"))
        normalized = tuple(sorted(keys[:-1])) + keys[-1:]
        reversible = not _irreversible(builtin, normalized not in _UNDOABLE_HOTKEYS)
        # A commands.json entry's type fully determines what it does: it takes
        # no arguments by construction.
        return classify_builtin(builtin), reversible, NO_ARGS_SCHEMA
    entry = _plugin_entry(cid)
    if entry is not None:
        declared = str((entry.get("metadata") or {}).get("risk_class", "") or "").lower()
        flat = str(entry.get("risk_class") or "unknown").lower()
        # Flat metadata is authoritative only for older entries with no metadata view.
        risk = _PLUGIN_RISK_CLASS.get(declared if "metadata" in entry else flat, RISK_UNKNOWN)
        if risk == RISK_UNKNOWN and not declared_only:
            # User routes: a command that declares nothing takes the reviewed
            # commands_catalog.json rating. Model routes keep "unknown".
            risk = catalog_risk(entry, cid) or RISK_UNKNOWN
        return risk, not _irreversible(entry), _declared_schema(entry)
    return RISK_UNKNOWN, False, None


def _availability_block(command_id: str, *, executor=None, app=None):
    """(reason, detail) when the executor says this command cannot run here
    (pack switched off, scope not live), else None. Delegated to
    CommandExecutor.command_availability so pack/scope membership has ONE
    definition; an executor without it (a stub, a tool) blocks nothing."""
    ex = executor if executor is not None else getattr(app, "command_executor", None)
    fn = getattr(ex, "command_availability", None)
    if not callable(fn):
        return None
    # Queue 126/B: ask about the EFFECT, not about the spelling of the id.
    # command_availability looks its argument up in the registry and blocks
    # nothing when it finds nothing, so a synthetic id used to walk past a
    # disabled pack that stopped the identical spoken command.
    governed = governing_command_id(command_id)
    try:
        blocked = fn(governed)
    except Exception as exc:                      # never fail an effect on the check itself
        logger.debug("availability check failed for %r: %s", command_id, exc)
        return None
    # A stub or mock executor can answer anything; only a real (reason, detail)
    # pair blocks an effect, never a truthy object of an unexpected shape.
    if not isinstance(blocked, (tuple, list)) or len(blocked) != 2:
        return None
    reason, detail = blocked
    return str(reason), str(detail)


def command_exists(command_id: str, *, executor=None, app=None) -> bool:
    cid = (command_id or "").strip().lower()
    if cid in SESSION_CONTROL_IDS:
        return True
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
    "permanent delete", "going dark",
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
    if risk == RISK_UNKNOWN and command_exists(cid, executor=executor, app=app):
        return True  # registered but unclassified: authorize must prompt
    extra = set(DEFAULT_MODEL_EXTRA_ALLOWLIST)
    extra.update(str(x).lower() for x in (cfg.get("model_tool_extra") or []))
    return cid in extra


# ---------------------------------------------------------------------------
# Args validation (plugin param_schema)
# ---------------------------------------------------------------------------

#: The one argument key that is the user's own spoken words (process_text's
#: remainder). It may accompany a USER route whatever the schema says.
SPOKEN_REMAINDER_KEY = "remainder"


def _type_ok(typ, value) -> bool:
    if typ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "bool":
        return isinstance(value, bool)
    if typ == "str":
        return isinstance(value, str)
    return False          # an unknown type name in a schema validates nothing


def validate_args(schema, args: dict, *, route: Route = Route.EXACT) -> Optional[tuple]:
    """Check ``args`` against ``schema``. Returns None when valid, else
    ``(reason, detail)`` with reason "unvalidated" or "invalid_args".

    * schema None / empty (undeclared): model routes -> "unvalidated". User
      routes may carry only the user's own non-empty spoken remainder; any
      other key -> "invalid_args".
    * NO_ARGS_SCHEMA: any argument at all -> "invalid_args".
    * a declared schema: missing required, extra keys (the spoken remainder
      is tolerated on user routes only), wrong types (no coercion), out of
      range, not in choices, and strings longer than max_len (default
      DEFAULT_MAX_STR_LEN) -> "invalid_args".
    """
    args = dict(args or {})
    user_route = route in USER_ROUTES
    declares_remainder = (isinstance(schema, (dict, MappingProxyType))
                          and SPOKEN_REMAINDER_KEY in schema)
    # The spoken remainder is set aside only when the schema does not name it;
    # a schema that declares "remainder" validates it like any other slot.
    spoken = (args.pop(SPOKEN_REMAINDER_KEY, None)
              if user_route and not declares_remainder else None)
    if spoken is not None and not isinstance(spoken, str):
        return "invalid_args", f"argument '{SPOKEN_REMAINDER_KEY}' must be a string"

    if schema is NO_ARGS_SCHEMA:
        if args or (spoken or "").strip():
            return "invalid_args", f"takes no arguments, got {sorted(args) or [SPOKEN_REMAINDER_KEY]}"
        return None

    if not isinstance(schema, (dict, MappingProxyType)) or not schema:
        if not user_route:
            return "unvalidated", "no declared argument schema"
        if args:
            return "invalid_args", f"undeclared arguments {sorted(args)}"
        return None

    extra = sorted(k for k in args if k not in schema)
    if extra:
        return "invalid_args", f"unexpected arguments {extra}"
    for name, spec in schema.items():
        if not isinstance(spec, dict):
            return "invalid_args", f"schema entry for '{name}' is not a mapping"
        if name not in args:
            if spec.get("required"):
                return "invalid_args", f"missing required argument '{name}'"
            continue
        value = args[name]
        typ = spec.get("type")
        if not _type_ok(typ, value):
            return "invalid_args", f"argument '{name}' must be a {typ}"
        if typ == "str":
            limit = spec.get("max_len", DEFAULT_MAX_STR_LEN)
            if len(value) > limit:
                return "invalid_args", f"argument '{name}' longer than {limit} characters"
        if "min" in spec and value < spec["min"]:
            return "invalid_args", f"argument '{name}'={value} below minimum {spec['min']}"
        if "max" in spec and value > spec["max"]:
            return "invalid_args", f"argument '{name}'={value} above maximum {spec['max']}"
        choices = spec.get("choices")
        if choices and value not in choices:
            return "invalid_args", f"argument '{name}'={value!r} not one of {list(choices)}"
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


def capture_generation(app) -> int:
    """The generation a NEW request must carry. Call it where the request is
    created (the utterance's capture/dispatch), not where it is evaluated."""
    return current_generation(app)


def is_fresh(app, generation) -> bool:
    """Strict freshness, as authorize() applies it: a missing, non-integer or
    non-current generation is stale."""
    return (isinstance(generation, int) and not isinstance(generation, bool)
            and generation == current_generation(app))


def is_current(app, generation: Optional[int]) -> bool:
    """Legacy predicate kept for the ask_ollama teaching records (alias /
    vocabulary / correction confirmations), which edit Samsara's own data,
    never reach authorize(), and were never generation-bound: None passes.
    Anything that causes an effect goes through authorize(), which uses
    is_fresh() and denies None."""
    return generation is None or is_fresh(app, generation)


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
    place a request can be waiting: the answer already being spoken (queue
    110), the staged confirmation, the AVA-session queue, the D3 waterfall
    queue, the scheduler. Drafts (staged dictation) are deliberately
    untouched. Returns what was cleared, for feedback.
    """
    # bump=False is for a caller that has ALREADY bumped under its own lock
    # (exit_ava_command_session) and only needs the clearing half.
    gen = bump_generation(app, reason) if bump else current_generation(app)
    cleared = {"generation": gen, "pending": False, "queued": 0, "in_flight": False,
               "schedule": False, "speech": False}

    # Queue 110. The stop also has to reach the ANSWER ALREADY BEING SPOKEN.
    # Until now stop_all cleared every queue and bumped the generation but
    # never touched TTS, so the owner could switch modes mid-answer, have the
    # turn correctly invalidated, and still sit through the rest of the
    # sentence. AudioCoordinator.speak(category="ava_response") is handed
    # interruptible=False, which only governs barge-in; cancel_speech() is the
    # explicit stop and cancels it regardless.
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        try:
            cleared["speech"] = bool(getattr(coordinator, "is_speaking", False))
            coordinator.cancel_speech()
        except Exception as exc:
            logger.debug("[POLICY] stop_all: speech not cancelled: %s", exc)

    # Staged confirmation (Ava pending action, incl. a Smart Actions dialog wait).
    try:
        from plugins.commands import ask_ollama  # noqa: PLC0415
        with ask_ollama._pending_action_lock:
            record = ask_ollama._pending_action
        cleared["pending"] = record is not None
        if isinstance(record, dict) and isinstance(record.get("op"), PendingOperation):
            # Reject (not just forget) so a blocked waiter -- the Smart
            # Actions dialog -- returns now and runs nothing.
            record["op"].cancel(app, reason, chip=False)
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
        _chip(app, "stopped", "warning")
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


# ---------------------------------------------------------------------------
# Confirmation wording: LOCAL templates + resolved values only
# ---------------------------------------------------------------------------

_CONFIRM_TEMPLATES = {
    "action2:close": "Close {target}?",
    "action2:open": "Open {target}?",
    "action2:focus": "Switch to {target}?",
}
_VALUE_MAX_LEN = 60
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_TEMPLATE_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _template_value(value) -> str:
    """One resolved argument value, made safe to speak: one line, no control
    characters, bounded length."""
    text = _CONTROL_CHARS.sub(" ", str(value))
    text = " ".join(text.split())
    return text if len(text) <= _VALUE_MAX_LEN else text[:_VALUE_MAX_LEN - 3].rstrip() + "..."


def _fill(template: str, args: dict) -> Optional[str]:
    """Substitute {name} fields from args; None if a field is unresolved.
    Deliberately not str.format: no attribute/index access into values."""
    missing = []

    def _sub(match):
        name = match.group(1)
        if name not in args:
            missing.append(name)
            return ""
        return _template_value(args[name])

    filled = _TEMPLATE_FIELD.sub(_sub, template)
    if missing or "{" in filled or "}" in filled:
        return None     # unresolved or non-simple placeholder: use the generic form
    return filled


def confirmation_prompt(command_id: str, args: Optional[dict] = None) -> str:
    """The question the user is asked before a NeedsConfirmation runs.

    Built ONLY from local templates (this module's table, or the plugin's
    registry-declared preview_template) and the resolved argument values.
    Model-supplied text never reaches it (Invocation.prompt is ignored).
    """
    cid = (command_id or "").strip().lower()
    args = dict(args or {})
    template = _CONFIRM_TEMPLATES.get(cid)
    if template is None and cid.startswith("key:"):
        return f"Press {_template_value(cid[4:])}?"
    if template is None and cid.startswith("smart_action:"):
        return f"Run {_template_value(cid[13:].replace('_', ' '))}?"
    if template is None:
        entry = _plugin_entry(cid) or {}
        metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        declared = metadata.get("preview_template") if "metadata" in entry else entry.get("preview_template")
        if isinstance(declared, str) and declared.strip() and declared.strip() != "unknown":
            template = declared.strip()
    if template is not None:
        filled = _fill(template, args)
        if filled:
            return filled
    return f"{_template_value(cid).capitalize()}?"


def _quiet_emit(app, inv, decision):
    """The no-op stand-in for _emit when a decision is only being asked
    about, not made (authorize(quiet=True))."""
    return None


def authorize(inv: Invocation, *, app=None, executor=None, confirmed: bool = False,
              quiet: bool = False):
    """Decide whether `inv` may cause its effect right now.

    ``confirmed=True`` means the user already answered a NeedsConfirmation
    for this exact invocation; the generation, allow-list and args checks
    still apply (a confirmation can be stale too).

    ``quiet=True`` asks the same question WITHOUT recording a decision: it is
    how CommandExecutor.ava_menu builds Ava's menu (queue 107), so what she is
    offered is decided by this function and cannot drift from what she may
    run. Nothing is staged or emitted; only a real invocation is logged.
    """
    cid = (inv.command_id or "").strip().lower()
    # One decision function; `quiet` only silences the record of it.
    emit = _quiet_emit if quiet else _emit

    # 1. Request identity: anything from a superseded request is dead, and a
    # request that never captured a generation has no identity at all.
    if not is_fresh(app, inv.generation):
        detail = ("no generation captured" if inv.generation is None
                  else f"generation {inv.generation} != {current_generation(app)}")
        d = Denied("stale", detail=detail)
        emit(app, inv, d)
        return d

    # 2. The id must resolve to something the registry knows.
    if not command_exists(cid, executor=executor, app=app):
        d = Denied("unknown_command", detail=cid)
        emit(app, inv, d)
        return d

    ex = executor if executor is not None else getattr(app, "command_executor", None)

    # 2b. Availability: an enabled pack, and a scope that is live HERE (queue
    # 107 / Astra F2). The matcher applies both, but only the spoken path goes
    # through the matcher -- a model proposal, a scheduled repeat and a
    # confirmed callback reach the effect without ever being matched, and used
    # to run past a restriction that blocked the same words spoken aloud. The
    # refusal is emitted like any other Denied, never silent.
    blocked = _availability_block(cid, executor=ex, app=app)
    if blocked:
        d = Denied(blocked[0], detail=blocked[1])
        emit(app, inv, d)
        return d

    command = (getattr(ex, "commands", None) or {}).get(cid, {})
    if command.get("method") == "repeat_last_command":
        last_id = getattr(app, "_last_command_name", None)
        last = getattr(app, "_last_command", None)
        target = (getattr(ex, "commands", None) or {}).get(last_id, {})
        if not isinstance(last_id, str) or not last or last_id == cid or target.get("method") == "repeat_last_command":
            d = Denied("nothing to repeat")
            emit(app, inv, d)
            return d
        # The repeat handler dispatches this stored command, not the repeat verb.
        if target and target != last:
            d = Denied("repeat target changed", detail=last_id)
            emit(app, inv, d)
            return d
        return authorize(Invocation(last_id, inv.args, inv.route, inv.generation,
                                    source_text=inv.source_text),
                         app=app, executor=ex, confirmed=confirmed, quiet=quiet)

    # Model routes see only what the registry DECLARES (undeclared = unknown).
    risk, _reversible, schema = classify(cid, executor=executor, app=app,
                                         declared_only=inv.route in MODEL_ROUTES)

    # 3. Arguments must satisfy the command's own schema; an undeclared schema
    # makes the tool unavailable to a model.
    err = validate_args(schema, inv.args, route=inv.route)
    if (err and err[0] == "unvalidated" and not inv.args and risk in (RISK_READ, RISK_UI)):
        # Queue 107 / Astra F1, narrowly: a proposal carrying NO arguments has
        # nothing to validate -- an ACTION line is the whole instruction (the
        # system prompt gives it no argument slot). Denying these made almost
        # every plugin offerable but unrunnable: "volume up" is declared safe
        # and takes no arguments, yet a model naming it could never run it.
        # Deliberately limited to read/ui, which the table below allows
        # without a question anyway: nothing write, destructive or
        # unclassified gains model reach from this -- those still read
        # "unvalidated" here.
        err = None
    if err:
        d = Denied(err[0], risk=risk, detail=err[1])
        emit(app, inv, d)
        return d

    # 4. A model may only ever name tools on the allow-list.
    if inv.route in MODEL_ROUTES and not model_may_call(cid, app=app, executor=executor, risk=risk):
        d = Denied("not_allowed_for_model", risk=risk, detail=cid)
        emit(app, inv, d)
        return d

    # 5. Already confirmed by the user for this invocation.
    if confirmed:
        a = Allowed("confirmed", risk=risk)
        emit(app, inv, a)
        return a

    # 6. The policy table.
    if risk in (RISK_READ, RISK_UI):
        a = Allowed("low_risk", risk=risk)
    elif risk == RISK_UNKNOWN and inv.route == Route.EXACT and cid in _SAFE_UNKNOWN_VERBS:
        a = Allowed("known_safe_verb", risk=_SAFE_UNKNOWN_VERBS[cid], hint="unknown metadata; known ui verb")
    elif risk == RISK_WRITE and inv.route in USER_ROUTES:
        a = Allowed("write_user_route", risk=risk)
    elif risk == RISK_DESTRUCTIVE and _reversible and inv.route == Route.EXACT:
        a = Allowed("undoable", risk=risk, hint="undoable")
    else:
        a = NeedsConfirmation(confirmation_prompt(cid, inv.args), risk=risk)
    emit(app, inv, a)
    return a


# ---------------------------------------------------------------------------
# Pending confirmation record (shared by voice yes/no and the Smart Actions
# dialog). Stored in ask_ollama's pending slot so "yes"/"ava cancel"/
# "scratch that" keep resolving ONE object.
# ---------------------------------------------------------------------------

CONFIRM_TTL_S = 30.0

#: Where an answer to a pending question came from. Only a user's own reply
#: (voice or the dialog button) can approve; a model can never say yes.
ANSWER_VOICE, ANSWER_DIALOG, ANSWER_MODEL = "voice", "dialog", "model"


def argument_hash(command_id: str, args: Optional[dict]) -> str:
    """Stable hash of what exactly was proposed (command + arguments)."""
    payload = json.dumps({"command": (command_id or "").strip().lower(), "args": args or {}},
                         sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PendingOperation:
    """The ONE staged invocation waiting for the user's answer.

    Record: ``op_id``, ``generation`` (captured by the request), ``arg_hash``,
    ``targets`` (bound target versions, re-read through ``target_probe`` at
    approval) and a ``CONFIRM_TTL_S`` monotonic ``deadline``. Single-use:
    the first resolution wins; approve() refuses (and cancels) a record that
    is expired, stale, superseded or whose targets moved, and refuses a
    model-sourced answer without consuming the record.

    ``wait()`` lets a blocking consumer (the Smart Actions dispatcher) sit on
    it while voice or a dialog resolves it.
    """

    def __init__(self, inv: Invocation, prompt: str, on_approve=None, on_reject=None, *,
                 app=None, targets: Optional[dict] = None,
                 target_probe: Optional[Callable[[], dict]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.invocation = inv
        self.prompt = prompt
        self.op_id = uuid.uuid4().hex
        self.generation = inv.generation
        self.arg_hash = argument_hash(inv.command_id, inv.args)
        self.targets = dict(targets or {})
        self._target_probe = target_probe
        self._app = app
        self._clock = clock
        self.deadline = clock() + CONFIRM_TTL_S
        # Wall-clock mirror for readers of the legacy pending-slot "expires" key.
        self.expires = time.time() + CONFIRM_TTL_S
        self.extended = False
        self.cancel_reason: Optional[str] = None
        #: Set by refusal() when the bound target moved, for the spoken reason.
        self.target_change: Optional[str] = None
        self._on_approve = on_approve
        self._on_reject = on_reject
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.approved: Optional[bool] = None
        self.always: bool = False

    # -- liveness ---------------------------------------------------------

    def is_expired(self) -> bool:
        return self._clock() > self.deadline

    def refusal(self, app=None) -> Optional[str]:
        """Why this record cannot be approved right now, or None if it can."""
        if self.approved is not None:
            return "already answered"
        if self.is_expired():
            return "expired"
        app = app if app is not None else self._app
        if app is not None and not is_fresh(app, self.generation):
            return "stale"
        if self._target_probe is not None:
            try:
                now = dict(self._target_probe() or {})
            except Exception as exc:
                logger.debug("[POLICY] target probe failed: %s", exc)
                return "target changed"
            if now != self.targets:
                # Queue 126/C: named, not just reported. The caller speaks
                # this, so "I did nothing" comes with the reason.
                self.target_change = describe_target_change(self.targets, now)
                return f"target changed: {self.target_change}"
        return None

    def is_live(self, app=None) -> bool:
        return self.refusal(app) is None

    def extend(self) -> bool:
        """"wait": push the deadline out once. False if already extended or dead."""
        with self._lock:
            if self.extended or self.approved is not None or self.is_expired():
                return False
            self.extended = True
            self.deadline = self._clock() + CONFIRM_TTL_S
            self.expires = time.time() + CONFIRM_TTL_S
        return True

    # -- resolution -------------------------------------------------------

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

    def approve(self, *, always: bool = False, source: str = ANSWER_VOICE, app=None) -> bool:
        if source not in (ANSWER_VOICE, ANSWER_DIALOG):
            logger.warning("[POLICY] refused a %s-sourced approval of %s", source,
                           self.invocation.command_id)
            return False                      # the record stays pending for the user
        why = self.refusal(app)
        if why is not None:
            if why != "already answered":
                self.cancel(app, why)
            return False
        return self.resolve(True, always=always)

    def reject(self) -> bool:
        return self.resolve(False)

    def cancel(self, app=None, reason: str = "cancelled", *, chip: bool = True) -> bool:
        """Reject with a visible reason ("superseded", "expired", "stale", ...)."""
        resolved = self.resolve(False)
        if resolved:
            self.cancel_reason = reason
            logger.info("[POLICY] pending %s %s cancelled: %s", self.op_id[:8],
                        self.invocation.command_id, reason)
            if chip:
                _chip(app if app is not None else self._app, f"cancelled: {reason}", "warning")
        return resolved

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)


#: Queue 126/C. Approval binds a TARGET, not just a command and arguments.
#:
#: PendingOperation has supported `targets`/`target_probe` since queue 107 and
#: NOTHING passed them (grepped: the only references were the definitions), so
#: `refusal()` skipped the target check on every real approval. Reproduced:
#: "Close the window?" asked while the bank was in front, the user alt-tabbed,
#: said yes, and the unsaved document was closed instead -- refusal() returned
#: None throughout.
#:
#: So a target is now resolved AT THE MOMENT THE QUESTION IS ASKED and
#: re-resolved when the answer arrives. A difference is refused and named. It
#: is never silently re-resolved, which would be the same bug with a tidier
#: implementation.
TARGET_ARG_NAMES = ("target", "app_name", "window", "label")


def resolve_target(name: str) -> Optional[dict]:
    """The identity of the window a spoken target names, or None when it
    cannot be resolved right now. Never raises: a probe that throws must not
    be able to approve something, and `bind_target` treats None as "gone"."""
    if not name:
        return None
    try:
        from plugins.commands.app_verbs import resolve_window  # noqa: PLC0415
        found = resolve_window(name)
    except Exception as exc:
        logger.debug("[POLICY] target resolve failed for %r: %s", name, exc)
        return None
    if not found:
        return None
    hwnd, title, process = (list(found) + [None, None, None])[:3]
    return {"hwnd": hwnd, "process": (process or "").lower()}


def bind_target(inv: Invocation, resolver=None):
    """(targets, probe) for an invocation that names a target, else (None, None).

    `targets` is what the question was asked about. `probe` re-reads it at
    approval; PendingOperation.refusal() compares the two and refuses on any
    difference -- including the target having disappeared, which the probe
    reports as {"gone": <name>} so it cannot compare equal to a real window.
    """
    name = None
    for key in TARGET_ARG_NAMES:
        value = (inv.args or {}).get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    if name is None:
        return None, None

    def _probe():
        # Looked up here, not bound as a default argument: the module-level
        # resolver is the seam tests and callers replace.
        fn = resolver if resolver is not None else resolve_target
        try:
            found = fn(name)
        except Exception as exc:
            # A probe that throws must REFUSE, never approve. {"gone": ...}
            # cannot compare equal to a resolved window, so the mismatch
            # check refuses on its own.
            logger.debug("[POLICY] target probe raised for %r: %s", name, exc)
            return {"gone": name}
        return found or {"gone": name}

    return _probe(), _probe


def describe_target_change(before: Optional[dict], after: Optional[dict]) -> str:
    """One sentence naming what moved, for the spoken refusal."""
    before, after = before or {}, after or {}
    if after.get("gone"):
        return f"{after['gone']} is not open any more"
    if before.get("gone"):
        return f"{before['gone']} was not open when I asked"
    if before.get("process") and after.get("process") and before["process"] != after["process"]:
        return f"that is {after['process']} now, not {before['process']}"
    return "a different window is in front now"


def stage_pending(app, inv: Invocation, prompt: str, *, on_approve=None, on_reject=None,
                  extra: Optional[dict] = None, record_type: str = "invocation",
                  targets: Optional[dict] = None,
                  target_probe: Optional[Callable[[], dict]] = None) -> PendingOperation:
    """Put THE pending operation into the shared pending slot. An older record
    is superseded: cancelled visibly ("cancelled: superseded") so a blocked
    waiter wakes up and a late "yes" cannot reach it.

    ``targets`` / ``target_probe``: the target versions the question was
    asked about (e.g. {"hwnd": 1234, "title_rev": 7}) and a callable that
    re-reads them; approval is refused if they differ. Queue 126/C: when the
    caller supplies neither and the invocation NAMES a target, they are
    derived here (``bind_target``) -- the support existed and no caller used
    it, which is how "close this window" closed a different one.

    ``record_type`` keeps the slot's existing vocabulary ("action" for a
    command id, "action2" for a verb+argument, "invocation" for anything
    else) so scratch-that / cancel / tests keep reading it; every record
    carries ``op`` and "yes" resolves that, whatever the type says.
    """
    if targets is None and target_probe is None:
        targets, target_probe = bind_target(inv)
    ex = getattr(app, "command_executor", None)
    table = getattr(ex, "commands", None) or {}
    if table.get(inv.command_id, {}).get("method") == "repeat_last_command":
        target_id = getattr(app, "_last_command_name", None)
        snapshot = copy.deepcopy(getattr(app, "_last_command", None))
        target_inv = Invocation(target_id, inv.args, inv.route, inv.generation,
                                source_text=inv.source_text)

        def approve_repeat(op):
            # Bind yes to the history we prompted for, and dispatch the target
            # directly so the repeat method cannot drop route/confirmation.
            if (getattr(app, "_last_command_name", None) != target_id or
                    getattr(app, "_last_command", None) != snapshot or
                    (target_id in table and table[target_id] != snapshot)):
                _emit(app, inv, Denied("repeat target changed"))
                return
            decision = authorize(target_inv, app=app, executor=ex, confirmed=True)
            if not isinstance(decision, Allowed):
                return
            if target_id in table:
                ex.execute_command(target_id, app, route=inv.route,
                                   generation=inv.generation, confirmed=True, args=inv.args)
            else:
                entry = _plugin_entry(target_id)
                if entry is not None:
                    entry["handler"](app, inv.args.get("remainder", ""))

        on_approve = approve_repeat
    op = PendingOperation(inv, prompt, on_approve=on_approve, on_reject=on_reject,
                          app=app, targets=targets, target_probe=target_probe)
    record = {
        "type": record_type,
        "op": op,
        "op_id": op.op_id,
        "confirm_text": prompt,
        "generation": inv.generation,
        "arg_hash": op.arg_hash,
        "targets": dict(op.targets),
        "expires": op.expires,
        "original_text": inv.source_text,
        "command": inv.command_id,
    }
    if extra:
        record.update(extra)
    _install_record(app, record)
    return op


def _install_record(app, record: dict) -> None:
    """Put `record` in the shared pending slot. A previous yes/no question is
    superseded (cancelled visibly); a previous CANCEL WINDOW is flushed -- run
    now -- because the user moving on to the next command without saying "no"
    is not a cancel (queue 69)."""
    try:
        from plugins.commands import ask_ollama  # noqa: PLC0415
        with ask_ollama._pending_action_lock:
            old = ask_ollama._pending_action
            ask_ollama._pending_action = record
        old_op = old.get("op") if isinstance(old, dict) else None
        if isinstance(old_op, TimedOperation) and old_op.approved is None and not old_op.converted:
            old_op.run_now("the next command arrived", release=False)
        elif isinstance(old_op, PendingOperation):
            old_op.cancel(app, "superseded")
        elif isinstance(old, dict):
            _chip(app, "cancelled: superseded", "warning")
    except Exception as exc:
        logger.debug("[POLICY] pending slot unavailable: %s", exc)


def pending_operation() -> Optional[PendingOperation]:
    """The PendingOperation in the shared slot, if any (live or not)."""
    try:
        from plugins.commands import ask_ollama  # noqa: PLC0415
        with ask_ollama._pending_action_lock:
            record = ask_ollama._pending_action
    except Exception:
        return None
    op = record.get("op") if isinstance(record, dict) else None
    return op if isinstance(op, PendingOperation) else None


def _release_slot(op: PendingOperation) -> None:
    try:
        from plugins.commands import ask_ollama  # noqa: PLC0415
        with ask_ollama._pending_action_lock:
            record = ask_ollama._pending_action
            if isinstance(record, dict) and record.get("op") is op:
                ask_ollama._pending_action = None
    except Exception as exc:
        logger.debug("[POLICY] pending slot release: %s", exc)


# ---------------------------------------------------------------------------
# Replies to the pending question: complete utterances only
# ---------------------------------------------------------------------------

REPLY_YES, REPLY_NO, REPLY_WAIT = "yes", "no", "wait"

#: The confirmation phrases (ask_ollama's "yes" command and its aliases).
CONFIRM_UTTERANCES = frozenset({
    "yes", "yeah", "yep", "yup", "sure", "confirm", "confirm it",
    "do it", "go ahead", "yeah do it", "yes please",
})
REJECT_UTTERANCES = frozenset({"no", "nope", "no thanks", "don't", "do not", "no don't"})
WAIT_UTTERANCES = frozenset({"wait", "hold on", "wait a moment", "wait a second", "one moment"})

# Quotation marks of any style: a quoted "yes" is payload, never an answer.
_QUOTES = "\"'\u2018\u2019\u201c\u201d\u00ab\u00bb`"
_REPLY_TRIM = ".,!?;: \t\r\n"


def _normalize_reply(text: str) -> str:
    return " ".join((text or "").lower().split()).strip(_REPLY_TRIM)


def classify_reply(text: str) -> Optional[str]:
    """REPLY_YES / REPLY_NO / REPLY_WAIT when ``text`` is, as a COMPLETE
    utterance, an answer to a pending question; else None. A reply inside a
    longer sentence, or wrapped in quotation marks, is not an answer."""
    raw = (text or "").strip()
    if not raw or any(q in raw for q in _QUOTES if q != "'") or raw[:1] == "'" or raw[-1:] == "'":
        return None
    normalized = _normalize_reply(raw)
    if normalized in CONFIRM_UTTERANCES:
        return REPLY_YES
    if normalized in REJECT_UTTERANCES:
        return REPLY_NO
    if normalized in WAIT_UTTERANCES:
        return REPLY_WAIT
    return None


def mentions_confirmation(text: str) -> bool:
    """True when ``text`` starts with a confirmation phrase but is NOT a
    complete-utterance yes (e.g. 'yes but not now', a quoted 'yes')."""
    normalized = _normalize_reply((text or "").replace("\u201c", " ").replace("\u201d", " ")
                                  .strip(_QUOTES + " "))
    if classify_reply(text) == REPLY_YES:
        return False
    return any(normalized == p or normalized.startswith(p + " ") for p in CONFIRM_UTTERANCES)


def answer_pending(app, text: str, *, source: str = ANSWER_VOICE) -> Optional[str]:
    """Apply a reply to the live pending question.

    Returns None when ``text`` is not a complete-utterance reply or nothing
    is pending; otherwise one of "approved", "rejected", "extended",
    "refused:<why>" (stale / expired / target changed / already answered /
    model / wait already used). Only this function and the dialog buttons
    resolve a pending operation."""
    op = pending_operation()
    if isinstance(op, (TimedOperation, ChoiceOperation)):
        return answer_cancel_window(app, text, source=source)
    reply = classify_reply(text)
    if reply is None or op is None:
        return None
    if source not in (ANSWER_VOICE, ANSWER_DIALOG):
        logger.warning("[POLICY] ignored a %s-sourced %r for pending %s", source, reply, op.op_id[:8])
        return "refused:model"
    if reply == REPLY_WAIT:
        if op.is_live(app) and op.extend():
            try:
                from plugins.commands import ask_ollama  # noqa: PLC0415
                with ask_ollama._pending_action_lock:
                    if isinstance(ask_ollama._pending_action, dict) and ask_ollama._pending_action.get("op") is op:
                        ask_ollama._pending_action["expires"] = op.expires
            except Exception:
                pass
            _chip(app, "waiting", "pending")
            return "extended"
        return "refused:wait already used" if op.extended else f"refused:{op.refusal(app) or 'not live'}"
    if reply == REPLY_NO:
        _release_slot(op)
        op.cancel(app, "declined")
        return "rejected"
    why = op.refusal(app)
    _release_slot(op)
    if why is not None:
        op.cancel(app, why)
        return f"refused:{why}"
    return "approved" if op.approve(app=app) else "refused:already answered"


# ---------------------------------------------------------------------------
# Cancel window (queue 69): a third state between "run it" and "ask yes/no"
# ---------------------------------------------------------------------------
#
# No comparable tool solved command-vs-dictation with a classifier (queue 66);
# they make a wrong execution cheap instead. A non-read command recognised
# INSIDE the dictation lane is staged here for a short window, shown on the
# chip as "running <X>... say no", and runs when the window elapses unless:
#
#   * the user says a cancel word ("no", "cancel", "stop" ...) as a complete
#     utterance -> cancelled;
#   * the user starts speaking inside the window -> the command is HELD until
#     that utterance is decided: a cancel word cancels, dictation cancels
#     ("you kept talking" -- the shadow log's false positives were followed by
#     more dictation 5/7 times within 3 s, real commands 0/37), anything else
#     runs it;
#   * "yes" runs it now; "wait" turns it into an ordinary yes/no question;
#   * a stop bumped the generation, the session ended, or the foreground
#     window changed -> refused, never run.
#
# Read commands never get a window; destructive ones keep their spoken yes/no
# (queue 63) and unknown-risk ones keep the NeedsConfirmation path. Only the
# dictation lane opens windows; ordinary dictation never touches this code.
#
# Default 3.0 s, from the measurement in reports/69: a short utterance takes a
# median 1.78 s (p90 2.41 s) from speech onset to dispatch, so a window cannot
# wait for a cancel to be DECIDED; it only has to see it START (the hold), and
# 3.0 s leaves room to notice the chip and begin speaking.

CANCEL_WINDOW_DEFAULT_S = 3.0
CANCEL_WINDOW_MAX_S = 5.0
#: Longest a held window waits for the utterance that started inside it.
CANCEL_HOLD_MAX_S = 6.0
#: How long a numbered choice stays open.
CHOICE_TTL_S = 12.0
#: Complete-utterance cancel words while a window is open.
CANCEL_WINDOW_UTTERANCES = frozenset({
    "no", "nope", "no thanks", "don't", "do not", "no don't", "no no",
    "cancel", "cancel that", "stop", "stop that", "stop it",
})
#: Session outcomes that mean "the user kept dictating".
DICTATION_OUTCOMES = frozenset({"dictate_staged", "dictate_injected"})
#: Outcomes that do not resolve a held window (the reply itself, the window's
#: own staging).
_NEUTRAL_OUTCOMES = frozenset({"pending_reply", "command_cancel_window"})


def cancel_window_settings(app) -> tuple:
    """(seconds, applies_to_everyday_words). seconds 0 = off."""
    cfg = {}
    try:
        cfg = (getattr(app, "config", {}) or {}).get("command_mode", {}) or {}
    except Exception:
        cfg = {}
    raw = cfg.get("cancel_window_s", CANCEL_WINDOW_DEFAULT_S)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = CANCEL_WINDOW_DEFAULT_S
    if seconds != seconds or seconds < 0:          # NaN / negative
        seconds = CANCEL_WINDOW_DEFAULT_S
    seconds = min(seconds, CANCEL_WINDOW_MAX_S)
    return seconds, bool(cfg.get("cancel_window_all_commands", False))


def cancel_window_for(command_id: str, app, *, executor=None, reserved: bool = False) -> float:
    """Seconds of cancel window for a command recognised in the dictation
    lane, or 0 for none. `reserved`: matched as one of the lane's curated
    everyday words (submit, enter, next tab, focus ...), which only get a
    window when the user extends it to them."""
    seconds, everyday = cancel_window_settings(app)
    if seconds <= 0 or (reserved and not everyday):
        return 0.0
    try:
        risk, reversible, _schema = classify(command_id, executor=executor, app=app)
    except Exception as exc:
        logger.debug("[POLICY] cancel window: classify(%s) failed: %s", command_id, exc)
        return 0.0
    if risk in (RISK_UI, RISK_WRITE) or (risk == RISK_DESTRUCTIVE and reversible):
        return seconds
    # read: immediate. destructive (not undoable) / unknown: the yes/no path.
    return 0.0


class _SpeechEdges:
    """Speech onset / utterance dispatch edges, for holding a window. Edges
    are ordered by a counter, not a clock: two edges inside one Windows clock
    tick (~15 ms) must still compare in the order they happened."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.last_onset = 0
        self.last_dispatch_start = 0


_speech = _SpeechEdges()


def _foreground_target() -> dict:
    try:
        import ctypes  # noqa: PLC0415
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        return {"hwnd": hwnd} if hwnd else {}
    except Exception:
        return {}


class TimedOperation(PendingOperation):
    """A cancel window in the shared pending slot. Runs itself when the window
    elapses; see the section comment above."""

    kind = "cancel_window"

    def __init__(self, inv: Invocation, label: str, delay_s: float, run, *, app=None,
                 targets: Optional[dict] = None, target_probe: Optional[Callable[[], dict]] = None,
                 clock: Callable[[], float] = time.monotonic, timer_factory=None):
        super().__init__(inv, f"running {label}", on_approve=lambda _op: run(), app=app,
                         targets=targets, target_probe=target_probe, clock=clock)
        self.label = label
        self.delay_s = float(delay_s)
        self.deadline = clock() + self.delay_s
        self.expires = time.time() + self.delay_s
        self.held = False
        self.converted = False            # "wait" turned it into a yes/no
        self.outcome_reason: Optional[str] = None
        self._timer = None
        self._timer_factory = timer_factory

    # A window's deadline is when it RUNS, not when it becomes unanswerable.
    def is_expired(self) -> bool:
        if self.converted:
            return self._clock() > self.deadline
        return self._clock() > self.deadline + CANCEL_HOLD_MAX_S + 1.0

    def _arm(self, seconds: float, fn) -> None:
        self._disarm()
        factory = self._timer_factory
        if factory is None:
            from samsara.runtime import thread_registry  # noqa: PLC0415
            factory = lambda s, f: thread_registry.timer("policy.cancel_window", s, f, daemon=True)  # noqa: E731
        self._timer = factory(max(0.0, seconds), fn)

    def _disarm(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def start(self) -> None:
        self._arm(self.delay_s, self._elapsed)

    def _elapsed(self) -> None:
        with self._lock:
            if self.approved is not None or self.held or self.converted:
                return
        self.run_now("window elapsed")

    def hold(self) -> bool:
        """Speech started inside the window: wait for that utterance."""
        with self._lock:
            if self.approved is not None or self.held or self.converted:
                return False
            self.held = True
            self.deadline = max(self.deadline, self._clock() + CANCEL_HOLD_MAX_S)
        self._arm(CANCEL_HOLD_MAX_S, self._hold_expired)
        logger.info("[POLICY] cancel window %s held: speech started inside it", self.invocation.command_id)
        return True

    def _hold_expired(self) -> None:
        with self._lock:
            if self.approved is not None or not self.held:
                return
        _release_slot(self)
        self.cancel(self._app, "no answer heard in time")

    def run_now(self, reason: str, *, release: bool = True) -> bool:
        if release:
            _release_slot(self)
        why = self.refusal(self._app)
        if why is not None:
            if why != "already answered":
                self.cancel(self._app, why)
            return False
        self.outcome_reason = reason
        logger.info("[POLICY] cancel window ran %s (%s) after %.2fs", self.invocation.command_id,
                    reason, self.delay_s)
        return self.resolve(True)

    def convert_to_question(self) -> bool:
        """"wait": stop the clock; ask yes/no with the ordinary TTL."""
        with self._lock:
            if self.approved is not None or self.converted:
                return False
            self.converted = True
            self.held = False
            self.deadline = self._clock() + CONFIRM_TTL_S
            self.expires = time.time() + CONFIRM_TTL_S
        self._disarm()
        return True

    def resolve(self, approved: bool, *, always: bool = False) -> bool:
        self._disarm()
        return super().resolve(approved, always=always)

    def cancel(self, app=None, reason: str = "cancelled", *, chip: bool = True) -> bool:
        resolved = self.resolve(False)
        if resolved:
            self.cancel_reason = reason
            logger.info("[POLICY] cancel window %s cancelled: %s", self.invocation.command_id, reason)
            if chip:
                _chip(app if app is not None else self._app, f"cancelled {self.label}: {reason}", "warning")
        return resolved


def stage_cancel_window(app, inv: Invocation, label: str, delay_s: float, run, *,
                        bind_foreground: bool = True, timer_factory=None) -> TimedOperation:
    """Stage `run` behind a cancel window of `delay_s` seconds. `run()` is
    called at most once, on the thread that resolves the window."""
    targets, probe = None, None
    if bind_foreground:
        targets = _foreground_target()
        if targets:
            probe = _foreground_target
    op = TimedOperation(inv, label, delay_s, run, app=app, targets=targets, target_probe=probe,
                        timer_factory=timer_factory)
    record = {
        "type": "cancel_window",
        "op": op,
        "op_id": op.op_id,
        "confirm_text": op.prompt,
        "generation": inv.generation,
        "arg_hash": op.arg_hash,
        "targets": dict(op.targets),
        "expires": op.expires,
        "original_text": inv.source_text,
        "command": inv.command_id,
    }
    _install_record(app, record)
    with _speech.lock:
        speaking = _speech.last_onset > _speech.last_dispatch_start > 0
    logger.info("[POLICY] cancel window %.2fs: %s risk-window op=%s%s", op.delay_s, inv.command_id,
                op.op_id[:8], " (held: speech already under way)" if speaking else "")
    if speaking:
        op.hold()
    else:
        op.start()
    return op


class ChoiceOperation(PendingOperation):
    """Numbered alternatives ("1 X · 2 Y -- say a number") when two commands
    match near-equally: the user picks, the app never guesses."""

    kind = "choice"

    def __init__(self, inv: Invocation, options: list, *, app=None,
                 clock: Callable[[], float] = time.monotonic, timer_factory=None):
        labels = [str(label) for label, _run in options]
        super().__init__(inv, "choose: " + " / ".join(labels), app=app, clock=clock)
        self.options = list(options)
        self.chosen: Optional[int] = None
        self.deadline = clock() + CHOICE_TTL_S
        self.expires = time.time() + CHOICE_TTL_S
        self._timer = None
        factory = timer_factory
        if factory is None:
            from samsara.runtime import thread_registry  # noqa: PLC0415
            factory = lambda s, f: thread_registry.timer("policy.choice", s, f, daemon=True)  # noqa: E731
        self._timer = factory(CHOICE_TTL_S, self._timed_out)

    def _timed_out(self) -> None:
        if self.approved is None:
            _release_slot(self)
            self.cancel(self._app, "no choice made")

    def choose(self, number: int) -> bool:
        if not 1 <= number <= len(self.options) or self.approved is not None:
            return False
        why = self.refusal(self._app)
        _release_slot(self)
        if why is not None:
            self.cancel(self._app, why)
            return False
        self.chosen = number
        _label, run = self.options[number - 1]
        self._on_approve = lambda _op: run()
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
        logger.info("[POLICY] choice %d of %d: %s", number, len(self.options), _label)
        return self.resolve(True)


def choice_chip_label(options: list) -> str:
    parts = [f"{i} {label}" for i, (label, _run) in enumerate(options, start=1)]
    return " · ".join(parts) + " -- say a number"


def stage_choice(app, inv: Invocation, options: list, *, timer_factory=None) -> ChoiceOperation:
    """Offer 2..9 numbered alternatives [(label, run), ...]."""
    if not 2 <= len(options) <= 9:
        raise ValueError("a choice needs 2 to 9 options")
    op = ChoiceOperation(inv, options, app=app, timer_factory=timer_factory)
    _install_record(app, {"type": "choice", "op": op, "op_id": op.op_id, "confirm_text": op.prompt,
                          "generation": inv.generation, "arg_hash": op.arg_hash, "targets": {},
                          "expires": op.expires, "original_text": inv.source_text,
                          "command": inv.command_id})
    _chip(app, choice_chip_label(options), "pending")
    logger.info("[POLICY] choice staged: %s", " / ".join(str(l) for l, _r in options))
    return op


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "won": 1, "to": 2, "too": 2, "for": 4}


def _spoken_number(text: str) -> Optional[int]:
    norm = _normalize_reply(text)
    for prefix in ("number ", "option "):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    if norm.isdigit() and len(norm) == 1:
        return int(norm)
    return _NUMBER_WORDS.get(norm)


def answer_cancel_window(app, text: str, *, source: str = ANSWER_VOICE) -> Optional[str]:
    """A complete-utterance reply to an open cancel window or choice; None
    when nothing of that kind is open or `text` is not a reply to it (the
    utterance then goes on to its lane as usual). Wired as the session's
    pending_reply_fn, so it runs before any lane interprets the words."""
    op = pending_operation()
    if not isinstance(op, (TimedOperation, ChoiceOperation)) or op.approved is not None:
        return None
    if source not in (ANSWER_VOICE, ANSWER_DIALOG):
        logger.warning("[POLICY] ignored a %s-sourced reply to %s", source, op.op_id[:8])
        return "refused:model"
    raw = (text or "").strip()
    if not raw or any(q in raw for q in _QUOTES if q != "'"):
        return None
    normalized = _normalize_reply(raw)
    if isinstance(op, ChoiceOperation):
        number = _spoken_number(raw)
        if number is not None:
            return "approved" if op.choose(number) else "refused:not an option"
        if normalized in CANCEL_WINDOW_UTTERANCES:
            _release_slot(op)
            op.cancel(app, "you said no")
            return "rejected"
        return None
    if normalized in CANCEL_WINDOW_UTTERANCES:
        _release_slot(op)
        op.cancel(app, "you said no")
        return "rejected"
    reply = classify_reply(raw)
    if reply == REPLY_YES:
        return "approved" if op.run_now("you said yes") else f"refused:{op.cancel_reason or 'not live'}"
    if reply == REPLY_WAIT and not op.converted:
        if op.convert_to_question():
            _chip(app, f"{op.label}? yes or no", "pending")
            return "extended"
    return None


def flush_cancel_window(reason: str = "the next command arrived") -> bool:
    """Run an open (not converted) cancel window now. Called before a command
    that does NOT get a window is dispatched in the lane, so two commands
    still run in the order they were said."""
    op = pending_operation()
    if not isinstance(op, TimedOperation) or op.approved is not None or op.converted:
        return False
    return op.run_now(reason)


def note_utterance_start() -> None:
    """The session began dispatching an utterance (queue 69 hold bookkeeping)."""
    with _speech.lock:
        _speech.seq += 1
        _speech.last_dispatch_start = _speech.seq


def note_speech_onset(app=None) -> None:
    """A fresh speech onset: holds an open cancel window until that utterance
    is decided. Never raises; costs a lock when nothing is open."""
    with _speech.lock:
        _speech.seq += 1
        _speech.last_onset = _speech.seq
    op = pending_operation()
    if isinstance(op, TimedOperation) and op.approved is None and not op.converted:
        op.hold()


def after_utterance(app, outcome_kind: str) -> Optional[str]:
    """Resolve a HELD window by what the utterance inside it turned out to be.
    Returns "cancelled" / "ran" / None (nothing held, or a neutral outcome)."""
    op = pending_operation()
    if not isinstance(op, TimedOperation) or op.approved is not None or not op.held or op.converted:
        return None
    if outcome_kind in _NEUTRAL_OUTCOMES:
        return None
    if outcome_kind in DICTATION_OUTCOMES:
        _release_slot(op)
        op.cancel(app, "you kept talking")
        return "cancelled"
    return "ran" if op.run_now(f"next utterance was {outcome_kind}") else None
