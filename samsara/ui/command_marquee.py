"""The status strip's command example (queue 89, replaced in 111).

ONE complete, stationary command phrase in the blank right-hand end of the
hub's bottom status strip, with one quiet drawn chevron to step forward.
Left / Right on that control step backwards / forwards; the phrase clicks too.

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
  * Only safe, universal phrases: nothing destructive, nothing needing an
    argument, no whole-utterance control word, and nothing outside the
    catalog's universal pack allow-list.
  * The strip never takes focus and never announces a new sentence: the
    example label is NoFocus and its accessible name is updated in place,
    which a screen reader does not speak. Only the next control is in the
    tab chain in S1, and its name never changes. S2 focuses the phrase instead.

The rules that changed:

  * NO reduced-motion branch. Nothing moves, so there is nothing to
    suppress; the branch was deleted rather than left dead.
  * NEVER ELIDED. An example is shown whole or not at all. When the strip is
    too narrow for the current phrase, `_step` walks on in the direction the
    user asked until it finds one that fits -- see `fits` and `pick_fitting`.
"""
from __future__ import annotations

import random
import re
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QFontMetrics, QPainter, QPen
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
NEXT_NAME = "next example"
#: Minimum hit target, the same 44 px floor home_qt._button uses.
MIN_TARGET = 44
#: How many phrases one randomised pass carries. Enough that the pool is not
#: obviously short, few enough that stepping comes round in reasonable time.
PHRASE_COUNT = 12
# The command is registered by core_utils and works without optional packs.
# Frozen builds can paint the strip before their generated catalog is loaded;
# a real universal command is more useful than a blank status strip there.
FRESH_INSTALL_FALLBACK = "what can I say"
#: Risk classes safe to show: reading the screen or moving the UI. Never
#: "write", never "destructive".
SAFE_RISKS = frozenset({"read", "ui"})
#: Catalog packs whose examples are useful on a first screen regardless of
#: optional integrations.  This is deliberately an allow-list: a new plugin,
#: app integration, or hardware pack is excluded until its pack has been
#: judged suitable for an invitation to a new user.
#:
#: Pack names are catalog data, not inferred from plugin filenames.  The
#: groups cover Samsara controls (core/session/window-cube-numbers), dictation
#: and text work (core/text-editing), windows (window-management), system and
#: audio controls (utilities/accessibility/audio), and navigation/click
#: controls (browsers/mouse).
EXAMPLE_ALLOWED_PACKS = frozenset({
    "accessibility",
    "audio",
    "browsers",
    "core",
    "mouse",
    "session",
    "text-editing",
    "utilities",
    "window-cube-numbers",
    "window-management",
})


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
    examples), from EXAMPLE_ALLOWED_PACKS only, and live for `ctx` (queue
    68).

    Unchanged from the marquee it replaces: 111 changed how one example is
    SHOWN, and deliberately did not widen the pool it is drawn from.
    """
    rng = rng or random.Random()
    phrases = []
    for record in records or []:
        if record.get("risk") not in SAFE_RISKS:
            continue
        if record.get("pack") not in EXAMPLE_ALLOWED_PACKS:
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
    phrase = re.sub(r"\bi\b", "I", str(phrase or "").strip())
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


class _NextExampleButton(QPushButton):
    """A drawn chevron, with a transparent 44 px keyboard/click target."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = theme.ACCENT if self.underMouse() or self.hasFocus() else theme.TEXT_SECONDARY
        pen = QPen(theme.qcolor(ink), 1.75)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        x, y = self.width() / 2, self.height() / 2
        painter.drawPolyline([QPointF(x-2, y-4), QPointF(x+2, y), QPointF(x-2, y+4)])
        if self.hasFocus():
            painter.drawRoundedRect(self.rect().adjusted(3, 3, -3, -3), 4, 4)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()


