"""Home page of the Samsara hub window (queue 26).

Samsara controls the computer; dictation is one thing it does. Home makes
that obvious at a glance, top to bottom:

    1. STATE ROW      one compact row (41): the live mark, ONE plain state
                      line read from RUNTIME state (never from config
                      alone), ONE instruction line generated from config
                      and the catalog, and ONE enabled primary control. The
                      headline and the control both come from STATE_TABLE
                      (74), so no state can contradict itself.
    2. LATEST         the newest dictation, labelled, with Show all / Copy /
                      Open History and a secondary Teach a word (74); beneath
                      it the newest non-dictation outcome from
                      DictationApp._outcome_ring, kind first ("Ran a
                      command", "Didn't catch") with Why?. No Undo: see the
                      note on _build_last_outcome.
    3. WHAT YOU CAN DO six cards from the live command catalog, each with a
                      one-line description and a label naming its
                      destination (CARD_ROUTES, 74).
    4. IDENTITY STRIP words today, the infinity card and the creed --
                      the owner's own elements, cut by 86 and restored
                      by 89 -- plus the small text links (website &
                      docs, GitHub) in the same row as the creed, and
                      the Ko-fi line beneath it.

Every text size comes from theme.HOME_TYPE_SCALE; spoken phrases are always
shown in double quotes (quoted()).

Every actionable element is a QPushButton whose visible text equals its
accessible name (WCAG Label in Name), at least 44 px tall, in top-to-bottom
tab order. Nothing is hover-only.

Runtime sources (the "what will happen when I speak" facts):
    listening   DictationApp.recording / snoozed / wake_word_active /
                continuous_active / command_mode_active / toggle_active --
                the same flags _tray_mark reads (dictation.py) -- plus
                available_mics vs config['microphone'] for mic presence.
    destination ava_command_session_active / ava_mode_active /
                command_mode_active, SessionModeManager.mode, and
                SessionModeManager._dictate_target_hwnd (session_modes.py)
                resolved to the window title.
"""
from __future__ import annotations

import datetime as _dt
import random
import re
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from samsara import __version__, command_catalog, config_defaults
from samsara.log import get_logger
from samsara.session_modes import CHIP_CHECK, CHIP_CROSS
from samsara.ui import home_signals, theme
from samsara.ui.command_cheatsheet_qt import UNAVAILABLE_TEXT
# One wrapping row layout, shared with the History view that introduced it
# (queue 79): controls wrap to the next line instead of clipping.
from samsara.ui.history_view import _FlowLayout as FlowLayout
from samsara.ui.tray_qt import MarkFrame, paint_mark

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Layout constants: one column, 16/24 grid, cards on BG1
# ---------------------------------------------------------------------------

CONTENT_MAX_W = 760
GRID = 16
GRID_LG = 24
STATE_MARK_PX = 44
MIN_TARGET = 44
TYPE = theme.HOME_TYPE_SCALE
OUTCOME_RING_MAX = 8
BASE_POINT_SIZE = 9.0   # Segoe UI 9 pt is Windows' 100% text size

#: The cards' routing table (74): (visible label, action, arg, glyph). A
#: card's label names where it TAKES you, never an action it cannot start
#: ("Ask Ava" opened Ava settings). The label must begin with the verb its
#: action kind implies (DESTINATION_VERBS); capability_cards() and the tests
#: both read this one table, so a label and its route cannot drift apart.
#: Queue 86 cut this to the THREE destinations Home keeps: control the
#: windows, find help, teach it words. "Configure hands-free" and "Configure
#: dictation" are gone -- hands-free is information about the wake word and
#: now lives in Guides; dictation is what the app IS, and belongs in the
#: tagline. "Configure Ava" is gone too: the Ava presence row links there.
#: Queue 89: "View the command list" (the suggestion slot's button) and
#: "View window commands" (a card) were two doors into the same room. There
#: is now ONE: "View commands", which opens the whole list unfiltered.
#: Window commands are a section inside that list, not a second entry point.
#: Queue 111 (Astra's second Home review, owner agreed): "View commands",
#: "Show guides & help" and "Voice help" overlapped conceptually -- a new
#: user could not tell which one held command instructions and which
#: diagnosed recognition problems. The labels now state PURPOSE, as bare
#: nouns: "Commands", "Guides & tutorials", "Voice troubleshooting". The
#: destinations are unchanged; only the words are.
CARD_ROUTES = (
    ("Commands", "cheatsheet", None, "windows"),
    ("Guides & tutorials", "guides", None, "guides"),
    ("Open Dictionary", "page", "Dictionary", "words"),
)
DESTINATION_VERBS = {"cheatsheet": "View", "settings": "Configure", "page": "Open", "guides": "Show"}
#: Every verb a label could open with that PROMISES something. A label
#: starting with one of these is making a claim about what it does, and the
#: claim has to be the right one for its route.
KNOWN_VERBS = frozenset(DESTINATION_VERBS.values()) | {"Ask", "Run", "Start", "Edit", "Add"}
CAPABILITY_TITLES = tuple(route[0] for route in CARD_ROUTES)


def label_names_destination(label: str, action: str, arg) -> bool:
    """True when a card label is honest about its route.

    The rule this enforces changed in 111, and the reason it exists did not.
    It was written (86) because "Ask Ava" opened Ava SETTINGS: a label that
    opens with a verb promises an action, and the card could not perform the
    one it named. What has to be prevented is a label promising the WRONG
    thing -- not a label declining to promise anything.

    So: a label that opens with a known verb must open with the RIGHT verb
    for its action kind, and a hub page card must name its page. A bare
    destination noun ("Commands") promises no action at all and is honest by
    construction -- it names the room, which is exactly what a card does.

    An empty label is never honest: a card the user cannot name is a card
    they cannot ask for by voice.
    """
    if not label:
        return False
    verb = DESTINATION_VERBS.get(action)
    if verb is None:
        return False
    first = label.split(" ", 1)[0]
    if first in KNOWN_VERBS:
        if first != verb:
            return False
        return action != "page" or label == f"{verb} {arg}"
    # A bare noun. It must still name the page it opens.
    return action != "page" or label == str(arg)


def card_filter_is_honest(label: str, action: str, arg) -> bool:
    """A cheat-sheet card that applies a filter must NAME that filter (89).
    "View window commands" opened a list filtered to "window"; "View
    commands" names no filter, so it must open all of them."""
    if action != "cheatsheet" or not arg:
        return True
    return str(arg).lower() in label.lower()


#: The one name for the command list, wherever the door is (95). It is
#: written once, in CARD_ROUTES, and everything else borrows it.
COMMAND_LIST_LABEL = CARD_ROUTES[0][0]


def slot_action_label(label: str, action: str, arg) -> str:
    """The label Home paints on the suggestion slot's button.

    89 merged the two command-list cards into one door called "View
    commands". The hint signals still ask for that door by two older names:
    "View the command list" and "View window commands" -- and the second has
    not applied a window filter since 89, its arg is "". One door under three
    names is the same redundancy one step down the page, and the third name
    is now a lie about where it goes. Every unfiltered cheat-sheet action is
    therefore painted with the one name. A genuinely filtered one keeps its
    own label, which card_filter_is_honest already makes name its filter.

    Queue 111 extends the same treatment to the Voice help door. The hint
    signals ask for it as "Open Voice help" (home_signals.py:436); Home now
    calls it "Voice troubleshooting" everywhere else, and a slot button that
    disagreed with the notice button above it would be the same redundancy
    this function was written to kill. home_signals.py belongs to queue 109,
    so the literal is normalised HERE rather than edited there -- which is
    also why this function already exists.
    """
    if action == "cheatsheet" and not arg:
        return COMMAND_LIST_LABEL
    if action == "page" and arg == VOICE_HELP:
        return OPEN_VOICE_HELP
    return label
#: The guidance hub behind the "Guides & help" card (40): visible label ->
#: (app method or settings tab). Each is an existing surface.
GUIDES = (
    ("Command reference", "cheatsheet", None),
    ("Quick reference", "app", "open_quick_reference"),
    ("Tutorial", "app", "show_tutorial"),
    ("Help & support", "settings", "Help & Support"),
)
#: Queue 86: Home's own vocabulary.
TITLE = "Samsara"
WHERE_TO_GO = "Where to go"
PROBLEM_NOTICE = "Problem notice"
#: Queue 111: the Home button that opens Voice help says what it is FOR.
#: "Open Voice help" sat beside "View commands" and "Show guides & help"
#: and a new user could not tell which of the three diagnosed a
#: recognition problem. VOICE_HELP below is the page KEY and is
#: deliberately NOT renamed -- see its own note.
OPEN_VOICE_HELP = "Voice troubleshooting"
#: The nav page name, so Home, the notice and the hints all route to the
#: same destination string the hub registers.
#:
#: Queue 111 renamed what Home SAYS (OPEN_VOICE_HELP) and deliberately not
#: this. It is an identity, not a label: main_window_qt registers the nav
#: row under it, _open_page routes by it, and home_signals.py:436 hard-
#: codes arg="Voice help" -- and home_signals is queue 109's file, which
#: this prompt may report on but not edit. Changing the key here would
#: route that hint at a page that no longer exists. The nav row and the
#: page heading therefore still read "Voice help"; see the 111 report.
VOICE_HELP = "Voice help"
#: Queue 92's memo list, registered the same way: one string that Home, the
#: hub nav and the tray all route through, so none of them can drift.
MEMOS = "Memos"
SUGGESTION = "Suggestion"
ANOTHER_SUGGESTION = "Another suggestion"
HIDE_SUGGESTION = "Hide this suggestion"
#: Queue 111. The slot used to print its own working underneath the
#: sentence -- "Because: 4/8 recent captures recorded outcome empty or gated
#: (threshold 3)". That is the debugger's view: it names internal outcome
#: kinds, cites a threshold nobody set, and tells the user nothing to DO.
#: The sentence now carries an ACTION and the working moves behind this
#: disclosure, collapsed, reachable by keyboard like any other button.
DETAILS = "Details"
#: What the user should do about each diagnostic, keyed by Hint.id.
#:
#: Every one of these stays CONDITIONAL, and diag.no_text is why the rule
#: exists: its counter combines the `empty` and `gated` outcome kinds, and a
#: gated capture may be a cough, a door, or the user deciding not to speak
#: after all. The evidence does not establish that intended speech was lost,
#: so the sentence may not assert it was. "If you were speaking" is the
#: whole difference between a diagnosis and a guess wearing one's clothes.
#:
#: {door} is filled with whatever the slot's button actually says, so the
#: sentence can never name a door by a name the button has stopped using.
HINT_ACTIONS = {
    "diag.no_text": "If you were speaking, open {door} to investigate.",
    "diag.misses": "If those were meant as commands, open {door} to check the wording.",
    "diag.corrections_waiting": "Open {door} when you have a moment to review them.",
}
DEFAULT_HINT_ACTION = "Open {door} to look into it."


def hint_action_sentence(hint_id: str, door: str) -> str:
    """The slot's second line: what to DO, never the evidence (111)."""
    if not door:
        return ""
    template = HINT_ACTIONS.get(hint_id, DEFAULT_HINT_ACTION)
    return template.format(door=door)
