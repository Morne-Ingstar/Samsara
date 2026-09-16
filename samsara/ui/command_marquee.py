"""The status strip's command example (queue 89, replaced in 111).

ONE complete, stationary command phrase in the blank right-hand end of the
hub's bottom status strip, with a left and a right arrow either side to step
to the previous or next example.

Why it stopped scrolling (111, Astra's second Home review, owner agreed).
The marquee was the visually weakest thing on the page, and partially-visible
moving text is the wrong default for the person this app is for: someone who
needs to read an example long enough to say it out loud cannot do that while
it slides past. A scrolling line also has no "previous" -- miss a phrase and
it is gone for a full cycle. Stepping puts the pace in the user's hands and
costs nothing: the same catalog, the same filters, the same pool.

The file keeps its name because the module path is what other files import;
the class is `CommandExampleStrip`. Renaming the file is a follow-up.

The rules it keeps, unchanged from 89:

  * Source of truth is the catalog (commands_catalog.json via
    command_catalog.guidance_catalog), never a hand-written list. If the
    catalog cannot be read the strip shows nothing at all.
  * Only safe phrases: nothing destructive, nothing needing an argument,
    no whole-utterance control word, nothing from a hardware pack, and
    nothing scoped to an app that is not live right now (queue 68 scopes).
  * The strip never takes focus and never announces a new sentence: the
    example label is NoFocus and its accessible name is updated in place,
    which a screen reader does not speak. Only the two arrows are in the
    tab chain, and their names never change.

The rules that changed:

  * NO reduced-motion branch. Nothing moves, so there is nothing to
    suppress; the branch was deleted rather than left dead.
  * NEVER ELIDED. An example is shown whole or not at all. When the strip is
    too narrow for the current phrase, `_step` walks on in the direction the
    user asked until it finds one that fits -- see `fits` and `pick_fitting`.
"""
from __future__ import annotations

import random
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from samsara import command_catalog, command_scope
from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)

#: The fixed accessible name of the strip as a whole. It never changes.
ACCESSIBLE_NAME = "Command examples"
#: The lead-in, then the phrase in double quotes -- the same convention Home
#: uses for anything spoken (home_qt.quoted).
PREFIX = "Try saying"
#: The two arrows. Visible glyph and accessible name are separate here (a
#: glyph is not a name), which is why each carries an explicit one.
PREV_GLYPH = chr(0x2039)          # single left-pointing angle quote
NEXT_GLYPH = chr(0x203A)          # single right-pointing angle quote
PREV_NAME = "previous example"
NEXT_NAME = "next example"
#: Minimum hit target, the same 44 px floor home_qt._button uses.
MIN_TARGET = 44
#: How many phrases one randomised pass carries. Enough that the pool is not
#: obviously short, few enough that stepping comes round in reasonable time.
PHRASE_COUNT = 12
#: Risk classes safe to show: reading the screen or moving the UI. Never
#: "write", never "destructive".
SAFE_RISKS = frozenset({"read", "ui"})
#: Catalog PACKS whose commands drive physical hardware, excluded whatever
#: their risk class says (queue 95).
#:
#: An example is an invitation to try the phrase right now, and the risk
#: classes only describe what the command does to the SCREEN. "print me a
#: gun" is a real FlashForge command, honestly classed "ui" -- it needs no
#: argument, destroys nothing on the machine, and so passed every filter
#: here and greeted first-time users. What it actually does is start a
#: physical machine heating and moving in the owner's room, which is not
#: something to suggest to somebody who has just opened the app.
#:
#: Excluded by PACK, not by plugin name or phrase: the pack is the catalog's
#: own grouping, so a second printer or a new smart-home plugin is excluded
#: the day it lands without anybody remembering to edit this file. Software
#: that merely plays or pauses what is already on the user's own screen
#: (the "media" and "stremio" packs) is not hardware and stays in.
HARDWARE_PACKS = frozenset({"3d-printing", "smart-home"})


def _live(record: dict, ctx) -> bool:
    """True when this record's queue-68 scope is live right now. A record
    with no scope is global and always live. A malformed scope is treated
    as not live -- the strip must never advertise a phrase that will not
    match."""
    try:
        scope = command_scope.parse_scope(record.get("scope"))
    except ValueError:
        return False
    live, _why = command_scope.scope_live(scope, ctx)
    return live


