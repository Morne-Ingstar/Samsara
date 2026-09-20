"""Focused Microsoft Word commands.

The keyboard tier is deliberately small and explicit.  Every entry checks the
foreground executable itself immediately before injecting keys; these commands
must never land in a browser, terminal, or the Samsara window.  The COM tier
only attaches to an already running Word instance and operates through its
current Selection.
"""
from __future__ import annotations

import threading
from typing import Callable

import pyautogui

from samsara.command_scope import capture_context
from samsara.plugin_commands import command


WORD_EXE = "winword.exe"
COM_TIMEOUT_S = 1.5


def _report(app, text: str, kind: str = "warning") -> None:
    """Show and speak a short operational result without raising."""
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        try:
            show(text, kind)
        except Exception:
            pass
    coordinator = getattr(app, "audio_coordinator", None)
    speak = getattr(coordinator, "speak", None)
    if callable(speak):
        try:
            speak(text, category="agent_response", interruptible=False)
            return
        except Exception:
            pass
    engine = getattr(app, "tts_engine", None)
    speak = getattr(engine, "speak", None)
    if callable(speak):
        try:
            speak(text)
        except Exception:
            pass


def _word_is_focused() -> bool:
    context = capture_context()
    return bool(context.resolved and context.exe == WORD_EXE)


def _require_word_focus(app) -> bool:
    if _word_is_focused():
        return True
    _report(app, "Word isn't focused")
    return False


def _shortcut(app, *keys: str) -> bool:
    if not _require_word_focus(app):
        return False
    try:
        pyautogui.hotkey(*keys)
    except Exception:
        _report(app, "Word couldn't receive that shortcut")
        return False
    return True


def _com_call(action: Callable[[object], object]) -> tuple[str, object | None]:
    """Run one Selection-only operation on a daemon COM worker.

    COM calls can hang behind a Word modal dialog.  The command worker waits a
    bounded time, then returns a spoken/chip failure while the daemon thread is
    allowed to unwind naturally.  CoInitialize belongs inside that worker
    thread, never at module import.
    """
    done = threading.Event()
    result: dict[str, object] = {}

    def worker() -> None:
        pythoncom = None
        initialized = False
        try:
            import pythoncom as _pythoncom  # noqa: PLC0415
            from win32com.client import GetActiveObject  # noqa: PLC0415
            pythoncom = _pythoncom
        except Exception:
            result["status"] = "unavailable"
            done.set()
            return
        try:
            pythoncom.CoInitialize()
            initialized = True
            try:
                word = GetActiveObject("Word.Application")
            except Exception:
                result["status"] = "not_open"
                return
            try:
                result["value"] = action(word)
                result["status"] = "ok"
            except Exception:
                result["status"] = "error"
        except Exception:
            result["status"] = "error"
        finally:
            if initialized:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
            done.set()

    thread = threading.Thread(target=worker, name="word-command-com", daemon=True)
    thread.start()
    if not done.wait(COM_TIMEOUT_S):
        return "timeout", None
    return str(result.get("status", "error")), result.get("value")


def _selection_call(app, action: Callable[[object], object]) -> tuple[bool, object | None]:
    if not _require_word_focus(app):
        return False, None

    def on_word(word):
        return action(word.Selection)

    status, value = _com_call(on_word)
    if status == "ok":
        return True, value
    messages = {
        "not_open": "Word isn't open",
        "unavailable": "Word automation unavailable",
        "timeout": "Word automation timed out",
        "error": "Word couldn't complete that",
    }
    _report(app, messages.get(status, "Word automation unavailable"))
    return False, None


