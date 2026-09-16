"""Window Cube -- say ONE number to switch windows.

    "show cube"      pin a numbered list of open windows (stays until hidden)
    "three"          switch to window 3          (COMMAND mode / hold only)
    "window three"   switch to window 3          (works in every mode)
    "hide cube"      put it away

Builds ON plugins/commands/window_switcher.py -- enumeration, letter
assignment, focus and NATO parsing are imported from there, never
reimplemented, so the cube and the letter overlay can never disagree about
what a window is or how to focus it.

Frozen numbering
----------------
While the cube is pinned, a number always means the same window. "refresh
cube" picks up windows opened since pinning (appended with the NEXT free
number) and greys out ones that have closed (their number is retained, so
nothing below them shifts). Numbers are only ever reassigned by unpinning
and pinning again -- the user's muscle memory is the whole point of the
feature, so a renumber is never something that happens to them silently.

Scoping of the bare number commands
-----------------------------------
"one".."nine" / "1".."9" must not eat a dictated number. The command
registry has no conditional-activation hook (see _numbers_active below), so
the guard lives in the handler and returns False ("not handled") outside
COMMAND/hold context. Read the caveat in _numbers_active before relying on
that: returning False is NOT the same as the phrase never matching.
"""

import threading

from samsara.plugin_commands import command

# Reuse -- never fork. Enumeration, ordering, focus, NATO parsing and the
# letter mapping all come from the existing switcher.
from plugins.commands.window_switcher import (
    _assign_letters,
    _force_focus,
    _get_all_windows,
    _get_pid,
    _own_hwnd,
    _speak,
    handle_window_copy,
    handle_window_tile,
)

MAX_ROWS_DEFAULT = 9

_lock = threading.Lock()
_slots: list = []        # ordered [{number, hwnd, app, title, closed}]
_pinned = False
_page = 1
_app_ref = None

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _cfg(app) -> dict:
    try:
        return dict(app.config.get("window_cube", {}) or {})
    except Exception:
        return {}


def _max_rows(app) -> int:
    try:
        return max(1, int(_cfg(app).get("max_rows", MAX_ROWS_DEFAULT)))
    except Exception:
        return MAX_ROWS_DEFAULT


# ---------------------------------------------------------------------------
# Friendly names -- reuse windows.APP_ALIASES rather than a second table
# ---------------------------------------------------------------------------

def _friendly_app_name(hwnd: int) -> str:
    """Display name for a window's owning process.

    Inverts plugins/commands/windows.APP_ALIASES (the spoken-name -> exe map
    app_verbs already resolves against) so "Code.exe" shows as "Vscode"
    rather than a raw image name; falls back to the process stem.
    """
    proc = ""
    try:
        import psutil
        proc = psutil.Process(_get_pid(hwnd)).name() or ""
    except Exception:
        proc = ""
    if not proc:
        return "Window"
    try:
        from plugins.commands.windows import APP_ALIASES
        for spoken, exe in APP_ALIASES.items():
            if exe.lower() == proc.lower():
                return spoken.title()
    except Exception:
        pass
    return proc.rsplit(".", 1)[0].title()


def _build_rows(app):
    """Enumerate via window_switcher and return [(hwnd, app_name, title)] in
    the switcher's own stable order (visible left-to-right, then minimized)."""
    windows = _get_all_windows(_own_hwnd(app))
    ordered = _assign_letters(windows)
    out = []
    for letter in sorted(ordered, key=lambda k: (len(k), k)):
        hwnd, title, _is_min = ordered[letter]
        out.append((hwnd, _friendly_app_name(hwnd), title))
    return out


# ---------------------------------------------------------------------------
# Frozen numbering
# ---------------------------------------------------------------------------

def _rebuild(app) -> list:
    """Fresh numbering from 1 -- only on a new pin."""
    global _slots
    rows = _build_rows(app)
    with _lock:
        _slots = [
            {"number": i, "hwnd": h, "app": a, "title": t, "closed": False}
            for i, (h, a, t) in enumerate(rows, start=1)
        ]
        return list(_slots)