AVA_NAME = "Ava"
AVA_GUIDE = "Open Ava guide"
AVA_SETTINGS = "Configure Ava"
AVA_MARK_PX = 36
GUIDE_QUICK_REFERENCE = "Quick reference"
#: Dismissals are remembered in QSettings, not config.json: Home must never
#: write the running app's config file.
SETTINGS_ORG = "Samsara"
SETTINGS_APP = "Samsara"
DISMISSED_KEY = "home/dismissed_suggestions"

ANY_TEXTBOX = "whatever you're typing in"
STOP_LISTENING = "Stop listening"
STOPPING = "Stopping"
PAUSE_HANDS_FREE = "Pause hands-free"
PAUSE_LISTENING = "Pause listening"
RESUME_LISTENING = "Resume listening"
PAUSED_INSTRUCTION = "Hotkeys and the wake word are off until you resume."
MIC_NOT_FOUND = "Microphone not found."
# The latest dictation block (74).
LATEST_DICTATION = "Latest dictation"
NOTHING_DICTATED = "Nothing dictated yet."
SHOW_ALL = "Show all"
SHOW_LESS = "Show less"
COPY = "Copy"
COPIED = "Copied."
OPEN_HISTORY = "Open History"
TEACH_A_WORD = "Teach a word"

#: The state panel's ONE mapping (74): state id -> (headline, primary control
#: label, primary action). Each state has exactly one headline and one
#: enabled primary control, so no state can say "waiting" beside a disabled
#: Stop and "Nothing to stop". Pausing is real in the idle states too: a
#: snooze blocks every dictation hotkey (dictation.py on_key_press).
#: listening_state_id() picks the row; the order there is the precedence.
STATE_TABLE = {
    "recording":   ("Listening now.", STOP_LISTENING, "stop"),
    "ava_session": ("Ava is listening.", STOP_LISTENING, "stop"),
    "hands_free":  ("Hands-free is on.", STOP_LISTENING, "stop"),
    "continuous":  ("Listening continuously.", STOP_LISTENING, "stop"),
    "paused":      ("Listening is paused.", RESUME_LISTENING, "resume"),
    "waiting":     ("Waiting for {phrase}.", PAUSE_HANDS_FREE, "pause"),
    "ready":       ("Ready.", PAUSE_LISTENING, "pause"),
}
# The outcome row's kind labels (41, owner's words): the kind carries the
# weight, the content is secondary.
KIND_DICTATED = "Dictated"
KIND_RAN = "Ran a command"
KIND_MISSED = "Didn't catch"
NOTHING_YET = "Nothing yet."
CARD_DESCRIPTIONS = {
    "Commands": "{count}, including windows, text and music.",
    "Open Dictionary": "Fix names and words it mishears, so they come out right.",
    # Hands-free moved here from its own card (86): it is information about
    # the wake word, and Guides is where information lives.
    "Guides & tutorials": "Command list, quick reference, tutorial, support, "
                          "and how to talk without holding a key.",
}
AVA_WHERE = {
    (True, True): "Runs on your own machine, or your own API key.",
    (True, False): "Runs on your own machine.",
    (False, True): "Runs on your own API key.",
    (False, False): "Turned off; set it up in Settings.",
}
# How soon the page re-reads runtime state after a control is pressed, so
# the mark, the state line and the button itself change within 200 ms.
FEEDBACK_MS = 150
# How long "Stopping" may stay disabled while the capture is still reported
# running. After this the control re-enables as Stop listening, so a stop
# path that failed or hung can never leave a dead primary button.
STOP_GRACE_MS = 3000

# One icon vocabulary for the hub's nav rows and Home's capability cards
# (38): plain glyphs from Segoe UI Symbol, built with chr() so this file
# stays ASCII, painted in the theme's text tokens by glyph_icon().
ICON_GLYPHS = {
    "home": chr(0x2302),        # house
    "history": chr(0x21BA),     # anticlockwise arrow
    "dictionary": chr(0x2261),  # three lines
    "settings": chr(0x2699),    # gear
    "windows": chr(0x25A3),     # square in square
    "hands_free": chr(0x25C9),  # fisheye
    "dictate": chr(0x270E),     # pencil
    "ava": chr(0x2726),         # four-pointed star
    "words": chr(0x2261),       # three lines (same as dictionary)
    "guides": chr(0x2139),      # information source
    "memos": chr(0x266B),       # beamed eighth notes -- something recorded
    "snippets": chr(0x2261),    # three lines -- a stored block of text (121)
}
CARD_GLYPHS = tuple(route[3] for route in CARD_ROUTES)
WORDS_TODAY = "words today"
WORDS_REMAINING = "words remaining"
INFINITY = chr(0x221E)
# Glyphs via chr() so this file stays pure ASCII (non-ASCII literals have
# caused encoding trouble on this machine; see session_modes.py).
MIDDLE_DOT = chr(0xB7)
EM_DASH = chr(0x2014)
CREED = f"Free {MIDDLE_DOT} Open source {MIDDLE_DOT} Accessibility first"
# Interpunct-free form for callers that cannot show the middle dot.
CREED_ASCII = "Free - Open source - Accessibility first"
_CATALOG_UNAVAILABLE_SHORT = "command list unavailable"

#: The conditional slot (89). It renders ONLY when it has something real to
#: say, and collapses to ZERO height otherwise -- no padding, no placeholder,
#: no manufactured tip. Priority: a DIAGNOSTIC hint (86's tier 1, which cites
#: its evidence and never fires on a single occurrence), else a one-shot
#: "what's new" note after the version changes, else nothing. Generic command
#: tips do NOT belong here: the status strip's marquee owns those.
SUGGESTION_HEADING = {"diagnostic": "Suggestion", "whatsnew": "What's new"}
DISMISS = "Dismiss"
CHANGELOG_PATH = Path(__file__).resolve().parent.parent.parent / "CHANGELOG.md"
#: QSettings key holding the version whose note has already been seen.
WHATS_NEW_SEEN_KEY = "home/whats_new_seen"
#: A heading like "## [0.23.0-beta.1] - 2026-09-13". "[Unreleased]" is
#: skipped: it is not a version anybody is running.
_RELEASE_HEADING = re.compile(r"^##\s*\[(?P<version>[^\]]+)\]")
_UNRELEASED = "unreleased"


def changelog_summary(path: Optional[Path] = None) -> tuple:
    """(version, one-sentence summary) from the top released section of
    CHANGELOG.md, or (None, None).

    The summary is the first sentence of the section's own lead paragraph --
    the prose the release was written with. A release with no lead paragraph
    (straight into "### Added") gets no note rather than a bulleted fragment:
    an empty slot is honest, a manufactured one is not.
    """
    path = Path(path) if path is not None else CHANGELOG_PATH
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.debug(f"changelog_summary: {exc}")
        return None, None
    version = None
    paragraph = []
    for line in lines:
        match = _RELEASE_HEADING.match(line)
        if match:
            if version is not None:
                break
            found = match.group("version").strip()
            if found.lower() == _UNRELEASED:
                continue
            version = found
            continue
        if version is None:
            continue
        stripped = line.strip()
        if stripped.startswith(("#", "-", "*")):
            break
        if not stripped:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    if version is None or not paragraph:
        return version, None
    text = " ".join(paragraph)
    sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].strip()
    return version, sentence or None


def whats_new_note(seen_version: Optional[str], path: Optional[Path] = None,
                   current: Optional[str] = None) -> Optional[tuple]:
    """(version, sentence) when a note is due, else None.

    Due only when the changelog's newest released version differs from the
    one already seen. A fresh install has seen nothing, so the first run
    shows the note for the version it shipped with -- once.
    """
    version, sentence = changelog_summary(path)
    if not version or not sentence:
        return None
    if seen_version == version:
        return None
    # Never announce a release the running build is behind: the note
    # describes what the user just got, not what they have not got yet.
    if current is not None and version != current:
        return None
    return version, sentence


#: The identity strip's small text links (89). Small TEXT links, never
#: cards: they sit in the strip beside the creed and must not add a row of
#: their own. (visible label, URL). The URLs are the ones the rest of the
#: app already publishes -- support_feedback.DOCUMENTATION_URL, the repo the
#: updater polls, and the Ko-fi address in funding.json -- so there is one
#: place each address is true.
#: No "&" in a link label: QPushButton reads it as a mnemonic and
#: swallows it, so "Website & docs" drew as "Website  docs".
DOCS_LINK = "Website and docs"
GITHUB_LINK = "GitHub"
KOFI_LINK = "Ko-fi"
DOCS_URL = "https://morneis.com/samsara/docs/"
GITHUB_URL = "https://github.com/Morne-Ingstar/Samsara"
KOFI_URL = "https://ko-fi.com/morneingstar"
IDENTITY_LINKS = (
    (DOCS_LINK, DOCS_URL),
    (GITHUB_LINK, GITHUB_URL),
)
#: Queue 111 (Astra, owner accepted). Was "Support is never needed. But
#: always appreciated." -- two problems beside a help-oriented interface:
#: "support" reads as technical support, which IS needed and IS offered
#: two cards away, and "never needed" talks the Ko-fi link out of itself
#: before the user reaches it. The new line keeps the same promise (the
#: app is free and nothing is gated) without the ambiguity or the
#: self-undercut. Same placement, same subtle styling, same link.
SUPPORT_LINE = "Always free. Contributions welcome."


def glyph_icon(key: str, colour: str, px: int = 18) -> QIcon:
    """A QIcon of one ICON_GLYPHS entry painted in `colour` (a theme token),
    so a button keeps its text as its accessible name and still shows an
    icon. Renders at 2x for crisp scaling."""
    glyph = ICON_GLYPHS.get(key, "")
    size = px * 2
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    font = QFont("Segoe UI Symbol")
    font.setPixelSize(int(size * 1.0))  # type-floor: exempt -- glyph fills its icon pixmap, not text
    painter.setFont(font)
    # theme.qcolor, not QColor(colour): the text tokens are CSS rgba(), which
    # QColor(str) cannot parse and paints black (74).
    painter.setPen(theme.qcolor(colour))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


def text_scale() -> float:
    """How much larger the user's text is than the Windows default (Settings
    > Accessibility > Text size changes QApplication's default font). Every
    px size on this page is multiplied by it, so a 20% larger system font
    gives 20% larger text here instead of clipping."""
    app = QApplication.instance()
    if app is None:
        return 1.0
    size = app.font().pointSizeF()
    if size <= 0:
        return 1.0
    return max(0.5, size / BASE_POINT_SIZE)


def _px(n: float) -> int:
    return max(1, round(n * text_scale()))


# ---------------------------------------------------------------------------
# Runtime state (pure functions over the app object -- tested with mocks)
# ---------------------------------------------------------------------------

def _cfg(app) -> dict:
    cfg = getattr(app, 'config', None)
    return cfg if isinstance(cfg, dict) else {}


def _cfg_get(cfg: dict, key: str):
    try:
        return config_defaults.cfg_get(cfg, key)
    except KeyError:
        node = cfg
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node


def listening_state(app) -> tuple:
    """(text, live). `live` is True while something is capturing or armed
    right now -- the condition under which Stop listening does anything."""
    if getattr(app, 'recording', False):
        return ("Recording", True)
    if getattr(app, 'snoozed', False):
        return ("Snoozed", False)
    if getattr(app, 'wake_word_active', False):
        return ("Wake word armed", True)
    if getattr(app, 'continuous_active', False):
        return ("Continuous", True)
    if (getattr(app, 'command_mode_active', False)
            or getattr(app, 'ava_command_session_active', False)
            or getattr(app, 'toggle_active', False)):
        return ("Listening", True)
    mode = str(_cfg_get(_cfg(app), 'mode') or 'hold')
    if mode == 'hold':
        return ("Hold key", False)
    if mode == 'toggle':
        return ("Toggle key", False)
    return ("Off", False)