@command("font bigger", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def font_bigger(app, remainder=""):
    """Increases the selected Word text's font size."""
    return _shortcut(app, "ctrl", "]")


@command("font smaller", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def font_smaller(app, remainder=""):
    """Decreases the selected Word text's font size."""
    return _shortcut(app, "ctrl", "[")


@command("bold that", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def bold_that(app, remainder=""):
    """Toggles bold for the selected Word text."""
    return _shortcut(app, "ctrl", "b")


@command("italic that", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def italic_that(app, remainder=""):
    """Toggles italics for the selected Word text."""
    return _shortcut(app, "ctrl", "i")


@command("underline that", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def underline_that(app, remainder=""):
    """Toggles underline for the selected Word text."""
    return _shortcut(app, "ctrl", "u")


@command("single spacing", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def single_spacing(app, remainder=""):
    """Applies single line spacing in Word."""
    return _shortcut(app, "ctrl", "1")


@command("one and a half spacing", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def one_and_a_half_spacing(app, remainder=""):
    """Applies one-and-a-half line spacing in Word."""
    return _shortcut(app, "ctrl", "5")


@command("double spacing", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def double_spacing(app, remainder=""):
    """Applies double line spacing in Word."""
    return _shortcut(app, "ctrl", "2")


@command("align left", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def align_left(app, remainder=""):
    """Aligns the current Word paragraph left."""
    return _shortcut(app, "ctrl", "l")


@command("align center", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def align_center(app, remainder=""):
    """Centers the current Word paragraph."""
    return _shortcut(app, "ctrl", "e")


@command("align right", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def align_right(app, remainder=""):
    """Aligns the current Word paragraph right."""
    return _shortcut(app, "ctrl", "r")


@command("justify text", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def justify_text(app, remainder=""):
    """Justifies the current Word paragraph."""
    return _shortcut(app, "ctrl", "j")


@command("bullet list", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def bullet_list(app, remainder=""):
    """Toggles the selected Word paragraphs as a bulleted list."""
    return _shortcut(app, "ctrl", "shift", "l")


@command("select paragraph in word", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def select_paragraph(app, remainder=""):
    """Extends the Word selection through the current paragraph."""
    return _shortcut(app, "ctrl", "shift", "down")


@command("undo in word", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def undo_in_word(app, remainder=""):
    """Undoes the last Word edit without invoking Samsara dictation history."""
    return _shortcut(app, "ctrl", "z")


@command("redo in word", pack="word", risk_class="ui", reversible=True,
         side_effects=["keystrokes"], preconditions=["word_focused"])
def redo_in_word(app, remainder=""):
    """Redoes the last Word edit without invoking Samsara dictation history."""
    return _shortcut(app, "ctrl", "y")


@command("numbered list", pack="word", risk_class="ui", reversible=True,
         side_effects=["word_com"], preconditions=["word_focused"])
def numbered_list(app, remainder=""):
    """Toggles numbering on the current Word selection."""
    def toggle(selection):
        listing = selection.Range.ListFormat
        if int(getattr(listing, "ListType", 0) or 0):
            listing.RemoveNumbers()
        else:
            listing.ApplyNumberDefault()
    ok, _ = _selection_call(app, toggle)
    return ok


@command("font size", pack="word", risk_class="ui", reversible=True,
         side_effects=["word_com"], preconditions=["word_focused"],
         param_schema={"size": {"type": "int", "required": True, "min": 6, "max": 72}})
def font_size(app, remainder=""):
    """Sets selected Word text to a font size from 6 through 72."""
    try:
        size = int(str(remainder).strip())
    except (TypeError, ValueError):
        _report(app, "Font size must be from 6 to 72")
        return False
    if not 6 <= size <= 72:
        _report(app, "Font size must be from 6 to 72")
        return False
    ok, _ = _selection_call(app, lambda selection: setattr(selection.Font, "Size", size))
    return ok


_SPACING_RULES = {"1": 0, "1.0": 0, "1.5": 1, "2": 2, "2.0": 2}


@command("line spacing", pack="word", risk_class="ui", reversible=True,
         side_effects=["word_com"], preconditions=["word_focused"],
         param_schema={"spacing": {"type": "number", "required": True, "enum": [1, 1.5, 2]}})
def line_spacing(app, remainder=""):
    """Sets selected Word paragraphs to 1, 1.5, or 2 line spacing."""
    raw = str(remainder).strip()
    rule = _SPACING_RULES.get(raw)
    if rule is None:
        _report(app, "Line spacing must be 1, 1.5, or 2")
        return False
    ok, _ = _selection_call(
        app, lambda selection: setattr(selection.ParagraphFormat, "LineSpacingRule", rule))
    return ok


@command("set font", pack="word", risk_class="ui", reversible=True,
         side_effects=["word_com"], preconditions=["word_focused"],
         param_schema={"name": {"type": "text", "required": True}})
def set_font(app, remainder=""):
    """Sets selected Word text to an installed font by name."""
    heard = str(remainder).strip()
    if not heard:
        _report(app, "Say the font name")
        return False

    def choose_font(selection):
        fonts = [str(name) for name in selection.Application.FontNames]
        matches = {name.casefold(): name for name in fonts}
        selected = matches.get(heard.casefold())
        if selected is None:
            return None
        selection.Font.Name = selected
        return selected

    ok, selected = _selection_call(app, choose_font)
    if not ok:
        return False
    if selected is None:
        _report(app, f"Word doesn't have {heard}")
        return False
    return True


def _select_unit(app, unit: int) -> bool:
    ok, _ = _selection_call(app, lambda selection: selection.Expand(unit))
    return ok


@command("select sentence", pack="word", risk_class="ui", reversible=True,
         side_effects=["word_com"], preconditions=["word_focused"])
def select_sentence(app, remainder=""):
    """Selects the current Word sentence."""
    return _select_unit(app, 3)  # wdSentence


@command("select line", pack="word", risk_class="ui", reversible=True,
         side_effects=["word_com"], preconditions=["word_focused"])
def select_line(app, remainder=""):
    """Selects the current Word line."""
    return _select_unit(app, 5)  # wdLine


@command("select word", pack="word", risk_class="ui", reversible=True,
         side_effects=["word_com"], preconditions=["word_focused"])
def select_word(app, remainder=""):
    """Selects the current Word word."""
    return _select_unit(app, 2)  # wdWord


@command("word count", pack="word", risk_class="ui", reversible=True,
         side_effects=["word_com"], preconditions=["word_focused"])
def word_count(app, remainder=""):
    """Speaks the word count of the current Word selection."""
    ok, count = _selection_call(app, lambda selection: int(selection.Words.Count))
    if not ok:
        return False
    _report(app, f"{count} words", "success")
    return True
