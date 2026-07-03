"""Installed-app index for parameterized voice verbs (focus/open/close <x>).

Enumerates installed Windows apps (Store + desktop) so a spoken app name can
be resolved to a launch target WITHOUT a hardcoded per-app macro. Two sources,
unioned:

  - Shell AppsFolder (shell:AppsFolder via win32com's Shell.Application) --
    covers UWP/Store apps (no .exe path, launched by AUMID) and desktop apps
    pinned to Start.
  - Start Menu .lnk scan (user + common Programs folders) -- covers desktop
    apps that may be missing or under a less useful name in AppsFolder.

Deterministic resolution only: resolve() never guesses beyond a fixed
exact > prefix > token-subset > fuzzy-ratio cascade with a floor. The LLM
(Ava's ACTION2 grammar) supplies intent + the user's own words; this module
is the only thing that ever picks a concrete target.

Built on a background thread at boot (must never delay startup) and cached
to ~/.samsara/app_index.json so resolve() has something to work with on the
very next launch, before the first fresh enumeration finishes.
"""

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from samsara.paths import samsara_home_dir
from samsara.runtime import thread_registry

# ---------------------------------------------------------------------------
# Shared name-matching primitive -- also used by the window resolver
# (plugins/commands/app_verbs.py) so both share identical scoring/floor logic.
# ---------------------------------------------------------------------------

import string

MATCH_FLOOR = 0.75

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def normalize_name(text: Optional[str]) -> str:
    """lowercase, strip punctuation -- matching-only, same spirit as
    session_modes.normalize_utterance but this module has no dependency on
    session_modes (different layer, no filler-stripping needed here)."""
    return (text or "").strip().lower().translate(_PUNCT_TABLE)


def score_name_match(query: str, candidate: str) -> float:
    """Deterministic exact > prefix > token-subset > fuzzy-ratio score in
    [0.0, 1.0]. No NLU. Shared by AppIndex.resolve() and
    app_verbs.resolve_window() so app and window resolution behave
    identically for the same spoken name.
    """
    q = normalize_name(query)
    c = normalize_name(candidate)
    if not q or not c:
        return 0.0

    if q == c:
        return 1.0

    if c.startswith(q) or q.startswith(c):
        return 0.9

    q_tokens = set(q.split())
    c_tokens = set(c.split())
    if q_tokens and c_tokens and (q_tokens <= c_tokens or c_tokens <= q_tokens):
        return 0.85

    return SequenceMatcher(None, q, c).ratio()


def rank_candidates(query: str, candidates: list, name_fn) -> list:
    """Score every candidate against query, best-first. name_fn(candidate)
    -> str extracts the comparable name. Returns a list of (score, candidate)
    tuples, NOT filtered by MATCH_FLOOR -- callers apply the floor themselves
    (and can inspect the full ranking for top-3 diagnostic logging first).
    """
    scored = [(score_name_match(query, name_fn(c)), c) for c in candidates]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def log_top3(tag: str, query: str, ranked: list, name_fn) -> None:
    """Diagnostic top-3 log line, shared format for app_index and the
    window resolver (distinguished by `tag`)."""
    top3 = ranked[:3]
    if not top3:
        print(f"[{tag}] resolve({query!r}): no candidates")
        return
    summary = ", ".join(f"{name_fn(c)!r}={score:.2f}" for score, c in top3)
    print(f"[{tag}] resolve({query!r}) top-3: {summary}")


# ---------------------------------------------------------------------------
# App entry + enumeration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppEntry:
    display_name: str
    launch_kind: str   # "aumid" | "lnk"
    launch_spec: str   # AUMID string, or absolute .lnk path
    name_tokens: tuple


def _make_entry(display_name: str, launch_kind: str, launch_spec: str) -> Optional[AppEntry]:
    if not display_name or not launch_spec:
        return None
    tokens = tuple(normalize_name(display_name).split())
    if not tokens:
        return None
    return AppEntry(display_name=display_name, launch_kind=launch_kind,
                     launch_spec=launch_spec, name_tokens=tokens)


def _enumerate_appsfolder() -> list:
    """Enumerate shell:AppsFolder -- covers Store/UWP apps (no .exe path,
    launched by AUMID) and desktop apps pinned to Start. Wrapped defensively:
    if the exact COM shape differs across Windows builds, this degrades to
    an empty list rather than breaking the whole index (the .lnk scan below
    still covers most desktop apps)."""
    apps = []
    try:
        import win32com.client
        shell = win32com.client.Dispatch("Shell.Application")
        namespace = shell.Namespace("shell:AppsFolder")
        if namespace is None:
            return apps
        for item in namespace.Items():
            try:
                display_name = item.Name
                # AppsFolder items' shell path is (or ends in) the AUMID --
                # there is no on-disk .exe path for packaged apps.
                path = item.Path or ""
                aumid = path.rsplit("\\", 1)[-1] if path else display_name
                entry = _make_entry(display_name, "aumid", aumid)
                if entry:
                    apps.append(entry)
            except Exception:
                continue
    except Exception as exc:
        print(f"[APP-INDEX] AppsFolder enumeration unavailable: {exc}")
    return apps


