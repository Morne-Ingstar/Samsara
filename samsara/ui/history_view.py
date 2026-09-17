"""
Reusable dictation-history view for Samsara.

What History is for (queue 79): getting back something you dictated. So the
view is built around recovery, not around a table of records:

  * newest first, grouped under day headers;
  * one search box that always covers ALL history (a 7-day search that hides
    the entry you lost is the failure this window exists to prevent) --
    the date range only scopes browsing;
  * every row shows enough to recognise it without opening anything: two
    lines of text, the window it went to, how it was captured, and what
    happened to it (Not typed / Failed / Corrected / Command);
  * copying is one action: the row's Copy button, double-click, Enter or
    Ctrl+C; the detail card also copies the ORIGINAL words when a
    correction changed them.

Everything shown comes from columns HistoryManager already records
(display_text, raw_text, app_context, mode, status, entry_type,
matched_command, duration_ms) -- no new storage.

Embedded by BOTH the standalone history window (samsara/ui/history_qt.py)
and the main window's History page (samsara/ui/main_window_qt.py) -- one
implementation, not two. Takes the store it reads from directly (no globals,
no `app` object), so tests construct it against a tmp store.

Row loading runs on a background thread (samsara.runtime.thread_registry),
results marshaled back to the Qt thread via a Signal.

Colour and type come only from samsara.ui.theme tokens; tints are derived
from those tokens with theme._rgba, never written as literals. User text is
always rendered as PLAIN text: QLabel's default AutoText treats a dictated
"<b>" as markup and swallows it (the queue 55 bug class).
"""

import html
import re
from pathlib import Path
from collections import namedtuple
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, Signal, Slot, QTimer, QSize, QSettings, QRect, QPoint
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut, QTextLayout, QFont
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLayout, QFrame,
    QListWidget, QListWidgetItem, QAbstractItemView, QButtonGroup,
    QLineEdit, QComboBox, QPushButton, QLabel, QPlainTextEdit, QCheckBox,
    QMenu, QMessageBox, QSizePolicy, QFileDialog,
)

from samsara import history_export
from samsara import outcome_ring
from samsara.ui import theme
from samsara.runtime import thread_registry

from samsara.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_PAGE_SIZE = 200
_CONTROL_H = 40           # >= 40px accessibility minimum for interactive controls
# Qt drops a rounded background whose radius exceeds half the inner height
# (40 px minus the 1 px borders): a 20 px radius left the checked chip with
# dark text and no fill.
_CHIP_RADIUS = 16
_ROW_MIN_HEIGHT = 56
_ROW_PAD_V = 9
_ROW_PAD_H = 12
_ROW_SPACING = 12
_TEXT_MAX_LINES = 2
# 14 px top + the day label's line box + 4 px bottom (see
# _build_day_header_widget). 34 fitted the old 12 px label; at the TYPE_MIN
# floor (83) the line box is 19 px, so the label was clipped by 3 px.
_HEADER_HEIGHT = 38
_LOAD_OLDER_HEIGHT = 48
_TIME_COL_WIDTH = 48
_COPY_BTN_WIDTH = 72
_TOAST_MS = 1800          # transient "Copied" confirmation
_DETAIL_TEXT_MAX_LINES = 6
_APP_CONTEXT_MAX = 48
_EMPTY_HEIGHT = 160

_TRANSPARENT = "historyTransparent"   # object name for non-painting containers

_SCOPE_LAST_7_DAYS = "Last 7 days"
_SCOPE_ALL = "All time"
_SCOPE_ALIASES = {"All": _SCOPE_ALL}          # value saved by the pre-79 view
_SCOPE_SETTING = "history/date_scope"
_EMPTY_WAKE_SETTING = "history/show_empty_wake_attempts"   # key kept: saved choices survive
_NO_SPEECH_MARKERS = {"(no speech detected)", "no speech detected"}

EXPORT_LABEL = "Export…"
FILTER_ALL = "All"
FILTER_DICTATION = "Dictation"
FILTER_COMMANDS = "Commands"
FILTER_NOT_TYPED = "Not typed"
_FILTERS = (FILTER_ALL, FILTER_DICTATION, FILTER_COMMANDS, FILTER_NOT_TYPED)
_FILTER_ALIASES = {"Failed": FILTER_NOT_TYPED}

NO_SPEECH_TOGGLE_LABEL = "Show attempts with no speech"
SEARCH_PLACEHOLDER = "Search all history"
TITLE = "History"
SUBTITLE = "Newest first. Double-click an entry or press Enter to copy it."

# The history `mode` column says how the words were captured. "hold" is
# written by the hotkey path in BOTH hold and toggle modes, so it reads as
# "Hotkey", never "Hold".
_MODE_LABELS = {
    "hold": "Hotkey",
    "toggle": "Hotkey",
    "continuous": "Continuous",
    "wake": "Wake word",
    "dictate": "Hands-free",
    "streaming": "Streaming",
}

# Outcome kinds -> (text token, tint token). Plain typed dictation has no
# outcome pill: it is the normal case and a pill on every row is noise.
KIND_COMMAND = "command"
KIND_NOT_TYPED = "not_typed"
KIND_FAILED = "failed"
KIND_NO_SPEECH = "no_speech"
KIND_CORRECTED = "corrected"

Outcome = namedtuple("Outcome", "label kind")


# ---------------------------------------------------------------------------
# Pure helpers -- no Qt dependency, directly unit-testable
# ---------------------------------------------------------------------------

def day_label(dt: datetime, now: "datetime | None" = None) -> str:
    """Section-header label for the day `dt` falls on, relative to `now`.

    "Today" / "Yesterday" / "Mon, Jul 6" (this year) / "Jul 6, 2025"
    (other years). Built from plain int day-of-month (dt.day) rather than
    a platform-specific strftime no-leading-zero flag."""
    if now is None:
        now = datetime.now()
    d, today = dt.date(), now.date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    month = dt.strftime("%b")
    if d.year == today.year:
        weekday = dt.strftime("%a")
        return f"{weekday}, {month} {dt.day}"
    return f"{month} {dt.day}, {d.year}"


def _parse_ts(raw_ts: str) -> "datetime | None":
    try:
        return datetime.fromisoformat(raw_ts)
    except Exception:
        return None


def _row_text(row: dict) -> str:
    return str(row.get('display_text') or row.get('raw_text') or '')


def is_no_speech_attempt(row: dict) -> bool:
    """A capture that produced no usable words, from ANY capture path.

    The recorder writes these as status='empty' with display_text
    "(no speech detected)" -- from the wake word (entry_type 'failed', mode
    'wake'), the hotkey (mode 'hold') and streaming (entry_type 'dictation').
    The pre-79 filter only matched mode 'wake', so hotkey no-speech rows
    still showed up as red "Failed" entries."""
    if str(row.get('status') or '') == 'empty':
        return True
    text = _row_text(row).strip().casefold()
    if text in _NO_SPEECH_MARKERS:
        return True
    is_wake = row.get('mode') == 'wake' or row.get('entry_type') in ('wake', 'wake_command')
    return is_wake and not text


# Kept for callers/tests written against the pre-79 name.
_is_empty_wake_attempt = is_no_speech_attempt


def _normalise_words(text: str) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold()).strip()


def is_corrected(row: dict) -> bool:
    """True when post-processing changed the WORDS Whisper heard (smart
    correction, vocabulary, number rules) -- not just case, spacing or
    punctuation, which every row gets. Measured on the owner's store: word
    changes on ~8% of dictations; case/punctuation-only changes on ~55%,
    which would put the marker on most rows and mean nothing."""
    if row.get('entry_type') != 'dictation':
        return False
    raw = str(row.get('raw_text') or '')
    shown = str(row.get('display_text') or '')
    if not raw.strip() or not shown.strip():
        return False
    return _normalise_words(raw) != _normalise_words(shown)


