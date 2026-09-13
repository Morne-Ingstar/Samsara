"""Canonical command table (SAMSARA_MAP Move B, prompt 09a).

One machine-readable record per registered command -- built-in
(commands.json) or plugin (plugins/commands/*.py) -- describing what it is,
what it takes and what every spoken phrase maps to. Generated from the LIVE
registry (the production CommandExecutor, loaded the same way
tools/dump_command_metadata.py loads it -- no dictation.py import), never
hand-maintained. No behaviour change: nothing in the app reads this file.

    CommandSpec
        canonical_id     "<plugin>.<verb>_<object>" -- stable: the plugin's
                         module stem (or "builtin") + the registry-normalised
                         canonical phrase with spaces as underscores
        plugin           module stem ("windows", "ask_ollama", ...) or "builtin"
        verb / object    first token of the canonical phrase / the rest
        args             [ArgSpec(name, type, required)] -- declared
                         param_schema first, else inferred (see infer_args)
        aliases          every phrase the registry maps to this command,
                         normalised the way the registry normalises (view_tokens)
        description      handler docstring first sentence, else the
                         commands.json description, else the phrase
        risk             read | ui | write | destructive (see classify_risk)
        undoable         best guess (see guess_undoable)
        source           "path:line" of the handler def / the commands.json key
        whole_utterance  the phrase is a reserved whole-utterance control word
                         in samsara.session_modes
        pack, kind       command pack; "builtin" | "plugin"

Risk precedence: declared registry risk_class (safe->ui, reversible->write,
destructive->destructive, read/ui/write as written) -> built-in type+keys via
samsara.execution_policy.classify_builtin -> verb heuristic (show/list/read
-> read; switch/move/snap/volume -> ui; type/paste/send/save -> write;
close/delete/kill/quit/shutdown -> destructive) -> "ui".
"""
from __future__ import annotations

import inspect
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent

ARG_TYPES = ("int", "nato_letter", "app_name", "monitor", "side", "playlist", "text", "none")
RISKS = ("read", "ui", "write", "destructive")
KINDS = ("builtin", "plugin")

# JSON schema for commands_catalog.json (validated by tests with jsonschema;
# validate_catalog() below checks the same shape without the dependency).
CATALOG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Samsara command catalog",
    "type": "object",
    "required": ["version", "commands"],
    "additionalProperties": False,
    "properties": {
        "version": {"type": "integer", "const": 1},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["canonical_id", "plugin", "verb", "object", "args", "aliases", "description",
                             "risk", "undoable", "source", "whole_utterance", "pack", "kind"],
                "additionalProperties": False,
                "properties": {
                    "canonical_id": {"type": "string", "pattern": r"^[a-z0-9_]+\.[a-z0-9_]+$"},
                    "plugin": {"type": "string", "minLength": 1},
                    "verb": {"type": "string", "minLength": 1},
                    "object": {"type": "string"},
                    "args": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "type", "required"],
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string", "minLength": 1},
                                "type": {"type": "string", "enum": list(ARG_TYPES)},
                                "required": {"type": "boolean"},
                            },
                        },
                    },
                    "aliases": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                    "description": {"type": "string", "minLength": 1},
                    "risk": {"type": "string", "enum": list(RISKS)},
                    "undoable": {"type": "boolean"},
                    "source": {"type": "string", "minLength": 1},
                    "whole_utterance": {"type": "boolean"},
                    "pack": {"type": "string", "minLength": 1},
                    "kind": {"type": "string", "enum": list(KINDS)},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class ArgSpec:
    name: str
    type: str
    required: bool


@dataclass
class CommandSpec:
    canonical_id: str
    plugin: str
    verb: str
    object: str
    args: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    description: str = ""
    risk: str = "ui"
    undoable: bool = True
    source: str = ""
    whole_utterance: bool = False
    pack: str = "core"
    kind: str = "plugin"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["args"] = [asdict(a) if not isinstance(a, dict) else a for a in self.args]
        d["aliases"] = sorted(set(self.aliases))
        return d


# ---------------------------------------------------------------------------
# Normalisation -- the registry's own view
# ---------------------------------------------------------------------------

def normalize_phrase(phrase: str) -> str:
    """Exactly what the matcher compares against: lowercase, non-word
    characters stripped per token (samsara.command_registry.view_tokens)."""
    from samsara.command_registry import view_tokens  # noqa: PLC0415
    return " ".join(tok for tok, _s, _e in view_tokens(phrase or ""))


def slug(phrase: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize_phrase(phrase)).strip("_")


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------