def _sync(app) -> list:
    """Pick up new/closed windows WITHOUT renumbering anything that exists.

    New windows append with the next free number; closed ones keep their
    number and are greyed. This is what "refresh cube" runs, so a refresh can
    never shift a number out from under the user mid-session.
    """
    global _slots
    rows = _build_rows(app)
    live = {h: (a, t) for h, a, t in rows}
    with _lock:
        seen = set()
        for slot in _slots:
            if slot["hwnd"] in live:
                slot["app"], slot["title"] = live[slot["hwnd"]]
                slot["closed"] = False
                seen.add(slot["hwnd"])
            else:
                slot["closed"] = True
        next_number = max((s["number"] for s in _slots), default=0) + 1
        for hwnd, app_name, title in rows:
            if hwnd in seen:
                continue
            _slots.append({"number": next_number, "hwnd": hwnd,
                           "app": app_name, "title": title, "closed": False})
            next_number += 1
        return list(_slots)


def _page_slots(app, slots=None) -> list:
    """The slots visible on the current page."""
    per_page = _max_rows(app)
    data = list(_slots) if slots is None else list(slots)
    start = (max(1, _page) - 1) * per_page
    return data[start:start + per_page]


def _to_cube_rows(app, slots) -> list:
    """Translate slots into panel rows, adding the disambiguating second
    line ONLY where two rows share an app name."""
    from samsara.ui.window_cube_qt import CubeRow

    counts = {}
    for slot in slots:
        counts[slot["app"]] = counts.get(slot["app"], 0) + 1
    rows = []
    for slot in slots:
        detail = ""
        if counts.get(slot["app"], 0) > 1:
            title = slot["title"] or ""
            detail = title if len(title) <= 34 else title[:33] + "…"
        rows.append(CubeRow(number=slot["number"], app=slot["app"],
                            detail=detail, closed=slot["closed"]))
    return rows


def _render(app, slots=None):
    """Push the current page to the panel (no-ops cleanly without Qt)."""
    try:
        from samsara.ui.window_cube_qt import get_panel
    except Exception as exc:
        print(f"[CUBE] panel unavailable: {exc}")
        return
    cfg = _cfg(app)
    panel = get_panel()
    panel.configure(
        on_click=lambda number: _switch_to_number(app, number),
        opacity=cfg.get("opacity", 0.6),
        position=cfg.get("position"),
    )
    rows = _to_cube_rows(app, _page_slots(app, slots))
    if _pinned:
        panel.show(rows)
    else:
        panel.refresh(rows)


def _remember_position(app):
    """Persist the panel's position so the cube reopens where the user left
    it. Best-effort: never let a config write break a switch."""
    try:
        from samsara.ui.window_cube_qt import get_panel
        pos = get_panel().current_position()
        if pos is None:
            return
        updates = dict(app.config.get("window_cube", {}) or {})
        if updates.get("position") == list(pos):
            return
        updates["position"] = list(pos)
        app.update_config_and_save({"window_cube": updates})
    except Exception as exc:
        print(f"[CUBE] position not remembered: {exc}")


# ---------------------------------------------------------------------------
# Switching
# ---------------------------------------------------------------------------

def _slot_for_number(number: int):
    with _lock:
        for slot in _slots:
            if slot["number"] == number:
                return dict(slot)
    return None


def _switch_to_number(app, number: int) -> bool:
    """Focus the window in slot `number` using the switcher's own focus path."""
    slot = _slot_for_number(number)
    if slot is None or slot["closed"]:
        _speak(app, f"No window {number}.")
        return True
    _force_focus(slot["hwnd"])
    try:
        app.play_sound("success")
    except Exception:
        pass
    print(f"[CUBE] Switched to {number}: {slot['app']} -- {slot['title'][:40]}")
    return True


# ---------------------------------------------------------------------------
# Scoping for the bare number commands
# ---------------------------------------------------------------------------

def _in_command_context(app) -> bool:
    """True when a spoken bare number is a COMMAND, not dictated content.

    COMMAND when the unified session is in COMMAND mode, or when there is no
    session manager at all and classic hold-to-command is configured (the
    hold path has no DICTATE lane to steal from).
    """
    manager = getattr(app, "_session_mode_manager", None)
    mode = getattr(manager, "mode", None)
    if mode is not None:
        name = str(getattr(mode, "name", mode)).upper()
        if "DICTATE" in name or "AVA" in name:
            return False
        if "COMMAND" in name:
            return True
    if getattr(app, "command_mode_active", False):
        return True
    try:
        return app.config.get("command_mode", {}).get("mode", "hold") == "hold"
    except Exception:
        return False


