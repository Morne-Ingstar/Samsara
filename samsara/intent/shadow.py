"""Shadow-mode intent gate over DICTATE -- an OBSERVER ONLY (SAMSARA_MAP Move A, step 2).

For every utterance the DICTATE lane has already turned into dictation
(outcome dictate_staged / dictate_injected), ask samsara.intent.resolve what
the gate WOULD have done and append one JSON line to

    ~/.samsara/shadow/intent-YYYY-MM-DD.jsonl      (config: intent.shadow_dir)

Nothing here can change what the app does: the caller hands over the text
and the outcome KIND (a string) after dispatch has returned, the decision
runs on a background worker (thread_registry 'intent-shadow'), and its only
side effect is that file. Every failure is counted, logged once, swallowed.

Line schema (v2 -- v1 rows are still readable; the reader keys off `would`):
    v            2
    ts           local ISO-8601 timestamp with offset
    text         the utterance as dispatched
    delivery     "staged" | "injected"   (the DICTATE outcome it came from)
    would        "dictate" | "command:<canonical_id>" | "suggest:<canonical_id>" | "miss"
                 miss = the gate produced no decision (catalog unavailable or resolve raised)
    confidence   float or null
    tier         "exact" | "grammar" | "similarity" | null
    elapsed_us   microseconds of WALL CLOCK around the whole record() decision:
                 the lazy resolver build on the first utterance after the
                 worker respawns, plus tier 3, plus whatever else this
                 background thread waited on. NOT the tier 1+2 budget, and
                 not comparable to it -- see t12_us. (Queue 93: every row
                 over 30 ms in the first 1,777 followed an idle gap of more
                 than a minute, i.e. a cold worker, not slow resolution.)
    t12_us       microseconds spent in tiers 1+2 -- the measurement
                 resolve.LATENCY_BUDGET_MS is stated against -- or null
    rules_version which execution rules decided this row (resolve.
                 EXECUTION_RULES_VERSION); null when the gate produced no
                 decision. Lets one log hold before/after populations.
    blocked      the execution rule that demoted a resolved decision
                 ("one_word_command" | "one_word_utterance" |
                 "inexact_match"), else null. A blocked row's `would` is
                 already the demoted outcome; this says what it would have
                 been without the rule.
    forced       true when the utterance opened with the configured command
                 prefix (intent.command_prefix), which waives rule 1
    literal      true when the command words spoken ARE a registered alias or
                 canonical phrase, in order, with nothing inserted (queue
                 127). Zero word-penalty does NOT imply this: a hand-written
                 grammar rule returns a form that is in no alias, and a
                 template accepts inserted determiners for free, and a filled
                 slot is the user's words rather than the catalog's. Rule 2
                 has always CLAIMED to test this and has always tested the
                 penalty instead; queue 127 records the difference so it can
                 be counted rather than assumed. Nothing gates on it.
    suggestions  [canonical_id, ...] for suggest, else []
    chain        [canonical_id, ...] for an "and"/"then" chain, else []
    app          focused process image name ("warp.exe") or null -- NEVER a window title
    error        exception type name when would == "miss" because resolve raised, else absent

Privacy: plain readable JSONL in the user's own home; no audio, no audio
path, no window title; never uploaded and not part of any diagnostics
bundle. Delete the folder to clear it. Disabled (intent.shadow_enabled =
false) means no worker thread, no directory, no file handle.
"""
from __future__ import annotations

import json
import queue
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from samsara.log import get_logger

logger = get_logger(__name__)

DEFAULT_ENABLED = True
#: DICTATE outcomes whose text became dictation -- the only ones observed.
OBSERVED_OUTCOMES = {"dictate_staged": "staged", "dictate_injected": "injected"}
#: Utterances waiting for the worker beyond this are dropped (counted).
QUEUE_BOUND = 256
SCHEMA_VERSION = 2
_STOP = object()


