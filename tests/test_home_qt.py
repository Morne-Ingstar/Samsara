"""Home (samsara/ui/home_qt.py), its signals (home_signals.py) and Voice help
(voice_help_qt.py), after queue 86 turned Home from a control surface into a
front door.

The invariants this file exists to hold:
  * no Home control can start, stop or pause capture -- the only pause lives
    in Voice help, and only when it can actually act;
  * a healthy app shows no status row and no notice;
  * a fault in one capability names THAT capability's consequence, and paused
    or unknown never reads as failed or healthy;
  * a dismissed suggestion does not come back, and an empty slot disappears;
  * Ava's four states each render with text and an accessible name;
  * Voice help is in the nav on every screen.

Widgets are built directly against the session `qapp` fixture (the precedent
of test_main_window_qt.py); qt_runtime is not exercised, dictation is never
imported, and QSettings is faked so the owner's registry is untouched.
"""

import collections
import json
import os
import random
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QPushButton, QWidget

from samsara import command_catalog, command_scope
from samsara.ui import (
    command_marquee, home_qt, home_signals, main_window_qt, theme,
)
from samsara.ui import voice_help_qt

PY = sys.executable
REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeSettings:
    values = {}

    def __init__(self, *_args):
        pass

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value


class _Store:
    def __init__(self, text="hello world", words=0):
        self.text, self.words = text, words

    def query(self, search=None, type_filter=None, limit=200, **kw):
        if type_filter == 'dictation' and self.text:
            return [{'display_text': self.text}]
        return []

    def words_typed_since(self, since_iso):
        return self.words


class _WakeConsumer:
    """The WakeConsumer surface home_signals asks about (queue 109): whether
    the poll thread is actually servicing frames."""

    def __init__(self, running=True):
        self.running = running


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    _FakeSettings.values = {}
    monkeypatch.setattr(home_qt, "QSettings", _FakeSettings)
    # No hint tier may reach the real machine's files in a test.
    monkeypatch.setattr(home_signals, "corrections_waiting", lambda app: 0)
    monkeypatch.setattr(home_signals, "_user_words_exist", lambda: True)
    monkeypatch.setattr(home_signals, "generic_hints", lambda app: [])
    monkeypatch.setattr(home_signals, "_diag_records", lambda app, limit: [])


def _app(**over):
    """A mocked DictationApp with the runtime facts Home and Voice help read."""
    app = types.SimpleNamespace(
        config={'mode': 'hold', 'hotkey': 'ctrl+shift', 'microphone': 7,
                'wake_word_enabled': True, 'wake_word_configured': True,
                'wake_word_config': {'phrase': 'jarvis'},
                'command_mode': {'mode': 'toggle'},
                'ollama': {'enabled': True}, 'ava_declined': True},
        available_mics=[{'id': 7, 'name': 'USB mic'}],
        recording=False, snoozed=False, wake_word_active=True, continuous_active=False,
        command_mode_active=False, ava_command_session_active=False, ava_mode_active=False,
        toggle_active=False, audio_coordinator=object(),
        _outcome_ring=collections.deque(maxlen=8),
        history_store=_Store(),
        snooze_calls=[], stop_calls=[],
        # Hands-free readiness is read from the RUNTIME lifecycle since
        # queue 109 (Astra F12: config plus a microphone is not listening),
        # so a healthy app has to carry the things production carries -- a
        # running wake consumer, loaded wake models and no fatal on record.
        _wake_consumer=_WakeConsumer(running=True),
        _hands_free_fault=None, _wake_start_pending=False,
        wake_ready_state=lambda: 'ready',
    )

    def snooze_listening(minutes=None):
        app.snooze_calls.append(minutes)
        app.snoozed = True

    def resume_listening():
        app.snoozed = False

    def stop_recording():
        app.stop_calls.append("stop_recording")

    app.snooze_listening = snooze_listening
    app.resume_listening = resume_listening
    app.stop_recording = stop_recording
    for k, v in over.items():
        setattr(app, k, v)
    return app


def _ready(app, state=home_signals.READY, detail="", monkeypatch=None):
    """Force Ava's readiness, which otherwise depends on the machine."""
    monkeypatch.setattr(home_signals, "ava_state", lambda _a: home_signals.CapabilityState(
        home_signals.AVA, state, detail, "a fixed test readiness"))


@pytest.fixture
def page(qapp):
    def make(app=None):
        p = home_qt.HomePage(app or _app())
        p.resize(760, 900)
        p.show()
        qapp.processEvents()
        return p
    return make


@pytest.fixture
def help_page(qapp):
    def make(app=None):
        p = voice_help_qt.VoiceHelpPage(app or _app())
        p.resize(760, 900)
        p.show()
        qapp.processEvents()
        return p
    return make


def _hub(qapp, monkeypatch=None, width=1065, height=906, app=None):
    """A real hub window with Home showing, off-screen. The status poll is
    stopped so nothing repaints behind the assertions."""
    win = main_window_qt._MainWindow(app or _app())
    win._poll_timer.stop()
    win.resize(width, height)
    win._activate("Home")
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    win.show()
    for _ in range(60):
        qapp.processEvents()
    return win


def _buttons(widget):
    return [b for b in widget.findChildren(QPushButton) if b.isVisibleTo(widget)]


# ---------------------------------------------------------------------------
# 1. The state panel is gone, and Home cannot control capture
# ---------------------------------------------------------------------------

class TestNoControlSurface:
    def test_no_home_control_can_start_stop_or_pause_capture(self, page):
        home = page()
        for button in _buttons(home):
            button.click()
        assert home._app.stop_calls == []
        assert home._app.snooze_calls == []
        assert home._app.snoozed is False

    def test_every_destination_is_navigation_never_capture(self, page):
        home = page()
        forbidden = {"snooze_listening", "resume_listening", "stop_recording",
                     "stop_continuous_mode", "exit_command_mode", "exit_ava_command_session"}
        for label, (kind, target) in home.destinations().items():
            assert str(target) not in forbidden, label
            # "url" (89) is an external link opened in the browser -- still
            # navigation, still never a capture control.
            assert kind in ("page", "settings", "cheatsheet", "guides", "app",
                            "inline", "url"), label

    def test_the_state_panel_and_its_widgets_are_gone(self, page):
        """86 removed the capture CONTROLS. Queue 89 put the owner's own
        identity strip back, so "words today" and _words_val are expected
        here now -- they were never controls."""
        home = page()
        for attribute in ("_state_card", "_state_line", "_instruction", "_primary_btn",
                          "_mark", "_dictation_text", "_action_kind"):
            assert not hasattr(home, attribute), attribute
        texts = " ".join(home.visible_texts())
        for word in ("Stop listening", "Pause listening", "Resume listening",
                     "Latest dictation", "Ready."):
            assert word not in texts, word

    def test_the_hub_still_feeds_a_mark_without_error(self, page):
        """main_window_qt hands Home the live capture frame; Home ignores it
        now, and must not raise for the hub that still offers it."""
        home = page()
        assert home.set_mark_frame(object()) is None


# ---------------------------------------------------------------------------
# 2. The tagline slot
# ---------------------------------------------------------------------------

class TestTagline:
    def test_the_slot_is_empty_and_hidden_until_the_owner_writes_it(self, page):
        home = page()
        assert home._tagline.text() == ""
        assert not home._tagline.isVisibleTo(home)

    def test_setting_a_tagline_shows_it(self, page):
        home = page()
        home.set_tagline("Dictate anywhere.")
        assert home._tagline.isVisibleTo(home)
        assert "Dictate anywhere." in home.visible_texts()


# ---------------------------------------------------------------------------
# 3. The suggestion slot
# ---------------------------------------------------------------------------