def capturing(app) -> bool:
    """True while something is actually capturing speech: a hold/toggle
    recording, continuous mode, or a hands-free (command / Ava) session. An
    armed wake listener is ambient, not a capture."""
    return bool(getattr(app, 'recording', False)
                or getattr(app, 'continuous_active', False)
                or getattr(app, 'toggle_active', False)
                or getattr(app, 'command_mode_active', False)
                or getattr(app, 'ava_command_session_active', False))


def mic_present(app) -> bool:
    """False when the configured microphone is not among the devices the app
    last enumerated (available_mics), or when it enumerated none. An app
    that never enumerated (no attribute) makes no claim -> True."""
    mics = getattr(app, 'available_mics', None)
    if mics is None:
        return True
    ids = {m.get('id') for m in mics if isinstance(m, dict)}
    mic_id = _cfg(app).get('microphone')
    if mic_id is None:
        return bool(ids)
    return mic_id in ids


def quoted(phrase) -> str:
    """A spoken phrase as Home shows it: in double quotes, with the lone
    pronoun capitalised ("what can i say" -> '"what can I say"')."""
    text = re.sub(r"\bi\b", "I", str(phrase or '').strip())
    return f'"{text}"'


def wake_phrase(cfg: dict) -> str:
    return str(_cfg_get(cfg, 'wake_word_config.phrase')
               or config_defaults.DEFAULTS['wake_word_config.phrase'])


def destination_sentence(app) -> str:
    """Where what the user says goes next, in plain words. The session's
    internal mode names never reach the page."""
    if getattr(app, 'ava_command_session_active', False) or getattr(app, 'ava_mode_active', False):
        return "What you say goes to Ava."
    if getattr(app, 'command_mode_active', False):
        manager = getattr(app, '_session_mode_manager', None)
        mode = getattr(getattr(manager, 'mode', None), 'value', None)
        if mode == 'ava':
            return "What you say goes to Ava."
        if mode == 'command':
            return "What you say is taken as a command."
    target = target_text(app)
    return f"What you say goes to {target}."


def listening_state_id(app) -> str:
    """The STATE_TABLE row for the app's runtime flags, in precedence order.
    Read from runtime flags, never from config alone."""
    if getattr(app, 'recording', False) or getattr(app, 'toggle_active', False):
        return "recording"
    if getattr(app, 'ava_command_session_active', False):
        return "ava_session"
    if getattr(app, 'command_mode_active', False):
        return "hands_free"
    if getattr(app, 'continuous_active', False):
        return "continuous"
    if getattr(app, 'snoozed', False):
        return "paused"
    if getattr(app, 'wake_word_active', False):
        return "waiting"
    return "ready"


def state_view(app) -> tuple:
    """(state id, headline, primary label, primary action) -- the one row of
    STATE_TABLE the page shows."""
    state = listening_state_id(app)
    headline, label, action = STATE_TABLE[state]
    return state, headline.format(phrase=quoted(wake_phrase(_cfg(app)))), label, action


def state_line(app) -> str:
    """The page's ONE state line: the headline, then where speech goes (not
    while paused: nothing is going anywhere)."""
    state, headline, _label, _action = state_view(app)
    if state == "paused":
        return headline
    return f"{headline} {destination_sentence(app)}"


def _window_title(hwnd) -> str:
    """Title of a top-level window handle ('' when unreadable). Module-level
    so tests can substitute it."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(int(hwnd))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(int(hwnd), buf, length + 1)
        return buf.value
    except Exception as exc:
        logger.debug(f"_window_title: {exc}")
        return ""


def target_text(app) -> str:
    """Where the next utterance goes: the focus-locked window's title while
    a hands-free session holds one, else "whatever you're typing in"."""
    session = (getattr(app, 'command_mode_active', False)
               or getattr(app, 'ava_command_session_active', False))
    manager = getattr(app, '_session_mode_manager', None)
    hwnd = getattr(manager, '_dictate_target_hwnd', None)
    if session and hwnd:
        title = _window_title(hwnd)
        if title:
            return title
    return ANY_TEXTBOX


def hotkey_label(hotkey) -> str:
    """'ctrl+shift' -> 'Ctrl+Shift'."""
    parts = [p.strip() for p in str(hotkey or '').split('+') if p.strip()]
    return "+".join(p[:1].upper() + p[1:] for p in parts)


def help_phrase(records) -> Optional[str]:
    """The catalog's spoken help command ("what can i say"), or None when
    the catalog is absent or has no such command. Never hardcoded."""
    for record in records or []:
        cid = str(record.get('canonical_id', ''))
        if cid.endswith('.what_can_i_say'):
            return command_catalog.canonical_phrase(record)
    for record in records or []:
        aliases = [str(a) for a in record.get('aliases', [])]
        if any('quick reference' in a for a in aliases):
            return command_catalog.canonical_phrase(record)
    return None


def instruction_line(cfg: dict, help_cmd: Optional[str], state: Optional[str] = None) -> str:
    """One line generated from live config: the hotkey, and the catalog's
    help phrase (quoted) when the catalog has one. The wake word is on the
    state line and the hands-free card, not repeated here (41). `state` is
    the STATE_TABLE row: paused says the hotkeys are off instead of offering
    one, and a continuous setup that is not running says how to start it."""
    if state == "paused":
        return PAUSED_INSTRUCTION
    mode = str(_cfg_get(cfg, 'mode') or 'hold')
    hotkey = hotkey_label(_cfg_get(cfg, 'hotkey') or config_defaults.DEFAULTS['hotkey'])
    if mode == 'hold':
        parts = [f"Hold {hotkey} and speak."]
    elif mode == 'toggle':
        parts = [f"Press {hotkey} to start and again to stop."]
    elif state in (None, "continuous"):
        parts = ["Speak any time."]
    else:
        continuous = hotkey_label(_cfg_get(cfg, 'continuous_hotkey') or 'ctrl+alt+d')
        parts = [f"Press {continuous} to listen continuously."]
    if help_cmd:
        parts.append(f"Say {quoted(help_cmd)} for commands.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Outcomes (the ring _show_outcome_chip appends to)
# ---------------------------------------------------------------------------

def outcome_ring(app) -> list:
    ring = getattr(app, '_outcome_ring', None)
    if not ring:
        return []
    return list(ring)[-OUTCOME_RING_MAX:]


def classify_outcome(label: str, kind: str) -> str:
    """'typed' | 'miss' | 'command' | 'other'."""
    label = str(label or '')
    if label == "MISS":
        return "miss"
    if label == "typed" and kind == "success":
        return "typed"
    if label.startswith(CHIP_CHECK):
        return "command"
    return "other"


def outcome_reason(label: str, kind: str) -> Optional[str]:
    """The reason an outcome carries in its label, or None. MISS carries
    none; 'refused: why' / 'cancelled: why' and error chips do."""
    label = str(label or '')
    if label == "MISS":
        return None
    for prefix in ("refused: ", "cancelled: "):
        if label.startswith(prefix):
            return label[len(prefix):]
    if kind == "error" and label.startswith(CHIP_CROSS + " "):
        return label[len(CHIP_CROSS) + 1:]
    return None


def last_typed_text(app) -> str:
    """The most recent typed text: the history store's newest dictation
    entry (what the correction window pre-fills with too)."""
    store = getattr(app, 'history_store', None)
    if store is None:
        return ""
    try:
        rows = store.query(type_filter='dictation', limit=1)
    except Exception as exc:
        logger.debug(f"last_typed_text: {exc}")
        return ""
    if not rows:
        return ""
    try:
        return str(rows[0]['display_text'] or '')
    except (KeyError, IndexError, TypeError):
        return ""


def render_outcome(app, outcome) -> tuple:
    """(kind label, content) for an outcome tuple (label, kind, ts): the
    kind in plain words first, then what it was about.

    A command chip stores only the first two words of its phrase
    (session_modes.outcome_chip), which can sever the object ("switch to"),
    so the row claims no verb for it: "Ran a command" plus the stored
    fragment. Chips outside the three kinds show their own label as the
    kind and no content."""
    label, kind = str(outcome[0]), str(outcome[1])
    cls = classify_outcome(label, kind)
    if cls == "typed":
        text = last_typed_text(app).replace('\n', ' ').strip()
        return (KIND_DICTATED, text)
    if cls == "miss":
        heard = str(getattr(app, '_last_miss_text', '') or '').strip()
        return (KIND_MISSED, quoted(heard) if heard else "")
    if cls == "command":
        fragment = label[len(CHIP_CHECK):].strip()
        return (KIND_RAN, quoted(fragment) if fragment else "")
    return (label, "")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def catalog_records(app) -> Optional[list]:
    """Live catalog records the way the cheat sheet builds them (registry
    rows first, commands_catalog.json second), or None when unavailable."""
    rows = None
    matcher = getattr(getattr(app, 'command_executor', None), '_matcher', None)
    if matcher is not None and hasattr(matcher, 'list_commands'):
        try:
            rows = matcher.list_commands()
        except Exception as exc:
            logger.debug(f"catalog_records: {exc}")
            rows = None
    try:
        return command_catalog.guidance_catalog(rows)
    except Exception as exc:
        logger.warning(f"[HOME] command catalog unavailable: {exc}")
        return None


def matching_records(records, needle: str) -> list:
    """The records the cheat sheet's text filter would show for `needle`."""
    needle = needle.strip().lower()
    out = []
    for r in records or []:
        phrase = command_catalog.canonical_phrase(r).lower()
        aliases = [str(a).lower() for a in r.get('aliases', [])]
        if needle in phrase or any(needle in a for a in aliases):
            out.append(r)
    return out


def format_count(n) -> str:
    """A figure a person reads at a glance: 10,905 rather than 10905 (111).

    Python's own "," grouping, which is comma-per-thousand regardless of
    the machine's locale. That is the convention, chosen because the app
    has no other: there is no QLocale and no `locale` import anywhere in
    samsara/ui, so nothing existed to be consistent WITH. Every figure
    Home paints goes through here, so adopting QLocale later is one edit
    in one function rather than a hunt through format strings.
    """
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def catalog_example(records) -> Optional[str]:
    picks = command_catalog.pick_examples(records, 1)
    return command_catalog.canonical_phrase(picks[0]) if picks else None


def _user_word_count() -> Optional[int]:
    try:
        from samsara import phonetic_wash
        return len(phonetic_wash.get_user_corrections() or {})
    except Exception as exc:
        logger.debug(f"_user_word_count: {exc}")
        return None


def _ava_sources(cfg: dict) -> tuple:
    return (bool(_cfg_get(cfg, 'ollama.enabled')), bool(_cfg_get(cfg, 'cloud_llm.enabled')))


def ava_text(cfg: dict) -> str:
    """The Ask Ava card's value: on or off. Where it runs is the card's
    description (41: "local or your key" said nothing to the owner)."""
    return "on" if any(_ava_sources(cfg)) else "off"