def example_phrases(records, *, ctx=None, count: int = PHRASE_COUNT,
                    rng: Optional[random.Random] = None) -> list:
    """`count` spoken phrases from catalog `records`, in random order.

    Safe only: risk in SAFE_RISKS, not a whole utterance, no required
    argument, not a demo/sample plugin or a network verb (the same
    exclusions command_catalog.pick_examples applies to "try saying"
    examples), not from a hardware pack (queue 95, see HARDWARE_PACKS),
    and live for `ctx` (queue 68).

    Unchanged from the marquee it replaces: 111 changed how one example is
    SHOWN, and deliberately did not widen the pool it is drawn from.
    """
    rng = rng or random.Random()
    phrases = []
    for record in records or []:
        if record.get("risk") not in SAFE_RISKS:
            continue
        if record.get("pack") in HARDWARE_PACKS:
            continue
        if record.get("whole_utterance"):
            continue
        if record.get("plugin") in command_catalog.EXAMPLE_EXCLUDED_PLUGINS:
            continue
        if record.get("verb") in command_catalog.EXAMPLE_EXCLUDED_VERBS:
            continue
        if any(a.get("required") for a in record.get("args", []) if isinstance(a, dict)):
            continue
        if not _live(record, ctx):
            continue
        try:
            phrase = command_catalog.canonical_phrase(record)
        except (KeyError, IndexError):
            continue
        if phrase:
            phrases.append(phrase)
    # Sorted first, so the shuffle is the ONLY source of order and a seeded
    # rng gives the same pool every time in a test.
    phrases = sorted(set(phrases))
    rng.shuffle(phrases)
    return phrases[:count]


def example_text(phrase: str) -> str:
    """One complete example: 'Try saying "close this window"'."""
    return f'{PREFIX} "{phrase}"' if phrase else ""


def fits(metrics: QFontMetrics, phrase: str, width: int) -> bool:
    """True when the whole example fits `width` with nothing cut off.

    The measurement is the rendered advance of the FULL string, so a phrase
    is judged by exactly what would be painted. There is no tolerance and no
    ellipsis: an example is complete or it is not shown.
    """
    if not phrase:
        return False
    if width <= 0:
        return False
    return metrics.horizontalAdvance(example_text(phrase)) <= width


def pick_fitting(metrics: QFontMetrics, phrases, width: int, start: int,
                 step: int = 1) -> Optional[int]:
    """The index of the first phrase that FITS, from `start`, walking `step`.

    This is the whole answer to "never clipped". Stepping moves in the
    direction the user asked and keeps going past anything too long for the
    strip as it is right now, wrapping once around the pool. None means no
    phrase in the pool fits at this width -- the caller then shows nothing,
    because half a command is worse than no command: a user cannot say it,
    and cannot tell that it is incomplete.
    """
    if not phrases:
        return None
    total = len(phrases)
    for offset in range(total):
        index = (start + offset * step) % total
        if fits(metrics, phrases[index], width):
            return index
    return None


