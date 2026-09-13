"""PySide6 "Quick Reference" window for Samsara.

A read-only, at-a-glance summary of the user's ACTUAL current controls:
dictation hotkeys, voice-session phrases, the COMMAND/DICTATE/AVA lanes,
and (if it has landed on this branch) inline formatting tokens.

HARD RULE: every value shown is read from live config/registries at
window-open time -- never hardcoded. The small resolver functions below
(_resolve_hotkeys, _resolve_session_phrases, _resolve_modes_overview,
_resolve_formatting_tokens) each re-read config/registries fresh on every
call; refresh() re-invokes all four and rebuilds the body from scratch, and
both the Refresh button and every show() call it -- there is no cached
snapshot anywhere in this file. Where a feature is off in config, its row
(or group of rows) renders dimmed with "(disabled)" appended rather than
being omitted, so the user learns the feature exists.

qt_runtime discipline copied from samsara/ui/history_qt.py: the public
QuickReferenceQt wrapper is safe to call from any thread; all Qt widget
construction is posted to the Qt thread via qt_runtime.post(); the window
itself is never destroyed on close (closeEvent ignores + hides) so a
second show() after closing just re-shows and re-refreshes the same
window instead of re-registering with qt_runtime a second time (blocked by
_init_posted).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from samsara.constants import DEFAULT_WAKE_PHRASE, DEFAULT_WAKE_PHRASE_OPTIONS
from samsara.ui import qt_runtime
from samsara.ui import theme
from samsara import config_schema
from samsara import formatting_tokens as ft
from samsara import session_modes
from samsara.session_modes import SessionMode, resolve_ava_invocations

from samsara.log import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Fallback defaults -- ONLY used when a key is absent from both the live
# config AND config_schema.SETTINGS_SCHEMA (should not happen in practice:
# dictation.py's load_config() backfills every one of these into the live
# config on every load). Mirrors dictation.py's load_config() default_config
# dict exactly (same convention config_schema.py itself documents using --
# "Extracted from samsara/ui/settings_qt.py widget parameters"). Re-verify
# against dictation.py's load_config() if these ever drift.
# ---------------------------------------------------------------------------

_HOTKEY_FALLBACKS = {
    "hotkey": "ctrl+shift",
    "undo_hotkey": "ctrl+alt+z",
    "dictate_commit_hotkey": "ctrl+space",
    # dictation.py load_config() default_config, 2026-09-13 (guidance audit row 200):
    "continuous_hotkey": "ctrl+alt+d",
    "wake_word_hotkey": "ctrl+alt+w",
    "command_hotkey": "ctrl+alt+c",
    "cancel_hotkey": "escape",
    "memo_hotkey": "ctrl+alt+m",
    "correction_hotkey": "ctrl+alt+r",
    "hotkeys.capture_correction": "ctrl+alt+x",
    "continuous_commit_hotkey": "ctrl+space",
    "continuous_commit_trigger": "silence",
}

# The streaming key is NOT a config value: dictation.py _install_capslock_hook
# hardcodes keyboard.hook_key("caps lock") and only installs it while
# streaming_mode is on (the tray toggle). The old streaming_hotkey config key
# is read by nothing (guidance audit row 170) -- shown here as fixed text.
STREAMING_KEY_LABEL = "CapsLock (fixed)"

_DICTATE_MODE_LABELS = {
    "hold": "Dictate (hold to talk)",
    "toggle": "Dictate (press to start, press to stop)",
    "continuous": "Dictate (continuous)",
}

_WAKE_FALLBACKS = {
    "phrase": DEFAULT_WAKE_PHRASE,
    "phrase_options": DEFAULT_WAKE_PHRASE_OPTIONS,
    "end_words": ["over", "done", "end dictation"],
    "wake_abort_phrase": ["cancel", "cancel dictation", "abort"],
}


def _schema_default(key: str, fallback):
    """Prefer config_schema.SETTINGS_SCHEMA's documented default; fall back
    to the small mirrors above only for keys the schema doesn't cover
    (mainly the top-level hotkey strings and wake_word_config sub-keys)."""
    try:
        entry = config_schema.SETTINGS_SCHEMA.get(key)
        if entry and "default" in entry:
            return entry["default"]
    except Exception:
        pass
    return fallback


def _pretty_key_combo(raw: str) -> str:
    if not raw:
        return "(not set)"
    if raw.strip().lower() == "capslock":
        return "CapsLock"
    if raw.strip().lower() in ("mouse4", "mouse5"):
        return _pretty_button(raw.strip().lower())   # main hotkey on a side button
    return "+".join(part.strip().capitalize() for part in raw.split("+") if part.strip())


def _pretty_button(raw: str) -> str:
    """Reuse settings_qt's existing button-code -> display-label registry
    (command_mode.button stores codes like 'rctrl'/'mouse4') instead of
    hand-rolling a second mapping that could drift from it."""
    try:
        from samsara.ui.settings_qt import _CMD_BUTTON_KEY_TO_LABEL
        return _CMD_BUTTON_KEY_TO_LABEL.get(raw, raw)
    except Exception:
        return raw


def _commit_value(commit: dict) -> str:
    extra = commit.get("homophones") or []
    if extra:
        return commit["phrase"] + "  (an isolated " + " / ".join(f'"{w}"' for w in extra) + " commits too)"
    return commit["phrase"]


def _lane_switch_phrases(mode: SessionMode) -> list[str]:
    """Read session_modes' OWN registry of whole-utterance switch phrases --
    never a phrase list copied into this file, so a change to
    session_modes._WHOLE_UTTERANCE_SWITCHES is reflected here automatically.

    For AVA this returns the static whole-utterance switch ("ava mode",
    2026-09-11); the configurable invocations ("hey ava", ...) come from
    _ava_invocation_phrases(app) and the two are shown together."""
    return sorted(
        phrase for phrase, m in session_modes._WHOLE_UTTERANCE_SWITCHES.items()
        if m is mode
    )


def _ava_invocation_phrases(app) -> list[str]:
    """Ava's counterpart to _lane_switch_phrases.

    Uses the shared resolve helper from session_modes.py so Quick Reference and
    the hands-free session path share fallback behavior.
    """
    cfg = getattr(app, "config", None) or {}
    return sorted(set(resolve_ava_invocations(cfg)) | set(_lane_switch_phrases(SessionMode.AVA)))


# ---------------------------------------------------------------------------
# Resolvers -- each reads config/registries fresh on every call, no caching.
# ---------------------------------------------------------------------------

def _resolve_hotkeys(app) -> list[dict]:
    """Section 1: Dictation hotkeys. Returns a list of
    {"label", "value", "enabled"} rows."""
    cfg = getattr(app, "config", None) or {}
    cm_cfg = cfg.get("command_mode", {}) or {}
    cm_enabled = bool(cm_cfg.get("enabled", _schema_default("command_mode.enabled", False)))
    cm_button = cm_cfg.get("button", _schema_default("command_mode.button", "rctrl"))
    cm_mode = cm_cfg.get("mode", _schema_default("command_mode.mode", "hold"))

    streaming_enabled = bool(cfg.get("streaming_mode", False))
    dictate_mode = str(cfg.get("mode", _schema_default("mode", "hold")))
    hotkeys_cfg = cfg.get("hotkeys", {}) or {}
    commit_trigger = cfg.get("continuous_commit_trigger", _HOTKEY_FALLBACKS["continuous_commit_trigger"])

    def _key(name):
        return _pretty_key_combo(cfg.get(name, _HOTKEY_FALLBACKS[name]))

    ava_phrases = _ava_invocation_phrases(app)
    ava_phrase = ava_phrases[0] if ava_phrases else "(none configured)"

    return [
        {
            # Row 148: the label follows config `mode` (hold / toggle / continuous).
            "label": _DICTATE_MODE_LABELS.get(dictate_mode, f"Dictate ({dictate_mode})"),
            "value": _key("hotkey"),
            "enabled": True,
        },
        {"label": "Continuous mode (toggle)", "value": _key("continuous_hotkey"), "enabled": True},
        {"label": "Wake word listener (toggle)", "value": _key("wake_word_hotkey"), "enabled": True},
        {"label": "Command only (hold)", "value": _key("command_hotkey"), "enabled": True},
        {"label": "Cancel recording", "value": _key("cancel_hotkey"), "enabled": True},
        {
            "label": ("Hands-free Session" if cm_mode == "toggle" else "Command Mode (hold)"),
            "value": _pretty_button(cm_button),
            "enabled": cm_enabled,
        },
        {
            "label": "Paste staged thought",
            "value": _pretty_key_combo(cfg.get("dictate_commit_hotkey", _HOTKEY_FALLBACKS["dictate_commit_hotkey"])),
            "enabled": cm_enabled and cm_mode == "toggle",
        },
        {
            # Ava has no dedicated physical hotkey -- it remains a voice
            # lane switch available from the latched session.
            "label": "Ava",
            "value": f'Say "{ava_phrase}" during the voice session',
            "enabled": cm_enabled,
        },
        {
            "label": "Streaming (live partials)",
            "value": STREAMING_KEY_LABEL + "  (live only while streaming mode is on -- tray toggle)",
            "enabled": streaming_enabled,
        },
        {"label": "Undo last dictation", "value": _key("undo_hotkey"), "enabled": True},
        {"label": "Voice memo", "value": _key("memo_hotkey"), "enabled": True},
        {"label": "Correction report", "value": _key("correction_hotkey"), "enabled": True},
        {
            "label": "Correction capture",
            "value": _pretty_key_combo(hotkeys_cfg.get("capture_correction", _HOTKEY_FALLBACKS["hotkeys.capture_correction"])),
            "enabled": True,
        },
        {
            "label": "Continuous commit",
            "value": _key("continuous_commit_hotkey") + "  (continuous mode, commit trigger = key)",
            "enabled": commit_trigger == "key",
        },
    ]


def _resolve_session_phrases(app) -> dict:
    """Section 2: Voice Session. Groups gate independently -- wake
    phrase/send word depend on wake_word_enabled; lane switches/scratch-that
    depend on command_mode.enabled (session_modes.py's own subject); the
    abort phrase(s) apply to whichever of those two is active, so it's only
    dimmed when BOTH are off."""
    cfg = getattr(app, "config", None) or {}
    ww_cfg = cfg.get("wake_word_config", {}) or {}
    cm_cfg = cfg.get("command_mode", {}) or {}

    wake_enabled = bool(cfg.get("wake_word_enabled", False))
    cm_enabled = bool(cm_cfg.get("enabled", _schema_default("command_mode.enabled", False)))
    cm_mode = cm_cfg.get("mode", _schema_default("command_mode.mode", "hold"))

    wake_phrase = ww_cfg.get("phrase", _WAKE_FALLBACKS["phrase"])
    phrase_options = ww_cfg.get("phrase_options", _WAKE_FALLBACKS["phrase_options"])
    alternates = [p for p in phrase_options if p != wake_phrase]

    end_words = ww_cfg.get("end_words", _WAKE_FALLBACKS["end_words"])
    abort_words = ww_cfg.get("wake_abort_phrase", _WAKE_FALLBACKS["wake_abort_phrase"])
    if isinstance(abort_words, str):
        abort_words = [abort_words]
    configured_abort = cm_cfg.get("abort_phrases", _schema_default("command_mode.abort_phrases", []))
    if isinstance(configured_abort, str):
        configured_abort = [configured_abort]
    sleep_words = list(getattr(session_modes, "SESSION_SLEEP_PHRASES", ()))
    stop_words = list(getattr(session_modes, "SESSION_STOP_PHRASES", ()))
    # Rows 489/516: command_mode.abort_phrases is merged exactly as
    # dictation.py merges it into the live session; sleep phrases are listed
    # on their own row because they keep the staged draft.
    abort_words = list(dict.fromkeys([
        *abort_words,
        *(configured_abort or []),
        *[p for p in session_modes.GLOBAL_SESSION_EXIT_PHRASES if p not in sleep_words],
    ]))
    prefix_switches = sorted(getattr(session_modes, "_PREFIX_SWITCHES", {}))
    homophones = sorted(getattr(session_modes, "_DICTATE_COMMIT_HOMOPHONES", {session_modes.DICTATE_COMMIT_PHRASE}) - {session_modes.DICTATE_COMMIT_PHRASE})
    pause_words = ww_cfg.get("pause_words", ["pause", "hold on", "wait"])
    resume_words = ww_cfg.get("resume_words", ["resume", "continue", "go on"])
    opens_session = bool(ww_cfg.get("opens_session", _schema_default("wake_word_config.opens_session", False)))

    return {
        "hands_free_toggle": {
            "enabled": cm_enabled,
            "latches": cm_enabled and cm_mode == "toggle",
            "button": _pretty_button(cm_cfg.get("button", _schema_default("command_mode.button", "rctrl"))),
            "mode": cm_mode,
        },
        "wake": {
            "enabled": wake_enabled,
            "phrase": wake_phrase,
            "alternates": alternates,
            "opens_session": opens_session,
            "pause_words": list(pause_words) if isinstance(pause_words, (list, tuple)) else [str(pause_words)],
            "resume_words": list(resume_words) if isinstance(resume_words, (list, tuple)) else [str(resume_words)],
        },
        "send_word": {
            "enabled": wake_enabled,
            "words": end_words,
        },
        "lane_switches": {
            "enabled": cm_enabled,
            "phrases": {
                "Command": _lane_switch_phrases(SessionMode.COMMAND),
                "Dictate": _lane_switch_phrases(SessionMode.DICTATE),
                "Ava": _ava_invocation_phrases(app),
            },
        },
        "abort": {
            "enabled": wake_enabled or cm_enabled,
            "words": abort_words,
        },
        "dictate_commit": {
            "enabled": cm_enabled,
            "phrase": session_modes.DICTATE_COMMIT_PHRASE,
            "homophones": homophones,        # row 250: an isolated "and" commits too
        },
        "stop": {"enabled": cm_enabled, "words": stop_words},          # keeps the draft, mic stays armed
        "sleep": {"enabled": cm_enabled, "words": sleep_words},        # keeps the draft, ends the session
        "prefix_switch": {"enabled": cm_enabled, "words": prefix_switches},
        "literal": {"enabled": cm_enabled, "word": "literal"},         # session_modes.match_literal_payload
        "scratch_that": {
            "enabled": cm_enabled,
            "phrase": session_modes.SCRATCH_THAT_PHRASE,
        },
    }


_MODE_DESCRIPTIONS = {
    SessionMode.COMMAND: "Legacy command-only lane retained for compatibility.",
    SessionMode.DICTATE: 'HANDS FREE: ordinary speech is buffered dictation; exact navigation commands execute immediately. Say "end" to paste and continue.',
    SessionMode.AVA: 'Everything you say goes to the local AI agent as natural language. Say "submit that" / "the text" to attach anything you just dictated.',
}


def _resolve_modes_overview(app) -> dict:
    """Section 3: Modes at a glance. Descriptions are prose about code
    behavior (session_modes.py's own SessionMode/docstrings), not a
    resolvable config value -- only the enabled flag is live-read."""
    cfg = getattr(app, "config", None) or {}
    cm_cfg = cfg.get("command_mode", {}) or {}
    cm_enabled = bool(cm_cfg.get("enabled", _schema_default("command_mode.enabled", False)))
    dictate_desc = _MODE_DESCRIPTIONS[SessionMode.DICTATE]
    extra = sorted(getattr(session_modes, "_DICTATE_COMMIT_HOMOPHONES", set()) - {session_modes.DICTATE_COMMIT_PHRASE})
    if extra:
        dictate_desc += " An isolated " + " / ".join(f'"{w}"' for w in extra) + " commits too."
    return {
        "enabled": cm_enabled,
        "modes": [
            {"name": "COMMAND", "description": _MODE_DESCRIPTIONS[SessionMode.COMMAND]},
            {"name": "HANDS FREE", "description": dictate_desc},
            {"name": "AVA", "description": _MODE_DESCRIPTIONS[SessionMode.AVA]},
        ],
    }


def _resolve_formatting_tokens(app) -> dict:
    """Section 4: Formatting tokens (only meaningful if formatting_tokens.py
    has landed -- it has on this branch). Token phrases/insertions are read
    from the module's own registry, not copied into this file."""
    cfg = getattr(app, "config", None) or {}
    ft_cfg = cfg.get("formatting_tokens", {}) or {}
    enabled = bool(ft_cfg.get("enabled", _schema_default("formatting_tokens.enabled", True)))

    def _describe(repl: str) -> str:
        if repl == "\n\n":
            return "paragraph break"
        if repl == "\n":
            return "line break"
        if repl == "\t":
            return "tab character"
        if repl.strip() == "•":
            return "bullet point"
        return repr(repl)

    grouped: dict[str, list[str]] = {}
    for phrase, repl in ft._SIMPLE_TOKENS:
        grouped.setdefault(repl, []).append(phrase)

    rows = [
        {"phrase": " / ".join(f'"{p}"' for p in phrases), "inserts": _describe(repl)}
        for repl, phrases in grouped.items()
    ]
    return {"enabled": enabled, "tokens": rows}


# ---------------------------------------------------------------------------
# Public wrapper (thread-safe)
# ---------------------------------------------------------------------------

class QuickReferenceQt:
    def __init__(self, app):
        self.app = app
        self._window = None
        self._init_posted = False

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._window.refresh)
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
            qt_runtime.post(self._window.activateWindow)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _QuickReferenceWindow(self.app)
        self._window.show()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class _QuickReferenceWindow(QMainWindow):

    def __init__(self, app):
        super().__init__()
        self.app = app

        self.setWindowTitle("Quick Reference")
        self.resize(640, 760)
        self.setMinimumSize(480, 400)
        self.setStyleSheet(theme.build_stylesheet())

        outer = QWidget()
        self.setCentralWidget(outer)
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer_lay.addWidget(scroll, stretch=1)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(28, 24, 28, 24)
        self._body_layout.setSpacing(20)
        scroll.setWidget(self._body)

        footer = QWidget()
        theme.style_footer(footer)
        footer_lay = QHBoxLayout(footer)
        footer_lay.setContentsMargins(20, 12, 20, 12)
        footer_note = QLabel("Values reflect your current settings")
        footer_note.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.FONT_SIZE_CAPTION}px;"
        )
        footer_lay.addWidget(footer_note)
        footer_lay.addStretch()
        self._refresh_btn = QPushButton("Refresh")
        theme.make_secondary(self._refresh_btn)
        self._refresh_btn.clicked.connect(self.refresh)
        footer_lay.addWidget(self._refresh_btn)
        outer_lay.addWidget(footer)

        self.refresh()

    # ------------------------------------------------------------------
    # Refresh -- re-invokes every resolver and rebuilds the body from
    # scratch. Called by __init__, the Refresh button, and every show()
    # of an already-open window (via the public wrapper).
    # ------------------------------------------------------------------

    def refresh(self):
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                # setParent(None) detaches it from the QObject tree
                # synchronously -- deleteLater() alone only SCHEDULES C++
                # destruction for the next event-loop tick, so a caller
                # (or a test) querying findChildren() right after refresh()
                # returns would still see the stale widget otherwise.
                w.setParent(None)
                w.deleteLater()

        title = QLabel("Quick Reference")
        title.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: {theme.FONT_SIZE_TITLE}px; font-weight: 700;"
        )
        self._body_layout.addWidget(title)

        self._body_layout.addWidget(self._build_hotkeys_section())
        self._body_layout.addWidget(self._build_hands_free_session_section())
        self._body_layout.addWidget(self._build_session_section())
        self._body_layout.addWidget(self._build_modes_section())
        ft_state = _resolve_formatting_tokens(self.app)
        self._body_layout.addWidget(self._build_formatting_section(ft_state))
        self._body_layout.addStretch()

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _section_card(self, heading: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        theme.style_card(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)
        head = QLabel(heading)
        head.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: {theme.FONT_SIZE_HEADING}px; font-weight: 600;"
        )
        lay.addWidget(head)
        return card, lay

    def _row(self, layout: QVBoxLayout, label: str, value: str, enabled: bool = True):
        row = QHBoxLayout()
        row.setSpacing(12)
        lbl = QLabel(label)
        color = theme.TEXT_SECONDARY if enabled else theme.TEXT_DISABLED
        lbl.setStyleSheet(f"color: {color}; font-size: {theme.FONT_SIZE_BODY}px;")
        row.addWidget(lbl)
        row.addStretch()
        text = value if enabled else f"{value}  (disabled)"
        val = QLabel(text)
        val.setWordWrap(True)
        val_color = theme.TEXT_PRIMARY if enabled else theme.TEXT_DISABLED
        val.setStyleSheet(
            f"color: {val_color}; font-size: {theme.FONT_SIZE_HEADING}px; font-weight: 600;"
        )
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(val)
        layout.addLayout(row)

    def _build_hotkeys_section(self) -> QFrame:
        card, lay = self._section_card("Dictation Hotkeys")
        for row in _resolve_hotkeys(self.app):
            self._row(lay, row["label"], row["value"], row["enabled"])
        return card

    def _build_hands_free_session_section(self) -> QFrame:
        state = _resolve_session_phrases(self.app)
        card, lay = self._section_card("Hands-Free Session")

        hands_free = state["hands_free_toggle"]
        hf_hint = (
            "latches a session" if hands_free.get("latches")
            else "requires command_mode.enabled and mode=toggle"
        )
        self._row(
            lay,
            "Hands-free toggle hotkey",
            f'{hands_free["button"]}  ({hf_hint})',
            hands_free["enabled"],
        )

        send = state["send_word"]
        self._row(
            lay, "Wake send word", ", ".join(send["words"]) or "(none)", send["enabled"]
        )

        lanes = state["lane_switches"]
        for lane_name, phrases in lanes["phrases"].items():
            self._row(
                lay,
                f"{lane_name} lane switch",
                ", ".join(phrases) or "(none)",
                lanes["enabled"],
            )

        commit = state["dictate_commit"]
        self._row(lay, "DICTATE commit", _commit_value(commit), commit["enabled"])

        prefix = state["prefix_switch"]
        self._row(lay, "Dictate prefix", ", ".join(f'"{w} <text>"' for w in prefix["words"]) or "(none)", prefix["enabled"])

        literal = state["literal"]
        self._row(lay, "Say a reserved word as text", f'"{literal["word"]} <words>"', literal["enabled"])

        scratch = state["scratch_that"]
        self._row(lay, "Undo phrase", scratch["phrase"], scratch["enabled"])

        stop = state["stop"]
        self._row(lay, "Stop (keeps the draft, mic stays on)", ", ".join(stop["words"]) or "(none)", stop["enabled"])

        sleep = state["sleep"]
        self._row(lay, "Sleep (keeps the draft, ends the session)", ", ".join(sleep["words"]) or "(none)", sleep["enabled"])

        abort = state["abort"]
        self._row(lay, "Exit session phrase(s)", ", ".join(abort["words"]) or "(none)", abort["enabled"])

        return card

    def _build_session_section(self) -> QFrame:
        state = _resolve_session_phrases(self.app)
        card, lay = self._section_card("Voice Session")

        wake = state["wake"]
        wake_value = wake["phrase"]
        if wake["alternates"]:
            wake_value += "  (also: " + ", ".join(wake["alternates"]) + ")"
        if wake.get("opens_session"):
            wake_value += "  -- opens the hands-free session"
        self._row(lay, "Wake phrase", wake_value, wake["enabled"])
        self._row(lay, "Pause / resume words",
                  ", ".join(wake.get("pause_words", [])) + "  /  " + ", ".join(wake.get("resume_words", [])),
                  wake["enabled"])

        send = state["send_word"]
        self._row(lay, "Send word", ", ".join(send["words"]) or "(none)", send["enabled"])

        lanes = state["lane_switches"]
        for lane_name, phrases in lanes["phrases"].items():
            self._row(
                lay, f"{lane_name} lane switch", ", ".join(phrases) or "(none)", lanes["enabled"]
            )

        commit = state["dictate_commit"]
        self._row(lay, "Commit DICTATE thought", _commit_value(commit), commit["enabled"])

        abort = state["abort"]
        self._row(lay, "Abort phrase(s)", ", ".join(abort["words"]) or "(none)", abort["enabled"])

        scratch = state["scratch_that"]
        self._row(lay, "Undo phrase", scratch["phrase"], scratch["enabled"])
        return card

    def _build_modes_section(self) -> QFrame:
        state = _resolve_modes_overview(self.app)
        card, lay = self._section_card("Modes at a Glance")
        enabled = state["enabled"]
        for mode in state["modes"]:
            row = QVBoxLayout()
            row.setSpacing(2)
            name_color = theme.TEXT_PRIMARY if enabled else theme.TEXT_DISABLED
            name = QLabel(mode["name"])
            name.setStyleSheet(
                f"color: {name_color}; font-size: {theme.FONT_SIZE_BODY}px; font-weight: 700;"
            )
            row.addWidget(name)
            desc_color = theme.TEXT_SECONDARY if enabled else theme.TEXT_DISABLED
            desc_text = mode["description"]
            if not enabled:
                desc_text += "  (disabled)"
            desc = QLabel(desc_text)
            desc.setWordWrap(True)
            desc.setStyleSheet(f"color: {desc_color}; font-size: {theme.FONT_SIZE_CAPTION}px;")
            row.addWidget(desc)
            lay.addLayout(row)
        return card

    def _build_formatting_section(self, state: dict) -> QFrame:
        card, lay = self._section_card("Formatting Tokens")
        enabled = state["enabled"]
        for token in state["tokens"]:
            self._row(lay, token["phrase"], token["inserts"], enabled)
        return card

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        e.ignore()
        self.hide()