def card_description(title: str, cfg: dict, count: str = "") -> str:
    template = CARD_DESCRIPTIONS.get(title, "")
    return template.format(phrase=quoted(wake_phrase(cfg)),
                           where=AVA_WHERE[_ava_sources(cfg)],
                           count=count)


def commands_card_example(records) -> Optional[str]:
    """The phrase the Commands card shows instead of a count (111).

    "487 commands" tells a new user the app is big. It does not tell them
    one thing they could say, which is the only fact that gets them started.
    The card's VALUE therefore becomes a real command and the figure moves
    into the description, which is where breadth was already being claimed.

    Two constraints shape the choice, and both come from the card itself:

      * The value label does not wrap and is not elided, so a long phrase
        would fail overflowing_widgets and clip the card. A SHORT safe phrase
        is taken, which is what keeps it inside the 150 px the card can spare
        at 900x650.
      * Shortest alone was wrong and the render caught it: the shortest safe
        phrase in the live catalog is "yes", which is a reply, not a command
        anybody would think to try. The phrase must have at least two words
        to read as an instruction; the shortest of THOSE is the pick. A pool
        with nothing but single words falls back to the shortest of them,
        because one real phrase still beats a number.
      * It is drawn from command_marquee's pool, not a second filter, so the
        card and the status strip can never advertise a phrase the other
        considers unsafe. Deterministic: shortest, ties broken
        alphabetically, so the card does not change under the user between
        two refreshes of the same catalog.
    """
    try:
        from samsara.ui import command_marquee  # noqa: PLC0415  (no cycle; see 111)
        phrases = command_marquee.example_phrases(
            records, count=len(records or []) + 1, rng=random.Random(0))
    except Exception as exc:
        logger.debug(f"commands_card_example: {exc}")
        return None
    if not phrases:
        return None
    instructions = [p for p in phrases if len(p.split()) >= 2]
    return min(instructions or phrases, key=lambda p: (len(p), p))


def capability_cards(app, records) -> list:
    """The six cards, in order. Each: {title, description, value, action,
    arg}. action is 'cheatsheet' (arg = filter text), 'settings' (arg = tab
    name), 'page' (arg = hub page name) or 'guides'."""
    cfg = _cfg(app)
    if records is None:
        commands_value = _CATALOG_UNAVAILABLE_SHORT
        commands_count = "Every phrase it knows"
    else:
        # 111: the VALUE is a phrase the user can say; the figure it replaced
        # moved into the description, which already claimed breadth. The
        # whole catalog, not the "window" subset: the card opens the
        # unfiltered list, so its figure must count the unfiltered list (89).
        example = commands_card_example(records)
        commands_value = quoted(example) if example else f"{format_count(len(records))} commands"
        commands_count = f"{format_count(len(records))} phrases"
    phrase = wake_phrase(cfg)
    mode = str(_cfg_get(cfg, 'mode') or 'hold')
    hotkey = hotkey_label(_cfg_get(cfg, 'hotkey') or config_defaults.DEFAULTS['hotkey'])
    if mode == 'hold':
        dictate_value = f"hold {hotkey}"
    elif mode == 'toggle':
        dictate_value = f"press {hotkey}"
    else:
        dictate_value = "always on"
    words = _user_word_count()
    values = (
        commands_value,
        f"{len(GUIDES)} guides",
        f"{format_count(words)} words" if words is not None else "your dictionary",
    )
    cards = []
    for (title, action, arg, glyph), value in zip(CARD_ROUTES, values):
        cards.append({"title": title, "value": value, "action": action, "arg": arg,
                      "glyph": glyph,
                      "description": card_description(title, cfg, commands_count)})
    return cards


# ---------------------------------------------------------------------------
# Words today
# ---------------------------------------------------------------------------

def local_midnight_iso(now: Optional[_dt.datetime] = None) -> str:
    now = now or _dt.datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def words_today(app, now: Optional[_dt.datetime] = None) -> int:
    store = getattr(app, 'history_store', None)
    if store is None or not hasattr(store, 'words_typed_since'):
        return 0
    try:
        return int(store.words_typed_since(local_midnight_iso(now)))
    except Exception as exc:
        logger.debug(f"words_today: {exc}")
        return 0


# ---------------------------------------------------------------------------
# Actions on the app (each one an existing path)
# ---------------------------------------------------------------------------

def stop_capture(app) -> Optional[str]:
    """Stop the CURRENT capture through the app's own per-lane stop path
    (38): the one the hotkey release / session end use, never the snooze.
    Returns the name of the method called, or None when nothing was
    capturing. Order matters: a hands-free session owns its recording."""
    for flag, method in (
        ('ava_command_session_active', 'exit_ava_command_session'),
        ('command_mode_active', 'exit_command_mode'),
        ('continuous_active', 'stop_continuous_mode'),
        ('recording', 'stop_recording'),
        ('toggle_active', 'stop_recording'),
    ):
        if getattr(app, flag, False):
            fn = getattr(app, method, None)
            if not callable(fn):
                continue
            try:
                fn()
            except Exception as exc:
                logger.warning(f"[HOME] {method} failed: {exc}")
                return None
            return method
    return None


def pause_hands_free(app) -> bool:
    """The explicit pause: the tray's snooze until resumed. It stops the wake
    listener AND blocks the dictation hotkeys, so it is a real action from
    the waiting and the ready states alike (74). Not while already paused."""
    if getattr(app, 'snoozed', False):
        return False
    fn = getattr(app, 'snooze_listening', None)
    if not callable(fn):
        return False
    try:
        fn(None)
    except Exception as exc:
        logger.warning(f"[HOME] pause hands-free failed: {exc}")
        return False
    return True


def resume_hands_free(app) -> bool:
    """Undo the pause: the app's resume_listening."""
    if not getattr(app, 'snoozed', False):
        return False
    fn = getattr(app, 'resume_listening', None)
    if not callable(fn):
        return False
    try:
        fn()
    except Exception as exc:
        logger.warning(f"[HOME] resume hands-free failed: {exc}")
        return False
    return True


def stop_listening(app) -> bool:
    """Kept for callers of the old name: stops the current capture. It
    never snoozes (that was the wiring the owner found dead-ended)."""
    return stop_capture(app) is not None