_VERB_RISK = {
    "read": {"show", "list", "read", "what", "where", "which", "find", "check", "status", "is", "how", "who"},
    "ui": {"switch", "move", "snap", "volume", "focus", "scroll", "go", "next", "previous", "back", "open",
           "minimize", "minimise", "maximize", "maximise", "restore", "mute", "unmute", "pause", "play",
           "resume", "bring", "cursor", "mouse", "pointer", "zoom", "select", "page", "tab", "hide", "refresh",
           "toggle", "louder", "quieter", "turn", "increase", "decrease", "silence", "unsilence"},
    "write": {"type", "paste", "send", "save", "insert", "dictate", "new", "undo", "redo", "cut", "copy",
              "enter", "submit", "add", "complete", "take", "set", "put", "tell", "ask", "message", "record",
              "start", "mark", "log", "note", "remind", "schedule", "voice", "write", "correct", "teach"},
    "destructive": {"close", "delete", "kill", "quit", "shutdown", "shut", "exit", "remove", "forget", "clear",
                    "stop", "cancel", "restart", "reboot", "lock", "sign", "log"},
}
# "log" appears in both write and destructive lists above; the write sense
# ("log a note") wins -- see classify_risk ordering.

_DECLARED_RISK = {"safe": "ui", "reversible": "write", "destructive": "destructive",
                  "read": "read", "ui": "ui", "write": "write"}


def classify_risk(kind: str, phrase: str, declared: Optional[str], builtin_data: Optional[dict]) -> str:
    """See the module docstring for precedence."""
    if declared and str(declared).lower() in _DECLARED_RISK:
        return _DECLARED_RISK[str(declared).lower()]
    if kind == "builtin" and builtin_data is not None:
        try:
            from samsara.execution_policy import classify_builtin  # noqa: PLC0415
            risk = classify_builtin(builtin_data)
            if risk in RISKS:
                return risk
        except Exception:
            pass
    verb = (normalize_phrase(phrase).split() or [""])[0]
    for risk in ("read", "write", "ui", "destructive"):
        if verb in _VERB_RISK[risk]:
            return risk
    return "ui"


_UNDOABLE_WRITE_VERBS = {"type", "paste", "insert", "dictate", "undo", "redo", "new", "add", "mark", "note", "log"}


def guess_undoable(risk: str, phrase: str) -> bool:
    verb = (normalize_phrase(phrase).split() or [""])[0]
    if risk in ("read", "ui"):
        return True
    if risk == "write":
        return verb in _UNDOABLE_WRITE_VERBS
    return verb == "close"      # a closed window can be reopened; deletes cannot


# Phrase -> argument spec for handlers that consume a remainder without a
# declared param_schema. The registry is remainder-tolerant for every plugin
# phrase, so only handlers whose BODY reads `remainder` are given an argument.
_ARG_HINTS = {
    "send": [("app_name", "app_name", False), ("monitor", "monitor", True)],
    "bring": [("app_name", "app_name", False)],
    "cursor to": [("monitor", "monitor", True)],
    "find window": [("app_name", "app_name", True)],
    "save layout": [("name", "text", False)],
    "restore layout": [("name", "text", False)],
    "delete layout": [("name", "text", True)],
    "snap": [("side", "side", True)],
    "focus": [("app_name", "app_name", True)],
    "open": [("app_name", "app_name", True)],
    "close": [("app_name", "app_name", True)],
    "click": [("label", "int", True)],
    "window switch": [("label", "nato_letter", True)],
    "window bring": [("label", "nato_letter", True)],
    "window move": [("label", "nato_letter", True)],
    "window close": [("label", "nato_letter", True)],
    "window mute": [("label", "nato_letter", True)],
    "window unmute": [("label", "nato_letter", True)],
    "window copy": [("label", "nato_letter", True)],
    "window tile": [("labels", "nato_letter", True)],
    "cube copy": [("label", "nato_letter", True)],
    "cube tile": [("labels", "nato_letter", True)],
    "cube page": [("page", "int", False)],
    "play music": [("playlist", "playlist", False)],
    "set volume": [("level", "int", True)],
    "volume": [("level", "int", False)],
    "find tab": [("title", "text", True)],
    "go to": [("site", "text", True)],
    "claude message submit": [("draft_id", "text", True)],
}
_SCHEMA_TYPE = {"int": "int", "integer": "int", "float": "int", "str": "text", "string": "text",
                "bool": "text", "nato_letter": "nato_letter", "app_name": "app_name", "monitor": "monitor",
                "side": "side", "playlist": "playlist", "text": "text"}


