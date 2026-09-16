"""Voice commands for named text: "insert <name>" and "save snippet <name>".

The expensive part of dictating a paragraph is saying it. "save snippet
<name>" names the paragraph you just dictated; "insert <name>" types it
again, at the cursor, for ever. For someone who cannot use a keyboard that
is the difference between re-saying a signature every time and saying two
words.

Delivery reuses `DictationApp._paste_preserving_clipboard` -- the app's ONE
text-delivery chokepoint, the same one hold-to-talk, the wake lane and the
session commit all go through. It is not a new typing routine and it is
deliberately not MacroHandler's `type` action, which is a bare
`pyautogui.typewrite` with no clipboard preservation, no elevated-window
refusal and no undo capture. See the queue 121 report.

A snippet's text is DATA. It is typed, never dispatched, whatever words it
contains -- there is no path from here back into the command matcher.
"""
from samsara import snippets
from samsara.log import get_logger
from samsara.plugin_commands import command
from samsara.command_registry import DispatchState

logger = get_logger(__name__)

#: A snippet is text, so its own delivery guard is the same one dictation
#: uses; nothing here needs a second policy.
_SIDE_EFFECTS = ["keystrokes", "clipboard", "file"]

NOTHING_TO_SAVE = (
    "Nothing to save: dictate the text first, then say \"save snippet\" and a name."
)


def _chip(app, label, kind):
    show = getattr(app, "_show_outcome_chip", None)
    if not callable(show):
        return
    try:
        show(label, kind)
    except Exception as exc:
        logger.debug("[SNIPPET] chip failed: %s", exc)


def _say(app, text):
    """Speak the outcome where the app can. Silent when it cannot -- a
    snippet must not fail because TTS is unavailable."""
    speaker = getattr(app, "_speak_session_notice", None)
    if not callable(speaker):
        return
    try:
        speaker(text, "confirmation")
    except Exception as exc:
        logger.debug("[SNIPPET] speech failed: %s", exc)


def last_dictation(app) -> str:
    """The last committed dictation, for "save snippet".

    Two sources, in this order:
      1. `app._last_dictation_text` -- what the delivery chokepoint recorded
         on the last successful insert. It is exactly what "undo that" would
         undo, which is the same mental model the user has for "that text I
         just said". It is cleared 60 s after delivery (the undo window).
      2. the history store, `type_filter='dictation', limit=1` -- the same
         lookup `open_correction_capture` uses for "the most recent
         dictation". This survives the undo window, so naming a paragraph
         five minutes later still works.
    Returns "" when there is nothing, and the caller refuses clearly.
    """
    live = getattr(app, "_last_dictation_text", None)
    if isinstance(live, str) and live.strip():
        return live
    store = getattr(app, "history_store", None)
    query = getattr(store, "query", None)
    if callable(query):
        try:
            rows = query(type_filter="dictation", limit=1)
            if rows:
                text = rows[0]["display_text"]
                if isinstance(text, str) and text.strip():
                    return text
        except Exception as exc:
            logger.debug("[SNIPPET] history lookup failed: %s", exc)
    return ""


@command(
    "insert",
    aliases=["insert snippet", "paste snippet"],
    pack="text-editing",
    risk_class="write",
    # A DECLARED, required, named argument -- not an undeclared remainder
    # string. Queue 107: an undeclared argument is what makes a command
    # invisible to Ava, because the menu cannot say what to pass it.
    param_schema={"name": {"type": "str", "required": True}},
    side_effects=_SIDE_EFFECTS,
    reversible=True,          # it is an ordinary paste: "undo that" undoes it
    ai_composable=False,
    voice_triggerable=True,
)
def insert_snippet(app, remainder):
    """Types a snippet you saved earlier, such as insert sign off.

    The name must match exactly. A near miss asks which one you meant and
    types nothing -- putting the wrong paragraph into your document without
    saying so would be worse than doing nothing.
    """
    name = (remainder or "").strip()
    if not name:
        names = [r.get("name") for r in snippets.load_snippets()][:3]
        hint = (" Try " + ", ".join(f'"insert {n}"' for n in names)) if names else ""
        _chip(app, "insert what?", "warning")
        _say(app, "Say insert and the name of a snippet." + hint)
        return DispatchState.FAILED

    records = snippets.load_snippets()
    record = snippets.find_snippet(name, records=records)
    if record is None:
        suggestions = snippets.suggest_names(name, records=records)
        if suggestions:
            _chip(app, f'did you mean "{suggestions[0]}"?', "warning")
            _say(app, f'No snippet called {name}. Did you mean {suggestions[0]}?')
        else:
            _chip(app, f'no snippet "{name}"', "error")
            _say(app, f"There is no snippet called {name}.")
        return DispatchState.FAILED

    deliver = getattr(app, "_paste_preserving_clipboard", None)
    if not callable(deliver):
        logger.error("[SNIPPET] no delivery path on the app")
        return DispatchState.FAILED
    if not deliver(record["text"]):
        # The chokepoint already logged why (elevated window, paste refused).
        _chip(app, "could not type it", "error")
        return DispatchState.FAILED

    snippets.record_use(record["id"])
    _chip(app, f"inserted {record['name']}", "success")
    return True


@command(
    snippets.SAVE_PHRASE,          # "save snippet", shared with the store
    aliases=["save as snippet", "name that snippet"],
    pack="text-editing",
    risk_class="write",
    param_schema={"name": {"type": "str", "required": True}},
    side_effects=["file"],
    reversible=True,          # delete it on the Snippets page
    ai_composable=False,
    voice_triggerable=True,
)
def save_snippet(app, remainder):
    """Saves what you just dictated under a name, such as save snippet sign off.

    The text is the last dictation Samsara delivered. Say the paragraph
    first, then name it.
    """
    name = (remainder or "").strip()
    if not name:
        _chip(app, "name it what?", "warning")
        _say(app, 'Say "save snippet" and the name you want to give it.')
        return DispatchState.FAILED

    text = last_dictation(app)
    if not text:
        _chip(app, "nothing to save", "warning")
        _say(app, NOTHING_TO_SAVE)
        return DispatchState.FAILED

    try:
        record = snippets.add_snippet(name, text, app=app)
    except snippets.SnippetError as exc:
        # The conflict is reported HERE, at save time, naming what it clashes
        # with -- not as a silent wrong answer later at use time.
        _chip(app, str(exc)[:40], "error")
        _say(app, str(exc))
        return DispatchState.FAILED

    words = len(text.split())
    _chip(app, f"saved {record['name']}", "success")
    _say(app, f'Saved {record["name"]}, {words} words. Say insert {record["name"]} to use it.')
    return True


@command("open snippets", aliases=["show my snippets", "snippet list"],
         pack="text-editing", risk_class="ui")
def open_snippet_list(app, remainder):
    """Opens the Snippets page, where your saved snippets are kept."""
    opener = getattr(app, "open_hub_page", None)
    if not callable(opener):
        return False
    # Imported here, not at module scope: a plugin is imported during command
    # registration, long before any Qt window exists.
    from samsara.ui.snippets_qt import SNIPPETS  # noqa: PLC0415
    return bool(opener(SNIPPETS))