class CommandExampleStrip(QWidget):
    """One stationary example between a previous and a next arrow.

    `rng` exists so a test can pin the pool; in the app it is real
    randomness.
    """

    def __init__(self, records=None, parent=None, *,
                 rng: Optional[random.Random] = None, ctx=None,
                 background: Optional[str] = None):
        super().__init__(parent)
        self.setObjectName("commandExampleStrip")
        self.setAccessibleName(ACCESSIBLE_NAME)
        # The strip itself is never a tab stop; only its two arrows are.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._background = background or theme.BG1
        self._phrases = example_phrases(records, ctx=ctx, rng=rng)
        self._index = 0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._prev_btn = self._arrow(PREV_GLYPH, PREV_NAME)
        self._prev_btn.clicked.connect(self.previous)
        lay.addWidget(self._prev_btn)

        self._label = QLabel("")
        # Ambient text, not a control: it is not in the tab chain, and its
        # accessible name is updated rather than announced.
        self._label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        font = self._label.font()
        # The same px token the status segments beside it use, so the example
        # reads as part of the strip rather than a second type scale.
        font.setPixelSize(theme.TYPE_BODY)
        self._label.setFont(font)
        self._label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; background: transparent;")
        self._label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._label, 1)

        self._next_btn = self._arrow(NEXT_GLYPH, NEXT_NAME)
        self._next_btn.clicked.connect(self.next)
        lay.addWidget(self._next_btn)

        self._metrics = QFontMetrics(self._label.font())
        self.setMinimumHeight(max(MIN_TARGET, self._metrics.height()))
        # The status bar gives it whatever is left; it never demands the
        # width an example would need, because when the width is not there
        # the answer is a shorter example, not a wider strip.
        self.setMinimumWidth(2 * MIN_TARGET + 12)
        self._arrows_visible(bool(self._phrases))
        self._apply(self._index)

    # ---- Construction ----------------------------------------------------

    def _arrow(self, glyph: str, name: str) -> QPushButton:
        """A 44 x 44 arrow whose ACCESSIBLE NAME is words, not a glyph."""
        btn = QPushButton(glyph)
        btn.setAccessibleName(name)
        btn.setToolTip(name.capitalize())
        btn.setFixedSize(MIN_TARGET, MIN_TARGET)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.TEXT_SECONDARY};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px;"
            f" font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ border-color: {theme.ACCENT};"
            f" color: {theme.TEXT_PRIMARY}; }}"
            f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
        )
        return btn

    def _arrows_visible(self, visible: bool) -> None:
        """With fewer than two examples there is nothing to step to, and an
        arrow that cannot do anything is a lie about what is available."""
        show = visible and len(self._phrases) > 1
        self._prev_btn.setVisible(show)
        self._next_btn.setVisible(show)

    # ---- State -----------------------------------------------------------

    @property
    def phrases(self) -> list:
        return list(self._phrases)

    @property
    def index(self) -> int:
        return self._index

    @property
    def phrase(self) -> str:
        """The phrase currently shown, or "" when none fits or none exist."""
        if not self._label.text() or not self._phrases:
            return ""
        return self._phrases[self._index]

    @property
    def text(self) -> str:
        """Exactly what is painted. Never an elided fragment."""
        return self._label.text()

    @property
    def prev_button(self) -> QPushButton:
        return self._prev_btn

    @property
    def next_button(self) -> QPushButton:
        return self._next_btn

    @property
    def label(self) -> QLabel:
        return self._label

    # ---- Stepping --------------------------------------------------------

    def _available(self) -> int:
        """The width the label actually has for text."""
        width = self._label.width()
        if width > 0:
            return width
        # Before the first layout pass the label has no width yet; derive it
        # from the strip so the first example is chosen against a real
        # budget rather than shown and then swapped.
        spacing = 6 * 2
        arrows = 2 * MIN_TARGET if len(self._phrases) > 1 else 0
        return max(0, self.width() - arrows - spacing)

    def _apply(self, index: int, step: int = 1) -> None:
        """Show the phrase at `index`, or the first one from there that fits.

        Setting the label's text does not announce anything; the accessible
        name is updated in place so a screen-reader user who navigates to the
        strip reads the current example, and one who does not is never
        interrupted.
        """
        if not self._phrases:
            self._label.setText("")
            self._label.setAccessibleName(ACCESSIBLE_NAME)
            return
        found = pick_fitting(self._metrics, self._phrases, self._available(),
                             index, step)
        if found is None:
            # Nothing in the pool fits this width. Show nothing rather than
            # a fragment: a clipped command cannot be spoken.
            self._index = index % len(self._phrases)
            self._label.setText("")
            self._label.setAccessibleName(ACCESSIBLE_NAME)
            return
        self._index = found
        text = example_text(self._phrases[found])
        self._label.setText(text)
        self._label.setAccessibleName(text)

    def next(self) -> None:
        """Step forward to the next example that fits."""
        if not self._phrases:
            return
        self._apply(self._index + 1, step=1)

    def previous(self) -> None:
        """Step back to the previous example that fits."""
        if not self._phrases:
            return
        self._apply(self._index - 1, step=-1)

    # ---- Layout ----------------------------------------------------------

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The strip got narrower or wider. Re-pick FORWARD from where we are,
        # which keeps the current example when it still fits and otherwise
        # moves on rather than clipping it.
        self._apply(self._index, step=1)


def build_example_strip(app=None, parent=None, **kw) -> CommandExampleStrip:
    """A strip fed from the live registry when the app offers one, else from
    commands_catalog.json. Never raises: a catalog that cannot be read yields
    an empty (textless, arrowless) strip."""
    records = None
    try:
        rows = None
        registry = getattr(app, "command_registry", None)
        if registry is not None and hasattr(registry, "rows"):
            rows = registry.rows()
        records = command_catalog.guidance_catalog(rows)
    except Exception as exc:
        logger.debug(f"build_example_strip: catalog unavailable: {exc}")
    ctx = kw.pop("ctx", None)
    return CommandExampleStrip(records, parent, ctx=ctx, **kw)
