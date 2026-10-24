"""823: ordinary selection keystrokes; last dictation requires exact readback."""
from __future__ import annotations

import re

from samsara import delivery_verify as dv


def _notice(app, text):
    from samsara.ava_edit import session
    session._chip(app, "edit_refused", {"reason": text})
    session._say(app, text)


def select_unit(unit):
    """Best effort keyboard conventions. Word's COM handler chooses its tier."""
    import pyautogui as keys
    if unit == "word":
        keys.hotkey("ctrl", "left")
        keys.hotkey("ctrl", "shift", "right")
    elif unit == "line":
        keys.press("home")
        keys.hotkey("shift", "end")
    else:
        return False
    return True


def select_range(proof, start, end):
    """Select a known range and confirm it before authorizing any deletion.

    Arrow keys count editor characters, which can differ for emoji/newlines.
    The final UTF-16 range check rejects those mismatches without deleting.
    """
    import pyautogui as keys
    state = proof[0]
    if dv.snapshot() != proof or state[3] != state[4]:
        return False
    caret = state[3]
    if not 0 <= start <= caret <= end <= len(state[2]) // 2:
        return False
    left = state[2][start * 2:caret * 2].decode("utf-16-le")
    selected = state[2][start * 2:end * 2].decode("utf-16-le")
    if not selected or not 0 <= start <= caret <= end:
        return False
    if left:
        keys.press("left", presses=len(left), interval=0)
    moved = dv.snapshot()
    if (moved is None or moved[1] != proof[1] or moved[0][:3] != state[:3]
            or moved[0][3:5] != (start, start)):
        return False
    keys.keyDown("shift")
    try:
        keys.press("right", presses=len(selected), interval=0)
    finally:
        keys.keyUp("shift")
    actual = dv.snapshot()
    return (actual is not None and actual[1] == proof[1]
            and actual[0][:3] == state[:3] and actual[0][3:5] == (start, end))


def select_sentence(app):
    proof = dv.snapshot()
    if proof is None or proof[0][3] != proof[0][4]:
        _notice(app, "Can't identify a sentence here. Try select line.")
        return True
    state = proof[0]
    before = state[2][:state[3] * 2].decode("utf-16-le")
    text = state[2].decode("utf-16-le")
    boundaries = [0] + [m.end() for m in re.finditer(r"[.!?](?:\s+|$)|\n+", text)]
    caret = len(before)
    start = max((n for n in boundaries if n < caret), default=0)
    end = next((n for n in boundaries if n >= caret and n > start), len(text))
    units = lambda n: len(text[:n].encode("utf-16-le")) // 2
    if not select_range(proof, units(start), units(end)):
        _notice(app, "Can't select that sentence here. Try select line.")
    return True


def last_delivery(app, *, delete=False):
    receipt = getattr(app, "_last_delivery", None)
    app._last_delivery = None  # consume once, including any refusal
    if receipt is None:
        _notice(app, "Can't use the last dictation - its text and cursor weren't verified.")
        return True
    proof, text = receipt
    state = proof[0]
    start = state[3] - len(text.encode("utf-16-le")) // 2
    if not select_range(proof, start, state[3]):
        _notice(app, "Can't use the last dictation - the text, cursor or target has changed.")
        return True
    if delete:
        import pyautogui
        # Recheck immediately before the destructive key, including last input.
        selected = dv.snapshot()
        if (selected is None or selected[1] != proof[1]
                or selected[0][:3] != state[:3] or selected[0][3:5] != (start, state[3])
                or dv.snapshot() != selected):
            _notice(app, "Can't delete that - the text, cursor or target has changed.")
            return True
        pyautogui.press("delete")
    return True


class SelectionHandler:
    def execute(self, cmd, ctx):
        action = cmd["selection_action"]
        if action in {"select that", "delete that"}:
            return last_delivery(ctx.app, delete=action == "delete that")
        if action == "sentence":
            return select_sentence(ctx.app)
        import pyautogui as keys
        keys.hotkey("ctrl", "shift", "left")
        return True