def shadow_settings(config) -> dict:
    section = config.get("intent") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def shadow_enabled(config) -> bool:
    return bool(shadow_settings(config).get("shadow_enabled", DEFAULT_ENABLED))


def command_prefix(config) -> str:
    """The escape-hatch prefix word (queue 93). Empty = off, the default:
    the owner picks the word from shadow data later, so nothing is
    hard-coded here."""
    return str(shadow_settings(config).get("command_prefix", "") or "")


def shadow_dir(config) -> Path:
    configured = shadow_settings(config).get("shadow_dir")
    if configured:
        return Path(str(configured)).expanduser()
    from samsara.paths import samsara_home_dir  # noqa: PLC0415
    return samsara_home_dir() / "shadow"


def foreground_pid() -> Optional[int]:
    """PID of the focused window's process (two cheap Win32 calls), or None."""
    try:
        import ctypes  # noqa: PLC0415
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    except Exception:
        return None


def process_name(pid: Optional[int]) -> Optional[str]:
    """Lowercase process image name for a PID (never the window title)."""
    if not pid:
        return None
    try:
        import psutil  # noqa: PLC0415
        return psutil.Process(pid).name().lower()
    except Exception:
        return None


def _tags_now() -> frozenset:
    """Scope tags active right now (queue 68); empty on any failure."""
    try:
        from samsara.command_scope import active_tags  # noqa: PLC0415
        return active_tags()
    except Exception:
        return frozenset()


def scope_context(app: Optional[str], pid: Optional[int], tags=None):
    """The command_scope.MatchContext for a shadowed utterance, from the
    focused process captured at observe time. The shadow never reads window
    titles, so title-scoped commands are judged as not live here."""
    import os  # noqa: PLC0415
    from samsara import command_scope  # noqa: PLC0415
    tags = frozenset(tags or ())
    if not app:
        return command_scope.MatchContext.unresolved(command_scope.UNRESOLVED_NO_NAME, tags)
    if pid is not None and pid == os.getpid():
        return command_scope.MatchContext(exe=app, resolved=False, own_window=True,
                                          reason=command_scope.OWN_WINDOW, tags=tags)
    return command_scope.MatchContext.for_app(app, tags=tags)


def would_have_done(resolution) -> str:
    kind = getattr(resolution, "kind", None)
    cid = getattr(resolution, "canonical_id", None)
    if kind == "resolved" and cid:
        return f"command:{cid}"
    if kind == "suggest" and cid:
        return f"suggest:{cid}"
    if kind == "dictation":
        return "dictate"
    return "miss"


def build_entry(text: str, delivery: str, resolution, *, elapsed_us: int, app: Optional[str],
                when: datetime, error: Optional[str] = None) -> dict:
    entry = {
        "v": SCHEMA_VERSION,
        "ts": when.isoformat(timespec="milliseconds"),
        "text": text,
        "delivery": delivery,
        "would": would_have_done(resolution) if resolution is not None else "miss",
        "confidence": round(float(resolution.confidence), 4) if resolution is not None else None,
        "tier": getattr(resolution, "tier", None) if resolution is not None else None,
        "elapsed_us": int(elapsed_us),
        "t12_us": int(round(resolution.t12_ms * 1000)) if resolution is not None else None,
        "suggestions": list(getattr(resolution, "suggestions", ()) or ()) if resolution is not None else [],
        "chain": [p.canonical_id for p in (getattr(resolution, "chain", ()) or ())] if resolution is not None else [],
        "rules_version": getattr(resolution, "rules_version", None) if resolution is not None else None,
        "blocked": getattr(resolution, "blocked", None) if resolution is not None else None,
        "forced": bool(getattr(resolution, "forced", False)) if resolution is not None else False,
        "literal": bool(getattr(resolution, "literal", False)) if resolution is not None else False,
        "app": app,
    }
    if error:
        entry["error"] = error
    return entry