def _start_menu_roots() -> list:
    roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    programdata = os.environ.get("PROGRAMDATA")
    if programdata:
        roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return roots


def _enumerate_start_menu_lnks() -> list:
    """Scan user + common Start Menu Programs folders for .lnk shortcuts."""
    apps = []
    for root in _start_menu_roots():
        if not root.exists():
            continue
        try:
            for lnk_path in root.rglob("*.lnk"):
                entry = _make_entry(lnk_path.stem, "lnk", str(lnk_path))
                if entry:
                    apps.append(entry)
        except OSError as exc:
            print(f"[APP-INDEX] Start Menu scan failed for {root}: {exc}")
    return apps


def build_index() -> list:
    """Union of both enumerations, deduped by normalized display name.
    AUMID entries win the collision (packaged-app launch via AUMID is more
    reliable than a possibly-stale .lnk path to the same display name)."""
    by_name = {}
    for entry in _enumerate_start_menu_lnks():
        by_name[normalize_name(entry.display_name)] = entry
    for entry in _enumerate_appsfolder():
        by_name[normalize_name(entry.display_name)] = entry
    return list(by_name.values())


def launch_app(entry: AppEntry) -> bool:
    """Launch an AppEntry. Returns True if the launch call itself didn't
    raise (best-effort -- there is no reliable synchronous "did it actually
    start" signal for either launch kind)."""
    try:
        if entry.launch_kind == "aumid":
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{entry.launch_spec}"])
        else:
            os.startfile(entry.launch_spec)
        return True
    except Exception as exc:
        print(f"[APP-INDEX] Launch failed for {entry.display_name!r}: {exc}")
        return False


# ---------------------------------------------------------------------------
# AppIndex -- boot-time background build, disk cache, resolve()
# ---------------------------------------------------------------------------

_CACHE_PATH = samsara_home_dir() / "app_index.json"
REFRESH_INTERVAL_S = 30 * 60


class AppIndex:
    """Thread-safe holder for the current app list. Construct via
    get_app_index() (module-level singleton) -- one instance per process."""

    def __init__(self):
        self._lock = threading.Lock()
        self._apps: list = []
        self._refresh_timer: "threading.Timer | None" = None

    @property
    def apps(self) -> list:
        with self._lock:
            return list(self._apps)

    def ensure_built_async(self) -> None:
        """Call once at boot. Loads the on-disk cache immediately (if any)
        so resolve() has something to work with before the first real
        enumeration finishes, then kicks a background thread to build fresh
        and overwrite the cache. Never blocks the caller."""
        cached = self._load_cache()
        if cached:
            with self._lock:
                self._apps = cached
        thread_registry.spawn("app-index-build", self._refresh_now, daemon=True)

    def refresh_async(self) -> None:
        """On-demand refresh, e.g. a future 'refresh app list' voice command."""
        thread_registry.spawn("app-index-refresh", self._refresh_now, daemon=True)

    def resolve(self, name: str) -> Optional[AppEntry]:
        apps = self.apps
        if not apps or not name:
            return None
        ranked = rank_candidates(name, apps, lambda a: a.display_name)
        log_top3("APP-INDEX", name, ranked, lambda a: a.display_name)
        if not ranked or ranked[0][0] < MATCH_FLOOR:
            return None
        return ranked[0][1]

    # -- internals ---------------------------------------------------------

    def _refresh_now(self) -> None:
        try:
            fresh = build_index()
            with self._lock:
                self._apps = fresh
            self._save_cache(fresh)
            print(f"[APP-INDEX] Built index: {len(fresh)} apps")
        except Exception as exc:
            print(f"[APP-INDEX] Build failed: {exc}")
        finally:
            self._schedule_next_refresh()

    def _schedule_next_refresh(self) -> None:
        t = thread_registry.timer("app_index.refresh", REFRESH_INTERVAL_S, self._refresh_now, daemon=True)
        self._refresh_timer = t

    def _load_cache(self) -> list:
        try:
            if not _CACHE_PATH.exists():
                return []
            data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            return [
                AppEntry(
                    display_name=d["display_name"],
                    launch_kind=d["launch_kind"],
                    launch_spec=d["launch_spec"],
                    name_tokens=tuple(d["name_tokens"]),
                )
                for d in data
            ]
        except Exception as exc:
            print(f"[APP-INDEX] Cache load failed: {exc}")
            return []

    def _save_cache(self, apps: list) -> None:
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {"display_name": a.display_name, "launch_kind": a.launch_kind,
                 "launch_spec": a.launch_spec, "name_tokens": list(a.name_tokens)}
                for a in apps
            ]
            _CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"[APP-INDEX] Cache save failed: {exc}")


_index: "AppIndex | None" = None
_index_lock = threading.Lock()


def get_app_index() -> AppIndex:
    global _index
    with _index_lock:
        if _index is None:
            _index = AppIndex()
        return _index
