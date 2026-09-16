"""App-scoped command sets (queue 68).

A command -- or a whole pack -- may declare WHEN it is a candidate:

    @command("open command palette", pack="obsidian",
             scope={"apps": ["obsidian.exe"]})
    @command("next note", scope={"apps": ["obsidian.exe"], "title": r" - Obsidian v"})
    command("two", aliases=["2"], scope={"tags": ["window_cube.visible"]})

    apps   process image names (case-insensitive, e.g. "warp.exe"); the
           foreground window's process must be one of them
    title  a regular expression searched (re.search, case-sensitive) in the
           foreground window's title
    tags   names that must ALL be active; a tag is a piece of app state such as
           "window_cube.visible", published by the code that owns that state
           (register_tag_source) -- the equivalent of a Talon tag

An undeclared scope is GLOBAL: the command is live everywhere, exactly as
before this module existed. Scopes combine with AND.

Why (queue 66): every comparable tool scopes commands per application (Talon
`.talon` context headers, Kaldi Active Grammar's per-utterance active grammars,
Dragon's application-specific commands). A smaller live phrase set means fewer
accidental exact matches in the combined lane, and per-app commands can drive
Electron and custom-drawn apps through their own keyboard shortcuts.

FALLBACK -- the dangerous part, decided here and logged:

  * Unscoped commands are always live. Nothing in this module can make a
    global command stop matching, whatever happens to foreground resolution.
  * An app/title-scoped command is live ONLY on a positive match. When the
    foreground window cannot be resolved (no foreground window, the process
    name cannot be read, the Win32 API failed) or it is Samsara's own window,
    app-scoped commands are NOT candidates. Rationale: an app-scoped command
    exists to send THAT app's keystrokes; guessing "live" would send an
    Obsidian shortcut into an unknown window, the exact class of false
    execution this feature exists to remove. Failing closed costs only the
    app's own commands, and only while the window is unknown.
  * Elevated windows: the process image name is normally readable for an
    elevated process (limited query rights), so the app scope still resolves;
    typing into it is refused separately by injection_safety. If the name
    cannot be read, it is the "unresolved" case above.
  * Tag-scoped commands depend only on their tags, never on the foreground.

Every change of the resolved foreground state is logged once at INFO
(``[SCOPE] ...``) -- never per utterance, and never a window title.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from samsara.log import get_logger

logger = get_logger(__name__)

SCOPE_KEYS = ("apps", "title", "tags", "argument_tags")


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scope:
    apps: frozenset = frozenset()
    title: Optional[str] = None
    tags: frozenset = frozenset()
    # Tags required only when the matched phrase has a non-empty remainder.
    # This keeps a bare command global while making its argument form a
    # candidate only in the state that gives that argument meaning.
    argument_tags: frozenset = frozenset()

    @property
    def needs_foreground(self) -> bool:
        return bool(self.apps) or self.title is not None

    def to_json(self) -> dict:
        """Deterministic, catalog-friendly form."""
        out = {}
        if self.apps:
            out["apps"] = sorted(self.apps)
        if self.title is not None:
            out["title"] = self.title
        if self.tags:
            out["tags"] = sorted(self.tags)
        if self.argument_tags:
            out["argument_tags"] = sorted(self.argument_tags)
        return out

    def describe(self) -> str:
        """Short human wording for logs and the cheat sheet."""
        parts = []
        if self.apps:
            parts.append("in " + ", ".join(sorted(self.apps)))
        if self.title is not None:
            parts.append("when the window title matches /" + self.title + "/")
        if self.tags:
            parts.append("while " + " and ".join(_tag_wording(t) for t in sorted(self.tags)))
        if self.argument_tags:
            parts.append("with an argument while " + " and ".join(
                _tag_wording(t) for t in sorted(self.argument_tags)))
        return "only " + "; ".join(parts) if parts else "everywhere"


_TAG_WORDING = {"window_cube.visible": "the window cube is on screen"}


def _tag_wording(tag: str) -> str:
    return _TAG_WORDING.get(tag, tag)


def _names(value, key: str) -> frozenset:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"scope {key!r} must be a string or a list of strings, got {type(value).__name__}")
    out = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"scope {key!r} entries must be non-empty strings, got {item!r}")
        out.add(item.strip().lower() if key == "apps" else item.strip())
    return frozenset(out)


def parse_scope(raw) -> Optional[Scope]:
    """Scope from a declaration dict; None (global) for None / {}.
    Raises ValueError on anything malformed, so a bad declaration fails at
    load time instead of silently scoping a command out."""
    if raw is None:
        return None
    if isinstance(raw, Scope):
        return raw if (raw.apps or raw.title is not None or raw.tags or raw.argument_tags) else None
    if not isinstance(raw, dict):
        raise ValueError(f"scope must be a dict, got {type(raw).__name__}")
    unknown = set(raw) - set(SCOPE_KEYS) - {"app"}
    if unknown:
        raise ValueError(f"unknown scope keys {sorted(unknown)}; allowed: {list(SCOPE_KEYS)}")
    apps = _names(raw.get("apps", raw.get("app")), "apps")
    title = raw.get("title")
    if title is not None:
        if not isinstance(title, str) or not title:
            raise ValueError("scope 'title' must be a non-empty regular expression string")
        try:
            re.compile(title)
        except re.error as exc:
            raise ValueError(f"scope 'title' is not a valid regular expression: {exc}") from exc
    tags = _names(raw.get("tags"), "tags")
    argument_tags = _names(raw.get("argument_tags"), "tags")
    if not apps and title is None and not tags and not argument_tags:
        return None
    return Scope(apps=apps, title=title, tags=tags, argument_tags=argument_tags)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

_tag_lock = threading.Lock()
_tag_sources: dict = {}      # name -> callable returning bool


def register_tag_source(name: str, predicate: Callable[[], bool]) -> None:
    """Publish a tag whose value is read from `predicate` whenever a context
    is captured. The owner of the state registers it once (a plugin at import);
    re-registering the same name replaces the predicate."""
    if not name or not callable(predicate):
        raise ValueError("register_tag_source needs a name and a callable")
    with _tag_lock:
        _tag_sources[name] = predicate


def unregister_tag_source(name: str) -> None:
    with _tag_lock:
        _tag_sources.pop(name, None)


def active_tags() -> frozenset:
    with _tag_lock:
        sources = list(_tag_sources.items())
    out = set()
    for name, predicate in sources:
        try:
            if predicate():
                out.add(name)
        except Exception as exc:
            logger.debug(f"[SCOPE] tag source {name!r} failed: {exc}")
    return frozenset(out)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

#: Reasons an app scope cannot be evaluated (MatchContext.reason).
UNRESOLVED_NO_WINDOW = "no_foreground_window"
UNRESOLVED_NO_NAME = "process_name_unavailable"
UNRESOLVED_API = "win32_unavailable"
UNRESOLVED_NO_PROVIDER = "no_context_provider"
OWN_WINDOW = "samsara_window"


@dataclass(frozen=True)
class MatchContext:
    """What the matcher knows about "now" for one utterance."""
    exe: Optional[str] = None           # lowercase process image name
    title: Optional[str] = None         # never logged
    resolved: bool = False              # exe known and not Samsara's own window
    own_window: bool = False
    reason: str = ""                    # why not resolved ("" when resolved)
    tags: frozenset = field(default_factory=frozenset)

    @classmethod
    def unresolved(cls, reason: str, tags: Iterable[str] = ()) -> "MatchContext":
        return cls(resolved=False, reason=reason, tags=frozenset(tags))

    @classmethod
    def for_app(cls, exe: str, title: Optional[str] = None, tags: Iterable[str] = ()) -> "MatchContext":
        return cls(exe=exe.lower(), title=title, resolved=True, tags=frozenset(tags))


def scope_live(scope: Optional[Scope], ctx: Optional[MatchContext], remainder: str = "") -> tuple:
    """(live, why) for one scope. A global scope (None) is always live."""
    if scope is None:
        return True, ""
    if ctx is None:
        ctx = MatchContext.unresolved(UNRESOLVED_NO_PROVIDER, active_tags())
    if scope.tags:
        missing = sorted(scope.tags - ctx.tags)
        if missing:
            return False, "needs " + ", ".join(missing)
    if remainder and remainder.strip() and scope.argument_tags:
        missing = sorted(scope.argument_tags - ctx.tags)
        if missing:
            return False, "argument needs " + ", ".join(missing)
    if scope.needs_foreground:
        if ctx.own_window:
            return False, "Samsara's own window is focused"
        if not ctx.resolved or not ctx.exe:
            return False, f"foreground app unknown ({ctx.reason or 'unresolved'})"
        if scope.apps and ctx.exe not in scope.apps:
            return False, f"foreground is {ctx.exe}"
        if scope.title is not None:
            if not ctx.title or re.search(scope.title, ctx.title) is None:
                return False, "window title does not match"
    return True, ""


def capture_context(own_pid: Optional[int] = None) -> MatchContext:
    """Resolve the foreground window now. Never raises."""
    tags = active_tags()
    try:
        import ctypes  # noqa: PLC0415
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
    except Exception as exc:
        return _noted(MatchContext.unresolved(f"{UNRESOLVED_API}:{type(exc).__name__}", tags))
    if not hwnd:
        return _noted(MatchContext.unresolved(UNRESOLVED_NO_WINDOW, tags))
    try:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid = int(pid.value)
    except Exception as exc:
        return _noted(MatchContext.unresolved(f"{UNRESOLVED_API}:{type(exc).__name__}", tags))
    title = None
    try:
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length > 0:
            buf = ctypes.create_unicode_buffer(min(length, 1024) + 1)
            user32.GetWindowTextW(hwnd, buf, len(buf))
            title = buf.value
    except Exception:
        title = None
    exe = None
    try:
        import psutil  # noqa: PLC0415
        exe = psutil.Process(pid).name().lower()
    except Exception as exc:
        reason = f"{UNRESOLVED_NO_NAME}:{type(exc).__name__}"
        return _noted(MatchContext(exe=None, title=title, resolved=False, reason=reason, tags=tags))
    own = pid == (own_pid if own_pid is not None else os.getpid())
    if own:
        return _noted(MatchContext(exe=exe, title=title, resolved=False, own_window=True,
                                   reason=OWN_WINDOW, tags=tags))
    ctx = MatchContext(exe=exe, title=title, resolved=True, tags=tags)
    _remember_external(ctx)
    return _noted(ctx)


# ---------------------------------------------------------------------------
# Change-only logging + the last external app (for display surfaces)
# ---------------------------------------------------------------------------

_note_lock = threading.Lock()
_last_noted = None
_last_external: Optional[MatchContext] = None


def _noted(ctx: MatchContext) -> MatchContext:
    """Log when the resolved foreground state changes (never per utterance,
    never the title)."""
    global _last_noted
    key = (ctx.resolved, ctx.own_window, ctx.exe, ctx.reason.split(":")[0])
    with _note_lock:
        changed = key != _last_noted
        _last_noted = key
    if changed:
        if ctx.resolved:
            logger.info(f"[SCOPE] foreground app: {ctx.exe}")
        elif ctx.own_window:
            logger.info("[SCOPE] Samsara's own window is focused: app-scoped commands are not "
                        "candidates; unscoped and tag-scoped commands are unaffected")
        else:
            logger.info(f"[SCOPE] foreground app unresolved ({ctx.reason}): app-scoped commands are "
                        f"not candidates; unscoped and tag-scoped commands are unaffected")
    return ctx


def _remember_external(ctx: MatchContext) -> None:
    global _last_external
    with _note_lock:
        _last_external = ctx


def display_context() -> MatchContext:
    """The context a guidance surface (cheat sheet) should describe: the
    current foreground, or -- when Samsara's own window has focus because the
    user just opened the sheet -- the last external app, with current tags."""
    ctx = capture_context()
    if ctx.own_window or not ctx.resolved:
        with _note_lock:
            last = _last_external
        if last is not None:
            return MatchContext(exe=last.exe, title=last.title, resolved=True, tags=ctx.tags)
    return ctx


def reset_for_tests() -> None:
    global _last_noted, _last_external
    with _note_lock:
        _last_noted = None
        _last_external = None