class IntentShadow:
    """Background observer. observe() is the only method the hot path calls."""

    def __init__(self, resolver_factory: Callable[[], object], config_source: Callable[[], dict], *,
                 spawn=None, now: Callable[[], datetime] = datetime.now,
                 pid_fn: Callable[[], Optional[int]] = foreground_pid,
                 name_fn: Callable[[Optional[int]], Optional[str]] = process_name):
        self._resolver_factory = resolver_factory
        self._config_source = config_source
        self._spawn = spawn
        self._now = now
        self._pid_fn = pid_fn
        self._name_fn = name_fn
        self._queue = queue.SimpleQueue()
        self._worker = None
        self._resolver = None
        self._resolver_failed = False
        #: Counters (read by tests and the log).
        self.observed = 0
        self.written = 0
        self.dropped = 0
        self.errors = 0
        self._error_logged = False

    # -- hot path ------------------------------------------------------------

    def observe(self, text: str, outcome_kind: str) -> bool:
        """Queue one finalised DICTATE utterance. Never raises; True if queued."""
        try:
            delivery = OBSERVED_OUTCOMES.get(outcome_kind)
            if delivery is None or not text or not shadow_enabled(self._config_source()):
                return False
            if self._queue.qsize() >= QUEUE_BOUND:
                self.dropped += 1
                return False
            self._queue.put((str(text), delivery, self._now(), self._pid_fn(), _tags_now()))
            self.observed += 1
            if self._worker is None or not self._worker.is_alive():
                spawn = self._spawn
                if spawn is None:
                    from samsara.runtime import thread_registry  # noqa: PLC0415
                    spawn = thread_registry.spawn
                self._worker = spawn("intent-shadow", self._run, daemon=True)
            return True
        except Exception as exc:
            self._count_error("observe", exc)
            return False

    # -- worker --------------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=30.0)
            except queue.Empty:
                return                      # idle: the next observe() respawns the worker
            if item is _STOP:
                return
            self.record(*item)

    def stop(self) -> None:
        self._queue.put(_STOP)

    def _get_resolver(self):
        if self._resolver is None and not self._resolver_failed:
            try:
                self._resolver = self._resolver_factory()
            except Exception as exc:
                self._resolver_failed = True
                self._count_error("catalog", exc)
        return self._resolver

    def record(self, text: str, delivery: str, when: datetime, pid: Optional[int],
               tags=None) -> Optional[dict]:
        """Decide and append one line (worker thread). Never raises.

        tags: the scope tags active when the utterance was observed (queue 68),
        so a command scoped to app state (e.g. the window cube on screen) is
        judged against the state at dispatch, not whenever the worker runs."""
        try:
            config = self._config_source()
            if not shadow_enabled(config):
                return None
            resolution, error = None, None
            app = self._name_fn(pid)
            t0 = time.perf_counter()
            resolver = self._get_resolver()
            if resolver is not None:
                try:
                    if hasattr(resolver, "excluded_ids"):
                        # An IntentResolver: it takes the scope context and the
                        # escape-hatch prefix, both read fresh from config so a
                        # setting change lands without rebuilding the catalog.
                        resolution = resolver.resolve(text, context=scope_context(app, pid, tags),
                                                      prefix=command_prefix(config))
                    else:
                        resolution = resolver.resolve(text)
                except Exception as exc:
                    error = type(exc).__name__
                    self._count_error("resolve", exc)
            elapsed_us = int((time.perf_counter() - t0) * 1_000_000)
            entry = build_entry(text, delivery, resolution, elapsed_us=elapsed_us,
                                app=app, when=when, error=error)
            folder = shadow_dir(config)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"intent-{when:%Y-%m-%d}.jsonl"
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.written += 1
            return entry
        except Exception as exc:
            self._count_error("record", exc)
            return None

    def _count_error(self, where: str, exc: BaseException) -> None:
        self.errors += 1
        if not self._error_logged:
            self._error_logged = True
            logger.warning(f"[INTENT-SHADOW] {where} failed ({type(exc).__name__}: {exc}); "
                           f"further shadow errors this session are counted, not logged")
