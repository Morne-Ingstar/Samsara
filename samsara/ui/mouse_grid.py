"""Mouse grid geometry and grammar (queue 71).

The universal last resort for pointing: a numbered grid over an area, say a
number, that cell subdivides, repeat. It needs NO accessibility information at
all, which is why every comparable tool ships one (Dragon's MouseGrid, Windows
Voice Access `show grid`, Talon). It is the only route to icon-only buttons,
canvases, games and video players -- the things `show numbers` (UI Automation,
queue 72) and `click <text>` (OCR, queue 70) structurally cannot see.

3x3, numbered in reading order, like Dragon and Voice Access:

    1 2 3
    4 5 6
    7 8 9

Why 3x3 and not 4x4 or the 9-of-many number pills:
  * Nine cells is one spoken digit per step, and the nine words already exist
    in the app's vocabulary (show_numbers._WORD_TO_NUM, the window cube).
  * Each step divides the remaining width AND height by three, so three steps
    are 1/27 of the screen: on the owner's 2560x1440 monitor that is 95x53 px
    after two steps and 32x18 px after three -- smaller than any toolbar icon.
    A 4x4 grid would need two spoken tokens for some cells ("twelve") or a
    letter alphabet; 3x3 keeps one short word per step.
  * Dragon's chained "MouseGrid 5 3 8" is exactly three steps for the same
    reason, and its numbering is reading order, so muscle memory transfers.

Everything here is pure: rectangles in and rectangles out, no Qt, no Win32.
Rectangles are (left, top, right, bottom) in PHYSICAL screen pixels -- the same
coordinate system UI Automation and SetCursorPos use (queue 56). The overlay
converts to Qt logical DIPs with numbers_overlay_qt.phys_to_logical at paint
time; nothing here ever deals in DIPs.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

ROWS = 3
COLS = 3
CELLS = ROWS * COLS

#: A refinement step stops when the cell is this small: further subdivision
#: would be below the smallest thing a pointer needs to hit, and the centre is
#: already inside it.
MIN_CELL_PX = 6

_NUMBER_WORDS = {
    "one": 1, "won": 1, "1": 1,
    "two": 2, "too": 2, "to": 2, "2": 2,
    "three": 3, "tree": 3, "3": 3,
    "four": 4, "for": 4, "fore": 4, "4": 4,
    "five": 5, "5": 5,
    "six": 6, "sicks": 6, "6": 6,
    "seven": 7, "7": 7,
    "eight": 8, "ate": 8, "8": 8,
    "nine": 9, "9": 9,
}

#: What to do once the pointer is at the final cell centre. The pointer is
#: moved on EVERY step, so "move" is simply "stop here" -- which is how this
#: composes with a later Interact-style verb ("the pointer names the target,
#: the voice supplies the verb").
ACTIONS = ("click", "right", "double", "move")

_ACTION_WORDS = {
    "click": "click", "press": "click", "tap": "click", "left click": "click",
    "click it": "click", "click that": "click", "click here": "click",
    "right click": "right", "right": "right", "context menu": "right",
    "right click it": "right", "right click here": "right",
    "double click": "double", "double": "double", "double click it": "double",
    "double click here": "double", "open it": "double",
    "move": "move", "move here": "move", "stop": "move", "park": "move",
    "park here": "move", "move mouse": "move", "just move": "move",
    "hover": "move", "hover here": "move",
}

#: Scope words: which area the first grid covers.
SCOPE_SCREEN = "screen"
SCOPE_WINDOW = "window"
SCOPE_MONITOR = "monitor"

#: Undo one refinement -- the step back after a mis-heard number.
_BACK_WORDS = frozenset({"back", "undo", "up", "oops", "go back", "back up", "grid back"})

_SCOPE_WORDS = {
    "screen": SCOPE_SCREEN, "this screen": SCOPE_SCREEN, "whole screen": SCOPE_SCREEN,
    "full screen": SCOPE_SCREEN, "display": SCOPE_SCREEN, "desktop": SCOPE_SCREEN,
    "window": SCOPE_WINDOW, "here": SCOPE_WINDOW, "this window": SCOPE_WINDOW,
    "in here": SCOPE_WINDOW, "on this window": SCOPE_WINDOW,
}


def word_to_digit(token: str) -> Optional[int]:
    """1-9 for one spoken number word or digit, else None."""
    return _NUMBER_WORDS.get(token.strip().lower())


def cell_rect(rect: tuple, digit: int) -> tuple:
    """The sub-rectangle of `rect` for `digit` (1-9, reading order).

    Splits are computed from the edges, not by multiplying a rounded cell
    size, so the nine cells tile the rectangle exactly with no seam and no
    drift after several levels.
    """
    if not 1 <= int(digit) <= CELLS:
        raise ValueError(f"grid digit must be 1..{CELLS}, got {digit!r}")
    left, top, right, bottom = (int(v) for v in rect)
    index = int(digit) - 1
    row, col = divmod(index, COLS)
    width, height = right - left, bottom - top
    x0 = left + round(width * col / COLS)
    x1 = left + round(width * (col + 1) / COLS)
    y0 = top + round(height * row / ROWS)
    y1 = top + round(height * (row + 1) / ROWS)
    return x0, y0, x1, y1


def cells(rect: tuple) -> list:
    """[(digit, sub-rect), ...] for all nine cells, in reading order."""
    return [(d, cell_rect(rect, d)) for d in range(1, CELLS + 1)]


def center(rect: tuple) -> tuple:
    """Centre point (x, y) of a rectangle, in physical pixels."""
    left, top, right, bottom = (int(v) for v in rect)
    return (left + right) // 2, (top + bottom) // 2


def too_small(rect: tuple) -> bool:
    left, top, right, bottom = (int(v) for v in rect)
    return (right - left) <= MIN_CELL_PX or (bottom - top) <= MIN_CELL_PX


def refine(rect: tuple, digits: Iterable[int]) -> tuple:
    """Apply a whole sequence of digits: the chained path in one call.

    `refine(screen, [5, 3, 8])` is exactly what saying "five", "three", "eight"
    one at a time produces -- the test for chaining asserts that equality.
    """
    out = tuple(int(v) for v in rect)
    for digit in digits:
        if too_small(out):
            break
        out = cell_rect(out, int(digit))
    return out


def order_monitors(rects: Iterable[tuple]) -> list:
    """[(number, rect), ...] numbered left-to-right, then top-to-bottom.

    The owner runs three monitors at mixed positions, so "monitor two" has to
    mean something stable: position on the desktop, not the order Windows
    happens to enumerate them in.
    """
    ordered = sorted(rects, key=lambda r: (int(r[0]), int(r[1])))
    return [(i, tuple(int(v) for v in r)) for i, r in enumerate(ordered, 1)]


def parse(text: str) -> dict:
    """Parse a grid utterance into {scope, monitor, digits, action}.

    Accepts the whole chain in one breath, which is what makes this usable
    rather than tedious (Dragon's is chainable for the same reason):

        "grid"                        -> level 1 on the active monitor
        "grid window"                 -> level 1 over the focused window
        "grid monitor two"            -> level 1 on the second monitor
        "five"                        -> refine (bare digits, while visible)
        "grid five three eight click" -> three refinements, then a left click
        "grid 5 3 8 move"             -> the same, pointer parked, no click

    The leading command word is optional, so the same parser handles both the
    command remainder and a refinement utterance. Unknown words are ignored
    rather than failing: "grid, uh, five" still means five.
    """
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    scope = None
    monitor = None
    digits: list = []
    action = None
    back = False
    i = 0
    while i < len(words):
        word = words[i]
        two = " ".join(words[i:i + 2])
        if word in ("grid", "mousegrid") or two in ("mouse grid", "show grid"):
            i += 2 if two in ("mouse grid", "show grid") else 1
            continue
        if two in _SCOPE_WORDS:
            scope = _SCOPE_WORDS[two]
            i += 2
            continue
        if word in _SCOPE_WORDS:
            scope = _SCOPE_WORDS[word]
            i += 1
            continue
        if word in ("monitor", "display") and scope is None:
            scope = SCOPE_MONITOR
            i += 1
            continue
        if two in _BACK_WORDS:
            back = True
            i += 2
            continue
        if word in _BACK_WORDS:
            back = True
            i += 1
            continue
        if two in _ACTION_WORDS:
            action = _ACTION_WORDS[two]
            i += 2
            continue
        if word in _ACTION_WORDS:
            action = _ACTION_WORDS[word]
            i += 1
            continue
        digit = word_to_digit(word)
        if digit is not None:
            # A number right after "monitor" selects the monitor, not a cell.
            if scope == SCOPE_MONITOR and monitor is None and not digits:
                monitor = digit
            else:
                digits.append(digit)
            i += 1
            continue
        i += 1
    if monitor is not None and scope is None:
        scope = SCOPE_MONITOR
    return {"scope": scope, "monitor": monitor, "digits": digits, "action": action, "back": back}


def describe(rect: tuple) -> str:
    left, top, right, bottom = (int(v) for v in rect)
    return f"{right - left}x{bottom - top} at ({left},{top})"