def row_outcome(row: dict) -> "Outcome | None":
    """What happened to this entry, as a short label, or None for plain
    typed dictation."""
    entry_type = str(row.get('entry_type') or 'dictation')
    status = str(row.get('status') or 'success')
    if is_no_speech_attempt(row):
        return Outcome("No speech", KIND_NO_SPEECH)
    if entry_type == 'failed':
        return Outcome("Failed", KIND_FAILED)
    if entry_type in ('command', 'wake_command'):
        if status == 'success':
            # Persistent history predates OutcomeRecord and stores the matched
            # spoken phrase. Resolve it through the same cached catalog index;
            # a macro or command removed in a later release retains that
            # stored phrase rather than exposing an internal id or a blank.
            stored = str(row.get("matched_command") or "").strip()
            label = outcome_ring.canonical_command_label_for_phrase(stored, stored)
            return Outcome(label or "Command", KIND_COMMAND)
        return Outcome(status.replace('_', ' ').capitalize() or "Command", KIND_NOT_TYPED)
    if status == 'failed':
        return Outcome("Not typed", KIND_NOT_TYPED)
    return None


def row_pills(row: dict) -> list:
    """Every pill a row shows, most important first."""
    pills = []
    outcome = row_outcome(row)
    if outcome is not None:
        pills.append(outcome)
    if is_corrected(row):
        pills.append(Outcome("Corrected", KIND_CORRECTED))
    return pills


def list_text(row: dict) -> str:
    """The text a row shows: whitespace collapsed to one line, and the
    recorder's placeholder strings turned into plain language."""
    text = _row_text(row)
    if is_no_speech_attempt(row):
        return "No speech detected"
    if text.startswith("[FAILED] "):
        return "Transcription failed: " + text[len("[FAILED] "):]
    return " ".join(text.split())


def copy_text(row: dict) -> str:
    """What Copy puts on the clipboard: exactly what was typed (or would
    have been), with its line breaks. Nothing for no-speech/failed rows."""
    if is_no_speech_attempt(row) or str(row.get('entry_type')) == 'failed':
        return ""
    return _row_text(row)