def _numbers_active(app) -> bool:
    """Whether bare "one".."nine" / "1".."9" should act right now.

    REGISTRY LIMITATION (reported deliberately): samsara/command_registry.py
    matches purely on phrase. Its only gate is set_enabled_packs(), which is
    static config applied before freeze(), and `preconditions=` on @command
    is metadata only -- samsara/plugin_commands.py's own docstring says
    "Enforcement is a later phase". There is no per-utterance predicate hook,
    so a command cannot be made conditionally *matchable*.

    The guard therefore lives here, and the handlers return False when it is
    False. That stops the ACTION but not the MATCH: in the hands-free DICTATE
    lane the reserved-command probe classifies the utterance by phrase before
    any handler runs, so "three" is consumed as a (failed) command rather
    than dictated. Closing that hole needs a registry/dictation.py change
    this task is not allowed to make -- hence the number rows also live in
    their own pack ("window-cube-numbers"), which IS something the user can
    switch off with the existing pack mechanism.
    """
    return bool(_pinned) and _in_command_context(app)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@command("show cube", aliases=["pin windows", "pin cube"],
         pack="window-management",
         risk_class="ui",
)
def handle_show_cube(app, remainder):
    """Pins a numbered grid of your open windows to the screen."""
    global _pinned, _page, _app_ref
    _app_ref = app
    _page = 1
    slots = _rebuild(app)
    _pinned = True
    _render(app, slots)
    print(f"[CUBE] pinned {len(slots)} windows")
    return True


@command("hide cube", aliases=["unpin windows", "unpin cube"],
         pack="window-management",
         risk_class="ui",
)
def handle_hide_cube(app, remainder):
    """Takes the numbered window grid off the screen."""
    global _pinned
    _remember_position(app)
    _pinned = False
    try:
        from samsara.ui.window_cube_qt import get_panel
        get_panel().hide()
    except Exception as exc:
        print(f"[CUBE] hide failed: {exc}")
    print("[CUBE] unpinned")
    return True


@command("refresh cube", aliases=["update cube", "rescan cube"],
         pack="window-management",
         risk_class="ui",
)
def handle_refresh_cube(app, remainder):
    """Rescans your open windows and redraws the numbered grid."""
    if not _pinned:
        return handle_show_cube(app, remainder)
    slots = _sync(app)
    _render(app, slots)
    print(f"[CUBE] refreshed -- {len(slots)} slots")
    return True


@command("cube page", aliases=["cube page two", "cube page one"],
         pack="window-management",
         risk_class="ui", param_schema={"page": {"type": "int", "required": False}},
)
def handle_cube_page(app, remainder):
    """Shows the next page of the numbered window grid, or the page you name."""
    global _page
    text = (remainder or "").strip().lower()
    page = None
    for word, value in _NUMBER_WORDS.items():
        if word in text:
            page = value
            break
    if page is None:
        for token in text.split():
            if token.isdigit():
                page = int(token)
                break
    if page is None:
        _speak(app, "Which page? Say cube page two.")
        return True
    _page = max(1, page)
    _render(app)
    print(f"[CUBE] page {_page}")
    return True


def _number_from(phrase: str, remainder: str):
    text = f"{phrase} {remainder or ''}".strip().lower()
    for token in text.split():
        if token.isdigit():
            return int(token)
        if token in _NUMBER_WORDS:
            return _NUMBER_WORDS[token]
    return None


def _make_bare_number_handler(word: str, value: int):
    def _handler(app, remainder):
        if not _numbers_active(app):
            # Not handled -- see _numbers_active for why this cannot stop the
            # phrase being consumed in the hands-free DICTATE lane.
            return False
        return _switch_to_number(app, value)
    _handler.__name__ = f"handle_cube_number_{value}"
    _handler.__doc__ = (
        f'Switches to window {value} in the numbered grid. '
        f'Active only while the grid is pinned and you are in command mode.'
    )
    return _handler


def _make_window_number_handler(value: int):
    def _handler(app, remainder):
        if not _pinned:
            return False
        return _switch_to_number(app, value)
    _handler.__name__ = f"handle_window_number_{value}"
    _handler.__doc__ = (f'Switches to window {value} in the numbered grid. '
                        f'Works in every mode while the grid is pinned.')
    return _handler