def restart_hands_free(app) -> bool:
    """Recovery, not a control (queue 109): this exists only behind the
    problem notice, when hands-free has STOPPED, and it is the one way back
    for a user who cannot reach a keyboard shortcut or the tray.

    Returns the app's own answer -- True only when the wake listener is
    running again, so a failed restart leaves the notice standing instead of
    being replaced by a quieter lie.
    """
    fn = getattr(app, "restart_hands_free", None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception as exc:
        logger.warning(f"[HOME] hands-free restart failed: {exc}")
        return False


def _clipboard():
    """The system clipboard. Module-level so tests substitute it rather than
    overwrite the real clipboard of the person running them."""
    return QApplication.clipboard()


def _post_after(fn) -> None:
    """Run fn on the Qt thread AFTER anything already posted (qt_runtime's
    posts are FIFO on one thread), so a callback posted after a window's
    own init post sees the built window. Falls back to a direct call
    outside the runtime."""
    try:
        from samsara.ui import qt_runtime  # noqa: PLC0415
        qt_runtime.post(fn)
    except Exception:  # noqa: BLE001
        fn()


def open_cheatsheet_filtered(app, needle: str) -> bool:
    """Show the command reference with its text filter set to `needle` and
    the category on All (the sheet's own filter path). The sheet's window
    is read when the posted callback RUNS, not when this is called: on the
    first open the wrapper has no window yet (its init is itself a post),
    which is why the filter never applied on a first press (40)."""
    sheet = getattr(app, 'cheat_sheet', None)
    if sheet is None:
        logger.error("[HOME] Command reference: the app has no cheat_sheet")
        return False
    try:
        sheet.show()
    except Exception as exc:
        logger.warning(f"[HOME] Command reference show failed: {exc}")
        return False

    def _apply():
        window = getattr(sheet, '_window', None)
        if window is None:
            logger.error("[HOME] Command reference: window not built after its init post")
            return
        try:
            window._set_category("All")
            window._filter.setText(needle)
        except Exception as exc:
            logger.warning(f"[HOME] Command reference filter failed: {exc}")

    _post_after(_apply)
    return True


def settings_tab_ids() -> list:
    """The settings page registry the hub links into (the split window's
    _TAB_NAMES); empty when the settings module cannot be imported."""
    try:
        from samsara.ui.settings_qt import _TAB_NAMES  # noqa: PLC0415
        return list(_TAB_NAMES)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[HOME] settings page registry unavailable: {exc}")
        return []


def open_settings_tab(app, tab: str) -> bool:
    """Open Settings ON `tab`. The tab id is asserted against the page
    registry at call time (a missing id is logged loudly and nothing
    opens), and the page is selected in a callback posted AFTER the
    window's own init post, reading the window when it runs. Before (40),
    the window was read at call time: None on the first open, so Settings
    landed on page one -- the owner's "Ask Ava opens Settings at the top".
    """
    ids = settings_tab_ids()
    if tab not in ids:
        logger.error("[HOME] Settings page %r is not in the registry %r; not opening", tab, ids)
        return False
    fn = getattr(app, 'open_settings', None)
    if not callable(fn):
        logger.error("[HOME] the app has no open_settings")
        return False
    try:
        fn()
    except Exception as exc:
        logger.warning(f"[HOME] open_settings failed: {exc}")
        return False

    def _select():
        window = getattr(getattr(app, '_settings_qt', None), '_window', None)
        if window is None or not hasattr(window, 'show_tab'):
            logger.error("[HOME] Settings window not built after its init post; page %r not selected", tab)
            return
        try:
            window.show_tab(tab)
        except Exception as exc:
            logger.error(f"[HOME] Settings page {tab!r} could not be selected: {exc}")

    _post_after(_select)
    return True


def open_guide(app, label: str) -> bool:
    """Open one GUIDES entry by its visible label."""
    for name, kind, target in GUIDES:
        if name != label:
            continue
        if kind == "cheatsheet":
            return open_cheatsheet_filtered(app, "")
        if kind == "settings":
            return open_settings_tab(app, target)
        fn = getattr(app, target, None)
        if not callable(fn):
            logger.error(f"[HOME] guide {label!r}: the app has no {target}")
            return False
        try:
            fn()
        except Exception as exc:
            logger.warning(f"[HOME] guide {label!r} failed: {exc}")
            return False
        return True
    logger.error(f"[HOME] unknown guide {label!r}")
    return False


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

def _label_css(role: str, color: str, weight: int, family: Optional[str] = None,
               spacing: Optional[str] = None) -> str:
    css = (f"color: {color}; font-size: {_px(TYPE[role])}px; font-weight: {weight};"
           " background: transparent; border: none;")
    if family:
        css += f" font-family: {family};"
    if spacing:
        css += f" letter-spacing: {spacing};"
    return css


def _label(text: str, *, role: str, color: Optional[str] = None,
           weight: int = 400, wrap: bool = False, family: Optional[str] = None,
           spacing: Optional[str] = None) -> QLabel:
    """A label sized by its text ROLE in theme.HOME_TYPE_SCALE (41); the role
    is kept on the widget so the type-scale test can read it back."""
    # None, not the token itself: a default argument is evaluated when
    # the def runs, so a token there keeps the palette that was live at
    # import (queue 129).
    if color is None:
        color = theme.TEXT_PRIMARY
    lbl = QLabel(text)
    lbl.setProperty("typeRole", role)
    lbl.setStyleSheet(_label_css(role, color, weight, family, spacing))
    lbl.setWordWrap(wrap)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return lbl


def _section_label(text: str) -> QLabel:
    return _label(text.upper(), role="section label", color=theme.TEXT_SECONDARY,
                  weight=700, spacing=theme.LETTER_SPACING_SECTION)


def _button(text: str, primary: bool = False) -> QPushButton:
    """A visible-label button: accessible name == text, 44 px target. A
    primary button (the state row's one) is ACCENT-filled."""
    btn = QPushButton(text)
    btn.setAccessibleName(text)
    btn.setProperty("typeRole", "button")
    btn.setProperty("primary", primary)
    btn.setMinimumHeight(MIN_TARGET)
    btn.setMinimumWidth(MIN_TARGET)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    if primary:
        fill = (f"background: {theme.ACCENT}; color: {theme.BG0};"
                f" border: 1px solid {theme.ACCENT}; font-weight: 600;")
        disabled = (f"QPushButton:disabled {{ background: {theme.BG2}; color: {theme.TEXT_DISABLED};"
                    f" border-color: {theme.BORDER_FAINT}; font-weight: 400; }}")
        focus = f"QPushButton:focus {{ border: 2px solid {theme.TEXT_PRIMARY}; }}"
    else:
        fill = (f"background: {theme.BG2}; color: {theme.TEXT_PRIMARY};"
                f" border: 1px solid {theme.BORDER};")
        disabled = (f"QPushButton:disabled {{ color: {theme.TEXT_DISABLED};"
                    f" border-color: {theme.BORDER_FAINT}; }}")
        focus = f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
    btn.setStyleSheet(
        f"QPushButton {{ {fill} border-radius: 6px;"
        f" padding: {_px(8)}px {_px(14)}px; font-size: {_px(TYPE['button'])}px; }}"
        f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
        f"{focus}{disabled}"
    )
    return btn


def _card() -> QFrame:
    frame = QFrame()
    frame.setObjectName("homeCard")
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    return frame


def _link(text: str, url: str) -> QPushButton:
    """A small TEXT link (89): a flat, underlined button, not a card and not
    a filled control. It is a QPushButton and not a rich-text QLabel so it
    is in the tab order, reachable by keyboard, and its accessible name is
    its visible text (WCAG Label in Name).

    It keeps the page's 44 px target floor: the text is small and quiet, but
    the button around it is not. The extra height is transparent padding, so
    the link still READS as one line of small type while being as easy to
    hit as every other control on Home.
    """
    btn = QPushButton(text)
    btn.setAccessibleName(text)
    btn.setAccessibleDescription(url)
    btn.setProperty("typeRole", "note")
    btn.setProperty("homeLink", True)
    btn.setFlat(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    btn.setMinimumHeight(MIN_TARGET)
    btn.setMinimumWidth(MIN_TARGET)
    btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    btn.setStyleSheet(
        f"QPushButton {{ background: transparent; border: none; padding: 0;"
        f" color: {theme.TEXT_SECONDARY}; text-decoration: underline;"
        f" font-size: {_px(TYPE['note'])}px; text-align: left; }}"
        f"QPushButton:hover {{ color: {theme.ACCENT}; }}"
        f"QPushButton:focus {{ color: {theme.ACCENT};"
        f" border: 1px solid {theme.ACCENT}; border-radius: 3px; }}"
    )
    btn.clicked.connect(lambda _=False, u=url: open_url(u))
    return btn


def open_url(url: str) -> bool:
    """Open an external link in the user's browser. Never raises: a link
    that cannot be opened is logged, not a crash on Home."""
    try:
        from PySide6.QtGui import QDesktopServices  # noqa: PLC0415
        from PySide6.QtCore import QUrl  # noqa: PLC0415
        return bool(QDesktopServices.openUrl(QUrl(url)))
    except Exception as exc:
        logger.debug(f"open_url({url}): {exc}")
        return False


class _StateMark(QWidget):
    """The mark at 44 px, fed the very same MarkFrame the header mark shows
    (set_frame is called by the owner whenever that frame changes)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = MarkFrame("idle", "off")
        self.setFixedSize(STATE_MARK_PX, STATE_MARK_PX)
        self.setStyleSheet("background: transparent;")
        self.setAccessibleName("Samsara mark")

    @property
    def frame(self):
        return self._frame

    def set_frame(self, frame):
        if not isinstance(frame, MarkFrame):
            frame = MarkFrame("idle", "off")
        if frame != self._frame:
            self._frame = frame
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        f = self._frame
        # Brand presentation (38): ACCENT at rest with the eye present,
        # the tray's idle grey and eyeless ring retired on this surface.
        paint_mark(painter, QRectF(0, 0, self.width(), self.height()),
                   f.capture, f.eye, f.rotation, f.opacity, brand=True)
        painter.end()


class _CapabilityCard(QPushButton):
    """A card that IS the button. Its text is the title, so the accessible
    name equals the visible label; the title is shown by a label in the
    card's own layout (with the icon, the one-line description and the live
    value beneath it) so the card grows with a wrapped description instead
    of clipping it. The button's own text is painted transparent: the title
    appears once."""

    def __init__(self, title: str, value: str, description: str = "", parent=None,
                 glyph: Optional[str] = None):
        super().__init__(title, parent)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self.setProperty("typeRole", "card title")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setStyleSheet(
            f"QPushButton {{ background: {theme.BG1}; color: transparent;"
            f" border: 1px solid {theme.BORDER}; border-radius: 8px; text-align: left;"
            f" font-size: {_px(TYPE['card title'])}px; }}"
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
            f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
        )
        lay = QVBoxLayout(self)
        # 89: 10 px side padding, not 14. The card's minimum width is its
        # title plus this padding, and at 14 the widest title ("View window
        # commands") needed 249 px -- seven pixels too wide for THREE cards
        # in the 760 px column, which forced the three destinations onto two
        # rows and pushed the identity strip below the fold.
        lay.setContentsMargins(_px(10), _px(12), _px(10), _px(12))
        lay.setSpacing(_px(6))

        head = QHBoxLayout()
        head.setSpacing(_px(8))
        if glyph:
            icon = QLabel()
            icon.setPixmap(glyph_icon(glyph, theme.ACCENT).pixmap(QSize(_px(18), _px(18))))
            icon.setStyleSheet("background: transparent; border: none;")
            icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            head.addWidget(icon, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._title = _label(title, role="card title", weight=600)
        self._title.setAccessibleName(f"{title} title")
        head.addWidget(self._title, stretch=1)
        lay.addLayout(head)

        self._description = _label(description, role="card description",
                                   color=theme.TEXT_SECONDARY, wrap=True)
        self._description.setAccessibleName(f"{title} description")
        lay.addWidget(self._description)
        lay.addStretch(1)
        self._value = _label(value, role="card value", color=theme.ACCENT, weight=500)
        self._value.setAccessibleName(f"{title} value")
        lay.addWidget(self._value, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        for child in (self._title, self._description, self._value):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._fit_width()

    def _fit_width(self):
        # The grid may not squeeze the card below its title and value; the
        # description wraps instead.
        m = self.layout().contentsMargins()
        head = self.layout().itemAt(0).sizeHint().width()
        need = max(head, self._value.sizeHint().width()) + m.left() + m.right() + 4
        self.setMinimumWidth(max(MIN_TARGET, need))

    def sizeHint(self):
        return self.layout().sizeHint().expandedTo(QSize(MIN_TARGET, MIN_TARGET))

    def minimumSizeHint(self):
        return self.layout().minimumSize().expandedTo(QSize(MIN_TARGET, MIN_TARGET))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return max(MIN_TARGET, self.layout().totalHeightForWidth(width))

    @property
    def value_text(self) -> str:
        return self._value.text()

    @property
    def description_text(self) -> str:
        return self._description.text()

    def set_value(self, value: str):
        self._value.setText(value)
        self._fit_width()


class _AvaMark(QWidget):
    """Ava's small presence: a blue disc with an eye, dimmed when she cannot
    answer. The avatar NEVER carries the meaning on its own -- the sentence
    beside it and the accessible name always say what state she is in."""

    SIZE = AVA_MARK_PX

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._state = home_signals.AVA_ASLEEP

    def set_state(self, state: str):
        if state != self._state:
            self._state = state
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        awake = self._state in (home_signals.AVA_AWAKE, home_signals.AVA_THINKING)
        disc = QColor(theme.qcolor(theme.AVA))
        disc.setAlphaF(1.0 if awake else 0.35)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(disc)
        painter.drawEllipse(rect)
        # The eye: open when she can answer, a closed line when she cannot,
        # and a smaller pupil while she is working -- shape, not colour alone.
        ink = QColor(theme.qcolor(theme.BG0))
        painter.setBrush(ink)
        centre = rect.center()
        if self._state == home_signals.AVA_ASLEEP:
            painter.setPen(QColor(ink))
            painter.drawLine(int(rect.left() + rect.width() * 0.28), int(centre.y()),
                             int(rect.right() - rect.width() * 0.28), int(centre.y()))
        else:
            pupil = rect.width() * (0.16 if self._state == home_signals.AVA_THINKING else 0.24)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(centre, pupil, pupil)
        painter.end()


#: The header Ava control's short state words (89). The header has no room
#: for the full sentence, but the state must never be carried by the dimmed
#: avatar alone, so a WORD is always visible; the sentence is the control's
#: accessible description and its tooltip.
AVA_SHORT_STATE = {
    home_signals.AVA_AWAKE: "ready",
    home_signals.AVA_THINKING: "working",
    home_signals.AVA_DIM: "unavailable",
    home_signals.AVA_ASLEEP: "off",
}
AVA_HEADER_MARK_PX = 18


class AvaHeaderControl(QPushButton):
    """Ava, in the hub header beside the listening indicator (89).

    She was a 91 px row in Home's body. Home is one column that has to fit a
    548 px viewport, and Ava is app-wide state, not a Home topic -- so she
    moved to the header, where every page can see her. One control: the
    avatar, her name, her state in a word, and a click that goes to her
    settings.
    """

    def __init__(self, app, on_click: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self._app = app
        self._on_click = on_click
        self._state = home_signals.AVA_ASLEEP
        self.setAccessibleName(AVA_NAME)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(MIN_TARGET)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; color: transparent;"
            f" border: 1px solid transparent; border-radius: 6px;"
            f" padding: {_px(4)}px {_px(10)}px; }}"
            f"QPushButton:hover {{ border-color: {theme.BORDER}; }}"
            f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(_px(10), _px(4), _px(10), _px(4))
        lay.setSpacing(_px(8))
        self._mark = _AvaMark()
        self._mark.setFixedSize(_px(AVA_HEADER_MARK_PX), _px(AVA_HEADER_MARK_PX))
        lay.addWidget(self._mark, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._text = _label(AVA_NAME, role="status value", color=theme.TEXT_PRIMARY, weight=500)
        lay.addWidget(self._text, alignment=Qt.AlignmentFlag.AlignVCenter)
        for child in (self._mark, self._text):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.clicked.connect(self._clicked)
        self.refresh()

    def sizeHint(self) -> QSize:
        # QPushButton sizes itself from its own (transparent) text and
        # ignores the layout inside it, which clipped "Ava - unavailable"
        # to "Ava - unavaila" in the header (89). The layout is the truth.
        return self.layout().sizeHint().expandedTo(QSize(MIN_TARGET, MIN_TARGET))

    def minimumSizeHint(self) -> QSize:
        return self.layout().minimumSize().expandedTo(QSize(MIN_TARGET, MIN_TARGET))

    def _clicked(self):
        if self._on_click is not None:
            self._on_click()

    @property
    def state(self) -> str:
        return self._state

    @property
    def visible_text(self) -> str:
        return self._text.text()

    def refresh(self):
        """Re-read Ava's presence. Never raises: the header must not fail
        because a readiness check did."""
        try:
            state, sentence = home_signals.ava_presence(self._app)
        except Exception as exc:
            logger.debug(f"[HOME] Ava presence unavailable: {exc}")
            return
        self._state = state
        self._mark.set_state(state)
        word = AVA_SHORT_STATE.get(state, state)
        self._text.setText(f"{AVA_NAME} {EM_DASH} {word}")
        self.setText(f"{AVA_NAME} {EM_DASH} {word}")
        self.setAccessibleName(f"{AVA_NAME} {EM_DASH} {word}")
        self.setAccessibleDescription(sentence)
        self.setToolTip(sentence)
        self.updateGeometry()


class _ContentColumn(QWidget):
    """Home's one content column, with an honest minimum height (89).

    Why this exists. QScrollArea.setWidgetResizable(True) sizes its widget
    to `max(viewport, widget.minimumSizeHint())`, and a plain QWidget's
    minimumSizeHint is its layout's totalMinimumSize -- which QLayout
    computes at the layout's MINIMUM width. Home is a column of wrapping
    labels (the Ava sentence, the suggestion, the card descriptions, the
    creed), so at the minimum width every one of them wraps to many more
    lines: the column reported a 904 px minimum while it only needed 771 px
    at the width it was actually given. The scroll area honoured the 904 and
    Home showed a scrollbar at EVERY window size, including sizes where
    everything fitted with room to spare.

    So the minimum height is reported for the width the column actually has,
    not for a width it will never be drawn at. It is still a real height --
    the wrapped height at this width -- so content that genuinely does not
    fit still scrolls instead of clipping.
    """

    def minimumSizeHint(self) -> QSize:
        lay = self.layout()
        if lay is None:
            return super().minimumSizeHint()
        base = lay.minimumSize()
        width = self.width() or base.width() or CONTENT_MAX_W
        try:
            height = lay.totalHeightForWidth(width)
        except (AttributeError, RuntimeError):
            return base
        return QSize(base.width(), max(0, height))


class HomePage(QWidget):
    """The hub's landing page (86): a front door, not a control surface.

    `open_page(name)` is the hub's own navigation, used by the destination
    cards, the notice and the hint slot.
    """

    def __init__(self, app, open_page: Optional[Callable[[str], None]] = None,
                 parent=None):
        super().__init__(parent)
        self._app = app
        self._open_page = open_page or (lambda name: None)
        self._records = catalog_records(app)
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self._dismissed = self._load_dismissed()
        self._hint_index = 0
        self._hint = None
        self._whats_new = None
        self._build()
        self.refresh()

    # ---- Build ----------------------------------------------------------

    def _build(self):
        self.setStyleSheet(
            f"QFrame#homeCard {{ background-color: {theme.BG1};"
            f" border: 1px solid {theme.BORDER}; border-radius: 8px; }}"
            f"QFrame#homeNotice {{ background-color: {theme.BG1};"
            f" border: 1px solid {theme.WARNING}; border-radius: 8px; }}"
            f"QFrame#homeDivider {{ background-color: {theme.BORDER_FAINT};"
            f" border: none; max-height: 1px; min-height: 1px; }}"
            f"QScrollArea {{ border: none; background: transparent; }}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.viewport().installEventFilter(self)
        outer.addWidget(self._scroll)

        host = QWidget()
        host.setStyleSheet("background: transparent;")
        host_lay = QHBoxLayout(host)
        # 89: the page gutter and the gap between sections come down from
        # GRID_LG to GRID so the restored identity strip fits above the fold.
        host_lay.setContentsMargins(GRID, GRID, GRID, GRID)
        host_lay.setSpacing(0)
        self._content = _ContentColumn()
        self._content.setObjectName("homeContent")
        self._content.setStyleSheet("background: transparent;")
        self._content.setMaximumWidth(CONTENT_MAX_W)
        self._content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        host_lay.addStretch(0)
        host_lay.addWidget(self._content, stretch=1)
        host_lay.addStretch(0)
        self._scroll.setWidget(host)

        col = QVBoxLayout(self._content)
        col.setContentsMargins(0, 0, 0, 0)
        # 89 fit pass: 12 px between sections, not 16. Home has to fit a
        # 548 px viewport at the 900x650 default and every gap is measured.
        col.setSpacing(_px(12))

        col.addWidget(self._build_title())
        col.addWidget(self._build_notice())
        col.addWidget(self._build_hint())
        col.addWidget(self._build_destinations())
        # Queue 95: the stretch sits ABOVE the identity strip, not below it.
        # 89 put it below, which top-aligned the whole column and left ~180 px
        # of dead window under the stats. The stats belong on the floor, where
        # they were: spare height now opens as a gap between the cards and the
        # strip, and the strip stays put at the bottom edge at every size.
        # When there is no spare height the stretch is 0 and nothing moves.
        col.addStretch(1)
        self._identity = self._build_identity_strip()
        col.addWidget(self._identity)

        # 111: the Details disclosure sits in the chain where it sits on
        # screen -- after the slot's own action, before "Another suggestion"
        # -- so tabbing through the slot reaches it without a mouse.
        chain = [self._notice_btn,
                 self._hint_action_btn, self._hint_details_btn,
                 self._hint_next_btn, self._hint_hide_btn,
                 *self._cards, *self._guide_btns.values(),
                 *(self._links[name] for name, _url in
                   (*IDENTITY_LINKS, (KOFI_LINK, KOFI_URL)))]
        for a, b in zip(chain, chain[1:]):
            QWidget.setTabOrder(a, b)
        self._tab_chain = chain

    def _build_title(self) -> QWidget:
        """The tagline slot only (89). The big "Samsara" heading is gone:
        the window's own title bar and the hub header already say the name,
        and a third copy cost Home 53 px it does not have."""
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        # The tagline slot the owner fills in his own words (86). It stays
        # EMPTY and HIDDEN until he writes it: this page will not invent a
        # sentence about what the app is.
        self._tagline = _label("", role="tagline", color=theme.TEXT_SECONDARY, wrap=True)
        self._tagline.setObjectName("home.tagline")
        self._tagline.setAccessibleName("home.tagline")
        self._tagline.setVisible(False)
        lay.addWidget(self._tagline)
        self._tagline_box = box
        box.setVisible(False)
        return box

    def set_tagline(self, text: str):
        """Fill the one-line tagline. Empty text keeps the slot hidden --
        and hides the box too, so an empty slot costs no row and no gap."""
        text = (text or "").strip()
        self._tagline.setText(text)
        self._tagline.setVisible(bool(text))
        self._tagline_box.setVisible(bool(text))

    def _build_notice(self) -> QWidget:
        """Shown ONLY for a known fault on an enabled capability. It states
        the consequence, offers the route to help, and never replaces the
        page or moves focus."""
        card = _card()
        card.setObjectName("homeNotice")
        card.setAccessibleName(PROBLEM_NOTICE)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(GRID, GRID, GRID, GRID)
        lay.setSpacing(GRID)
        self._notice_text = _label("", role="state line", color=theme.WARNING, wrap=True)
        self._notice_text.setAccessibleName("Problem")
        # Ignored horizontally: the sentence wraps to whatever width is left
        # instead of demanding its unwrapped width and widening the page.
        self._notice_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._notice_text, stretch=1)
        # The notice's ONE action. Voice help by default; a fault that has a
        # repair of its own names it instead (queue 109) -- the only one so
        # far is a stopped wake listener, whose repair is a restart. Same
        # button, same place in the tab order, so the recovery is reachable
        # without a mouse, which is the whole point for this user.
        self._notice_btn = _button(OPEN_VOICE_HELP, primary=True)
        self._notice_btn.clicked.connect(self._on_notice_action)
        lay.addWidget(self._notice_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        self._notice_card = card
        card.setVisible(False)
        return card

    def _build_hint(self) -> QWidget:
        """The conditional slot (89). ONE thing, never a carousel and never
        on a timer (82); hidden entirely -- no padding, no placeholder --
        whenever it has nothing real to say. See _refresh_hint for what
        counts as real."""
        card = _card()
        card.setObjectName("homeCard")
        card.setAccessibleName(SUGGESTION)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(GRID, _px(10), GRID, _px(10))
        lay.setSpacing(_px(6))
        self._hint_heading = _section_label(SUGGESTION)
        lay.addWidget(self._hint_heading)
        self._hint_text = _label("", role="card title", wrap=True)
        self._hint_text.setAccessibleName("Suggestion text")
        self._hint_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._hint_text)
        # The VISIBLE second line: what to do (111). For the what's-new note
        # it is the note's own sentence.
        self._hint_sub = _label("", role="note", color=theme.TEXT_SECONDARY, wrap=True)
        self._hint_sub.setAccessibleName("Suggestion action")
        self._hint_sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._hint_sub.setVisible(False)
        lay.addWidget(self._hint_sub)
        # The DISCLOSED line: counts, outcome kinds and the threshold. Hidden
        # until the Details button is pressed, and hidden again whenever the
        # slot changes, so "collapsed by default" holds for every suggestion
        # and not merely for the first one of the session.
        self._hint_evidence = _label("", role="note", color=theme.TEXT_SECONDARY, wrap=True)
        self._hint_evidence.setAccessibleName("Suggestion evidence")
        self._hint_evidence.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._hint_evidence.setVisible(False)
        lay.addWidget(self._hint_evidence)

        actions = QWidget()
        actions.setStyleSheet("background: transparent;")
        row = FlowLayout(actions, h_spacing=_px(8), v_spacing=_px(8))
        self._hint_action_btn = _button("", primary=True)
        self._hint_action_btn.clicked.connect(self._on_hint_action)
        row.addWidget(self._hint_action_btn)
        # A real button, not a chevron glyph: it is in the tab chain, carries
        # its own visible label as its accessible name, and keeps the same
        # 44 px target every other control on Home has.
        self._hint_details_btn = _button(DETAILS)
        self._hint_details_btn.setCheckable(True)
        self._hint_details_btn.toggled.connect(self._on_toggle_details)
        row.addWidget(self._hint_details_btn)
        self._hint_next_btn = _button(ANOTHER_SUGGESTION)
        self._hint_next_btn.clicked.connect(self._on_another_hint)
        row.addWidget(self._hint_next_btn)
        self._hint_hide_btn = _button(HIDE_SUGGESTION)
        self._hint_hide_btn.clicked.connect(self._on_hide_hint)
        row.addWidget(self._hint_hide_btn)
        lay.addWidget(actions)
        self._hint_card = card
        card.setVisible(False)
        return card

    def _build_destinations(self) -> QWidget:
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(_px(10))
        lay.addWidget(_section_label(WHERE_TO_GO))
        self._catalog_note = _label(UNAVAILABLE_TEXT, role="note", color=theme.TEXT_SECONDARY,
                                    wrap=True)
        self._catalog_note.setTextFormat(Qt.TextFormat.RichText)
        self._catalog_note.setOpenExternalLinks(True)
        self._catalog_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._catalog_note.setAccessibleName("Command list unavailable")
        self._catalog_note.setVisible(self._records is None)
        lay.addWidget(self._catalog_note)

        self._cards_grid = QGridLayout()
        self._cards_grid.setHorizontalSpacing(GRID)
        self._cards_grid.setVerticalSpacing(GRID)
        self._cards = []
        for spec in capability_cards(self._app, self._records):
            card = _CapabilityCard(spec["title"], spec["value"], spec["description"],
                                   glyph=spec["glyph"])
            card.clicked.connect(lambda _=False, s=spec: self._on_card(s))
            self._cards.append(card)
        self._cards_cols = 0
        self._relayout_cards(CONTENT_MAX_W)
        lay.addLayout(self._cards_grid)

        self._guides_row = QWidget()
        self._guides_row.setStyleSheet("background: transparent;")
        self._guides_row.setAccessibleName("Guides")
        glay = QHBoxLayout(self._guides_row)
        glay.setContentsMargins(0, 0, 0, 0)
        glay.setSpacing(GRID)
        self._guide_btns = {}
        for label, _kind, _target in GUIDES:
            btn = _button(label)
            btn.clicked.connect(lambda _=False, l=label: self._on_guide(l))
            glay.addWidget(btn)
            self._guide_btns[label] = btn
        glay.addStretch(1)
        self._guides_row.setVisible(False)
        lay.addWidget(self._guides_row)
        return box

    def _build_identity_strip(self) -> QWidget:
        """Restored unchanged from before queue 86 cut it (89): words today,
        the infinity card, the creed -- the owner's own design choices --
        with the three small text links added into the same strip so they
        cost no row of their own."""
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        # 89 fit pass: the strip's own gap is _px(8), not GRID -- it is the
        # last block on the page and every pixel it gives back is a pixel
        # Home does not have to scroll.
        lay.setSpacing(_px(8))
        divider = QFrame()
        divider.setObjectName("homeDivider")
        divider.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(divider)

        self._identity_grid = QGridLayout()
        self._identity_grid.setHorizontalSpacing(GRID)
        self._identity_grid.setVerticalSpacing(GRID)

        self._words_card, self._words_val = self._stat_card(WORDS_TODAY, "0", theme.TEXT_PRIMARY)
        self._infinity_card, _inf = self._stat_card(WORDS_REMAINING, INFINITY, theme.ACCENT)
        # Same size: the larger of the two preferred sizes, never smaller
        # than 150 x 78 (the figure changes width as the day goes on).
        # 111: the probe carries the separator too. "99,999" is wider than
        # "00000", and sizing the pair against the unseparated form would
        # clip the very figures the separator was added to make readable.
        self._words_val.setText(format_count(99999))
        hints = [self._words_card.sizeHint(), self._infinity_card.sizeHint()]
        # +4: the styled 1 px frame border on each side is outside the
        # layout's contents rect, and the hint rounds.
        # The NATURAL size -- what the pair take when the strip has room.
        # It is not a fixed size: pinning it made the strip's own minimum
        # 2 x 396 px at 150% text, which pushed the content column wider
        # than its scroll area and clipped it (89). _fit_stat_cards shrinks
        # the pair in step when the room is not there; the label wraps.
        self._stat_w = max(_px(150), *(hint.width() + 4 for hint in hints))
        self._stat_h = max(_px(78), *(hint.height() + 4 for hint in hints))
        # The floor is the live figure itself: "1428" may never be clipped,
        # however narrow the window gets.
        self._stat_floor = max(_px(110), *(v.sizeHint().width() + 2 * GRID + 4
                                           for v in (self._words_val, _inf)))
        self._stat_size = None
        self._words_val.setText("0")

        self._creed = _label(CREED, role="creed", color=theme.TEXT_SECONDARY, weight=400,
                             family=theme.FONT_FAMILY_DISPLAY, spacing="0.12em")
        self._creed.setAccessibleName("Creed")
        self._creed.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._creed.setWordWrap(True)
        # Stretches and wraps rather than demanding its unwrapped width, so
        # a wide face or a large scale factor folds the creed onto two lines.
        self._creed.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        # The creed, the support line and the links share ONE right-hand
        # cell, three right-aligned lines stacked beside the stat cards (89).
        # The support line used to be a fourth full-width row below the grid;
        # folding it into the cell gave Home back 50 px, which is what let
        # the conditional slot show a diagnostic hint at 900 x 650 without a
        # scrollbar. Nothing was dropped -- the same creed, the same three
        # links, the owner's sentence word for word.
        self._creed_box = QWidget()
        self._creed_box.setStyleSheet("background: transparent;")
        self._creed_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        cbl = QVBoxLayout(self._creed_box)
        cbl.setContentsMargins(0, 0, 0, 0)
        cbl.setSpacing(_px(4))
        # No stretch around these lines: a stretch competes for the same
        # pixels as a wrapped label, and squeezed the creed to one line's
        # height when a larger font pushed it onto two (89, caught by the
        # scaling gate).
        cbl.addWidget(self._creed)

        support_text = _label(SUPPORT_LINE, role="note", color=theme.TEXT_DISABLED, wrap=True)
        support_text.setAccessibleName(SUPPORT_LINE)
        support_text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Ignored horizontally, like the creed: the sentence wraps to the
        # room the cell has instead of demanding its unwrapped width and
        # clipping when a wide face or a large scale factor makes it longer.
        support_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        cbl.addWidget(support_text)
        self._support_text = support_text

        self._links = {}
        links_row = QWidget()
        links_row.setStyleSheet("background: transparent;")
        links_row.setAccessibleName("Links")
        lrl = QHBoxLayout(links_row)
        lrl.setContentsMargins(0, 0, 0, 0)
        lrl.setSpacing(GRID)
        lrl.addStretch(1)
        # Ko-fi last, immediately under the owner's sentence: at the bottom,
        # in the same subtle letters as the other two.
        for text, url in (*IDENTITY_LINKS, (KOFI_LINK, KOFI_URL)):
            btn = _link(text, url)
            lrl.addWidget(btn)
            self._links[text] = btn
        cbl.addWidget(links_row)
        self._links_row = links_row
        self._support_row = links_row

        self._identity_rows = 0
        self._relayout_identity(CONTENT_MAX_W)
        lay.addLayout(self._identity_grid)
        self._identity_box = box
        return box

    def _fit_stat_cards(self, width: int):
        """Both stat cards at one size: their natural size while the strip
        has room for the pair, shrunk in step when it does not, never below
        the width the live figure needs. The label wraps rather than clips,
        so the height follows the width (89)."""
        w = max(self._stat_floor, min(self._stat_w, (width - GRID) // 2))
        h = max(self._stat_h, *(card.layout().totalHeightForWidth(w)
                                for card in (self._words_card, self._infinity_card)))
        if (w, h) == self._stat_size:
            return
        self._stat_size = (w, h)
        for card in (self._words_card, self._infinity_card):
            card.setFixedSize(w, h)

    def _relayout_identity(self, width: int):
        """Cards left, creed right on one row when it fits; otherwise the
        creed takes its own row beneath the cards, still right-aligned."""
        self._fit_stat_cards(width)
        # The creed and the support sentence WRAP, so their minimum is
        # honest: squeezing them costs a line, not a letter. The links row
        # cannot wrap and a QPushButton does not elide -- its
        # minimumSizeHint is three 44 px targets, which the layout will
        # happily grant and then clip the labels inside. At 150% text that
        # drew "Website and d(", "GitH", "Ko-f" (89, caught by rendering).
        # So the links are measured at the width they actually need.
        need = (self._words_card.width() + self._infinity_card.width() + 2 * GRID
                + max(self._creed.minimumSizeHint().width(),
                      self._links_row.sizeHint().width()))
        rows = 1 if need <= width else 2
        if rows == self._identity_rows:
            return
        self._identity_rows = rows
        grid = self._identity_grid
        for w in (self._words_card, self._infinity_card, self._creed_box):
            grid.removeWidget(w)
        grid.addWidget(self._words_card, 0, 0)
        grid.addWidget(self._infinity_card, 0, 1)
        grid.setColumnStretch(2, 1)
        # The creed block under the cards is a continuation of the same
        # strip, not a second row of cards, so when it wraps it gets the
        # tighter gap: 8 px, not GRID (89).
        grid.setVerticalSpacing(_px(8) if rows == 2 else GRID)
        if rows == 1:
            grid.addWidget(self._creed_box, 0, 2)
        else:
            grid.addWidget(self._creed_box, 1, 0, 1, 3)

    def _stat_card(self, label: str, value: str, color: str) -> tuple:
        card = _card()
        card.setAccessibleName(label)
        card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(GRID, _px(12), GRID, _px(12))
        lay.setSpacing(_px(2))
        val = _label(value, role="usage figure", color=color, weight=600)
        val.setAccessibleName(f"{label} value")
        lay.addWidget(val)
        # Wrapped: "words remaining" folds onto two lines in a narrow strip
        # instead of demanding a width the strip has not got (89).
        lay.addWidget(_label(label, role="usage label", color=theme.TEXT_SECONDARY,
                             weight=600, wrap=True))
        return card, val

    def _relayout_cards(self, width: int):
        """As many columns (at most 3) as the widest card allows at this
        width, so a large text scale wraps instead of clipping."""
        widest = max((c.minimumWidth() for c in self._cards), default=1)
        cols = max(1, min(3, (width + GRID) // (widest + GRID)))
        if cols == self._cards_cols:
            return
        self._cards_cols = cols
        grid = self._cards_grid
        for card in self._cards:
            grid.removeWidget(card)
        for c in range(3):
            grid.setColumnStretch(c, 0)
        for i, card in enumerate(self._cards):
            grid.addWidget(card, i // cols, i % cols)
        for c in range(cols):
            grid.setColumnStretch(c, 1)

    # ---- Refresh --------------------------------------------------------

    def set_mark_frame(self, frame):
        """The hub feeds Home the live capture frame. Home no longer shows
        capture state (86), so this is accepted and ignored -- the listening
        indicator carries it where the user is actually working."""
        return None

    def refresh(self):
        self._refresh_notice()
        self._refresh_hint()
        # words today is a live figure; words remaining is the fixed
        # infinity glyph and never changes.
        self._words_val.setText(format_count(words_today(self._app)))

    def _refresh_notice(self):
        notice = home_signals.problem_notice(self._app)
        self._notice = notice
        if notice is None:
            self._notice_card.setVisible(False)
            self._notice_text.setText("")
            self._notice_card.setAccessibleDescription("")
            return
        self._notice_text.setText(notice.text)
        self._notice_card.setAccessibleDescription(f"{notice.text} Evidence: {notice.evidence}.")
        label = notice.action_label or OPEN_VOICE_HELP
        self._notice_btn.setText(label)
        self._notice_btn.setAccessibleName(label)
        self._notice_card.setVisible(True)

    def _on_notice_action(self):
        """The notice's one button. A repair runs and the page re-reads state
        immediately, so the notice disappears when -- and only when -- the
        capability actually came back."""
        notice = self._notice
        if notice is not None and notice.action == home_signals.ACTION_RESTART_HANDS_FREE:
            restart_hands_free(self._app)
            self.refresh()
            return
        self._open_page(VOICE_HELP)

    def eligible_diagnostics(self) -> list:
        """The DIAGNOSTIC tier only (89), minus what the user dismissed.

        Tier 1 of 86's three-tier system, reused as-is -- there is no second
        hint system here. Discovery and generic tips are deliberately NOT
        consulted: a generic command tip belongs in the status strip's
        marquee, and an empty slot is better than a manufactured one.
        """
        try:
            hints = home_signals.diagnostic_hints(self._app)
        except Exception as exc:
            logger.debug(f"[HOME] diagnostic hints failed: {exc}")
            return []
        return [h for h in hints if h.id not in self._dismissed]

    def _refresh_hint(self):
        """Fill the conditional slot, or collapse it to nothing.

        Priority (89): a diagnostic hint, else the one-shot what's-new note,
        else hidden -- and hidden means ZERO height, because a hidden widget
        takes neither a row nor the column's gap.

        The choice is made ONCE per refresh and never swapped while the page
        sits open (82): the hub's poll keeps what the user is reading unless
        it stopped being eligible.
        """
        hints = self.eligible_diagnostics()
        hint = hints[self._hint_index % len(hints)] if hints else None
        if self._hint is not None and hint is not None and hint.id != self._hint.id:
            # Keep what the user is reading, as long as it is still eligible.
            current = [h for h in hints if h.id == self._hint.id]
            if current:
                hint = current[0]
        self._hint = hint
        if hint is not None:
            self._whats_new = None
            door = slot_action_label(hint.action_label, hint.action, hint.arg)
            self._show_slot(SUGGESTION_HEADING["diagnostic"], hint.text,
                            hint_action_sentence(hint.id, door), door,
                            more=len(hints) > 1,
                            details=hint.evidence)
            return
        note = self._due_whats_new()
        if note is not None:
            version, sentence = note
            self._whats_new = version
            self._show_slot(SUGGESTION_HEADING["whatsnew"],
                            f"Samsara {version}", sentence, "", more=False,
                            hide_label=DISMISS)
            # A release note has no working to show.
            return
        self._whats_new = None
        self._hint_card.setVisible(False)

    def _due_whats_new(self) -> Optional[tuple]:
        seen = self._settings.value(WHATS_NEW_SEEN_KEY, "", type=str)
        return whats_new_note(seen or None, current=__version__)

    def _show_slot(self, heading: str, text: str, subtext: str, action_label: str,
                   *, more: bool, hide_label: str = HIDE_SUGGESTION,
                   details: str = ""):
        """Paint the slot.

        `subtext` is the VISIBLE second line -- the action for a diagnostic,
        the release sentence for a what's-new note. `details` is the working
        behind the Details disclosure and is never shown until asked for
        (111). Every call re-collapses the disclosure: a user who opened the
        details of one suggestion has not asked to see the next one's.
        """
        self._hint_heading.setText(heading.upper())
        self._hint_card.setAccessibleName(heading)
        self._hint_text.setText(text)
        self._hint_sub.setText(subtext)
        self._hint_sub.setVisible(bool(subtext))
        self._hint_evidence.setText(details)
        self._hint_details_btn.setChecked(False)
        self._hint_evidence.setVisible(False)
        self._hint_details_btn.setVisible(bool(details))
        self._hint_action_btn.setText(action_label)
        self._hint_action_btn.setAccessibleName(action_label)
        self._hint_action_btn.setVisible(bool(action_label))
        self._hint_next_btn.setVisible(more)
        self._hint_hide_btn.setText(hide_label)
        self._hint_hide_btn.setAccessibleName(hide_label)
        self._hint_card.setVisible(True)

    def _on_toggle_details(self, checked: bool):
        """Show or hide the working. Nothing is spoken: the label appears in
        the accessibility tree where the user just pressed, which a screen
        reader reads on navigation rather than announcing over them."""
        self._hint_evidence.setVisible(bool(checked) and bool(self._hint_evidence.text()))

    # ---- Suggestion actions ---------------------------------------------

    def _load_dismissed(self) -> set:
        raw = self._settings.value(DISMISSED_KEY, "", type=str) or ""
        return {part for part in str(raw).split(",") if part}

    def _save_dismissed(self):
        self._settings.setValue(DISMISSED_KEY, ",".join(sorted(self._dismissed)))

    def _on_hint_action(self):
        if self._hint is None:
            return
        self._route(self._hint.action, self._hint.arg)

    def _on_another_hint(self):
        self._hint_index += 1
        self._hint = None
        self._refresh_hint()

    def _on_hide_hint(self):
        """Durable: a dismissed suggestion does not come back, in this
        session or the next one. Dismissing the what's-new note records the
        version, so it is shown once per version and never again."""
        if self._hint is not None:
            self._dismissed.add(self._hint.id)
            self._save_dismissed()
            self._hint = None
        elif self._whats_new:
            self._settings.setValue(WHATS_NEW_SEEN_KEY, self._whats_new)
            self._whats_new = None
        else:
            return
        self._refresh_hint()

    # ---- Routing --------------------------------------------------------

    def _route(self, action: str, arg):
        if action == "cheatsheet":
            open_cheatsheet_filtered(self._app, arg)
        elif action == "settings":
            open_settings_tab(self._app, arg)
        elif action == "page":
            self._open_page(arg)
        elif action == "guides":
            self.show_guides(not self._guides_row.isVisibleTo(self))
        else:
            logger.error(f"[HOME] action {action!r} has no destination")

    def _on_card(self, spec: dict):
        self._route(spec.get("action"), spec.get("arg"))

    def show_guides(self, visible: bool = True):
        self._guides_row.setVisible(visible)
        if visible:
            first = next(iter(self._guide_btns.values()), None)
            if first is not None:
                first.setFocus()

    def _on_guide(self, label: str):
        open_guide(self._app, label)

    def _relayout(self):
        """Re-flow the two grids for the room the VIEWPORT has now."""
        avail = max(1, min(CONTENT_MAX_W, self._scroll.viewport().width() - 2 * GRID))
        self._relayout_cards(avail)
        self._relayout_identity(avail)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def eventFilter(self, obj, event):
        # The page's own resizeEvent fires BEFORE the scroll area has given
        # the viewport its final width, so relaying out from it alone left
        # the card grid at the column count for a stale, narrower width (89)
        # -- three destinations on two rows in a column wide enough for one.
        # The viewport's own resize is the event that knows the real width.
        if obj is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._relayout()
        return super().eventFilter(obj, event)

    def destinations(self) -> dict:
        """Every actionable control on this page -> its concrete destination
        (the table tests/test_home_qt.py checks). No entry starts, stops or
        pauses capture: Home is not a control surface (86).

        One exception, added deliberately by queue 109: when hands-free has
        STOPPED, the problem notice's button restarts it. That is recovery
        from a fault, not a control -- it appears only while the fault does,
        and for a user who cannot type it is the difference between a route
        back to their input method and none.
        """
        # Ava moved to the hub header (89), so she is no longer one of
        # Home's destinations; DISMISS is the what's-new note's own hide.
        notice = self._notice
        if notice is not None and notice.action_label:
            return {**self._destinations_base(),
                    notice.action_label: (notice.action, notice.capability)}
        return self._destinations_base()

    def _destinations_base(self) -> dict:
        table = {
            OPEN_VOICE_HELP: ("page", VOICE_HELP),
            ANOTHER_SUGGESTION: ("inline", SUGGESTION),
            HIDE_SUGGESTION: ("inline", SUGGESTION),
            DISMISS: ("inline", SUGGESTION),
            DOCS_LINK: ("url", DOCS_URL),
            GITHUB_LINK: ("url", GITHUB_URL),
            KOFI_LINK: ("url", KOFI_URL),
        }
        if self._hint is not None and self._hint.action_label:
            # The key is what the button actually says (95), so the table
            # and the page can never disagree about the one door's name.
            table[slot_action_label(self._hint.action_label, self._hint.action,
                                    self._hint.arg)] = (self._hint.action, self._hint.arg)
        for spec in capability_cards(self._app, self._records):
            table[spec["title"]] = (spec["action"], spec["arg"])
        for label, kind, target in GUIDES:
            table[label] = (kind, target)
        return table

    # ---- Introspection (tests, screenshots) ------------------------------

    @property
    def cards(self) -> list:
        return list(self._cards)

    @property
    def tab_chain(self) -> list:
        return [w for w in self._tab_chain if w.isVisibleTo(self)]

    @property
    def notice(self):
        """The Notice shown right now, or None."""
        return self._notice

    @property
    def hint(self):
        """The Hint shown right now, or None."""
        return self._hint

    def visible_texts(self) -> list:
        """Every string a user can see or hear on the page."""
        from PySide6.QtWidgets import QAbstractButton  # noqa: PLC0415
        out = []
        for w in [self, *self.findChildren(QWidget)]:
            if not w.isVisibleTo(self):
                continue
            if isinstance(w, QLabel) and w.text():
                out.append(w.text())
            if isinstance(w, QAbstractButton) and w.text():
                out.append(w.text())
            for s in (w.accessibleName(), w.accessibleDescription(), w.toolTip()):
                if s:
                    out.append(s)
        return out

    @property
    def content_widget(self) -> QWidget:
        return self._content


def find_by_accessible_name(root: QWidget, name: str) -> Optional[QWidget]:
    """The first descendant (or root) whose accessible name is `name` -- the
    way a voice or screen-reader user reaches a control."""
    if root.accessibleName() == name:
        return root
    for w in root.findChildren(QWidget):
        if w.accessibleName() == name:
            return w
    return None


def overflowing_widgets(root: QWidget) -> list:
    """Visible descendants whose preferred size does not fit the rect the
    layout gave them. Word-wrapped labels are judged by heightForWidth;
    labels marked elided (property 'elided') are judged by height only.
    Empty when nothing clips -- the scaling gate."""
    bad = []
    for w in root.findChildren(QWidget):
        if not w.isVisibleTo(root) or w.width() <= 0 or w.height() <= 0:
            continue
        if not isinstance(w, (QLabel, QPushButton)):
            continue
        hint = w.sizeHint()
        name = w.accessibleName() or w.objectName() or type(w).__name__
        if isinstance(w, QLabel) and w.wordWrap():
            need_h = w.heightForWidth(w.width())
            if need_h > w.height() + 1:
                bad.append(f"{name}: needs h={need_h} has h={w.height()}")
            continue
        if isinstance(w, QLabel) and w.property("elided"):
            if hint.height() > w.height() + 1:
                bad.append(f"{name}: needs h={hint.height()} has h={w.height()}")
            continue
        if not isinstance(w, QLabel) and w.hasHeightForWidth():
            # A card whose description wraps: judged at the width it was
            # given, like a wrapped label (its hint is the unwrapped size).
            need_h = w.heightForWidth(w.width())
            if w.minimumWidth() > w.width() + 1 or need_h > w.height() + 1:
                bad.append(f"{name}: needs w>={w.minimumWidth()} h={need_h}"
                           f" has {w.width()}x{w.height()}")
            continue
        if hint.width() > w.width() + 1 or hint.height() > w.height() + 1:
            bad.append(f"{name}: needs {hint.width()}x{hint.height()}"
                       f" has {w.width()}x{w.height()}")
    return bad