class CommandExampleStrip(QWidget):
    """One stationary complete example at the right end of the bar.

    `rng` exists so a test can pin the pool; in the app it is real
    randomness.
    """

    def __init__(self, records=None, parent=None, *,
                 rng: Optional[random.Random] = None, ctx=None,
                 background: Optional[str] = None, fallback_phrase: Optional[str] = None,
                 show_control: bool = True):
        super().__init__(parent)
        self.setObjectName("commandExampleStrip")
        self.setAccessibleName(ACCESSIBLE_NAME)
        # S1 has one tab stop; S2 makes the clickable phrase keyboard reachable.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._background = background or theme.BG1
        self._phrases = example_phrases(records, ctx=ctx, rng=rng)
        if not self._phrases and fallback_phrase:
            self._phrases = [fallback_phrase]
        self._index = 0
        self._show_control = show_control
        self._applying = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self._label = QLabel("")
        # Ambient text, not a control: it is not in the tab chain, and its
        # accessible name is updated rather than announced.
        self._label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._label.setFocusPolicy(Qt.FocusPolicy.NoFocus if show_control else Qt.FocusPolicy.StrongFocus)
        font = self._label.font()
        # The same px token the status segments beside it use, so the example
        # reads as part of the strip rather than a second type scale.
        font.setPixelSize(theme.TYPE_BODY)
        self._label.setFont(font)
        self._label.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT_SECONDARY}; background: transparent; padding: 0; border: none; }}"
            f"QLabel:focus {{ border: 1px solid {theme.ACCENT}; }}")
        self._label.setMinimumWidth(0)
        self._label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._label, 1)

        self._next_btn = _NextExampleButton()
        self._next_btn.setAccessibleName(NEXT_NAME)
        self._next_btn.setToolTip("Next example; Left / Right to step")
        self._next_btn.setFixedSize(MIN_TARGET, MIN_TARGET)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus if show_control else Qt.FocusPolicy.NoFocus)
        self._next_btn.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; }")
        policy = self._next_btn.sizePolicy()
        policy.setRetainSizeWhenHidden(show_control)
        self._next_btn.setSizePolicy(policy)
        self._next_btn.clicked.connect(self.next)
        lay.addWidget(self._next_btn)
        self._next_btn.setVisible(show_control and len(self._phrases) > 1)
        self._label.installEventFilter(self)
        self._next_btn.installEventFilter(self)
        self._label.ensurePolished()
        self.setMinimumHeight(max(MIN_TARGET, self._metrics.height()))
        # The status bar gives it whatever is left; it never demands the
        # width an example would need, because when the width is not there
        # the answer is a shorter example, not a wider strip.
        self.setMinimumWidth(0)
        self._apply(self._index)

    # ---- Construction ----------------------------------------------------

    @property
    def _metrics(self) -> QFontMetrics:
        """Measure the polished, current font, including later font/QSS changes."""
        return QFontMetrics(self._label.font())

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
    def next_button(self) -> QPushButton:
        return self._next_btn

    @property
    def label(self) -> QLabel:
        return self._label

    # ---- Stepping --------------------------------------------------------

    def _available(self) -> int:
        """The width the label actually has for text."""
        # Activate before measuring: neither an outer width (which includes
        # QSS padding) nor yesterday's layout geometry is a text budget.
        self.layout().activate()
        return max(0, self._label.contentsRect().width())

    def _apply(self, index: int, step: int = 1) -> None:
        """Show the phrase at `index`, or the first one from there that fits.

        Setting the label's text does not announce anything; the accessible
        name is updated in place so a screen-reader user who navigates to the
        strip reads the current example, and one who does not is never
        interrupted.
        """
        if self._applying:
            return
        self._applying = True
        try:
            found = pick_fitting(self._metrics, self._phrases, self._available(), index, step)
            if found is None:
                self._index = index % len(self._phrases) if self._phrases else 0
                text = ""
            else:
                self._index = found
                text = example_text(self._phrases[found])
            self._label.setText(text)
            self._label.setAccessibleName(text or ACCESSIBLE_NAME)
            self._next_btn.setVisible(bool(text) and self._show_control and len(self._phrases) > 1)
        finally:
            self._applying = False

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

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Right:
                self.next()
                return True
            if event.key() == Qt.Key.Key_Left:
                self.previous()
                return True
        if watched is self._label:
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self.next()
                return True
            if event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange,
                                QEvent.Type.StyleChange, QEvent.Type.ContentsRectChange):
                self._apply(self._index)
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._apply(self._index)

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
    return CommandExampleStrip(
        records, parent, ctx=ctx,
        fallback_phrase=FRESH_INSTALL_FALLBACK if not records else None,
        **kw,
    )