# "one".."nine" and "1".."9" -- own pack so the whole set can be disabled.
for _word, _value in _NUMBER_WORDS.items():
    command(_word, aliases=[str(_value)], pack="window-cube-numbers", scope={"tags": ["window_cube.visible"]},
            ai_visible=False, risk_class="ui")(_make_bare_number_handler(_word, _value))

# "window one".."window nine" -- unambiguous, so safe in every mode.
for _word, _value in _NUMBER_WORDS.items():
    command(f"window {_word}", aliases=[f"window {_value}"],
            pack="window-management",
            ai_visible=False, risk_class="ui")(_make_window_number_handler(_value))


# ---------------------------------------------------------------------------
# Delegation to the existing copy/tile handlers (number -> letter)
# ---------------------------------------------------------------------------

def _letters_for_numbers(app, numbers) -> list:
    """Translate cube numbers into the letters window_switcher's own mapping
    uses, refreshing that mapping first if it has never been built."""
    from plugins.commands import window_switcher as ws

    wanted = []
    for number in numbers:
        slot = _slot_for_number(number)
        if slot is None or slot["closed"]:
            return []
        wanted.append(slot["hwnd"])

    def _lookup():
        with ws._lock:
            return {hwnd: letter for letter, (hwnd, _t, _m) in ws._mapping.items()}

    by_hwnd = _lookup()
    if not all(h in by_hwnd for h in wanted):
        # Build the letter mapping from the SAME enumeration the switcher
        # uses, so the letters we hand it are the ones it will resolve.
        fresh = ws._assign_letters(ws._get_all_windows(ws._own_hwnd(app)))
        with ws._lock:
            ws._mapping = fresh
        by_hwnd = _lookup()

    letters = [by_hwnd.get(h) for h in wanted]
    return [l for l in letters if l] if all(letters) else []


def _numbers_in(text: str) -> list:
    out = []
    for token in (text or "").lower().replace(",", " ").split():
        if token.isdigit():
            out.append(int(token))
        elif token in _NUMBER_WORDS:
            out.append(_NUMBER_WORDS[token])
    return out


@command("cube copy", aliases=["cube copy into"], pack="window-management",
         risk_class="write", param_schema={"numbers": {"type": "int", "required": False}})
def handle_cube_copy(app, remainder):
    """Copies the text from one numbered window into another."""
    numbers = _numbers_in(remainder)
    if len(numbers) < 2:
        _speak(app, "Need two numbers. Say cube copy 2 into 4.")
        return True
    letters = _letters_for_numbers(app, numbers[:2])
    if len(letters) < 2:
        _speak(app, "Those windows are not available.")
        return True
    return handle_window_copy(app, f"{letters[0]} into {letters[1]}")


@command("cube tile", aliases=["cube tile and"], pack="window-management",
         risk_class="ui", param_schema={"numbers": {"type": "int", "required": False}})
def handle_cube_tile(app, remainder):
    """Arranges the numbered windows you name side by side."""
    numbers = _numbers_in(remainder)
    if len(numbers) < 2:
        _speak(app, "Need at least two numbers. Say cube tile 2 and 4.")
        return True
    letters = _letters_for_numbers(app, numbers)
    if len(letters) < 2:
        _speak(app, "Those windows are not available.")
        return True
    return handle_window_tile(app, " and ".join(letters))


# ---------------------------------------------------------------------------
# Test/introspection helpers (no side effects)
# ---------------------------------------------------------------------------

def _state_for_tests():
    with _lock:
        return {"pinned": _pinned, "page": _page, "slots": [dict(s) for s in _slots]}


def _reset_for_tests():
    global _slots, _pinned, _page, _app_ref
    with _lock:
        _slots = []
    _pinned = False
    _page = 1
    _app_ref = None


# Queue 68: the bare numbers above are scoped to this tag, so they are not even
# match CANDIDATES unless the cube is on screen (the Move A tribunal's M1). The
# panel has no close button and never takes focus, so "pinned" is "on screen".
# Registered at the end of the file so no decorator line number moves.
from samsara import command_scope as _command_scope  # noqa: E402
_command_scope.register_tag_source("window_cube.visible", lambda: bool(_pinned))
