"""Voice help: the hub page a user reaches when speech is not working
(queue 86).

It answers, in this order (queue 82):
  1. WHAT IS AFFECTED   every capability, with paused and off distinguished
                        from a fault, and unknown never dressed up as health;
  2. WHAT THE EVIDENCE IS   each line cites what was observed, so "no recent
                        wake detected" reads as evidence, not a diagnosis;
  3. WHAT TO DO NEXT    a guided check whose four stages are separated
                        (receiving audio, detecting the wake phrase,
                        recognising speech, delivering feedback), results
                        appearing as each finishes, plus the one pause/resume
                        control -- shown only when it can actually act.

It is in the nav on every screen, healthy or not: a user must be able to say
"voice is not working" when the app has detected no fault.

The page reads samsara.ui.home_signals, the same account of capability state
Home's notice reads, so the two cannot disagree.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from samsara.log import get_logger
from samsara.ui import home_signals, theme
from samsara.ui.home_qt import (
    GRID, GRID_LG, CONTENT_MAX_W, _button, _card, _label, _label_css, _px, _section_label,
    listening_state_id, pause_hands_free, resume_hands_free,
)

logger = get_logger(__name__)

TITLE = "Voice help"
SUBTITLE = "What is working, what the app actually observed, and what to try next."
AFFECTED = "What is affected"
NEXT_STEPS = "What to do next"
RUN_CHECK = "Run the check"
CHECK_RUNNING = "Checking..."
PAUSE = "Pause listening"
RESUME = "Resume listening"
REPORT_HINT = ("Nothing here is a diagnosis on its own. If speech is not working and every line "
               "below looks fine, that is worth reporting.")

#: Status -> the word the page uses, so one vocabulary covers every surface.
STATUS_WORDS = {
    home_signals.READY: "working",
    home_signals.PAUSED: "paused by you",
    home_signals.OFF: "switched off",
    home_signals.UNAVAILABLE: "not working",
    home_signals.UNKNOWN: "not checked",
}


def status_colours():
    return {
        home_signals.READY: theme.SUCCESS,
        home_signals.PAUSED: theme.TEXT_SECONDARY,
        home_signals.OFF: theme.TEXT_SECONDARY,
        home_signals.UNAVAILABLE: theme.ERROR,
        home_signals.UNKNOWN: theme.WARNING,
    }
#: The guided check, one stage per thing that can independently fail. Each
#: returns (status, sentence) and never blocks: every stage reads what the
#: app already recorded rather than opening the microphone itself.
CHECK_STAGES = (
    ("audio", "Receiving audio"),
    ("wake", "Detecting the wake phrase"),
    ("speech", "Recognising speech"),
    ("feedback", "Delivering feedback"),
)
#: How long between stages, so results appear one at a time instead of all at
#: once (queue 82: "show results as checks finish").
STAGE_DELAY_MS = 220


def check_stage(app, key: str) -> tuple:
    """(status, evidence sentence) for one stage of the guided check."""
    states = home_signals.capability_states(app)
    if key == "audio":
        state = states[home_signals.DICTATION]
        if state.status == home_signals.UNAVAILABLE:
            return home_signals.UNAVAILABLE, "The selected microphone is not in the device list."
        if state.status == home_signals.UNKNOWN:
            return home_signals.UNKNOWN, "The app has not listed microphones yet."
        if state.status == home_signals.PAUSED:
            return home_signals.PAUSED, "You paused listening, so nothing is being captured."
        empty, examined = home_signals.no_text_captures(app)
        if examined and empty >= home_signals.NO_TEXT_MIN:
            return home_signals.UNAVAILABLE, (
                f"{empty} of the last {examined} recordings produced no text.")
        if not examined:
            return home_signals.UNKNOWN, "Nothing has been recorded yet this session."
        return home_signals.READY, f"The last {examined} recordings reached the recogniser."
    if key == "wake":
        state = states[home_signals.HANDS_FREE]
        if state.status == home_signals.OFF:
            return home_signals.OFF, "The wake word is switched off in Settings."
        if state.status in (home_signals.UNAVAILABLE, home_signals.PAUSED, home_signals.UNKNOWN):
            return state.status, state.detail
        heard = _wake_records(app)
        if not heard:
            # Evidence, NOT a diagnosis: a user who has not spoken the wake
            # phrase since the app started looks exactly like a broken one.
            return home_signals.UNKNOWN, "No wake phrase has been detected since the app started."
        return home_signals.READY, f"{heard} wake captures since the app started."
    if key == "speech":
        texts = _records_with_text(app)
        if texts:
            return home_signals.READY, f"{texts} recent recordings produced text."
        empty, examined = home_signals.no_text_captures(app)
        if examined:
            return home_signals.UNAVAILABLE, f"None of the last {examined} recordings produced text."
        return home_signals.UNKNOWN, "Nothing has been recorded yet this session."
    if key == "feedback":
        coordinator = getattr(app, "audio_coordinator", None)
        if coordinator is None:
            return home_signals.UNKNOWN, "The audio coordinator is not running."
        ava = home_signals.capability_states(app)[home_signals.AVA]
        if ava.status == home_signals.UNAVAILABLE:
            return home_signals.UNKNOWN, (
                "Earcons play locally; spoken replies need Ava, which is offline.")
        return home_signals.READY, "Earcons and spoken replies have a working output path."
    return home_signals.UNKNOWN, "No check for this stage."


def _diag(app, limit=home_signals.NO_TEXT_WINDOW):
    try:
        from samsara import diagnostics                 # noqa: PLC0415
        return list(diagnostics.recent(limit))
    except Exception:
        return []


def _wake_records(app) -> int:
    return sum(1 for r in _diag(app) if getattr(r, "mode", "") == "wake")


def _records_with_text(app) -> int:
    return sum(1 for r in _diag(app) if str(getattr(r, "text", "")).strip())


def can_pause(app) -> bool:
    """True when pause/resume can actually do something -- a dead microphone
    needs repair guidance, and relabelling a dead button is not recovery."""
    if getattr(app, "snoozed", False):
        return callable(getattr(app, "resume_listening", None))
    if not callable(getattr(app, "snooze_listening", None)):
        return False
    states = home_signals.capability_states(app)
    return states[home_signals.DICTATION].status in (home_signals.READY, home_signals.UNKNOWN)


class VoiceHelpPage(QWidget):
    """The hub page. `open_page(name)` is the hub's navigation."""

    def __init__(self, app, open_page: Optional[Callable[[str], None]] = None, parent=None):
        super().__init__(parent)
        self._app = app
        self._open_page = open_page or (lambda name: None)
        self._stage_labels = {}
        self._pending = []
        self._build()
        self.refresh()

    def _build(self):
        self.setStyleSheet(
            f"QFrame#homeCard {{ background-color: {theme.BG1};"
            f" border: 1px solid {theme.BORDER}; border-radius: 8px; }}"
            f"QScrollArea {{ border: none; background: transparent; }}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        host_lay = QHBoxLayout(host)
        host_lay.setContentsMargins(GRID_LG, GRID_LG, GRID_LG, GRID_LG)
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        content.setMaximumWidth(CONTENT_MAX_W)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        host_lay.addStretch(0)
        host_lay.addWidget(content, stretch=1)
        host_lay.addStretch(0)
        scroll.setWidget(host)

        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(GRID_LG)

        title = _label(TITLE, role="page title", weight=600)
        title.setAccessibleName(TITLE)
        col.addWidget(title)
        subtitle = _label(SUBTITLE, role="tagline", color=theme.TEXT_SECONDARY, wrap=True)
        col.addWidget(subtitle)

        # ---- 1 + 2: what is affected, and the evidence --------------------
        affected = QWidget()
        affected.setStyleSheet("background: transparent;")
        affected.setAccessibleName(AFFECTED)
        alay = QVBoxLayout(affected)
        alay.setContentsMargins(0, 0, 0, 0)
        alay.setSpacing(GRID)
        alay.addWidget(_section_label(AFFECTED))
        self._capability_rows = {}
        for name in home_signals.NOTICE_ORDER:
            card = _card()
            card.setObjectName("homeCard")
            card.setAccessibleName(name)
            clay = QVBoxLayout(card)
            clay.setContentsMargins(GRID, GRID, GRID, GRID)
            clay.setSpacing(_px(4))
            headline = _label("", role="card title", weight=600)
            headline.setAccessibleName(f"{name} state")
            clay.addWidget(headline)
            evidence = _label("", role="note", color=theme.TEXT_SECONDARY, wrap=True)
            evidence.setAccessibleName(f"{name} evidence")
            clay.addWidget(evidence)
            alay.addWidget(card)
            self._capability_rows[name] = (headline, evidence)
        col.addWidget(affected)

        # ---- 3: what to do next -------------------------------------------
        nxt = QWidget()
        nxt.setStyleSheet("background: transparent;")
        nxt.setAccessibleName(NEXT_STEPS)
        nlay = QVBoxLayout(nxt)
        nlay.setContentsMargins(0, 0, 0, 0)
        nlay.setSpacing(GRID)
        nlay.addWidget(_section_label(NEXT_STEPS))

        row = QHBoxLayout()
        row.setSpacing(_px(8))
        self._check_btn = _button(RUN_CHECK, primary=True)
        self._check_btn.clicked.connect(self.run_check)
        row.addWidget(self._check_btn)
        self._pause_btn = _button(PAUSE)
        self._pause_btn.clicked.connect(self._on_pause)
        row.addWidget(self._pause_btn)
        row.addStretch(1)
        nlay.addLayout(row)

        for key, label in CHECK_STAGES:
            line = _label(f"{label}: not checked yet", role="card description",
                          color=theme.TEXT_SECONDARY, wrap=True)
            line.setAccessibleName(label)
            nlay.addWidget(line)
            self._stage_labels[key] = line

        note = _label(REPORT_HINT, role="note", color=theme.TEXT_SECONDARY, wrap=True)
        note.setAccessibleName("Reporting note")
        nlay.addWidget(note)
        col.addWidget(nxt)
        col.addStretch(1)

    # ---- Refresh --------------------------------------------------------

    def refresh(self):
        states = home_signals.capability_states(self._app)
        for name, (headline, evidence) in self._capability_rows.items():
            state = states[name]
            word = STATUS_WORDS.get(state.status, state.status)
            headline.setText(f"{name}: {word}")
            headline.setStyleSheet(_label_css(
                "card title", status_colours().get(state.status, theme.TEXT_PRIMARY), 600))
            detail = state.detail or ("" if state.status == home_signals.READY else "")
            because = f"Because: {state.evidence}." if state.evidence else ""
            consequence = state.consequence if state.is_fault else ""
            evidence.setText(" ".join(p for p in (detail, consequence, because) if p))
            headline.setAccessibleDescription(evidence.text())
        self._refresh_pause()

    def _refresh_pause(self):
        paused = bool(getattr(self._app, "snoozed", False))
        label = RESUME if paused else PAUSE
        if self._pause_btn.text() != label:
            self._pause_btn.setText(label)
            self._pause_btn.setAccessibleName(label)
        self._pause_btn.setVisible(can_pause(self._app))

    # ---- The guided check -------------------------------------------------

    def run_check(self):
        """Run the stages one at a time, showing each result as it lands."""
        for key, label in CHECK_STAGES:
            self._stage_labels[key].setText(f"{label}: checking...")
        self._check_btn.setEnabled(False)
        self._check_btn.setText(CHECK_RUNNING)
        for index, (key, _label_text) in enumerate(CHECK_STAGES):
            QTimer.singleShot(STAGE_DELAY_MS * (index + 1),
                              lambda k=key: self.run_stage(k))
        QTimer.singleShot(STAGE_DELAY_MS * (len(CHECK_STAGES) + 1), self._check_finished)

    def run_stage(self, key: str):
        """One stage, synchronously -- separated so tests drive it without a
        running event loop."""
        label = dict(CHECK_STAGES)[key]
        status, evidence = check_stage(self._app, key)
        word = STATUS_WORDS.get(status, status)
        line = self._stage_labels[key]
        line.setText(f"{label}: {word}. {evidence}")
        line.setStyleSheet(_label_css(
            "card description", status_colours().get(status, theme.TEXT_SECONDARY), 400))
        line.setAccessibleDescription(f"{label}: {word}. {evidence}")
        return status, evidence

    def _check_finished(self):
        self._check_btn.setEnabled(True)
        self._check_btn.setText(RUN_CHECK)

    def _on_pause(self):
        if getattr(self._app, "snoozed", False):
            resume_hands_free(self._app)
        else:
            pause_hands_free(self._app)
        self.refresh()

    # ---- Introspection ----------------------------------------------------

    def stage_texts(self) -> dict:
        return {key: self._stage_labels[key].text() for key, _label in CHECK_STAGES}

    def capability_texts(self) -> dict:
        return {name: head.text() for name, (head, _ev) in self._capability_rows.items()}

    @property
    def pause_button(self):
        return self._pause_btn