class TestConditionalSlot:
    """Queue 89: the slot renders ONLY for a diagnostic hint or the one-shot
    what's-new note, and collapses to zero height otherwise."""

    def test_no_diagnostic_and_no_new_version_means_no_slot_at_all(self, page, monkeypatch):
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
        monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: None)
        home = page()
        assert home.hint is None
        assert not home._hint_card.isVisibleTo(home)
        assert "SUGGESTION" not in " ".join(home.visible_texts()).upper()

    def test_generic_and_discovery_tips_never_reach_the_slot(self, page, monkeypatch):
        """The status-strip marquee owns generic command tips (89), not this."""
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
        monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: None)
        monkeypatch.setattr(home_signals, "discovery_hints", lambda app: [
            home_signals.Hint("discover.wake_word", home_signals.TIER_DISCOVERY,
                              "Start listening without reaching for a key.",
                              "Set up wake word", "settings", "Modes")])
        monkeypatch.setattr(home_signals, "generic_hints", lambda app: [
            home_signals.Hint("tip.0", home_signals.TIER_GENERIC, "A tip.",
                              "View commands", "cheatsheet", "")])
        home = page()
        assert home.hint is None
        assert not home._hint_card.isVisibleTo(home)

    def test_a_diagnostic_hint_fills_the_slot_and_cites_its_evidence(self, page, monkeypatch):
        hint = home_signals.Hint(
            "diag.no_text", home_signals.TIER_DIAGNOSTIC,
            "4 of your last 20 recordings produced no text.",
            "Open Voice help", "page", "Voice help",
            evidence="4/20 recent captures recorded outcome empty or gated (threshold 3)")
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [hint])
        home = page()
        assert home.hint is hint
        assert home._hint_card.isVisibleTo(home)
        texts = " ".join(home.visible_texts())
        assert hint.text in texts
        # 111: the working is no longer printed under the sentence. It is
        # behind Details, and the visible slot carries an ACTION instead.
        assert "Because:" not in texts
        assert "threshold 3" not in texts
        assert home._hint_sub.text() == (
            "If you were speaking, open Voice troubleshooting to investigate.")
        assert home._hint_evidence.text() == hint.evidence

    def test_a_diagnostic_never_fires_on_a_single_occurrence(self, monkeypatch):
        app = _app()
        monkeypatch.setattr(home_signals, "corrections_waiting",
                            lambda a: home_signals.CORRECTIONS_WAITING_MIN - 1)
        assert home_signals.diagnostic_hints(app) == []
        monkeypatch.setattr(home_signals, "corrections_waiting",
                            lambda a: home_signals.CORRECTIONS_WAITING_MIN)
        hint = home_signals.diagnostic_hints(app)[0]
        assert hint.tier == home_signals.TIER_DIAGNOSTIC
        assert "threshold" in hint.evidence and "correction queue" in hint.evidence

    def test_no_text_diagnostic_reads_real_records(self, monkeypatch):
        app = _app()
        records = [types.SimpleNamespace(outcome="empty") for _ in range(3)]
        records += [types.SimpleNamespace(outcome="ok") for _ in range(5)]
        monkeypatch.setattr(home_signals, "_diag_records", lambda a, limit: records)
        hint = [h for h in home_signals.diagnostic_hints(app) if h.id == "diag.no_text"][0]
        assert "3 of your last 8 recordings" in hint.text
        assert hint.action_label == "Open Voice help"

    def test_a_dismissed_diagnostic_does_not_come_back(self, page, monkeypatch):
        hint = home_signals.Hint("diag.misses", home_signals.TIER_DIAGNOSTIC, "Missed things.",
                                 "View commands", "cheatsheet", "", evidence="3/8 were misses")
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [hint])
        monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: None)
        home = page()
        assert home.hint is not None
        home._hint_hide_btn.click()
        assert home.hint is None
        assert not home._hint_card.isVisibleTo(home)
        assert page().hint is None                      # durable across pages

    def test_the_slot_does_not_change_while_home_is_open(self, page, monkeypatch):
        hints = [home_signals.Hint("diag.a", home_signals.TIER_DIAGNOSTIC, "First", "x",
                                   "page", "Dictionary", evidence="e"),
                 home_signals.Hint("diag.b", home_signals.TIER_DIAGNOSTIC, "Second", "x",
                                   "page", "Dictionary", evidence="e")]
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: hints)
        home = page()
        first = home.hint.id
        for _ in range(5):
            home.refresh()
        assert home.hint.id == first
        home._hint_next_btn.click()                      # only on request
        assert home.hint.id != first


class TestWhatsNewNote:
    def test_it_is_read_from_the_changelog_never_written_by_hand(self):
        version, sentence = home_qt.changelog_summary()
        assert version and sentence
        text = home_qt.CHANGELOG_PATH.read_text(encoding="utf-8")
        assert sentence in text and version in text

    def test_unreleased_is_never_announced(self, tmp_path):
        path = tmp_path / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## [Unreleased]\n\nNot shipped yet.\n\n"
                        "## [1.2.3] - 2026-01-01\n\nThe real one.\n", encoding="utf-8")
        assert home_qt.changelog_summary(path) == ("1.2.3", "The real one.")

    def test_a_release_with_no_prose_gets_no_note(self, tmp_path):
        path = tmp_path / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## [1.2.3] - 2026-01-01\n\n### Added\n\n- a thing\n",
                        encoding="utf-8")
        assert home_qt.changelog_summary(path) == ("1.2.3", None)
        assert home_qt.whats_new_note(None, path, current="1.2.3") is None

    def test_shown_once_per_version(self, tmp_path):
        path = tmp_path / "CHANGELOG.md"
        path.write_text("# Changelog\n\n## [1.2.3] - 2026-01-01\n\nThe real one.\n",
                        encoding="utf-8")
        assert home_qt.whats_new_note(None, path, current="1.2.3") == ("1.2.3", "The real one.")
        assert home_qt.whats_new_note("1.2.3", path, current="1.2.3") is None
        # A version the running build is not on is never announced.
        assert home_qt.whats_new_note(None, path, current="1.2.2") is None

    def test_dismissing_it_records_the_version_and_hides_it(self, page, monkeypatch):
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
        monkeypatch.setattr(home_qt, "whats_new_note",
                            lambda seen, *a, **k: None if seen == "9.9.9" else ("9.9.9", "New."))
        home = page()
        assert home._hint_card.isVisibleTo(home)
        assert "New." in " ".join(home.visible_texts())
        assert home._hint_hide_btn.text() == home_qt.DISMISS
        home._hint_hide_btn.click()
        assert not home._hint_card.isVisibleTo(home)
        assert _FakeSettings.values.get(home_qt.WHATS_NEW_SEEN_KEY) == "9.9.9"


# ---------------------------------------------------------------------------
# 4. Ava presence
# ---------------------------------------------------------------------------