def _handler_reads_remainder(func) -> bool:
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return False
    body = src.split("\n", 1)[1] if "\n" in src else ""
    # skip the decorator + def lines: count uses after the signature
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith(("@", "def "))]
    return sum(len(re.findall(r"\bremainder\b", ln)) for ln in lines) > 0


def infer_args(kind: str, phrase: str, param_schema: Optional[dict], func) -> list:
    if param_schema:
        out = []
        for name, spec in param_schema.items():
            spec = spec if isinstance(spec, dict) else {}
            out.append(ArgSpec(name, _SCHEMA_TYPE.get(str(spec.get("type", "text")).lower(), "text"),
                               bool(spec.get("required", False))))
        return out
    if kind == "builtin":
        return []
    norm = normalize_phrase(phrase)
    if norm in _ARG_HINTS:
        return [ArgSpec(n, t, r) for n, t, r in _ARG_HINTS[norm]]
    if func is not None and _handler_reads_remainder(func):
        return [ArgSpec("text", "text", False)]
    return []


# ---------------------------------------------------------------------------
# Reserved whole-utterance words (session_modes) and Whisper confusions
# ---------------------------------------------------------------------------

def reserved_whole_utterances() -> set:
    from samsara import session_modes as sm  # noqa: PLC0415
    words = set(sm._WHOLE_UTTERANCE_SWITCHES) | {sm.SCRATCH_THAT_PHRASE, sm.DICTATE_COMMIT_PHRASE}
    words |= set(sm._DICTATE_COMMIT_HOMOPHONES) | set(sm.GLOBAL_SESSION_EXIT_PHRASES)
    words |= set(getattr(sm, "SESSION_SLEEP_PHRASES", ())) | set(getattr(sm, "SESSION_STOP_PHRASES", ()))
    words |= set(sm.DEFAULT_AVA_INVOCATIONS)
    return {normalize_phrase(w) for w in words}


WHISPER_CONFUSIONS = [
    ("one", "won", "1"), ("to", "two", "too", "2"), ("for", "four", "4"), ("tab", "tap"),
    ("right", "write"), ("here", "hear"), ("new", "knew"), ("their", "there"), ("read", "red"),
    ("eight", "ate", "8"), ("three", "tree", "3"), ("six", "sicks", "6"), ("close", "clothes"),
    ("cube", "queue"), ("mute", "moot"), ("end", "and"), ("sent", "cent"), ("wait", "weight"),
]
_CONFUSION_INDEX = {}
for _group in WHISPER_CONFUSIONS:
    for _w in _group:
        _CONFUSION_INDEX.setdefault(_w, set()).update(x for x in _group if x != _w)


def confusable_aliases(specs: Iterable["CommandSpec"]) -> list:
    """(alias, token, look-alikes) for every alias containing a token Whisper
    is known to confuse -- even when the look-alike lands on no command (then
    the misrecognition is a silent miss rather than a wrong command)."""
    out = []
    for spec in specs:
        for a in spec.aliases:
            for tok in a.split():
                if tok in _CONFUSION_INDEX:
                    out.append((a, tok, tuple(sorted(_CONFUSION_INDEX[tok]))))
    return sorted(set(out))


def near_collisions(specs: Iterable["CommandSpec"]) -> list:
    """(phrase, confusable phrase, id_a, id_b) where swapping ONE token for a
    Whisper look-alike turns one command's alias into another's."""
    by_alias = {}
    for spec in specs:
        for a in spec.aliases:
            by_alias.setdefault(a, set()).add(spec.canonical_id)
    out = []
    for alias, ids in by_alias.items():
        toks = alias.split()
        for i, tok in enumerate(toks):
            for alt in _CONFUSION_INDEX.get(tok, ()):
                variant = " ".join(toks[:i] + [alt] + toks[i + 1:])
                for other in by_alias.get(variant, ()):
                    for me in ids:
                        if other != me and (variant, alias, other, me) not in out:
                            out.append((alias, variant, me, other))
    return sorted(set(out))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _first_sentence(doc: Optional[str]) -> str:
    text = " ".join((doc or "").strip().split())
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text).strip()


def _builtin_lines(commands_json: Path) -> dict:
    """commands.json key -> 1-based line of its opening line."""
    lines = {}
    for n, raw in enumerate(commands_json.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r'^    "(.+)": \{', raw)
        if m:
            lines.setdefault(json.loads('"' + m.group(1) + '"'), n)
    return lines


