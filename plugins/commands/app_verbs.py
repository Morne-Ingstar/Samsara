"""Parameterized app/window verbs -- focus/open/close <anything>, resolved
at runtime instead of one hardcoded macro per app.

  "focus chrome" / "switch to chrome"  -- force-focus a live window
  "open notepad"                        -- focus if running, else launch it
  "close notepad"                       -- graceful WM_CLOSE (never kills)

Deterministic resolution only (REDUCE, not generate): resolve_window()
(live windows) and samsara.app_index (installed apps) do the matching; the
handlers here never guess beyond what those return. Ava's ACTION2 grammar
(plugins/commands/ask_ollama.py) calls the SAME do_focus/do_open/do_close
functions this file's voice commands use -- one resolution path, two
feedback layers (earcon+speech here for plain voice commands, Ava's own
speak() there).

Reuses existing infrastructure rather than rebuilding it:
  - window_switcher._force_focus for stealing focus (foreground-lock
    workaround, minimized-window restore, AttachThreadInput dance).
  - windows.get_all_movable_windows for EnumWindows-based live window
    enumeration (title + pid per window; process name resolved via psutil,
    matching windows.py's own is_valid_app_window convention).
  - samsara.app_index for installed-app resolution + the shared
    score_name_match/rank_candidates scoring primitives.
"""

import os
from enum import Enum

import psutil
import win32con
import win32gui

from samsara.app_index import get_app_index, launch_app, log_top3, rank_candidates
from samsara.plugin_commands import command

from plugins.commands.window_switcher import _force_focus


def _speak(app, text):
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[APP-VERBS] {text}")


def _miss_earcon(app):
    if hasattr(app, "play_sound"):
        # No dedicated "miss" earcon in this codebase -- reuse scratch_refuse
        # (the established "this didn't go through" sound; same choice made
        # for the AVA substance gate and AVA dispatch queue-full-drop cases).
        app.play_sound("scratch_refuse")


# ---------------------------------------------------------------------------
# Window resolver -- reuses windows.py's enumeration, shares app_index's
# scoring primitives so app and window resolution behave identically.
# ---------------------------------------------------------------------------

def resolve_window(name: str):
    """Resolve a spoken name against LIVE windows: title AND process stem,
    same normalize/floor logic as samsara.app_index. Returns
    (hwnd, title, process_name) or None.
    """
    from plugins.commands.windows import get_all_movable_windows
    from samsara.app_index import MATCH_FLOOR, score_name_match

    if not name:
        return None

    windows = get_all_movable_windows()
    candidates = []
    for hwnd, title, pid in windows:
        try:
            proc_name = psutil.Process(pid).name()
        except Exception:
            proc_name = ""
        proc_stem = proc_name.rsplit(".", 1)[0] if proc_name else ""
        candidates.append((hwnd, title, proc_name, proc_stem))

    if not candidates:
        return None

    def _best_label(c):
        _, title, _, proc_stem = c
        title_score = score_name_match(name, title)
        stem_score = score_name_match(name, proc_stem)
        return title if title_score >= stem_score else proc_stem

    ranked = rank_candidates(name, candidates, _best_label)
    log_top3("WINDOW-RESOLVE", name, ranked,
             lambda c: f"{c[1]} ({c[2]})")
    if not ranked or ranked[0][0] < MATCH_FLOOR:
        return None
    hwnd, title, proc_name, _ = ranked[0][1]
    return (hwnd, title, proc_name)


# ---------------------------------------------------------------------------
# Shared action functions -- resolve + act, NO earcon/speech. Both the voice
# command handlers below AND Ava's ACTION2 executor (ask_ollama.py) call
# these; each layers its own feedback on top.
# ---------------------------------------------------------------------------

class ActionResult(Enum):
    DONE = "done"
    NOT_RUNNING = "not_running"  # app_index found it, but no live window
    NOT_FOUND = "not_found"      # neither a window nor an installed app matched


def do_focus(name: str) -> "ActionResult":
    """Focus a live window only -- never launches. If the app is installed
    but not currently running, that's NOT_RUNNING, not NOT_FOUND (distinct
    feedback: "not running" vs "no such app")."""
    match = resolve_window(name)
    if match is not None:
        hwnd, _title, _proc = match
        _force_focus(hwnd)
        return ActionResult.DONE
    if get_app_index().resolve(name) is not None:
        return ActionResult.NOT_RUNNING
    return ActionResult.NOT_FOUND


def do_open(name: str) -> "ActionResult":
    """Focus if a matching window is already running, else resolve against
    the installed-app index and launch."""
    match = resolve_window(name)
    if match is not None:
        hwnd, _title, _proc = match
        _force_focus(hwnd)
        return ActionResult.DONE
    app_match = get_app_index().resolve(name)
    if app_match is None:
        return ActionResult.NOT_FOUND
    launch_app(app_match)
    return ActionResult.DONE


def do_close(name: str) -> "ActionResult":
    """Graceful WM_CLOSE to the resolved window's top-level hwnd -- the app
    may prompt to save; this never TerminateProcess()es."""
    match = resolve_window(name)
    if match is None:
        return ActionResult.NOT_FOUND
    hwnd, _title, _proc = match
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    return ActionResult.DONE


# ---------------------------------------------------------------------------
# Voice commands
# ---------------------------------------------------------------------------

@command(
    "focus",
    aliases=["switch to"],
    pack="window-management",
)
def handle_focus(app, remainder):
    """"focus <x>" / "switch to <x>" -- registered as the bare single-token
    verb (no regex/prefix marker in this codebase's command matcher -- see
    command_registry.CommandMatcher.match(): matching is longest-registered-
    phrase-first, so every existing 2+-token literal macro ("open chrome",
    "close tab", etc.) already outranks this 1-token fallback for free)."""
    name = (remainder or "").strip()
    if not name:
        return False
    result = do_focus(name)
    if result is ActionResult.DONE:
        return True
    _miss_earcon(app)
    if result is ActionResult.NOT_RUNNING:
        _speak(app, f"{name} is not running.")
    else:
        _speak(app, f"No app called {name}.")
    return True


@command(
    "open",
    pack="window-management",
)
def handle_open(app, remainder):
    """"open <x>" -- focuses if already running, else launches. Never
    registered with a longer phrase, so any existing "open <literal>" macro
    (all 2+ tokens) wins precedence automatically."""
    name = (remainder or "").strip()
    if not name:
        return False
    result = do_open(name)
    if result is ActionResult.DONE:
        return True
    _miss_earcon(app)
    _speak(app, f"No app called {name}.")
    return True


@command(
    "close",
    pack="window-management",
    risk_class="reversible",
)
def handle_close(app, remainder):
    """"close <x>" -- graceful WM_CLOSE, never a forced kill."""
    name = (remainder or "").strip()
    if not name:
        return False
    result = do_close(name)
    if result is ActionResult.DONE:
        return True
    _miss_earcon(app)
    _speak(app, f"No app called {name}.")
    return True