class TestAvaInTheHeader:
    """Queue 89: Ava moved out of Home's body into the hub header, left of
    the listening indicator. She is app-wide state, shown on every page."""

    def _control(self, qapp, app=None, monkeypatch=None):
        control = home_qt.AvaHeaderControl(app or _app())
        control.show()
        qapp.processEvents()
        return control

    def test_home_no_longer_carries_an_ava_row(self, page):
        home = page()
        for attribute in ("_ava_box", "_ava_mark", "_ava_state_text",
                          "_ava_settings_btn", "_ava_guide_btn"):
            assert not hasattr(home, attribute), attribute
        assert home_qt.AVA_NAME not in [c.accessibleName() for c in home.cards]

    def test_the_hub_header_carries_it_left_of_the_listening_indicator(self, qapp, monkeypatch):
        win = _hub(qapp, monkeypatch)
        assert isinstance(win._ava, home_qt.AvaHeaderControl)
        header = win._ava.parent()
        order = [header.layout().itemAt(i).widget() for i in range(header.layout().count())]
        assert order.index(win._ava) < order.index(win._badge)

    @pytest.mark.parametrize("status,expected", [
        (home_signals.READY, home_signals.AVA_AWAKE),
        (home_signals.OFF, home_signals.AVA_ASLEEP),
        (home_signals.UNAVAILABLE, home_signals.AVA_DIM),
        (home_signals.UNKNOWN, home_signals.AVA_DIM),
    ])
    def test_every_state_shows_a_WORD_not_just_a_dimmed_avatar(self, qapp, monkeypatch,
                                                               status, expected):
        _ready(None, status, "Ava is offline: the host refused." if status ==
               home_signals.UNAVAILABLE else "", monkeypatch=monkeypatch)
        control = self._control(qapp)
        assert control.state == expected
        # The state is carried by visible text, never by colour alone.
        assert home_qt.AVA_SHORT_STATE[expected] in control.visible_text
        assert control.accessibleName() == control.visible_text
        assert control.accessibleDescription().strip()

    def test_thinking_is_shown_and_expires(self, qapp, monkeypatch):
        import time as _time
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        app = _app(ava_thinking_since=_time.monotonic())
        control = self._control(qapp, app)
        assert control.state == home_signals.AVA_THINKING
        app.ava_thinking_since = _time.monotonic() - home_signals.AVA_THINKING_MAX_S - 1
        control.refresh()
        assert control.state == home_signals.AVA_AWAKE          # never indefinite

    def test_awake_does_not_claim_a_microphone_or_a_network_send(self, qapp, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        control = self._control(qapp)
        text = (control.visible_text + " " + control.accessibleDescription()).lower()
        for word in ("listening", "microphone", "recording", "sending"):
            assert word not in text

    def test_clicking_it_goes_to_ava_settings(self, qapp, monkeypatch):
        opened = []
        control = home_qt.AvaHeaderControl(_app(), on_click=lambda: opened.append("Ava / Cloud"))
        control.show()
        qapp.processEvents()
        control.click()
        assert opened == ["Ava / Cloud"]

    def test_its_label_is_never_clipped(self, qapp, monkeypatch):
        """It read "Ava - unavaila" before its sizeHint came from the layout."""
        _ready(None, home_signals.UNAVAILABLE, "Ava is offline.", monkeypatch=monkeypatch)
        control = self._control(qapp)
        needed = control.layout().sizeHint().width()
        assert control.sizeHint().width() >= needed
        assert home_qt.overflowing_widgets(control) == []


# ---------------------------------------------------------------------------
# 5. The problem notice
# ---------------------------------------------------------------------------

class TestProblemNotice:
    def test_a_healthy_app_shows_no_notice_and_no_status_row(self, page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        home = page()
        assert home.notice is None
        assert not home._notice_card.isVisibleTo(home)
        assert "Microphone" not in " ".join(home.visible_texts())

    def test_a_missing_microphone_names_dictations_consequence(self, page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        home = page(_app(available_mics=[]))
        notice = home.notice
        assert notice.capability == home_signals.DICTATION
        assert notice.text == "Microphone unavailable. Dictation cannot hear you."
        assert home._notice_card.isVisibleTo(home)

    def test_ava_offline_does_not_declare_dictation_broken(self, page, monkeypatch):
        _ready(None, home_signals.UNAVAILABLE, "Ava is offline: the host refused.",
               monkeypatch=monkeypatch)
        home = page()
        notice = home.notice
        assert notice.capability == home_signals.AVA
        assert notice.consequence == "Voice questions will not get answers."
        assert "Dictation" not in notice.text

    def test_paused_is_not_failed(self, page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        home = page(_app(snoozed=True))
        assert home.notice is None
        states = home_signals.capability_states(home._app)
        assert states[home_signals.DICTATION].status == home_signals.PAUSED

    def test_unknown_is_not_healthy_and_makes_no_claim(self, monkeypatch):
        app = _app(available_mics=None)
        state = home_signals.dictation_state(app)
        assert state.status == home_signals.UNKNOWN
        assert not state.is_fault                      # no notice
        assert state.status != home_signals.READY      # and no claim of health

    def test_the_notice_does_not_replace_home_or_move_focus(self, page, qapp, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        home = page(_app(available_mics=[]))
        focus_before = qapp.focusWidget()
        home.refresh()
        assert qapp.focusWidget() is focus_before
        assert home._hint_card is not None and len(home.cards) == 3


# ---------------------------------------------------------------------------
# 5b. Recovery from a stopped wake listener (queue 109, Astra F6/F12)
# ---------------------------------------------------------------------------

def _stopped_app(**over):
    """An app whose wake listener died -- the state the user is left in when
    hands-free stops and they cannot type."""
    app = _app(_wake_consumer=_WakeConsumer(running=False), **over)
    app._hands_free_fault = types.SimpleNamespace(
        reason="Hands-free stopped: the microphone stopped feeding it.",
        classification="transient", exception="OSError: device unavailable",
        at=1.0, attempts=0, retrying=False,
        evidence="the wake listener stopped with OSError: device unavailable")
    app.restart_calls = []

    def restart_hands_free():
        app.restart_calls.append(True)
        if app.restart_succeeds:
            app._hands_free_fault = None
            app._wake_consumer = _WakeConsumer(running=True)
        return app.restart_succeeds

    app.restart_succeeds = True
    app.restart_hands_free = restart_hands_free
    return app


class TestStoppedHandsFreeIsRecoverable:
    def test_home_explains_the_stop_and_offers_the_restart(self, page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        home = page(_stopped_app())
        notice = home.notice
        assert notice.capability == home_signals.HANDS_FREE
        assert notice.headline.startswith("Hands-free stopped")
        assert notice.consequence == "The wake word cannot hear you."
        assert home._notice_card.isVisibleTo(home)
        assert home._notice_btn.text() == home_signals.RESTART_HANDS_FREE_LABEL
        assert home._notice_btn.accessibleName() == home._notice_btn.text()

    def test_the_restart_is_reachable_without_a_mouse(self, page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        home = page(_stopped_app())
        assert home._notice_btn in home.tab_chain
        assert home._notice_btn.height() >= home_qt.MIN_TARGET or \
            home._notice_btn.minimumHeight() >= home_qt.MIN_TARGET

    def test_pressing_it_restarts_and_the_notice_goes(self, page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        app = _stopped_app()
        home = page(app)
        home._notice_btn.click()
        assert app.restart_calls == [True]
        assert home.notice is None
        assert not home._notice_card.isVisibleTo(home)

    def test_a_restart_that_fails_leaves_the_explanation_up(self, page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        app = _stopped_app()
        app.restart_succeeds = False
        home = page(app)
        home._notice_btn.click()
        assert app.restart_calls == [True]
        assert home.notice is not None
        assert home._notice_btn.text() == home_signals.RESTART_HANDS_FREE_LABEL

    def test_a_stopped_consumer_is_never_reported_ready(self, monkeypatch):
        """F12 exactly: enabled, not snoozed, microphone present, dead."""
        app = _app(_wake_consumer=_WakeConsumer(running=False))
        assert home_signals.mic_available(app) is True
        state = home_signals.capability_states(app)[home_signals.HANDS_FREE]
        assert state.status == home_signals.UNAVAILABLE
        assert state.detail == "Hands-free is not listening."

    def test_a_microphone_fault_still_offers_voice_help(self, page, monkeypatch):
        """Only a fault with a repair names one; nothing reconnects a mic."""
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        home = page(_app(available_mics=[]))
        assert home.notice.capability == home_signals.DICTATION
        assert home._notice_btn.text() == home_qt.OPEN_VOICE_HELP

    def test_the_restart_appears_in_the_destination_table(self, page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        home = page(_stopped_app())
        table = home.destinations()
        action, _arg = table[home_signals.RESTART_HANDS_FREE_LABEL]
        assert action == home_signals.ACTION_RESTART_HANDS_FREE


# ---------------------------------------------------------------------------
# 5c. The miss diagnostic counts what the writer wrote (queue 109, Astra F11)
# ---------------------------------------------------------------------------

class TestMissDiagnosticReadsTheRealRing:
    def test_the_slot_fills_from_real_command_misses(self, page, monkeypatch):
        """The Hint fixtures elsewhere in this file inject a finished Hint and
        so cannot see a writer/reader schema mismatch. This one starts at the
        ring and ends at the rendered slot."""
        from samsara import outcome_ring as ring_schema

        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: None)
        app = _app()
        for _ in range(home_signals.MISS_MIN):
            app._outcome_ring.append(
                ring_schema.record("MISS", "error", 1.0, "command_miss"))
        home = page(app)
        assert home.hint is not None and home.hint.id == "diag.misses"
        assert "3 of your last 3 commands were not recognised." in home.visible_texts()


# ---------------------------------------------------------------------------
# Voice help
# ---------------------------------------------------------------------------

class TestVoiceHelp:
    def test_it_is_in_the_nav_on_every_screen(self, qapp, monkeypatch):
        monkeypatch.setattr(main_window_qt, "HistoryView", lambda *a, **k: QWidget())
        win = main_window_qt._MainWindow(_app())
        win._poll_timer.stop()
        try:
            assert home_qt.VOICE_HELP in win._nav_btns
            for page_name in ("Home", "History", "Dictionary", home_qt.VOICE_HELP):
                win._activate(page_name)
                assert win._nav_btns[home_qt.VOICE_HELP].isVisibleTo(win)
            assert isinstance(win._panel_cache[home_qt.VOICE_HELP], voice_help_qt.VoiceHelpPage)
        finally:
            win._header_mark.stop()
            win.hide()

    def test_it_answers_what_is_affected_then_evidence(self, help_page, monkeypatch):
        _ready(None, home_signals.UNAVAILABLE, "Ava is offline: the host refused.",
               monkeypatch=monkeypatch)
        help_ = help_page()
        texts = help_.capability_texts()
        assert texts[home_signals.DICTATION] == "Dictation: working"
        assert texts[home_signals.AVA] == "Ava: not working"
        head, evidence = help_._capability_rows[home_signals.DICTATION]
        assert "Because:" in evidence.text()

    def test_the_guided_check_separates_the_four_stages(self, help_page):
        help_ = help_page()
        keys = [key for key, _label in voice_help_qt.CHECK_STAGES]
        assert keys == ["audio", "wake", "speech", "feedback"]
        for key in keys:
            status, evidence = help_.run_stage(key)
            assert status in (home_signals.READY, home_signals.UNKNOWN, home_signals.OFF,
                              home_signals.PAUSED, home_signals.UNAVAILABLE)
            assert evidence.strip()

    def test_no_recent_wake_is_evidence_not_a_diagnosis(self, help_page):
        help_ = help_page()
        status, evidence = help_.run_stage("wake")
        assert status == home_signals.UNKNOWN
        assert "No wake phrase has been detected" in evidence

    def test_pause_is_shown_only_when_it_can_act(self, help_page, qapp):
        help_ = help_page()
        assert help_.pause_button.isVisibleTo(help_)
        help_.pause_button.click()
        assert help_._app.snoozed is True
        assert help_.pause_button.text() == voice_help_qt.RESUME

        dead = help_page(_app(available_mics=[]))
        assert not dead.pause_button.isVisibleTo(dead)

    def test_voice_help_and_home_agree_about_state(self, page, help_page, monkeypatch):
        _ready(None, home_signals.READY, monkeypatch=monkeypatch)
        app = _app(available_mics=[])
        home, help_ = page(app), help_page(app)
        assert home.notice.capability == home_signals.DICTATION
        assert help_.capability_texts()[home_signals.DICTATION] == "Dictation: not working"


# ---------------------------------------------------------------------------
# Retained invariants: honest destinations, type scale, no clipping
# ---------------------------------------------------------------------------

class TestDestinationsAreHonest:
    def test_every_card_label_names_where_it_goes(self, page):
        home = page()
        for title, action, arg, _glyph in home_qt.CARD_ROUTES:
            assert home_qt.label_names_destination(title, action, arg), title
            assert home_qt.card_filter_is_honest(title, action, arg), title

    def test_one_door_into_the_command_list_not_two(self, page):
        """89: "View the command list" and "View window commands" were two
        entry points to the same place. Window commands are a section inside
        the one list now."""
        home = page()
        labels = [c.accessibleName() for c in home.cards]
        assert labels.count("Commands") == 1
        assert "View window commands" not in labels
        assert "View the command list" not in labels
        # The one card opens the WHOLE list: no filter argument.
        assert home.destinations()["Commands"] == ("cheatsheet", None)

    def test_home_keeps_exactly_the_three_destinations(self, page):
        home = page()
        # 111: the labels state PURPOSE. Same three destinations.
        assert [c.accessibleName() for c in home.cards] == [
            "Commands", "Guides & tutorials", "Open Dictionary"]

    def test_every_visible_button_has_its_text_as_its_accessible_name(self, page):
        home = page()
        for button in _buttons(home):
            if button.text():
                assert button.accessibleName() in (button.text(), "") or \
                    button.accessibleName() == button.text(), button.text()


class TestTypeScale:
    def test_home_roles_are_in_the_shared_scale_and_above_the_floor(self):
        for role in ("page title", "tagline", "card title", "card description", "note",
                     "section label"):
            assert role in theme.HOME_TYPE_SCALE, role
            assert theme.HOME_TYPE_SCALE[role] >= theme.TYPE_MIN, role



# ---------------------------------------------------------------------------
# 7. The identity strip: the owner's own elements, restored (89)
# ---------------------------------------------------------------------------

class TestIdentityStripRestored:
    def test_words_today_is_present_with_its_original_label_and_a_live_figure(self, page):
        home = page(_app(history_store=_Store(words=1428)))
        card = home_qt.find_by_accessible_name(home, home_qt.WORDS_TODAY)
        assert card is not None and card.isVisibleTo(home)
        assert home_qt.WORDS_TODAY == "words today"
        # 111: grouped, so the figure is read at a glance rather than counted.
        assert home._words_val.text() == "1,428"

    def test_the_infinity_card_is_present_with_the_infinity_glyph(self, page):
        home = page()
        card = home_qt.find_by_accessible_name(home, home_qt.WORDS_REMAINING)
        assert card is not None and card.isVisibleTo(home)
        assert home_qt.WORDS_REMAINING == "words remaining"
        assert home_qt.INFINITY == chr(0x221E)
        assert home_qt.INFINITY in " ".join(home.visible_texts())

    def test_the_creed_is_present_with_its_original_wording(self, page):
        home = page()
        assert home._creed.isVisibleTo(home)
        assert home._creed.text() == home_qt.CREED
        assert home_qt.CREED_ASCII == "Free - Open source - Accessibility first"
        assert home._creed.accessibleName() == "Creed"

    def test_the_two_stat_cards_are_the_same_size(self, page):
        home = page()
        assert home._words_card.size() == home._infinity_card.size()
        assert home._words_card.height() >= home_qt._px(78)

    def test_the_strip_is_the_last_thing_on_the_page(self, page):
        home = page()
        col = home._content.layout()
        widgets = [col.itemAt(i).widget() for i in range(col.count())]
        visible = [w for w in widgets if w is not None and w.isVisibleTo(home)]
        assert visible[-1] is home._identity_box


class TestIdentityLinks:
    def test_all_three_links_exist_with_their_labels_as_accessible_names(self, page):
        home = page()
        for label in (home_qt.DOCS_LINK, home_qt.GITHUB_LINK, home_qt.KOFI_LINK):
            btn = home._links[label]
            assert btn.isVisibleTo(home), label
            assert btn.text() == label == btn.accessibleName(), label

    def test_they_are_text_links_not_cards(self, page):
        home = page()
        for btn in home._links.values():
            assert btn.isFlat()
            assert btn not in home.cards

    def test_each_opens_the_right_target(self, page, monkeypatch):
        opened = []
        monkeypatch.setattr(home_qt, "open_url", lambda url: opened.append(url) or True)
        home = page()
        for label, url in ((home_qt.DOCS_LINK, home_qt.DOCS_URL),
                           (home_qt.GITHUB_LINK, home_qt.GITHUB_URL),
                           (home_qt.KOFI_LINK, home_qt.KOFI_URL)):
            opened.clear()
            home._links[label].click()
            assert opened == [url], label
            assert home.destinations()[label] == ("url", url)

    def test_they_are_reachable_by_keyboard(self, page):
        home = page()
        for btn in home._links.values():
            assert btn.focusPolicy() == Qt.FocusPolicy.StrongFocus
            assert btn in home.tab_chain
        # ...and in the tab order, after the cards.
        chain = home.tab_chain
        assert chain.index(home._links[home_qt.DOCS_LINK]) > chain.index(home.cards[-1])

    def test_they_keep_the_44_px_target_floor(self, page):
        home = page()
        for label, btn in home._links.items():
            assert btn.height() >= home_qt.MIN_TARGET, label
            assert btn.width() >= home_qt.MIN_TARGET, label

    def test_the_support_line_is_unambiguous_and_does_not_undercut_the_link(self, page):
        home = page()
        # 111 (Astra, owner accepted): "support" is ambiguous beside a
        # help-oriented interface, and "never needed" undercut the Ko-fi link.
        assert home_qt.SUPPORT_LINE == "Always free. Contributions welcome."
        assert home_qt.SUPPORT_LINE in home.visible_texts()

    def test_kofi_is_the_last_link_in_the_strip(self, page):
        home = page()
        chain = home.tab_chain
        assert chain[-1] is home._links[home_qt.KOFI_LINK]

    def test_no_link_label_is_ever_clipped_however_narrow_the_strip(self, page):
        """A QPushButton does not elide: given less than its sizeHint it
        paints a cut-off label. So the strip must wrap the creed block to its
        own row rather than squeeze the links (89)."""
        home = page()
        for width in (760, 700, 640, 560, 480, 420):
            home._relayout_identity(width)
            home._identity_box.resize(width, home._identity_box.height())
            home._identity_box.layout().activate()
            for label, btn in home._links.items():
                assert btn.width() + 1 >= btn.sizeHint().width(), (
                    f"{label} clipped at strip width {width}: "
                    f"has {btn.width()} needs {btn.sizeHint().width()}")

    def test_the_strip_wraps_when_the_links_no_longer_fit_beside_the_cards(self, page):
        """The measurement that decides it is the links' REAL width, not the
        44 px floor a squeezed button would accept."""
        home = page()
        cards = home._words_card.width() + home._infinity_card.width() + 2 * home_qt.GRID
        links = home._links_row.sizeHint().width()
        home._relayout_identity(cards + links + 8)
        assert home._identity_rows == 1
        home._relayout_identity(cards + links - 8)
        assert home._identity_rows == 2


# ---------------------------------------------------------------------------
# 8. The status strip's command example (89, stationary since 111)
# ---------------------------------------------------------------------------

class TestCommandExampleSource:
    """The pool is unchanged by 111. Only the way one phrase is SHOWN
    changed, so these are the 89/95 guarantees re-asserted against the new
    function name -- if 111 had quietly widened the pool, these would say so.
    """

    def test_every_phrase_comes_from_the_catalog_never_a_hand_written_list(self):
        records = command_catalog.load_catalog_json()
        assert records
        phrases = command_marquee.example_phrases(records, rng=random.Random(1))
        catalog = {command_catalog.canonical_phrase(r) for r in records}
        assert phrases and set(phrases) <= catalog

    def test_it_never_offers_a_destructive_or_argument_hungry_command(self):
        records = command_catalog.load_catalog_json()
        phrases = set(command_marquee.example_phrases(records, count=500,
                                                      rng=random.Random(2)))
        for record in records:
            if command_catalog.canonical_phrase(record) not in phrases:
                continue
            assert record.get("risk") in command_marquee.SAFE_RISKS
            assert not record.get("whole_utterance")
            assert not any(a.get("required") for a in record.get("args", [])
                           if isinstance(a, dict))

    def test_a_command_scoped_to_another_app_is_not_offered(self):
        """Queue 68 scopes: an app-scoped command is only live on a match."""
        records = [
            {"canonical_id": "p.global_one", "phrase": "scroll down", "risk": "ui",
             "aliases": [], "args": [], "scope": None},
            {"canonical_id": "p.obsidian_one", "phrase": "next note", "risk": "ui",
             "aliases": [], "args": [], "scope": {"apps": ["obsidian.exe"]}},
        ]
        here = command_scope.MatchContext.for_app("warp.exe")
        phrases = command_marquee.example_phrases(records, ctx=here, rng=random.Random(3))
        assert "scroll down" in phrases
        assert "next note" not in phrases
        there = command_scope.MatchContext.for_app("obsidian.exe")
        assert "next note" in command_marquee.example_phrases(records, ctx=there,
                                                              rng=random.Random(3))


class TestTheExampleIsStationaryAndComplete:
    """111. It scrolled until now. Partially-visible moving text is the wrong
    default for someone who has to read an example long enough to say it out
    loud, and a marquee has no "previous"."""

    LONG = "x" * 200

    def _strip(self, qapp, width=520, records=None, seed=11):
        if records is None:
            records = command_catalog.load_catalog_json()
        strip = command_marquee.CommandExampleStrip(records, rng=random.Random(seed))
        strip.resize(width, 44)
        strip.show()
        qapp.processEvents()
        return strip

    def test_no_timer_is_started_anywhere(self, qapp):
        """The headline of the change: nothing moves. Not "a timer that does
        not fire" -- no timer at all, so there is nothing left running when
        the hub goes back to the tray."""
        strip = self._strip(qapp)
        assert strip.findChildren(QTimer) == []
        source = Path(command_marquee.__file__).read_text(encoding="utf-8")
        assert "QTimer" not in source
        assert "reduced_motion" not in source, (
            "the reduced-motion branch must be deleted, not left dead")
        assert "_system_reduced_motion" not in source

    def test_it_shows_exactly_one_complete_example(self, qapp):
        strip = self._strip(qapp)
        assert strip.text.startswith(command_marquee.PREFIX)
        assert strip.text.count('"') == 2, "one phrase, not a list"
        assert strip.text == command_marquee.example_text(strip.phrase)

    @pytest.mark.parametrize("width", [520, 700, 900])
    def test_the_example_is_never_elided_at_any_strip_width(self, qapp, width):
        strip = self._strip(qapp, width=width)
        qapp.processEvents()
        assert chr(0x2026) not in strip.text, "an ellipsis means it was cut"
        metrics = QFontMetrics(strip.label.font())
        assert metrics.horizontalAdvance(strip.text) <= strip.label.width() + 1

    def test_a_strip_too_narrow_for_anything_shows_nothing_not_a_fragment(self, qapp):
        """Half a command cannot be spoken, and the user cannot see that it is
        half. Nothing is the honest render."""
        strip = self._strip(qapp, width=2 * command_marquee.MIN_TARGET + 14)
        qapp.processEvents()
        assert strip.text == ""

    def test_both_arrows_step_the_example(self, qapp):
        strip = self._strip(qapp)
        first = strip.text
        strip.next_button.click()
        qapp.processEvents()
        second = strip.text
        assert second and second != first
        strip.prev_button.click()
        qapp.processEvents()
        assert strip.text == first, "back is back"

    def test_stepping_walks_the_pool_and_wraps(self, qapp):
        strip = self._strip(qapp)
        seen = {strip.text}
        for _ in range(len(strip.phrases) * 2):
            strip.next()
            seen.add(strip.text)
        assert len(seen) > 1
        assert all(t.count('"') == 2 for t in seen)

    def test_a_step_skips_an_example_too_long_for_the_strip(self, qapp):
        """"Never clipped" in action: stepping does not stop on a phrase that
        would not fit, it walks past it."""
        records = [
            {"canonical_id": "p.short", "phrase": "go back", "risk": "ui",
             "aliases": [], "args": [], "scope": None},
            {"canonical_id": "p.long", "phrase": self.LONG, "risk": "ui",
             "aliases": [], "args": [], "scope": None},
            {"canonical_id": "p.short2", "phrase": "scroll down", "risk": "ui",
             "aliases": [], "args": [], "scope": None},
        ]
        strip = self._strip(qapp, width=420, records=records)
        assert self.LONG in strip.phrases, "it is in the pool"
        for _ in range(6):
            assert self.LONG not in strip.text, "and it is never shown"
            strip.next()

    def test_the_arrows_are_44px_targets_with_spoken_names(self, qapp):
        strip = self._strip(qapp)
        for btn, name in ((strip.prev_button, command_marquee.PREV_NAME),
                          (strip.next_button, command_marquee.NEXT_NAME)):
            assert btn.width() >= 44 and btn.height() >= 44
            assert btn.accessibleName() == name
        assert command_marquee.PREV_NAME == "previous example"
        assert command_marquee.NEXT_NAME == "next example"

    def test_the_arrows_are_keyboard_reachable_and_the_strip_is_not(self, qapp):
        strip = self._strip(qapp)
        assert strip.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert strip.label.focusPolicy() == Qt.FocusPolicy.NoFocus
        for btn in (strip.prev_button, strip.next_button):
            assert btn.focusPolicy() == Qt.FocusPolicy.StrongFocus
            btn.setFocus()
            qapp.processEvents()
            assert btn.hasFocus()

    def test_stepping_never_steals_focus_and_never_renames_the_strip(self, qapp):
        """The example label's accessible name follows the phrase, so a reader
        that navigates to it reads the current one. The STRIP's name never
        changes, so stepping does not announce a new sentence over the user."""
        strip = self._strip(qapp)
        elsewhere = QPushButton("elsewhere")
        elsewhere.show()
        elsewhere.setFocus()
        qapp.processEvents()

        names = set()
        for _ in range(5):
            strip.next()
            names.add(strip.label.accessibleName())
            assert strip.label.accessibleName() == strip.label.text()
        assert strip.accessibleName() == command_marquee.ACCESSIBLE_NAME
        assert len(names) > 1
        assert elsewhere.hasFocus(), "stepping must not pull focus"
        elsewhere.close()

    def test_one_example_hides_the_arrows_rather_than_offering_a_dead_step(self, qapp):
        records = [{"canonical_id": "p.one", "phrase": "go back", "risk": "ui",
                    "aliases": [], "args": [], "scope": None}]
        strip = self._strip(qapp, records=records)
        assert strip.phrases == ["go back"]
        assert not strip.prev_button.isVisible()
        assert not strip.next_button.isVisible()

    def test_an_unreadable_catalog_shows_nothing_rather_than_inventing_tips(self, qapp):
        strip = self._strip(qapp, records=[])
        assert strip.phrases == [] and strip.text == ""
        strip.next()
        strip.previous()
        assert strip.text == ""

    def test_it_lives_in_the_status_strip_and_is_not_the_home_hint_slot(self, qapp, monkeypatch):
        win = _hub(qapp, monkeypatch)
        assert isinstance(win._example_strip, command_marquee.CommandExampleStrip)
        assert win._example_strip.parent() is not None
        home = win._panel_cache["Home"]
        assert win._example_strip is not getattr(home, "_hint_card", None)
        assert not win._example_strip.isAncestorOf(home)


# ---------------------------------------------------------------------------
# 9. Home fits the default window (89)
# ---------------------------------------------------------------------------

HINT = home_signals.Hint(
    "diag.no_text", home_signals.TIER_DIAGNOSTIC,
    "4 of your last 20 recordings produced no text.",
    "Open Voice help", "page", "Voice help",
    evidence="4/20 recent captures recorded outcome empty or gated (threshold 3)")


class TestCommandOutcomeRendering:
    """Queue 154: the live chip may be short; Home must not be."""

    @pytest.mark.parametrize(("spoken", "expected"), [
        ("switch to", "focus"),
        ("switch audio to", "switch audio to"),
    ])
    def test_catalog_id_renders_full_canonical_command(self, spoken, expected):
        from samsara import outcome_ring as ring_schema
        from samsara.session_modes import outcome_chip

        label, kind = outcome_chip("command_executed", {"phrase": spoken})
        record = ring_schema.record(label, kind, 1.0, "command_executed")

        assert record.canonical_id
        assert home_qt.render_outcome(_app(), record) == (
            home_qt.KIND_RAN, home_qt.quoted(expected))

    def test_missing_catalog_id_keeps_the_stored_human_label(self):
        from samsara import outcome_ring as ring_schema
        from samsara.session_modes import CHIP_CHECK

        record = ring_schema.record(
            f"{CHIP_CHECK} legacy macro", "success", 1.0,
            "command_executed", "macros.removed_command")

        assert home_qt.render_outcome(_app(), record) == (
            home_qt.KIND_RAN, home_qt.quoted("legacy macro"))


class TestHomeFitsTheDefaultWindow:
    """The owner's constraint (89): DEFAULT_WIDTH x DEFAULT_HEIGHT stays
    900x650 and Home's content has to fit the 548 px viewport it leaves.
    Scrolling is a safety net, never the plan."""

    @pytest.mark.parametrize("slot", ["empty", "diagnostic", "whatsnew"])
    def test_home_does_not_scroll_at_the_default_window_size(self, qapp, monkeypatch, slot):
        if slot == "diagnostic":
            monkeypatch.setattr(home_signals, "diagnostic_hints", lambda a: [HINT])
        else:
            monkeypatch.setattr(home_signals, "diagnostic_hints", lambda a: [])
        if slot == "empty":
            monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: None)
        else:
            monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: ("9.9.9", "New."))
        win = _hub(qapp, monkeypatch, width=main_window_qt.DEFAULT_WIDTH,
                   height=main_window_qt.DEFAULT_HEIGHT)
        home = win._panel_cache["Home"]
        bar = home._scroll.verticalScrollBar()
        assert bar.maximum() == 0, (
            f"Home scrolls at the default window size with the slot {slot}: "
            f"{bar.maximum()} px over a {home._scroll.viewport().height()} px viewport")
        assert home_qt.overflowing_widgets(home) == []

    def test_the_three_destination_cards_sit_on_one_row(self, qapp, monkeypatch):
        win = _hub(qapp, monkeypatch, width=main_window_qt.DEFAULT_WIDTH,
                   height=main_window_qt.DEFAULT_HEIGHT)
        home = win._panel_cache["Home"]
        assert home._cards_cols == 3

    def test_home_has_no_samsara_heading_of_its_own(self, page):
        home = page()
        assert home_qt.TITLE not in home.visible_texts()


_SCALE_SCRIPT = r'''
import json, sys, types, collections
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication, QWidget
app = QApplication([])
if sys.argv[2] != "1":
    f = app.font(); f.setPointSizeF(f.pointSizeF() * float(sys.argv[2])); app.setFont(f)
from samsara.ui import main_window_qt, home_qt
main_window_qt.HistoryView = lambda *a, **k: QWidget()
fake = types.SimpleNamespace(
    config={'mode': 'hold', 'hotkey': 'ctrl+shift', 'microphone': 7, 'wake_word_enabled': True,
            'wake_word_config': {'phrase': 'jarvis'}, 'command_mode': {'mode': 'toggle'},
            'ollama': {'enabled': True}},
    available_mics=[], recording=False, snoozed=False, wake_word_active=True,
    continuous_active=False, command_mode_active=False, toggle_active=False,
    audio_coordinator=object(), _outcome_ring=collections.deque(maxlen=8))
win = main_window_qt._MainWindow(fake)
win.resize(900, 650)
win.show()
for _ in range(40):
    app.processEvents()
home = win._panel_cache["Home"]
# Measure Home while it is the CURRENT page: a hidden page keeps stale
# geometry, which would make this gate lie.
home_metrics = {
    "home_overflow": home_qt.overflowing_widgets(home),
    "content_w": home.content_widget.width(),
    "viewport_w": home._scroll.viewport().width(),
}
win._activate("Voice help")
for _ in range(20):
    app.processEvents()
help_page = win._panel_cache["Voice help"]
out = {
    "help_overflow": home_qt.overflowing_widgets(help_page),
    **home_metrics,
    "scale": home_qt.text_scale(),
    "buttons_under_44": [b.text() for b in home.findChildren(main_window_qt.QPushButton)
                         if b.isVisibleTo(home) and (b.height() < 44 or b.width() < 44)],
}
print("RESULT " + json.dumps(out))
'''


#: ("1", "1.5") is the case 89 caught by rendering: text at 150% in a window
#: that did NOT grow with it. The identity strip kept the creed beside the
#: cards because it measured the links row at its minimumSizeHint (three
#: 44 px targets), so the layout squeezed the three buttons and clipped their
#: labels to "Website and d(", "GitH", "Ko-f". QT_SCALE_FACTOR alone never
#: reproduced it -- that grows the window too.
@pytest.mark.parametrize("scale_factor,font_factor",
                         [("1", "1"), ("1.25", "1"), ("1.5", "1"), ("1", "1.2"),
                          ("1.5", "1.2"), ("1", "1.5")])
def test_nothing_clips_at_windows_scaling_and_larger_text(tmp_path, scale_factor, font_factor):
    script = tmp_path / "scale_check.py"
    script.write_text(_SCALE_SCRIPT, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale_factor,
               PYTHONIOENCODING="utf-8")
    proc = subprocess.run([PY, str(script), str(REPO), font_factor], env=env,
                          capture_output=True, text=True, timeout=110, cwd=str(REPO))
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")]
    assert line, proc.stdout[-2000:] + proc.stderr[-2000:]
    result = json.loads(line[-1][len("RESULT "):])
    assert result["home_overflow"] == [], result
    assert result["help_overflow"] == [], result
    assert result["content_w"] <= result["viewport_w"], result
    assert result["buttons_under_44"] == [], result
    if font_factor != "1":
        assert result["scale"] > 1.1


# ---------------------------------------------------------------------------
# Queue 95: the redundancy is gone and the space it freed is used.
# ---------------------------------------------------------------------------

class TestTheNameIsSaidOnce:
    """101 corrects 95: the app says its name once INSIDE the window, and the
    one place is the shared header band. 95 read its brief as "delete the
    band's wordmark"; the duplicate the owner meant was Home's own body
    heading, which 89 had already taken. The band keeps the word; Home's
    body still does not get to repeat it."""

    def test_the_header_band_carries_the_wordmark(self, qapp, monkeypatch):
        win = _hub(qapp, monkeypatch)
        title = win.findChild(main_window_qt.QLabel, "hubWordmark")
        assert title is not None and title.text() == home_qt.TITLE
        header = win._header_mark.parentWidget()
        named = [label for label in header.findChildren(main_window_qt.QLabel)
                 if label.text() == home_qt.TITLE]
        assert named == [title], "exactly one Samsara in the band"

    def test_the_mark_is_still_there_and_still_leads_the_band(self, qapp, monkeypatch):
        win = _hub(qapp, monkeypatch)
        header = win._header_mark.parentWidget()
        layout = header.layout()
        assert layout.itemAt(0).widget() is win._header_mark
        assert win._header_mark.isVisibleTo(win)
        assert win._header_mark.width() == main_window_qt.HEADER_MARK_PX
        # 101: the word follows the mark across a 12 px gap, in that order.
        assert layout.itemAt(1).spacerItem() is not None
        assert layout.itemAt(2).widget().objectName() == "hubWordmark"

    def test_home_body_carries_no_second_title_either(self, page):
        """The body never gets to say it: the tagline slot is the top of the
        page. (The what's-new note says "Samsara <version>", which is a
        release line, not a title -- an exact-match check leaves it alone.)"""
        home = page()
        titles = [label.text() for label in home.findChildren(main_window_qt.QLabel)
                  if label.isVisibleTo(home) and label.text() == home_qt.TITLE]
        assert titles == []


class TestOneDoorToTheCommandList:
    """95, finishing 89: one card, one name, one destination -- and window
    commands live inside that destination rather than beside it."""

    def test_exactly_one_card_routes_to_the_command_list(self):
        app = _app()
        cards = home_qt.capability_cards(app, home_qt.catalog_records(app))
        sheet_cards = [c for c in cards if c["action"] == "cheatsheet"]
        assert len(sheet_cards) == 1
        assert sheet_cards[0]["title"] == home_qt.COMMAND_LIST_LABEL == "Commands"
        # Unfiltered: the one door opens the whole list, window commands included.
        assert not sheet_cards[0]["arg"]

    def test_the_hint_slot_calls_that_door_by_the_same_name(self):
        """The signals still ask for it as "View window commands" (which has
        applied no window filter since 89) and "View the command list". Home
        paints one name, so the page never offers two doors again."""
        for asked in ("View window commands", "View the command list", ""):
            assert home_qt.slot_action_label(asked, "cheatsheet", "") == "Commands"
            assert home_qt.slot_action_label(asked, "cheatsheet", None) == "Commands"
        # A genuinely filtered cheat-sheet action keeps its own name, which
        # card_filter_is_honest already requires to name the filter.
        assert home_qt.slot_action_label("View window commands", "cheatsheet", "window") \
            == "View window commands"
        assert home_qt.slot_action_label("Open Dictionary", "page", "Dictionary") == "Open Dictionary"

    def test_the_slot_button_and_the_destination_table_agree(self, page, monkeypatch):
        hint = home_signals.Hint(
            id="diag.misses", tier=home_signals.TIER_DIAGNOSTIC,
            text="9 of your last 20 commands were not recognised.",
            action_label="View window commands", action="cheatsheet", arg="",
            evidence="9/20 recent outcomes were misses (threshold 3)")
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [hint])
        home = page()
        home.refresh()
        assert home._hint_action_btn.text() == "Commands"
        assert home._hint_action_btn.accessibleName() == "Commands"
        # The card and the slot button now collapse onto ONE key in the
        # destination table, which is the point: same name, same door, and
        # neither carries a filter.
        table = home.destinations()
        action, arg = table["Commands"]
        assert action == "cheatsheet" and not arg
        assert "View window commands" not in table
        assert "View the command list" not in table

    def test_window_commands_are_inside_the_list_this_door_opens(self):
        """The fold-in, checked against the catalog the sheet renders: the
        window-management commands are records in it, so an unfiltered open
        shows them. They are not a separate surface any more."""
        records = command_catalog.load_catalog_json()
        window_records = [r for r in records if r.get("pack") == "window-management"]
        assert len(window_records) > 20
        # The sheet groups by plugin, so they are reachable as groups too.
        assert {r.get("plugin") for r in window_records} >= {"windows", "window_switcher"}


class TestTheStatsSitOnTheFloor:
    """95: 89 top-aligned Home's column, leaving a dead band under the stats.
    The stretch moved above the identity strip, so the strip sits on the floor
    of the window and the spare height opens between the cards and the stats."""

    def _column(self, qapp, monkeypatch, width, height, app=None):
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
        monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: None)
        win = _hub(qapp, monkeypatch, width=width, height=height, app=app)
        home = win._panel_cache["Home"]
        home.refresh()
        for _ in range(60):
            qapp.processEvents()
        return home

    @pytest.mark.parametrize("width,height", [(900, 650), (1065, 906)])
    def test_the_identity_strip_ends_on_the_floor_of_the_column(
            self, qapp, monkeypatch, width, height):
        home = self._column(qapp, monkeypatch, width, height)
        strip = home._identity
        column = home._content
        assert strip.y() + strip.height() == column.height(), "dead band under the stats"

    def test_the_stretch_is_above_the_strip_not_below_it(self, qapp, monkeypatch):
        from PySide6.QtWidgets import QSpacerItem

        home = self._column(qapp, monkeypatch, 1065, 906)
        column = home._content.layout()
        items = [column.itemAt(i) for i in range(column.count())]
        stretch_at = [i for i, it in enumerate(items) if isinstance(it, QSpacerItem)]
        strip_at = [i for i, it in enumerate(items)
                    if it.widget() is home._identity]
        assert stretch_at and strip_at
        assert max(stretch_at) < strip_at[0], "the stretch must push the strip down, not follow it"
        # And it really is carrying height at this size.
        assert items[max(stretch_at)].geometry().height() > 0

    def test_nothing_the_owner_asked_to_keep_moved_out_of_the_strip(self, page):
        """The band change is a layout change only: words today, the infinity
        card and the creed are still the strip's contents, still its words."""
        home = page()
        texts = [label.text() for label in home.findChildren(main_window_qt.QLabel)]
        assert home_qt.WORDS_TODAY in texts
        assert home_qt.WORDS_REMAINING in texts
        assert home_qt.INFINITY in texts
        assert home_qt.CREED in texts


class TestTheExamplePoolOffersNoHardware:
    """95: "print me a gun" is a real FlashForge command, honestly classed
    "ui" and needing no argument, so every existing filter passed it. What it
    does is start a physical machine in the owner's room."""

    def test_no_phrase_comes_from_a_hardware_pack(self):
        records = command_catalog.load_catalog_json()
        offered = set(command_marquee.example_phrases(records, count=1000,
                                                      rng=random.Random(11)))
        assert offered, "the pool must not be empty"
        for record in records:
            if command_catalog.canonical_phrase(record) in offered:
                assert record.get("pack") not in command_marquee.HARDWARE_PACKS, \
                    record.get("canonical_id")

    def test_the_printer_commands_are_gone_by_pack_not_by_phrase(self):
        """Excluded by the catalog's own grouping, so a second printer plugin
        is excluded the day it lands. Nothing here matches on the words."""
        records = command_catalog.load_catalog_json()
        printer = [r for r in records if r.get("plugin") == "flashforge_printer"]
        assert printer, "the catalog must still carry the printer plugin"
        assert {r.get("pack") for r in printer} == {"3d-printing"}
        offered = set(command_marquee.example_phrases(records, count=1000,
                                                      rng=random.Random(12)))
        assert not offered & {command_catalog.canonical_phrase(r) for r in printer}
        assert "print me a gun" not in offered

    def test_a_new_hardware_plugin_is_excluded_without_touching_this_code(self):
        records = [
            {"canonical_id": "p.safe", "phrase": "scroll down", "risk": "ui",
             "pack": "utilities", "aliases": [], "args": [], "scope": None},
            {"canonical_id": "p.kiln", "phrase": "fire the kiln", "risk": "ui",
             "pack": "3d-printing", "aliases": [], "args": [], "scope": None},
            {"canonical_id": "p.lamp", "phrase": "warm white", "risk": "ui",
             "pack": "smart-home", "aliases": [], "args": [], "scope": None},
        ]
        offered = command_marquee.example_phrases(records, count=10, rng=random.Random(13))
        assert offered == ["scroll down"]

    def test_software_playback_is_not_hardware_and_stays(self):
        """The rule is "drives a physical device", not "touches media": the
        pool must not quietly lose everything fun."""
        records = command_catalog.load_catalog_json()
        offered = set(command_marquee.example_phrases(records, count=1000,
                                                      rng=random.Random(14)))
        media = {command_catalog.canonical_phrase(r) for r in records
                 if r.get("pack") in ("media", "stremio")
                 and r.get("risk") in command_marquee.SAFE_RISKS
                 and not any(a.get("required") for a in r.get("args", []) if isinstance(a, dict))}
        assert media & offered, "media playback phrases should still be offered"
        assert len(offered) > 250, "the pool must stay large enough to feel random"


# ---------------------------------------------------------------------------
# 111. Plain language, a readable example, honest labels
# ---------------------------------------------------------------------------

#: Everything that made the old evidence line debug output. If any of this
#: reaches the visible slot again, the copy has regressed.
DEBUG_VOCABULARY = ("threshold", "Because:", "outcome", "empty or gated",
                    "gated", "/20", "/8")

NO_TEXT_HINT = home_signals.Hint(
    "diag.no_text", home_signals.TIER_DIAGNOSTIC,
    "4 of your last 8 recordings produced no text.",
    "Open Voice help", "page", "Voice help",
    evidence="4/8 recent captures recorded outcome empty or gated (threshold 3)")


class TestTheSuggestionSpeaksPlainly:
    """111.1. The slot used to print its own working: "Because: 4/8 recent
    captures recorded outcome empty or gated (threshold 3)". That names
    internal outcome kinds, cites a threshold nobody set, and says nothing to
    DO about any of it."""

    def _home(self, page, monkeypatch, hint=NO_TEXT_HINT):
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [hint])
        home = page()
        home.refresh()
        return home

    def test_the_visible_slot_carries_no_counts_thresholds_or_outcome_kinds(
            self, page, monkeypatch):
        home = self._home(page, monkeypatch)
        visible = " ".join(t for t in home.visible_texts())
        for token in DEBUG_VOCABULARY:
            assert token not in visible, f"{token!r} is debug output, not copy"

    def test_the_plain_first_sentence_is_kept_verbatim(self, page, monkeypatch):
        home = self._home(page, monkeypatch)
        assert home._hint_text.text() == NO_TEXT_HINT.text

    def test_the_second_line_is_an_action_not_evidence(self, page, monkeypatch):
        home = self._home(page, monkeypatch)
        assert home._hint_sub.text() == (
            "If you were speaking, open Voice troubleshooting to investigate.")
        assert home._hint_sub.isVisibleTo(home)

    def test_the_wording_stays_conditional(self, page, monkeypatch):
        """The counter combines the `empty` and `gated` outcome kinds, so it
        does NOT establish that intended speech was lost -- a gated capture
        may be a cough or a door. The sentence may not assert it did."""
        home = self._home(page, monkeypatch)
        sentence = home._hint_sub.text()
        assert sentence.startswith("If "), sentence
        for asserted in ("was lost", "did not hear you", "failed to",
                         "your speech was", "Samsara missed"):
            assert asserted not in sentence
        # Every diagnostic that infers user intent is conditional, not just
        # this one.
        for hint_id in ("diag.no_text", "diag.misses"):
            assert home_qt.HINT_ACTIONS[hint_id].startswith("If ")

    def test_the_evidence_is_present_but_disclosed(self, page, monkeypatch):
        home = self._home(page, monkeypatch)
        assert home._hint_evidence.text() == NO_TEXT_HINT.evidence
        assert "threshold 3" in home._hint_evidence.text()

    def test_details_is_collapsed_by_default(self, page, monkeypatch):
        home = self._home(page, monkeypatch)
        assert home._hint_details_btn.isVisibleTo(home)
        assert home._hint_details_btn.isChecked() is False
        assert not home._hint_evidence.isVisibleTo(home)

    def test_details_is_keyboard_reachable_and_opens_on_activation(
            self, qapp, page, monkeypatch):
        home = self._home(page, monkeypatch)
        btn = home._hint_details_btn
        assert btn.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert btn.height() >= home_qt.MIN_TARGET
        assert btn.text() == btn.accessibleName() == home_qt.DETAILS
        assert btn in home.tab_chain
        btn.setFocus()
        qapp.processEvents()
        assert btn.hasFocus()
        btn.click()
        qapp.processEvents()
        assert home._hint_evidence.isVisibleTo(home)
        assert "threshold 3" in home._hint_evidence.text()
        btn.click()
        qapp.processEvents()
        assert not home._hint_evidence.isVisibleTo(home)

    def test_details_re_collapses_when_the_suggestion_changes(
            self, qapp, page, monkeypatch):
        """A user who opened one suggestion's working has not asked to see
        the next one's."""
        other = home_signals.Hint(
            "diag.misses", home_signals.TIER_DIAGNOSTIC,
            "9 of your last 20 commands were not recognised.",
            "View window commands", "cheatsheet", "",
            evidence="9/20 recent outcomes were misses (threshold 3)")
        monkeypatch.setattr(home_signals, "diagnostic_hints",
                            lambda app: [NO_TEXT_HINT, other])
        home = page()
        home.refresh()
        home._hint_details_btn.click()
        qapp.processEvents()
        assert home._hint_evidence.isVisibleTo(home)
        home._on_another_hint()
        qapp.processEvents()
        assert home._hint_details_btn.isChecked() is False
        assert not home._hint_evidence.isVisibleTo(home)

    def test_a_whats_new_note_has_no_details_button(self, page, monkeypatch):
        """A release note has no working to show."""
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
        monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: ("9.9.9", "New."))
        home = page()
        home.refresh()
        assert home._hint_sub.text() == "New."
        assert not home._hint_details_btn.isVisibleTo(home)


class TestTheCardLabelsStatePurpose:
    """111.3. "View commands", "Show guides & help" and "Voice help"
    overlapped: a new user could not tell which held command instructions and
    which diagnosed recognition problems."""

    def test_the_labels_are_the_new_strings(self, page):
        home = page()
        assert [c.accessibleName() for c in home.cards] == [
            "Commands", "Guides & tutorials", "Open Dictionary"]

    def test_the_destinations_are_unchanged(self, page):
        """Only the words changed. Every route is the one it was."""
        home = page()
        table = home.destinations()
        assert table["Commands"] == ("cheatsheet", None)
        assert table["Guides & tutorials"] == ("guides", None)
        assert table["Open Dictionary"] == ("page", "Dictionary")
        assert table[home_qt.OPEN_VOICE_HELP] == ("page", home_qt.VOICE_HELP)

    def test_voice_help_is_named_for_what_it_does(self, page):
        home = page()
        assert home_qt.OPEN_VOICE_HELP == "Voice troubleshooting"
        # The page KEY is untouched, so every existing route still resolves --
        # including home_signals.py's hard-coded arg="Voice help".
        assert home_qt.VOICE_HELP == "Voice help"
        assert home._notice_btn.text() == "Voice troubleshooting"

    def test_the_slot_calls_voice_help_by_the_same_name_as_the_notice(self):
        """home_signals still asks for "Open Voice help" (its file belongs to
        queue 109). Home paints one name so the slot and the notice cannot
        disagree."""
        assert home_qt.slot_action_label(
            "Open Voice help", "page", "Voice help") == "Voice troubleshooting"

    def test_every_new_label_is_still_honest_about_its_route(self, page):
        for title, action, arg, _glyph in home_qt.CARD_ROUTES:
            assert home_qt.label_names_destination(title, action, arg), title
            assert home_qt.card_filter_is_honest(title, action, arg), title

    def test_a_label_that_promises_the_wrong_action_is_still_rejected(self):
        """The rule 86 wrote this for: "Ask Ava" opened Ava SETTINGS. A bare
        noun promises nothing and is fine; a WRONG verb never is."""
        assert not home_qt.label_names_destination("View Dictionary", "page", "Dictionary")
        assert not home_qt.label_names_destination("Show commands", "cheatsheet", None)
        assert not home_qt.label_names_destination("Open commands", "cheatsheet", None)
        assert not home_qt.label_names_destination("", "cheatsheet", None)
        # A bare noun, and the right verb, both pass.
        assert home_qt.label_names_destination("Commands", "cheatsheet", None)
        assert home_qt.label_names_destination("View commands", "cheatsheet", None)
        # A page card must still name its page, noun or not.
        assert home_qt.label_names_destination("Dictionary", "page", "Dictionary")
        assert not home_qt.label_names_destination("Words", "page", "Dictionary")


class TestFiguresAndExamples:
    """111.4 and 111.5."""

    @pytest.mark.parametrize("value,expected", [
        (0, "0"), (999, "999"),
        (1428, "1,428"),            # four digits
        (10905, "10,905"),          # five -- the owner's own figure
        (487213, "487,213"),        # six
        (1234567, "1,234,567"),
    ])
    def test_the_word_count_renders_with_a_separator(self, value, expected):
        assert home_qt.format_count(value) == expected

    @pytest.mark.parametrize("digits,words", [(4, 1428), (5, 10905), (6, 487213)])
    def test_home_paints_the_separated_figure(self, page, digits, words):
        home = page(_app(history_store=_Store(words=words)))
        painted = home._words_val.text()
        assert painted == home_qt.format_count(words)
        assert "," in painted
        assert len(str(words)) == digits

    def test_a_bad_figure_is_never_a_crash(self):
        assert home_qt.format_count(None) == "None"
        assert home_qt.format_count("many") == "many"

    def test_the_commands_card_shows_a_real_command_not_a_count(self):
        app = _app()
        records = home_qt.catalog_records(app)
        cards = home_qt.capability_cards(app, records)
        commands = [c for c in cards if c["action"] == "cheatsheet"][0]
        example = home_qt.commands_card_example(records)
        assert example
        assert commands["value"] == home_qt.quoted(example)
        assert "commands" not in commands["value"]

    def test_the_example_is_sayable_not_merely_short(self):
        """Shortest alone gave "yes", which is a reply, not a command anybody
        would think to try."""
        example = home_qt.commands_card_example(home_qt.catalog_records(_app()))
        assert len(example.split()) >= 2, example

    def test_the_example_comes_from_the_same_safe_pool_as_the_status_strip(self):
        records = home_qt.catalog_records(_app())
        example = home_qt.commands_card_example(records)
        safe = set(command_marquee.example_phrases(records, count=10000))
        assert example in safe

    def test_the_breadth_figure_moved_into_the_description(self):
        app = _app()
        records = home_qt.catalog_records(app)
        cards = home_qt.capability_cards(app, records)
        commands = [c for c in cards if c["action"] == "cheatsheet"][0]
        assert home_qt.format_count(len(records)) in commands["description"]
        assert "phrases" in commands["description"]

    def test_an_unreadable_catalog_still_says_something_true(self):
        app = _app()
        cards = home_qt.capability_cards(app, None)
        commands = [c for c in cards if c["action"] == "cheatsheet"][0]
        assert commands["value"] == home_qt._CATALOG_UNAVAILABLE_SHORT
        assert "Every phrase it knows" in commands["description"]


class TestTheExampleStripInTheHub:
    """111.2, measured in the real status bar at both window sizes."""

    @pytest.mark.parametrize("size", [(900, 650), (1065, 906)])
    def test_one_complete_example_never_elided(self, qapp, monkeypatch, size):
        win = _hub(qapp, monkeypatch, width=size[0], height=size[1])
        strip = win._example_strip
        assert strip.text, "the strip has something to say"
        assert strip.text.count('"') == 2
        assert chr(0x2026) not in strip.text
        metrics = QFontMetrics(strip.label.font())
        assert metrics.horizontalAdvance(strip.text) <= strip.label.width() + 1

    @pytest.mark.parametrize("size", [(900, 650), (1065, 906)])
    def test_the_longest_phrase_in_the_catalog_would_not_clip_the_strip(
            self, qapp, monkeypatch, size):
        """The worst case, forced: if even this fits, nothing in the pool
        can clip."""
        win = _hub(qapp, monkeypatch, width=size[0], height=size[1])
        strip = win._example_strip
        whole = command_marquee.example_phrases(
            command_catalog.load_catalog_json(), count=10000)
        longest = max(whole, key=len)
        strip._phrases = [longest] + [p for p in strip.phrases if p != longest]
        strip._apply(0, step=1)
        qapp.processEvents()
        metrics = QFontMetrics(strip.label.font())
        assert strip.text in ("", command_marquee.example_text(longest))
        if strip.text:
            assert metrics.horizontalAdvance(strip.text) <= strip.label.width() + 1

    def test_the_arrows_step_it_in_the_real_strip(self, qapp, monkeypatch):
        win = _hub(qapp, monkeypatch)
        strip = win._example_strip
        first = strip.text
        strip.next_button.click()
        qapp.processEvents()
        assert strip.text != first
        strip.prev_button.click()
        qapp.processEvents()
        assert strip.text == first