def _plugin_stem(entry) -> str:
    """Module stem of the handler ("windows" for plugins.commands.windows) --
    CommandEntry.source only says "plugin"; the module lives on the handler."""
    module = getattr(getattr(entry, "handler", None), "__module__", "") or ""
    return module.rsplit(".", 1)[-1] if module else "plugin"


def _rel(path: str) -> str:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def build_catalog(executor=None) -> list:
    """CommandSpec list from the live registry, sorted by canonical_id."""
    if executor is None:
        from tools.dump_command_metadata import build_executor  # noqa: PLC0415
        executor = build_executor()
    matcher = executor._matcher
    reserved = reserved_whole_utterances()
    builtin_lines = _builtin_lines(ROOT / "commands.json")

    seen = set()
    specs = []
    for entry in matcher._sorted:
        if id(entry) in seen:
            continue
        seen.add(id(entry))
        phrase = normalize_phrase(entry.phrase)
        kind = "builtin" if entry.source == "builtin" else "plugin"
        plugin = "builtin" if kind == "builtin" else _plugin_stem(entry)
        tokens = phrase.split()
        func = getattr(entry, "handler", None)
        data = getattr(entry, "data", None) if kind == "builtin" else None
        declared = (entry.metadata or {}).get("risk_class") if isinstance(entry.metadata, dict) else None
        if declared == "unknown":
            declared = None
        risk = classify_risk(kind, phrase, declared, data)
        description = _first_sentence(getattr(func, "__doc__", None)) if func is not None else ""
        description = description or _first_sentence(entry.description) or phrase
        if kind == "builtin":
            source = f"commands.json:{builtin_lines.get(entry.phrase, 0)}"
        else:
            try:
                source = f"{_rel(inspect.getsourcefile(func))}:{inspect.getsourcelines(func)[1]}"
            except (OSError, TypeError):
                source = f"{entry.source}"
        aliases = {phrase} | {normalize_phrase(a) for a in (entry.aliases or [])}
        aliases.discard("")
        specs.append(CommandSpec(
            canonical_id=f"{plugin}.{slug(phrase)}",
            plugin=plugin, verb=tokens[0] if tokens else phrase, object="_".join(tokens[1:]),
            args=infer_args(kind, phrase, entry.param_schema or None, func),
            aliases=sorted(aliases), description=description, risk=risk,
            undoable=guess_undoable(risk, phrase), source=source,
            whole_utterance=phrase in reserved, pack=entry.pack or "core", kind=kind,
        ))
    specs.sort(key=lambda s: s.canonical_id)
    return specs


def raw_phrase_claims(executor=None) -> dict:
    """Every phrase each source CLAIMS, before the registry resolved shadowing:
    {phrase: {canonical_id, ...}}. Built-ins claim their key; each plugin
    module claims its phrase + aliases (plugin_commands._MODULE_ENTRIES). A
    phrase claimed by two commands is a collision."""
    from samsara import plugin_commands  # noqa: PLC0415
    claims = {}
    for key in json.loads((ROOT / "commands.json").read_text(encoding="utf-8")).get("commands", {}):
        claims.setdefault(normalize_phrase(key), set()).add(f"builtin.{slug(key)}")
    for module, entries in plugin_commands._MODULE_ENTRIES.items():
        stem = module.rsplit(".", 1)[-1]
        for phrase, entry in entries.items():
            cid = f"{stem}.{slug(phrase)}"
            for p in [phrase, *entry.get("aliases", [])]:
                claims.setdefault(normalize_phrase(p), set()).add(cid)
    return claims


def collisions(claims: dict) -> list:
    return sorted((phrase, tuple(sorted(ids))) for phrase, ids in claims.items() if len(ids) > 1)


def orphans(claims: dict, specs: Iterable["CommandSpec"]) -> list:
    """Phrases a source claims that the LIVE registry maps to nothing: the
    collateral of a collision. When a plugin's canonical phrase is already
    taken (by a built-in, or by another plugin's alias), the matcher drops
    that whole command -- its other aliases vanish with it
    (samsara/command_registry.py load_plugins: "shadowed by built-in" /
    alias-first iteration). Listed as (phrase, (claiming_id,))."""
    live = set()
    for spec in specs:
        live.update(spec.aliases)
    return sorted((phrase, tuple(sorted(ids))) for phrase, ids in claims.items()
                  if len(ids) == 1 and phrase not in live)