def _elide_plain(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def meta_text(row: dict) -> str:
    """Secondary line: the window it went to and how it was captured."""
    parts = []
    context = str(row.get('app_context') or '').strip()
    if context and context.casefold() != 'unknown':
        parts.append(_elide_plain(context, _APP_CONTEXT_MAX))
    mode_label = _MODE_LABELS.get(str(row.get('mode') or ''))
    if mode_label:
        parts.append(mode_label)
    return " \u00b7 ".join(parts)


def _matches_type_filter(row: dict, filter_name: str) -> bool:
    """Client-side type-filter check, applied after the SQL fetch (a page
    may come back with fewer than _PAGE_SIZE matches). "Not typed" is
    everything whose words did not land: failed pastes, transcription
    errors and (when shown) no-speech attempts."""
    filter_name = _FILTER_ALIASES.get(filter_name, filter_name)
    if filter_name == FILTER_ALL:
        return True
    if filter_name == FILTER_COMMANDS:
        return row.get('entry_type') in ('command', 'wake_command')
    if filter_name == FILTER_DICTATION:
        return row.get('entry_type') == 'dictation' and not is_no_speech_attempt(row)
    if filter_name == FILTER_NOT_TYPED:
        return (row.get('entry_type') == 'failed'
                or row.get('status') in ('failed', 'empty')
                or is_no_speech_attempt(row))
    return True


def _scope_start(scope: str, now: "datetime | None" = None):
    if scope != _SCOPE_LAST_7_DAYS:
        return None
    return (now or datetime.now()) - timedelta(days=7)


def summary_text(count: int, scope: str, query: str, has_more: bool) -> str:
    """The line under the list: how many entries, and over what range."""
    more = "+" if has_more else ""
    if query:
        noun = "match" if count == 1 and not has_more else "matches"
        return f"{count}{more} {noun} for \u201c{query}\u201d in all history"
    noun = "entry" if count == 1 and not has_more else "entries"
    where = "in the last 7 days" if scope == _SCOPE_LAST_7_DAYS else "in all history"
    return f"{count}{more} {noun} {where}"


def empty_state_text(query: str, scope: str, filter_name: str, store_available: bool,
                     has_any_history: bool = True) -> str:
    if query:
        return f"No matches for \u201c{query}\u201d in all history."
    if not store_available:
        return "History isn't available right now."
    if not has_any_history:
        return "Nothing here yet. What you dictate will show up here, newest first."
    if filter_name != FILTER_ALL:
        where = "in the last 7 days" if scope == _SCOPE_LAST_7_DAYS else "yet"
        return f"No {filter_name.lower()} entries {where}."
    if scope == _SCOPE_LAST_7_DAYS:
        return "Nothing dictated in the last 7 days."
    return "Nothing here yet. What you dictate will show up here, newest first."


# ---------------------------------------------------------------------------
# Colour: every text/background pair the view draws, from tokens. The tests
# check each pair against WCAG AA; the report prints the same table.
# ---------------------------------------------------------------------------

_TINT_ALPHA = 0.14
_SELECTED_ALPHA = 0.14


def _tint(token: str, alpha: float) -> str:
    return theme._rgba(token, alpha)


def _selected_bg():
    return _tint(theme.ACCENT, _SELECTED_ALPHA)


def _rgba_parts(colour: str) -> tuple:
    """(r, g, b, a) from a '#rrggbb' or 'rgba(r,g,b,a)' token."""
    text = str(colour).strip()
    if text.startswith("#"):
        r, g, b = theme._hex_to_rgb(text)
        return r, g, b, 1.0
    m = theme._CSS_RGB.match(text)
    if not m:
        raise ValueError(f"not a colour token: {colour!r}")
    r, g, b = (float(c) for c in m.group(1, 2, 3))
    alpha = m.group(4)
    a = 1.0 if alpha is None else (float(alpha[:-1]) / 100 if alpha.endswith("%") else float(alpha))
    return r, g, b, a


def composite(*layers: str) -> tuple:
    """Flatten colour layers bottom -> top into an opaque (r, g, b)."""
    r, g, b, _a = _rgba_parts(layers[0])
    for layer in layers[1:]:
        lr, lg, lb, la = _rgba_parts(layer)
        r, g, b = (lr * la + r * (1 - la), lg * la + g * (1 - la), lb * la + b * (1 - la))
    return round(r), round(g), round(b)


def _luminance(rgb: tuple) -> float:
    out = []
    for v in rgb:
        c = v / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast_ratio(fg: tuple, bg: tuple) -> float:
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _solid(*layers: str) -> str:
    r, g, b = composite(*layers)
    return f"#{r:02x}{g:02x}{b:02x}"


# kind -> (text colour, pill background). Pill backgrounds are SOLID colours
# flattened from the tokens over BG0, not translucent tints: a translucent
# red pill on a selected (accent-tinted) row measured 4.1:1, below AA. Solid,
# a pill reads the same on a resting, hovered or selected row.
def _pill_colours():
    return {
        KIND_COMMAND: (theme.ACCENT, _solid(theme.BG0, _tint(theme.ACCENT, _TINT_ALPHA))),
        KIND_NOT_TYPED: (theme.WARNING, _solid(theme.BG0, _tint(theme.WARNING, _TINT_ALPHA))),
        KIND_FAILED: (theme.ERROR, _solid(theme.BG0, _tint(theme.ERROR, _TINT_ALPHA))),
        KIND_NO_SPEECH: (theme.TEXT_SECONDARY, _solid(theme.BG0, theme.BORDER_FAINT)),
        KIND_CORRECTED: (theme.TEXT_SECONDARY, _solid(theme.BG0, theme.BORDER_FAINT)),
    }


def text_colour_pairs() -> list:
    """(role, px, foreground layers, background layers) for every text the
    view draws, on each surface it can sit on. Foreground layers are
    composited over the background, so a translucent token is measured as
    it actually renders."""
    page = [theme.BG1]
    hovered = [theme.BG1, theme.BG2]
    selected = [theme.BG1, _selected_bg()]
    field = [theme.BG1, theme.BG2]
    pairs = [
        ("title", theme.FONT_SIZE_TITLE, [theme.TEXT_PRIMARY], page),
        ("subtitle", theme.TYPE_SECONDARY, [theme.TEXT_SECONDARY], page),
        ("search text", theme.TYPE_BODY, [theme.TEXT_PRIMARY], field),
        ("search placeholder", theme.TYPE_BODY, [theme.TEXT_SECONDARY], field),
        ("range combo", theme.TYPE_BODY, [theme.TEXT_PRIMARY], field),
        ("filter chip (off)", theme.TYPE_BODY, [theme.TEXT_SECONDARY], page),
        ("filter chip (on)", theme.TYPE_BODY, [theme.TEXT_ON_ACCENT], [theme.BG1, theme.ACCENT]),
        ("no-speech toggle", theme.TYPE_BODY, [theme.TEXT_PRIMARY], page),
        ("day header", theme.TYPE_SECTION_LABEL, [theme.TEXT_SECONDARY], page),
        ("summary / status", theme.TYPE_SECONDARY, [theme.TEXT_SECONDARY], page),
        ("copied confirmation", theme.TYPE_SECONDARY, [theme.SUCCESS], page),
        ("clear history button", theme.TYPE_BODY, [theme.TEXT_SECONDARY], page),
        ("clear history button (hover)", theme.TYPE_BODY, [theme.ERROR], page),
        ("empty state", theme.TYPE_STATE, [theme.TEXT_SECONDARY], page),
    ]
    for surface_name, surface in (("", page), (" (hover)", hovered), (" (selected)", selected)):
        pairs += [
            ("row time" + surface_name, theme.TYPE_MIN, [theme.TEXT_SECONDARY], surface),
            ("row text" + surface_name, theme.TYPE_BODY, [theme.TEXT_PRIMARY], surface),
            ("row meta" + surface_name, theme.TYPE_MIN, [theme.TEXT_SECONDARY], surface),
            ("row copy button" + surface_name, theme.TYPE_MIN, [theme.TEXT_SECONDARY], surface),
        ]
        for kind, (text_colour, pill_bg) in _pill_colours().items():
            pairs.append((f"pill {kind}{surface_name}", theme.TYPE_MIN, [text_colour], surface + [pill_bg]))
    card = [theme.BG1, theme.BG2]
    pairs += [
        ("detail meta", theme.TYPE_MIN, [theme.TEXT_SECONDARY], card),
        ("detail text", theme.TYPE_BODY, [theme.TEXT_PRIMARY], card),
        ("detail heard-as", theme.TYPE_SECONDARY, [theme.TEXT_SECONDARY], card),
        ("detail not-typed note", theme.TYPE_SECONDARY, [theme.WARNING], card),
        ("detail copy (primary)", theme.TYPE_BODY, [theme.TEXT_ON_ACCENT], card + [theme.ACCENT]),
        ("detail copy original", theme.TYPE_BODY, [theme.TEXT_PRIMARY], card),
        ("detail delete", theme.TYPE_BODY, [theme.TEXT_SECONDARY], card),
        ("menu item", theme.TYPE_BODY, [theme.TEXT_PRIMARY], [theme.BG2]),
        ("menu item (selected)", theme.TYPE_BODY, [theme.ACCENT], [theme.BG2, _tint(theme.ACCENT, 0.16)]),
    ]
    return pairs


def contrast_table() -> list:
    """[(role, px, ratio)] -- measured, not assumed."""
    rows = []
    for role, px, fg_layers, bg_layers in text_colour_pairs():
        bg = composite(*bg_layers)
        fg = composite(*([f"#{bg[0]:02x}{bg[1]:02x}{bg[2]:02x}"] + fg_layers))
        rows.append((role, px, contrast_ratio(fg, bg)))
    return rows


# ---------------------------------------------------------------------------
# Stylesheet. Scoped to the view by object name: a bare `QWidget { background }`
# rule here made every QLabel and QCheckBox paint a BG0 box, while the view
# itself (a plain QWidget) painted nothing -- the black strip behind
# "7 entries loaded" and behind "Show empty wake attempts" (queue 79).
# ---------------------------------------------------------------------------

def build_stylesheet() -> str:
    return f"""
QWidget#historyView {{
    background-color: {theme.BG1};
}}
QLabel, QCheckBox, QPushButton, QLineEdit, QComboBox, QPlainTextEdit, QMenu {{
    color: {theme.TEXT_PRIMARY};
    font-family: {theme.FONT_FAMILY};
    font-size: {theme.TYPE_BODY}px;
}}
QLabel, QCheckBox, QWidget#{_TRANSPARENT} {{
    background: transparent;
    border: none;
}}
QLabel#historyTitle {{
    font-size: {theme.FONT_SIZE_TITLE}px;
    font-weight: 600;
}}
QLabel#historySubtitle, QLabel#historyStatus {{
    color: {theme.TEXT_SECONDARY};
    font-size: {theme.TYPE_SECONDARY}px;
}}
QLabel#historyStatus[toast="true"] {{
    color: {theme.SUCCESS};
}}
QLabel#historyDayHeader {{
    color: {theme.TEXT_SECONDARY};
    font-size: {theme.TYPE_SECTION_LABEL}px;
    font-weight: 700;
    letter-spacing: {theme.LETTER_SPACING_SECTION};
}}
QLabel#historyTime, QLabel#historyMeta {{
    color: {theme.TEXT_SECONDARY};
    font-size: {theme.TYPE_MIN}px;
}}
QLabel#historyEmpty {{
    color: {theme.TEXT_SECONDARY};
    font-size: {theme.TYPE_STATE}px;
}}
QListWidget {{
    background-color: {theme.BG1};
    border: none;
    outline: none;
}}
QListWidget::item {{
    border: none;
    border-radius: 8px;
    padding: 0px;
}}
QListWidget::item:hover {{
    background-color: {theme.BG2};
}}
QListWidget::item:selected {{
    background-color: {_selected_bg()};
}}
QLineEdit {{
    background-color: {theme.BG2};
    border: 1px solid {theme.BORDER};
    border-radius: 8px;
    padding: 0px 12px;
    min-height: {_CONTROL_H - 2}px;
    color: {theme.TEXT_PRIMARY};
    selection-background-color: {theme.ACCENT};
    selection-color: {theme.TEXT_ON_ACCENT};
}}
QLineEdit:focus {{ border-color: {theme.ACCENT}; }}
QComboBox {{
    background-color: {theme.BG2};
    border: 1px solid {theme.BORDER};
    border-radius: 8px;
    padding: 0px 12px;
    min-height: {_CONTROL_H - 2}px;
    color: {theme.TEXT_PRIMARY};
}}
QComboBox:hover {{ border-color: {theme.ACCENT}; }}
QComboBox:disabled {{ color: {theme.TEXT_DISABLED}; border-color: {theme.BORDER_FAINT}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
}}
QComboBox::down-arrow {{
    image: url({theme.ARROW_PATH});
    width: 10px;
    height: 6px;
    margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background-color: {theme.BG2};
    color: {theme.TEXT_PRIMARY};
    selection-background-color: {theme.ACCENT};
    selection-color: {theme.TEXT_ON_ACCENT};
    border: 1px solid {theme.BORDER};
}}
QCheckBox {{
    spacing: 8px;
    min-height: {_CONTROL_H}px;
}}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {theme.BORDER};
    border-radius: 4px;
    background: {theme.BG2};
}}
QCheckBox::indicator:checked {{
    background: {theme.ACCENT};
    border-color: {theme.ACCENT};
}}
QPushButton {{
    background-color: transparent;
    border: 1px solid {theme.BORDER};
    border-radius: 8px;
    color: {theme.TEXT_PRIMARY};
    padding: 0px 14px;
    min-height: {_CONTROL_H - 2}px;
}}
QPushButton:hover {{
    background-color: {theme.BG2};
    border-color: {theme.ACCENT};
}}
QPushButton:pressed {{
    background-color: {_tint(theme.ACCENT, 0.18)};
}}
QPushButton[class="chip"] {{
    color: {theme.TEXT_SECONDARY};
    border-radius: {_CHIP_RADIUS}px;
    padding: 0px 16px;
}}
QPushButton[class="chip"]:checked {{
    background-color: {theme.ACCENT};
    border-color: {theme.ACCENT};
    color: {theme.TEXT_ON_ACCENT};
    font-weight: 600;
}}
QPushButton[class="rowCopy"] {{
    color: {theme.TEXT_SECONDARY};
    border-color: {theme.BORDER_FAINT};
    font-size: {theme.TYPE_MIN}px;
    padding: 0px 10px;
    min-height: 34px;
}}
QPushButton[class="rowCopy"]:hover {{
    color: {theme.ACCENT};
    border-color: {theme.ACCENT};
}}
QPushButton[class="danger"] {{
    color: {theme.TEXT_SECONDARY};
    border-color: transparent;
}}
QPushButton[class="danger"]:hover {{
    color: {theme.ERROR};
    border-color: {_tint(theme.ERROR, 0.45)};
    background-color: {_tint(theme.ERROR, 0.10)};
}}
QPushButton[class="export"] {{
    /* Queue 122: 44 px, not this file's _CONTROL_H of 40. A stylesheet
       min-height overrides setMinimumHeight(), so the number has to be
       stated here or the programmatic one is silently ignored. */
    min-height: 42px;
    padding: 0px 18px;
}}
QPushButton[class="primary"] {{
    background-color: {theme.ACCENT};
    border-color: {theme.ACCENT};
    color: {theme.TEXT_ON_ACCENT};
    font-weight: 600;
}}
QPushButton[class="primary"]:hover {{
    background-color: {theme.ACCENT_HOVER};
    border-color: {theme.ACCENT_HOVER};
}}
QFrame#historyDetail {{
    background-color: {theme.BG2};
    border: 1px solid {theme.BORDER};
    border-radius: 10px;
}}
QFrame#historyDetail QLabel#historyDetailMeta {{
    color: {theme.TEXT_SECONDARY};
    font-size: {theme.TYPE_MIN}px;
}}
QFrame#historyDetail QLabel#historyHeardAs {{
    color: {theme.TEXT_SECONDARY};
    font-size: {theme.TYPE_SECONDARY}px;
}}
QFrame#historyDetail QLabel#historyNote {{
    color: {theme.WARNING};
    font-size: {theme.TYPE_SECONDARY}px;
}}
QPlainTextEdit {{
    background: transparent;
    border: none;
    color: {theme.TEXT_PRIMARY};
    padding: 0px;
    selection-background-color: {theme.ACCENT};
    selection-color: {theme.TEXT_ON_ACCENT};
}}
QMenu {{
    background-color: {theme.BG2};
    color: {theme.TEXT_PRIMARY};
    border: 1px solid {theme.BORDER};
    padding: 4px 0;
    font-size: {theme.TYPE_BODY}px;
}}
QMenu::item {{ padding: 8px 24px 8px 16px; }}
QMenu::item:selected {{
    background-color: {_tint(theme.ACCENT, 0.16)};
    color: {theme.ACCENT};
}}
QMessageBox {{
    background-color: {theme.BG1};
}}
QMessageBox QLabel {{
    color: {theme.TEXT_PRIMARY};
}}
""" + theme.SCROLLBAR_QSS


def _set_class(widget: QWidget, name: str) -> None:
    widget.setProperty("class", name)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _make_transparent(widget: QWidget) -> None:
    """Container widgets inside the view must not paint. Done by object
    name against the view's own sheet, NOT with a per-widget
    setStyleSheet("background: transparent"): a selector-less sheet also
    applies to every child and, being closer, beats the view's rules --
    it stripped the checked filter chip's ACCENT fill."""
    widget.setObjectName(_TRANSPARENT)


def _danger_btn(label: str, parent: QWidget | None = None) -> QPushButton:
    b = QPushButton(label, parent)
    _set_class(b, "danger")
    return b


def _plain_label(text: str = "", object_name: str = "", parent: QWidget | None = None) -> QLabel:
    """A QLabel that can never interpret user text as markup."""
    lbl = QLabel(parent)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    if object_name:
        lbl.setObjectName(object_name)
    lbl.setText(text)
    return lbl


def plain_tooltip(text: str) -> str:
    """Tooltips are rich-text sniffed too; escape and force rich so the
    dictated characters show literally."""
    return "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"


def elide_lines(text: str, font: QFont, width: int, max_lines: int) -> "tuple[str, int]":
    """Word-wrap `text` into at most `max_lines` lines of `width` px and
    ellipsise the last one if it overflows. Returns (text with explicit line
    breaks, number of lines)."""
    width = max(int(width), 20)
    layout = QTextLayout(text, font)
    layout.beginLayout()
    lines = []
    while len(lines) < max_lines:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        lines.append((line.textStart(), line.textLength()))
    overflow = layout.createLine().isValid() if len(lines) == max_lines else False
    layout.endLayout()
    if not lines:
        return "", 1
    parts = [text[s:s + n].rstrip() for s, n in lines]
    if overflow:
        rest = text[lines[-1][0]:]
        parts[-1] = QFontMetrics(font).elidedText(rest, Qt.TextElideMode.ElideRight, width)
    return "\n".join(parts), len(parts)


# ---------------------------------------------------------------------------
# Flow layout -- the filter row wraps instead of clipping at narrow widths.
# ---------------------------------------------------------------------------

class _FlowLayout(QLayout):
    def __init__(self, parent=None, h_spacing=8, v_spacing=8):
        super().__init__(parent)
        self._items = []
        self._h = h_spacing
        self._v = v_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect, test_only):
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.x() + rect.width():
                x = rect.x()
                y += line_h + self._v
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class _FilterChips(QWidget):
    """Exclusive chip group. Exposes the QComboBox-shaped API the view,
    tests and tools already use (currentText/setCurrentText/
    currentTextChanged)."""

    currentTextChanged = Signal(str)

    def __init__(self, names, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = {}
        for name in names:
            btn = QPushButton(name, self)
            btn.setCheckable(True)
            btn.setAccessibleName(f"Show {name.lower()} entries" if name != FILTER_ALL else "Show all entries")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            _set_class(btn, "chip")
            self._group.addButton(btn)
            lay.addWidget(btn)
            self._buttons[name] = btn
        self._buttons[names[0]].setChecked(True)
        self._group.buttonToggled.connect(self._on_toggled)

    def buttons(self):
        return dict(self._buttons)

    def currentText(self) -> str:
        btn = self._group.checkedButton()
        return btn.text() if btn is not None else FILTER_ALL

    def setCurrentText(self, name: str) -> None:
        name = _FILTER_ALIASES.get(name, name)
        btn = self._buttons.get(name)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def _on_toggled(self, btn, checked):
        if checked:
            self.currentTextChanged.emit(btn.text())


# ---------------------------------------------------------------------------
# Row widgets
# ---------------------------------------------------------------------------

def _build_day_header_widget(label: str, parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    _make_transparent(w)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(_ROW_PAD_H, 14, _ROW_PAD_H, 4)
    lbl = _plain_label(label.upper(), "historyDayHeader", w)
    lay.addWidget(lbl)
    lay.addStretch()
    return w


def _pill(outcome: Outcome, parent: QWidget | None = None) -> QLabel:
    text_colour, background = _pill_colours()[outcome.kind]
    pill = _plain_label(outcome.label, "historyPill", parent)
    pill.setProperty("kind", outcome.kind)
    pill.setStyleSheet(
        f"color: {text_colour}; background: {background}; border: none; border-radius: 9px;"
        f" padding: 1px 8px; font-size: {theme.TYPE_MIN}px; font-weight: 600;"
    )
    return pill


class _HistoryRow(QWidget):
    """One entry: time | two lines of text over pills + meta | Copy."""

    def __init__(self, row: dict, on_copy, parent: QWidget | None = None):
        super().__init__(parent)
        _make_transparent(self)
        self.row = row
        lay = QHBoxLayout(self)
        lay.setContentsMargins(_ROW_PAD_H, _ROW_PAD_V, _ROW_PAD_H, _ROW_PAD_V)
        lay.setSpacing(_ROW_SPACING)

        dt = _parse_ts(str(row.get('timestamp', '')))
        self.time_label = _plain_label(dt.strftime("%H:%M") if dt else "", "historyTime", self)
        self.time_label.setFixedWidth(_TIME_COL_WIDTH)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.time_label, alignment=Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(4)
        self.full_text = list_text(row)
        self.text_label = _plain_label(self.full_text, "historyText", self)
        self.text_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if is_no_speech_attempt(row) or str(row.get('entry_type')) == 'failed':
            self.text_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY};")
        body.addWidget(self.text_label)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(6)
        self.pills = [_pill(p, self) for p in row_pills(row)]
        for pill in self.pills:
            meta_row.addWidget(pill)
        self.meta_label = _plain_label(meta_text(row), "historyMeta", self)
        self.meta_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        meta_row.addWidget(self.meta_label, stretch=1)
        body.addLayout(meta_row)
        lay.addLayout(body, stretch=1)

        self.copy_button = QPushButton("Copy", self)
        _set_class(self.copy_button, "rowCopy")
        self.copy_button.setFixedWidth(_COPY_BTN_WIDTH)
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        stamp = dt.strftime("%H:%M") if dt else ""
        self.copy_button.setAccessibleName(f"Copy entry from {stamp}".strip())
        self.copy_button.clicked.connect(lambda: on_copy(self))
        self.copy_button.setVisible(bool(copy_text(row)))
        lay.addWidget(self.copy_button, alignment=Qt.AlignmentFlag.AlignVCenter)

        if self.full_text:
            self.text_label.setToolTip(plain_tooltip(_row_text(row)))
        self._lines = 1

    def polish_children(self) -> None:
        for child in (self.time_label, self.text_label, self.meta_label, self.copy_button, *self.pills):
            child.ensurePolished()

    def text_width_for(self, row_width: int) -> int:
        return max(row_width - 2 * _ROW_PAD_H - _TIME_COL_WIDTH - 2 * _ROW_SPACING - _COPY_BTN_WIDTH, 40)

    def relayout(self, row_width: int) -> int:
        """Re-wrap the text for `row_width` and return the height the row
        needs."""
        text_w = self.text_width_for(row_width)
        shown, self._lines = elide_lines(self.full_text, self.text_label.font(), text_w, _TEXT_MAX_LINES)
        self.text_label.setText(shown)
        pills_w = sum(p.sizeHint().width() + 6 for p in self.pills)
        meta_fm = QFontMetrics(self.meta_label.font())
        self.meta_label.setText(meta_fm.elidedText(
            meta_text(self.row), Qt.TextElideMode.ElideRight, max(text_w - pills_w, 20)))
        text_h = self._lines * QFontMetrics(self.text_label.font()).lineSpacing()
        meta_h = max([meta_fm.height()] + [p.sizeHint().height() for p in self.pills])
        return max(_ROW_MIN_HEIGHT, 2 * _ROW_PAD_V + text_h + 4 + meta_h)


def _build_load_older_widget(on_click, parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    _make_transparent(w)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(12, 4, 12, 4)
    lay.addStretch()
    btn = QPushButton("Load older entries", w)
    btn.clicked.connect(on_click)
    lay.addWidget(btn)
    lay.addStretch()
    return w


def _build_empty_state_widget(
    message: str, action_label: str = "", on_action=None, parent: QWidget | None = None,
) -> QWidget:
    w = QWidget(parent)
    _make_transparent(w)
    lay = QVBoxLayout(w)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.setSpacing(12)
    lbl = _plain_label(message, "historyEmpty", w)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setWordWrap(True)
    lbl.setMinimumWidth(320)
    lay.addWidget(lbl)
    if action_label and on_action is not None:
        btn = QPushButton(action_label, w)
        btn.clicked.connect(on_action)
        lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
    w._message_label = lbl
    return w


class _DetailCard(QFrame):
    """The selected entry in full: text, where it went, the original words
    when a correction changed them, and the actions."""

    def __init__(self, on_copy, on_copy_original, on_delete, parent=None):
        super().__init__(parent)
        self.setObjectName("historyDetail")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        self.meta = _plain_label("", "historyDetailMeta", self)
        self.meta.setWordWrap(True)
        lay.addWidget(self.meta)

        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setFrameShape(QFrame.Shape.NoFrame)
        self.text.setAccessibleName("Entry text")
        self.text.document().setDocumentMargin(0)   # text lines up with the meta line
        lay.addWidget(self.text)

        self.heard_as = _plain_label("", "historyHeardAs", self)
        self.heard_as.setWordWrap(True)
        self.heard_as.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.heard_as)

        self.note = _plain_label("", "historyNote", self)
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.copy_button = QPushButton("Copy text", self)
        _set_class(self.copy_button, "primary")
        self.copy_button.clicked.connect(on_copy)
        actions.addWidget(self.copy_button)
        self.copy_original_button = QPushButton("Copy original words", self)
        self.copy_original_button.clicked.connect(on_copy_original)
        actions.addWidget(self.copy_original_button)
        actions.addStretch()
        self.delete_button = _danger_btn("Delete entry", self)
        self.delete_button.clicked.connect(on_delete)
        actions.addWidget(self.delete_button)
        lay.addLayout(actions)

    def toPlainText(self) -> str:
        return self.text.toPlainText()

    def fit_text(self) -> None:
        """Size the text box to its wrapped content, up to
        _DETAIL_TEXT_MAX_LINES lines (then it scrolls)."""
        try:
            doc = self.text.document()
            doc.setTextWidth(max(self.text.viewport().width(), 100))
            lines = max(int(doc.documentLayout().documentSize().height()), 1)
            fm = QFontMetrics(self.text.font())
            margins = self.text.contentsMargins()
            chrome = int(doc.documentMargin() * 2) + margins.top() + margins.bottom() + 2
            self.text.setFixedHeight(min(lines, _DETAIL_TEXT_MAX_LINES) * fm.lineSpacing() + chrome)
        except RuntimeError:
            pass   # card deleted before the deferred fit ran

    def show_row(self, row: dict) -> None:
        dt = _parse_ts(str(row.get('timestamp', '')))
        parts = []
        if dt is not None:
            parts.append(f"{day_label(dt)}, {dt.strftime('%H:%M')}")
        context = str(row.get('app_context') or '').strip()
        if context and context.casefold() != 'unknown':
            parts.append(context)
        mode_label = _MODE_LABELS.get(str(row.get('mode') or ''))
        if mode_label:
            parts.append(mode_label)
        try:
            seconds = int(row.get('duration_ms') or 0) / 1000.0
        except (TypeError, ValueError):
            seconds = 0
        if seconds >= 0.5:
            parts.append(f"{seconds:.0f} s" if seconds >= 10 else f"{seconds:.1f} s")
        self.meta.setText(" \u00b7 ".join(parts))

        body = copy_text(row) or list_text(row)
        self.text.setPlainText(body)
        self.fit_text()
        QTimer.singleShot(0, self.fit_text)   # again once the card has its real width

        corrected = is_corrected(row)
        self.heard_as.setVisible(corrected)
        self.copy_original_button.setVisible(corrected)
        if corrected:
            self.heard_as.setText("Heard as: " + " ".join(str(row.get('raw_text') or '').split()))

        outcome = row_outcome(row)
        note = ""
        if outcome is not None and outcome.kind == KIND_NOT_TYPED and str(row.get('entry_type')) == 'dictation':
            note = "This wasn't typed into the window. Copy it and paste it yourself."
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.copy_button.setVisible(bool(copy_text(row)))


def _size(height: int) -> QSize:
    """Item size hint with a fixed height. The width must be >= 0: the
    pre-79 QSize(-1, height) is an INVALID size, which QListWidgetItem
    silently discards -- every row fell back to the ~30 px default and the
    documented 44 px minimum never applied."""
    return QSize(0, height)


# ---------------------------------------------------------------------------
# HistoryView
# ---------------------------------------------------------------------------

class HistoryView(QWidget):
    """Day-grouped dictation history built for finding and copying things
    back out. Self-contained -- header, search, filters, list, detail card,
    footer.

    Args:
        store: a samsara.history_store.HistoryStore-shaped object
            (query/delete/clear) or None. No global/app-singleton lookup.
        legacy_history_fn: optional zero-arg callable returning the legacy
            in-memory (timestamp, text, is_command) tuple list. Used ONLY
            when `store` is None -- never merely because a query returned
            zero rows.
        legacy_clear_fn: optional zero-arg callable invoked (in addition to
            store.clear()) when the user confirms Clear history.
    """

    _rows_ready = Signal(list, bool, int)   # (rows, append, request_generation)

    def __init__(self, store, legacy_history_fn=None, legacy_clear_fn=None, parent=None):
        super().__init__(parent)
        self._store = store
        self._legacy_history_fn = legacy_history_fn
        self._legacy_clear_fn = legacy_clear_fn

        self._rows_by_item_id = {}   # QListWidgetItem id() -> row id
        self._oldest_loaded_id = None
        self._has_more = False
        self._empty_item = None
        self._load_older_item = None
        self._last_day = None
        self._request_generation = 0
        self._relayout_pending = False
        self._has_any_history = True
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._restore_status)
        self._settings = QSettings("Samsara", "Samsara")

        self.setObjectName("historyView")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(build_stylesheet())
        self._rows_ready.connect(self._on_rows_ready)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(10)

        # ---- Header -------------------------------------------------------
        self._title = _plain_label(TITLE, "historyTitle", self)
        root.addWidget(self._title)
        self._subtitle = _plain_label(SUBTITLE, "historySubtitle", self)
        self._subtitle.setWordWrap(True)
        root.addWidget(self._subtitle)

        # ---- Search + range ------------------------------------------------
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self._search = QLineEdit(self)
        self._search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search.setClearButtonEnabled(True)
        self._search.setAccessibleName("Search history")
        self._search.setMinimumWidth(160)
        pal = self._search.palette()
        pal.setColor(pal.ColorRole.PlaceholderText, theme.qcolor(theme.TEXT_SECONDARY))
        self._search.setPalette(pal)
        self._search.textChanged.connect(self._query_changed)
        search_row.addWidget(self._search, stretch=1)

        self._scope = QComboBox(self)
        self._scope.addItems([_SCOPE_LAST_7_DAYS, _SCOPE_ALL])
        saved_scope = self._settings.value(_SCOPE_SETTING, _SCOPE_LAST_7_DAYS, type=str)
        saved_scope = _SCOPE_ALIASES.get(saved_scope, saved_scope)
        if saved_scope in (_SCOPE_LAST_7_DAYS, _SCOPE_ALL):
            self._scope.setCurrentText(saved_scope)
        self._scope.setAccessibleName("Date range")
        self._scope.setToolTip("Which entries to list. Search always covers all history.")
        self._scope.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._scope.currentTextChanged.connect(self._scope_changed)
        search_row.addWidget(self._scope)
        root.addLayout(search_row)

        # ---- Filters (wrap, never clip) ------------------------------------
        filter_host = QWidget(self)
        _make_transparent(filter_host)
        flow = _FlowLayout(filter_host, h_spacing=16, v_spacing=6)
        self._filter = _FilterChips(_FILTERS, filter_host)
        self._filter.currentTextChanged.connect(lambda _: self._reload())
        flow.addWidget(self._filter)
        self._show_empty_wake = QCheckBox(NO_SPEECH_TOGGLE_LABEL, filter_host)
        self._show_empty_wake.setChecked(
            self._settings.value(_EMPTY_WAKE_SETTING, False, type=bool)
        )
        self._show_empty_wake.stateChanged.connect(self._empty_wake_changed)
        flow.addWidget(self._show_empty_wake)
        self._filter_host = filter_host
        root.addWidget(filter_host)

        # ---- List ---------------------------------------------------------
        self._list = QListWidget(self)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSpacing(1)
        self._list.itemActivated.connect(self._on_item_activated)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.currentItemChanged.connect(self._on_current_item_changed)
        self._list.setAccessibleName("History entries")
        # The viewport narrows when the scrollbar appears without the view
        # itself resizing -- re-wrap then too.
        self._list.viewport().installEventFilter(self)
        root.addWidget(self._list, stretch=1)

        # ---- Detail card (hidden until a row is selected) -----------------
        self._detail = _DetailCard(
            on_copy=self._copy_selected,
            on_copy_original=self._copy_selected_original,
            on_delete=self._delete_selected,
            parent=self,
        )
        self._detail.setVisible(False)
        root.addWidget(self._detail)

        # ---- Footer ---------------------------------------------------------
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self._status_lbl = _plain_label("", "historyStatus", self)
        self._status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer.addWidget(self._status_lbl, stretch=1)
        # Queue 122. The way out. 44 px explicitly, not this file's local
        # _CONTROL_H of 40: the brief asks for 44 and a button that hands
        # over everything you have ever dictated should not be the smallest
        # target on the page.
        self._export_btn = QPushButton(EXPORT_LABEL, self)
        self._export_btn.setAccessibleName(EXPORT_LABEL)
        _set_class(self._export_btn, "export")
        self._export_btn.setMinimumWidth(44)
        self._export_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._export_btn.setToolTip(
            "Save your history to a file you choose. Nothing is written until you pick a path.")
        self._export_btn.clicked.connect(self._export_history)
        footer.addWidget(self._export_btn)
        self._clear_btn = _danger_btn("Clear history\u2026", self)
        self._clear_btn.clicked.connect(self._clear_all)
        footer.addWidget(self._clear_btn)
        root.addLayout(footer)
        # Export sits before the destructive button in the tab order: the
        # thing you reach for by accident should not be the one that deletes.
        self.setTabOrder(self._export_btn, self._clear_btn)

        # ---- Keyboard ------------------------------------------------------
        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for keys, slot in (
            (QKeySequence.StandardKey.Find, self._focus_search),
            (QKeySequence.StandardKey.Refresh, self._reload),
            (QKeySequence.StandardKey.Copy, self._copy_selected_if_list_focused),
            (QKeySequence.StandardKey.Delete, self._delete_selected_if_list_focused),
        ):
            sc = QShortcut(keys if isinstance(keys, QKeySequence) else QKeySequence(keys), self)
            sc.setContext(ctx)
            sc.activated.connect(slot)

        self._reload()

    # ------------------------------------------------------------------
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._schedule_relayout()

    def eventFilter(self, obj, event):
        if event.type() == event.Type.Resize and obj is self._list.viewport():
            self._schedule_relayout()
        return super().eventFilter(obj, event)

    def showEvent(self, e):
        super().showEvent(e)
        self._schedule_relayout()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            if self._list.currentItem() is not None:
                self._list.setCurrentRow(-1)
                return
            if self._search.text():
                self._search.clear()
                return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self):
        """Re-fetch the current page (search/filter unchanged)."""
        self._reload()

    # ------------------------------------------------------------------
    # Data loading -- background thread, results marshaled back via Signal
    # ------------------------------------------------------------------

    def _current_query(self):
        return (
            self._search.text().strip(),
            self._filter.currentText(),
            self._scope.currentText(),
            self._show_empty_wake.isChecked(),
        )

    def _reload(self):
        """Fresh windowed load of the most recent page -- replaces the list."""
        self._request_generation += 1
        gen = self._request_generation
        query, filter_, scope, show_empty = self._current_query()
        thread_registry.spawn(
            "history-view-reload",
            lambda: self._fetch_and_emit(query, filter_, scope, show_empty, None, False, gen),
            daemon=True,
        )

    def _load_older(self):
        if self._store is None or self._oldest_loaded_id is None:
            return
        gen = self._request_generation   # continuing the current view, not a new search
        query, filter_, scope, show_empty = self._current_query()
        before_id = self._oldest_loaded_id
        thread_registry.spawn(
            "history-view-load-older",
            lambda: self._fetch_and_emit(query, filter_, scope, show_empty, before_id, True, gen),
            daemon=True,
        )

    def _fetch_and_emit(self, query, filter_, scope, show_empty_wake, before_id, append, gen):
        try:
            rows = self._fetch_rows(query, filter_, before_id, scope, show_empty_wake)
        except Exception as exc:
            logger.debug(f"[HISTORY] fetch failed: {exc}")
            rows = []
        self._rows_ready.emit(rows, append, gen)

    def _fetch_rows(self, query, filter_, before_id, scope=_SCOPE_LAST_7_DAYS,
                    show_empty_wake=False):
        filter_ = _FILTER_ALIASES.get(filter_, filter_)
        if self._store is not None:
            # Only "Dictation" maps onto a single entry_type column value;
            # the others need OR-across-columns logic, filtered client-side.
            type_filter = "dictation" if filter_ == FILTER_DICTATION else None
            # Search always covers all history; the range scopes browsing.
            start = None if query else _scope_start(scope)
            rows = self._store.query(
                search=query or None, type_filter=type_filter, limit=_PAGE_SIZE,
                before_id=before_id,
                since=(start.isoformat() if start else None),
            )
            rows = [dict(r) for r in rows]
            if not rows and before_id is None:
                # Tells "nothing in this range" apart from "nothing ever".
                self._has_any_history = bool(self._store.query(limit=1))
            if filter_ != FILTER_ALL:
                rows = [r for r in rows if _matches_type_filter(r, filter_)]
            if not show_empty_wake:
                rows = [r for r in rows if not is_no_speech_attempt(r)]
            return rows

        if before_id is not None:
            return []   # legacy fallback has no windowing/pagination support

        if self._legacy_history_fn is None:
            return []

        # Legacy fallback -- only when store itself is unavailable.
        q_lower = query.lower()
        rows = [
            {
                'id': None, 'timestamp': ts, 'entry_type': 'command' if is_cmd else 'dictation',
                'mode': '', 'display_text': text, 'raw_text': text, 'status': 'success',
            }
            for ts, text, is_cmd in reversed(self._legacy_history_fn())
            if not query or q_lower in text.lower()
        ]
        if filter_ != FILTER_ALL:
            rows = [r for r in rows if _matches_type_filter(r, filter_)]
        if not show_empty_wake:
            rows = [r for r in rows if not is_no_speech_attempt(r)]
        return rows

    def _query_changed(self, text):
        self._scope.setEnabled(not text.strip())
        self._reload()

    def _scope_changed(self, scope):
        self._settings.setValue(_SCOPE_SETTING, scope)
        self._reload()

    def _empty_wake_changed(self, state):
        self._settings.setValue(_EMPTY_WAKE_SETTING, bool(state))
        self._reload()

    @Slot(list, bool, int)
    def _on_rows_ready(self, rows, append, gen):
        if gen != self._request_generation:
            return  # stale result from a superseded search/filter change
        self._has_more = self._store is not None and len(rows) == _PAGE_SIZE
        self._render_rows(rows, append=append)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _row_width(self) -> int:
        return max(self._list.viewport().width() - 2, 200)

    def _render_rows(self, rows, append: bool):
        self._list.setUpdatesEnabled(False)
        try:
            if not append:
                self._list.clear()
                self._rows_by_item_id = {}
                self._last_day = None
                self._empty_item = None
                self._load_older_item = None
            else:
                if self._load_older_item is not None:
                    row_idx = self._list.row(self._load_older_item)
                    if row_idx >= 0:
                        self._list.takeItem(row_idx)
                    self._load_older_item = None

            width = self._row_width()
            last_day = self._last_day
            for row in rows:
                raw_ts = str(row['timestamp'])
                dt = _parse_ts(raw_ts)
                if dt is not None:
                    label = day_label(dt)
                    if label != last_day:
                        header_item = QListWidgetItem()
                        header_item.setFlags(Qt.ItemFlag.NoItemFlags)
                        header_item.setSizeHint(_size(_HEADER_HEIGHT))
                        self._list.addItem(header_item)
                        self._list.setItemWidget(
                            header_item, _build_day_header_widget(label, self._list.viewport())
                        )
                        last_day = label

                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, {
                    'id': row.get('id'), 'text': copy_text(row), 'timestamp': raw_ts,
                    'entry_type': str(row.get('entry_type', 'dictation')),
                    'status': str(row.get('status', 'success')),
                    'row': dict(row),
                })
                self._list.addItem(item)
                row_widget = _HistoryRow(row, self._copy_row_widget, self._list.viewport())
                self._list.setItemWidget(item, row_widget)
                # Polish after parenting: the view's stylesheet sets the
                # fonts the wrap/height maths must measure with.
                row_widget.polish_children()
                item.setSizeHint(_size(row_widget.relayout(width)))
                self._rows_by_item_id[id(item)] = row.get('id')

                if row.get('id') is not None:
                    self._oldest_loaded_id = row['id']

            self._last_day = last_day

            if rows and self._has_more:
                self._load_older_item = QListWidgetItem()
                self._load_older_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._load_older_item.setSizeHint(_size(_LOAD_OLDER_HEIGHT))
                self._list.addItem(self._load_older_item)
                self._list.setItemWidget(
                    self._load_older_item,
                    _build_load_older_widget(self._load_older, self._list.viewport()),
                )

            if self._list.count() == 0:
                query, filter_, scope, _show = self._current_query()
                available = self._store is not None or self._legacy_history_fn is not None
                message = empty_state_text(query, scope, filter_, available, self._has_any_history)
                offer_all_time = (not query and scope == _SCOPE_LAST_7_DAYS
                                  and self._store is not None and self._has_any_history)
                action = (("Show all time", lambda: self._scope.setCurrentText(_SCOPE_ALL))
                          if offer_all_time else ("", None))
                self._empty_item = QListWidgetItem()
                self._empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._empty_item.setSizeHint(_size(_EMPTY_HEIGHT))
                self._list.addItem(self._empty_item)
                self._list.setItemWidget(
                    self._empty_item,
                    _build_empty_state_widget(message, *action, parent=self._list.viewport()),
                )
        finally:
            self._list.setUpdatesEnabled(True)

        self._restore_status()

    def _schedule_relayout(self):
        if self._relayout_pending:
            return
        self._relayout_pending = True
        QTimer.singleShot(0, self._relayout_rows)

    def _relayout_rows(self):
        self._relayout_pending = False
        width = self._row_width()
        for i in range(self._list.count()):
            item = self._list.item(i)
            widget = self._list.itemWidget(item)
            if isinstance(widget, _HistoryRow):
                height = widget.relayout(width)
                if item.sizeHint().height() != height:
                    item.setSizeHint(_size(height))

    # Kept for callers written against the pre-79 name.
    _re_elide_visible_rows = _relayout_rows

    # ------------------------------------------------------------------
    # Row lookup helpers
    # ------------------------------------------------------------------

    def _row_data_for_item(self, item):
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _current_row_data(self):
        return self._row_data_for_item(self._list.currentItem())

    def _item_for_row_widget(self, widget):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if self._list.itemWidget(item) is widget:
                return item
        return None

    # ------------------------------------------------------------------
    # Detail card
    # ------------------------------------------------------------------

    def _on_current_item_changed(self, current, _previous):
        data = self._row_data_for_item(current)
        if data is None:
            self._detail.setVisible(False)
            self._detail.text.clear()
            return
        self._detail.show_row(data.get('row') or {
            'display_text': data.get('text', ''), 'raw_text': data.get('text', ''),
            'timestamp': data.get('timestamp', ''), 'entry_type': data.get('entry_type'),
            'status': data.get('status'),
        })
        self._detail.setVisible(True)
        self._list.scrollToItem(current)

    # ------------------------------------------------------------------
    # Copy / actions
    # ------------------------------------------------------------------

    def _set_clipboard(self, text: str, message: str = "Copied to clipboard") -> bool:
        if not text:
            return False
        QApplication.clipboard().setText(text)
        self._show_toast(message)
        return True

    def _copy_row_item(self, item):
        data = self._row_data_for_item(item)
        if data:
            self._set_clipboard(data.get('text', ''))

    def _copy_row_widget(self, widget):
        if self._set_clipboard(copy_text(widget.row)):
            widget.copy_button.setText("Copied")
            QTimer.singleShot(_TOAST_MS, lambda: _safe_set_text(widget.copy_button, "Copy"))

    def _on_item_activated(self, item):
        self._copy_row_item(item)

    def _on_context_menu(self, pos):
        item = self._list.itemAt(pos)
        data = self._row_data_for_item(item)
        if data is None:
            return
        self._list.setCurrentItem(item)
        row = data.get('row') or {}
        menu = QMenu(self)
        copy_act = menu.addAction("Copy text")
        copy_act.setEnabled(bool(data.get('text')))
        original_act = menu.addAction("Copy original words") if is_corrected(row) else None
        menu.addSeparator()
        del_act = menu.addAction("Delete entry")
        action = menu.exec(self._list.viewport().mapToGlobal(pos))
        if action == copy_act:
            self._copy_row_item(item)
        elif original_act is not None and action == original_act:
            self._copy_selected_original()
        elif action == del_act:
            self._delete_item(item)

    def _copy_selected(self):
        data = self._current_row_data()
        if not data or not data.get('text'):
            self._show_toast("Select an entry to copy", success=False)
            return
        self._set_clipboard(data['text'])

    def _copy_selected_original(self):
        data = self._current_row_data()
        row = (data or {}).get('row') or {}
        self._set_clipboard(str(row.get('raw_text') or ''), "Original words copied")

    def _copy_selected_if_list_focused(self):
        if self._list.hasFocus():
            self._copy_selected()

    def _delete_selected_if_list_focused(self):
        if self._list.hasFocus():
            self._delete_selected()

    def _focus_search(self):
        self._search.setFocus()
        self._search.selectAll()

    def _delete_item(self, item):
        data = self._row_data_for_item(item)
        if data is None:
            return
        row_id = data.get('id')
        if row_id is not None and self._store is not None:
            self._store.delete([row_id])
        row_idx = self._list.row(item)
        if row_idx >= 0:
            self._list.takeItem(row_idx)
        self._rows_by_item_id.pop(id(item), None)
        self._restore_status()

    def _delete_selected(self):
        item = self._list.currentItem()
        data = self._row_data_for_item(item)
        if data is None:
            self._show_toast("Select an entry to delete", success=False)
            return
        reply = QMessageBox.question(
            self, "Delete entry", "Delete this history entry?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._delete_item(item)

    # ------------------------------------------------------------------
    # Export (queue 122)
    # ------------------------------------------------------------------

    def export_rows(self, scope_all: bool = False) -> list:
        """The rows an export would write.

        `scope_all` ignores the toolbar and takes everything. Otherwise this
        exports EXACTLY WHAT THE VIEW IS SHOWING, by calling the view's own
        _fetch_rows with its own current query and paging through it with
        the same before_id cursor the list uses -- not a second query that
        could drift from the first. The search box, the type filter, the date
        scope and the "show empty wake attempts" checkbox all apply, because
        they applied to what the user was looking at when they pressed the
        button.
        """
        if self._store is None:
            return []
        if scope_all:
            return history_export.collect_all(self._store)
        query, filter_, scope, show_empty = self._current_query()
        rows, before_id, guard = [], None, 0
        while guard < 2000:
            guard += 1
            page = self._fetch_rows(query, filter_, before_id, scope, show_empty)
            if not page:
                break
            rows.extend(page)
            ids = [r.get("id") for r in page if r.get("id") is not None]
            if not ids:
                break
            nxt = min(ids)
            if before_id is not None and nxt >= before_id:
                break
            before_id = nxt
        return rows

    def _export_scope_label(self, scope_all: bool) -> str:
        if scope_all:
            return "all history"
        query, filter_, scope, show_empty = self._current_query()
        bits = [scope.lower()]
        if filter_ and filter_ != FILTER_ALL:
            bits.append(f"filter: {filter_.lower()}")
        if query:
            bits.append(f'search: "{query}"')
        if show_empty:
            bits.append("including empty wake attempts")
        return ", ".join(bits)

    def _export_history(self):
        """Ask where, then write. Nothing touches the disk before the user
        names a file -- the dialog's Cancel writes nothing at all."""
        scope_all = bool(QApplication.keyboardModifiers()
                         & Qt.KeyboardModifier.ShiftModifier)
        rows = self.export_rows(scope_all=scope_all)
        start_dir = history_export.default_export_dir()
        try:
            start_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.debug(f"[HISTORY] could not prepare the export folder: {exc}")
        suggested = str(start_dir / history_export.default_filename("json"))
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export history", suggested,
            "JSON, every field (*.json);;Markdown, readable (*.md);;Text, readable (*.txt)")
        if not path:
            return                       # cancelled: nothing written
        fmt = ("json" if "json" in (chosen or "").lower()
               else "markdown" if ".md" in (chosen or "").lower()
               else "text")
        if Path(path).suffix:
            fmt = history_export.format_for_path(path)
        try:
            written = history_export.export(
                rows, path, fmt, scope=self._export_scope_label(scope_all))
        except Exception as exc:
            logger.warning(f"[HISTORY] export failed: {exc}")
            self._show_toast(f"Export failed: {exc}", success=False)
            return
        n = len(rows)
        self._show_toast(f"Exported {n} entr{'y' if n == 1 else 'ies'} to {written.name}")

    def _clear_all(self):
        reply = QMessageBox.question(
            self, "Clear history",
            "Delete every history entry? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if self._store is not None:
            self._store.clear()
        if self._legacy_clear_fn is not None:
            try:
                self._legacy_clear_fn()
            except Exception as exc:
                logger.debug(f"[HISTORY] legacy_clear_fn failed: {exc}")

        self._reload()

    # ------------------------------------------------------------------
    def _set_status(self, text: str, toast: bool):
        self._status_lbl.setText(text)
        if bool(self._status_lbl.property("toast")) != toast:
            self._status_lbl.setProperty("toast", toast)
            self._status_lbl.style().unpolish(self._status_lbl)
            self._status_lbl.style().polish(self._status_lbl)

    def _show_toast(self, msg: str, success: bool = True):
        """Transient confirmation in the footer -- no popup."""
        self._set_status(msg, toast=success)
        self._toast_timer.start(_TOAST_MS)

    def _restore_status(self):
        query, _filter, scope, _show = self._current_query()
        n = len(self._rows_by_item_id)
        self._set_status(summary_text(n, scope, query, self._has_more), toast=False)


def _safe_set_text(button: QPushButton, text: str) -> None:
    try:
        button.setText(text)
    except RuntimeError:
        pass   # the row was rebuilt by a reload before the timer fired