def format_collisions(colls: list, orphaned: list = ()) -> str:
    lines = ["# Frozen by tests/test_command_catalog.py: a new line (or a vanished one) fails the test",
             "# until this file is regenerated on purpose (tools/gen_command_catalog.py --write-collisions).",
             "#",
             "# collision<TAB>phrase<TAB>canonical ids that claim it -- the registry keeps only one.",
             "# orphan<TAB>phrase<TAB>the one id that claims it -- the registry maps it to NOTHING,",
             "#   because that command's canonical phrase collided and the whole command",
             "#   (aliases included) was dropped at load time.", ""]
    lines += ["collision" + "\t" + phrase + "\t" + " ".join(ids) for phrase, ids in colls]
    lines += ["orphan" + "\t" + phrase + "\t" + " ".join(ids) for phrase, ids in orphaned]
    return "\n".join(lines) + "\n"


def parse_collisions(text: str) -> tuple:
    """-> (collisions, orphans), each sorted [(phrase, (ids...))]."""
    colls, orphaned = [], []
    for ln in text.splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        kind, phrase, ids = ln.split("\t", 2)
        (colls if kind == "collision" else orphaned).append((phrase, tuple(sorted(ids.split()))))
    return sorted(colls), sorted(orphaned)


# ---------------------------------------------------------------------------
# Serialisation + validation
# ---------------------------------------------------------------------------

def to_document(specs: list) -> dict:
    return {"version": 1, "commands": [s.to_dict() for s in specs]}


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def validate_catalog(doc: dict) -> list:
    """Dependency-free check of CATALOG_SCHEMA's shape; returns problems."""
    problems = []
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("commands"), list):
        return ["top level must be {version: 1, commands: [...]}"]
    required = CATALOG_SCHEMA["properties"]["commands"]["items"]["required"]
    seen_ids = set()
    for i, c in enumerate(doc["commands"]):
        missing = [k for k in required if k not in c]
        if missing:
            problems.append(f"commands[{i}] missing {missing}")
            continue
        if not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", c["canonical_id"]):
            problems.append(f"{c['canonical_id']}: bad canonical_id")
        if c["canonical_id"] in seen_ids:
            problems.append(f"{c['canonical_id']}: duplicate canonical_id")
        seen_ids.add(c["canonical_id"])
        if c["risk"] not in RISKS:
            problems.append(f"{c['canonical_id']}: risk {c['risk']!r}")
        if c["kind"] not in KINDS:
            problems.append(f"{c['canonical_id']}: kind {c['kind']!r}")
        if not c["aliases"] or not all(isinstance(a, str) and a for a in c["aliases"]):
            problems.append(f"{c['canonical_id']}: no alias")
        if not c["description"]:
            problems.append(f"{c['canonical_id']}: empty description")
        for a in c["args"]:
            if a.get("type") not in ARG_TYPES:
                problems.append(f"{c['canonical_id']}: arg type {a.get('type')!r}")
        for k in ("undoable", "whole_utterance"):
            if not isinstance(c[k], bool):
                problems.append(f"{c['canonical_id']}: {k} not bool")
    return problems


def render_markdown(specs: list) -> str:
    by_plugin = {}
    for s in specs:
        by_plugin.setdefault(s.plugin, []).append(s)
    n_alias = sum(len(s.aliases) for s in specs)
    lines = [
        "# Command catalog",
        "",
        "Generated by `tools/gen_command_catalog.py` from the live registry (commands.json + every plugin, "
        "loaded through `tools/dump_command_metadata.build_executor`). Do not edit; re-run the generator. "
        "`commands_catalog.json` is the machine-readable form; `samsara/command_catalog.py` defines the record and "
        "the schema. No code reads this file at runtime.",
        "",
        f"{len(specs)} commands, {n_alias} phrases, {len(by_plugin)} sources.",
        "",
        "Columns: canonical id; every phrase the registry maps to it; args as name:type (* = required); "
        "risk (read / ui / write / destructive); undoable; whole-utterance control word (session_modes); source.",
        "",
    ]
    for plugin in sorted(by_plugin):
        group = by_plugin[plugin]
        lines += [f"## {plugin} ({len(group)})", "",
                  "| canonical id | phrases | args | risk | undo | whole | description | source |",
                  "|---|---|---|---|---|---|---|---|"]
        for s in group:
            args = ", ".join(f"{a.name}:{a.type}{'*' if a.required else ''}" for a in s.args) or "-"
            phrases = ", ".join(f"`{a}`" for a in sorted(set(s.aliases)))
            desc = s.description.replace("|", "\\|")
            lines.append(f"| `{s.canonical_id}` | {phrases} | {args} | {s.risk} | {'yes' if s.undoable else 'no'} | "
                         f"{'yes' if s.whole_utterance else ''} | {desc} | `{s.source}` |")
        lines.append("")
    return "\n".join(lines)
