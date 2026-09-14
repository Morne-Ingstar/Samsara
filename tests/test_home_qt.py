"""Queue 26: the Home page (samsara/ui/home_qt.py) and its wiring into the
hub window. Queue 41: type scale, plain-language state row, one primary
control, card descriptions, the outcome row, no Undo on Home.

Widgets are built directly against the session `qapp` fixture (the
precedent of test_main_window_qt.py); qt_runtime is not exercised. The
scaling gate runs the page in a subprocess with QT_SCALE_FACTOR / a larger
default font on the offscreen platform, because a scale factor cannot be
changed once a QApplication exists.
"""

import collections
import datetime as dt
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QAbstractButton, QPushButton, QWidget

from samsara import command_catalog
from samsara.session_modes import CHIP_CHECK, CHIP_CROSS
from samsara.ui import home_qt, main_window_qt, theme

PY = sys.executable
REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Store:
    """history_store stand-in: a fixed newest dictation row."""

    def __init__(self, text="hello world", words=0):
        self.text = text
        self.words = words

    def query(self, search=None, type_filter=None, limit=200, **kw):
        if type_filter == 'dictation' and self.text:
            return [{'display_text': self.text}]
        return []

    def words_typed_since(self, since_iso):
        return self.words


def _app(**over):
    """A mocked DictationApp with the runtime flags Home reads. snooze_listening
    is the tray's stop path and flips the flags the way the real one does."""
    app = types.SimpleNamespace(
        config={'mode': 'hold', 'hotkey': 'ctrl+shift', 'microphone': 7,
                'wake_word_enabled': True, 'wake_word_config': {'phrase': 'jarvis'}},
        available_mics=[{'id': 7, 'name': 'USB mic'}],
        recording=False, snoozed=False, wake_word_active=True, continuous_active=False,
        command_mode_active=False, ava_command_session_active=False, ava_mode_active=False,
        toggle_active=False,
        _outcome_ring=collections.deque(maxlen=8),
        history_store=_Store(),
        snooze_calls=[], stop_calls=[], scratch_calls=0, teach_calls=0,
    )

    def snooze_listening(minutes=None):
        app.snooze_calls.append(minutes)
        app.recording = app.continuous_active = app.wake_word_active = False
        app.snoozed = True

    def resume_listening():
        app.snoozed = False
        app.wake_word_active = True

    # The per-lane stop paths the honest control uses (38)
    def stop_recording():
        app.stop_calls.append("stop_recording")
        app.recording = app.toggle_active = False

    def stop_continuous_mode():
        app.stop_calls.append("stop_continuous_mode")
        app.continuous_active = False

    def exit_command_mode():
        app.stop_calls.append("exit_command_mode")
        app.command_mode_active = app.recording = False

    def exit_ava_command_session():
        app.stop_calls.append("exit_ava_command_session")
        app.ava_command_session_active = app.recording = False

    app.snooze_listening = snooze_listening
    app.resume_listening = resume_listening
    app.stop_recording = stop_recording
    app.stop_continuous_mode = stop_continuous_mode
    app.exit_command_mode = exit_command_mode
    app.exit_ava_command_session = exit_ava_command_session
    app._handle_unified_scratch_that = lambda: setattr(app, 'scratch_calls', app.scratch_calls + 1)
    app.open_correction_capture = lambda: setattr(app, 'teach_calls', app.teach_calls + 1)
    for k, v in over.items():
        setattr(app, k, v)
    return app


@pytest.fixture
def page(qapp):
    def make(app=None):
        p = home_qt.HomePage(app or _app())
        p.resize(720, 900)
        p.show()
        qapp.processEvents()
        return p
    return make


def _buttons(page):
    return [b for b in page.findChildren(QPushButton)]


# ---------------------------------------------------------------------------
# Ring buffer (the whole dictation.py change)
# ---------------------------------------------------------------------------

class TestOutcomeRing:
    def _stub(self):
        import dictation
        stub = types.SimpleNamespace(listening_indicator=None)
        stub._show_outcome_chip = dictation.DictationApp._show_outcome_chip.__get__(stub)
        return stub

    def test_appends_and_caps_at_eight(self):
        stub = self._stub()
        for i in range(10):
            stub._show_outcome_chip(f"{CHIP_CHECK} cmd {i}", "success")
        ring = list(stub._outcome_ring)
        assert len(ring) == 8
        assert [r[0] for r in ring] == [f"{CHIP_CHECK} cmd {i}" for i in range(2, 10)]
        label, kind, ts = ring[-1]
        assert kind == "success" and abs(ts - time.time()) < 5

    def test_pending_and_live_chips_are_not_outcomes(self):
        stub = self._stub()
        stub._show_outcome_chip("...", "pending", None)
        stub._show_outcome_chip("REC", "live", None)
        assert not getattr(stub, '_outcome_ring', None)
        stub._show_outcome_chip("typed", "success", 900)
        assert [r[:2] for r in stub._outcome_ring] == [("typed", "success")]


# ---------------------------------------------------------------------------
# Last outcome row (41): kind first in plain words, then the content, with
# only that outcome's own actions
# ---------------------------------------------------------------------------

class TestLastAction:
    def _with(self, page, label, kind):
        app = _app()
        app._outcome_ring.append((label, kind, time.time()))
        return page(app), app

    def test_typed_reads_dictated_then_the_text_with_teach_only(self, page):
        p, app = self._with(page, "typed", "success")
        assert p.last_action_kind == "Dictated"
        assert p.last_action_text == "hello world"
        assert p._teach_btn.isVisibleTo(p) and p._teach_btn.isEnabled()
        assert not p._why_btn.isVisibleTo(p)
        p._teach_btn.click()
        assert app.teach_calls == 1

    def test_typed_text_is_one_line_and_elided(self, page):
        app = _app(history_store=_Store("line one\nline two " + "long " * 80))
        app._outcome_ring.append(("typed", "success", time.time()))
        p = page(app)
        assert "\n" not in p.last_action_text
        assert p.last_action_text.endswith(chr(0x2026))

    def test_command_claims_no_verb_it_cannot_name(self, page):
        """The chip stores only the first two words ("switch to"), so the row
        says "Ran a command" and shows the stored fragment, never "Ran switch to"."""
        p, _ = self._with(page, f"{CHIP_CHECK} switch to", "success")
        assert p.last_action_kind == "Ran a command"
        assert p.last_action_text == '"switch to"'
        assert not p._teach_btn.isVisibleTo(p) and not p._why_btn.isVisibleTo(p)

    def test_miss_reads_didnt_catch_then_what_it_heard(self, page):
        app = _app(_last_miss_text="opun crome")
        app._outcome_ring.append(("MISS", "error", time.time()))
        p = page(app)
        assert p.last_action_kind == "Didn't catch"
        assert p.last_action_text == '"opun crome"'
        assert not p._teach_btn.isVisibleTo(p) and not p._why_btn.isVisibleTo(p)

    def test_miss_without_the_heard_text_still_says_what_kind(self, page):
        p, _ = self._with(page, "MISS", "error")
        assert p.last_action_kind == "Didn't catch" and p.last_action_text == ""

    @pytest.mark.parametrize("label,kind,reason", [
        (f"{CHIP_CROSS} no mic", "error", "no mic"),
        ("refused: focus lock", "warning", "focus lock"),
        ("cancelled: open chrome", "warning", "open chrome"),
    ])
    def test_why_shows_the_reason_when_there_is_one(self, page, qapp, label, kind, reason):
        p, _ = self._with(page, label, kind)
        assert p._why_btn.isVisibleTo(p) and p._why_btn.isEnabled()
        assert not p._reason.isVisibleTo(p)
        p._why_btn.click()
        qapp.processEvents()
        assert p._reason.isVisibleTo(p) and p._reason.text() == reason

    def test_empty_state_offers_a_catalog_example(self, page):
        p = page(_app())
        records = command_catalog.guidance_catalog(None)
        example = command_catalog.canonical_phrase(command_catalog.pick_examples(records, 1)[0])
        assert p.last_action_kind == "Nothing yet."
        assert p.last_action_text == f"Try: {home_qt.quoted(example)}"
        assert not p._teach_btn.isVisibleTo(p) and not p._why_btn.isVisibleTo(p)

    def test_newest_outcome_wins_and_refresh_follows_the_ring(self, page):
        app = _app()
        p = page(app)
        app._outcome_ring.append((f"{CHIP_CHECK} open chrome", "success", time.time()))
        app._outcome_ring.append(("typed", "success", time.time()))
        p.refresh()
        assert p.last_action_kind == "Dictated" and p.last_action_text == "hello world"

    def test_the_outcome_is_a_row_not_a_titled_card(self, page):
        p, _ = self._with(page, "typed", "success")
        row = home_qt.find_by_accessible_name(p, "Last outcome")
        assert row is not None and row.objectName() != "homeCard"
        texts = [w.text() for w in row.findChildren(home_qt.QLabel)]
        assert "LAST ACTION" not in texts


# ---------------------------------------------------------------------------
# Runtime state block
# ---------------------------------------------------------------------------

class TestStateBlock:
    def test_facts_come_from_runtime_not_config(self, page):
        # Config says wake word enabled, runtime says the listener is not armed.
        app = _app(wake_word_active=False)
        p = page(app)
        assert p.state_texts["State"] == "Ready. What you say goes to whatever you're typing in."
        assert p._stop_btn.text() == "Stop listening" and not p._stop_btn.isEnabled()
        assert p._stop_reason.isVisibleTo(p) and p._stop_reason.text() == "Nothing to stop right now"
        assert not p._pause_act.isEnabled()
        app.wake_word_active = True
        p.refresh()
        assert p.state_texts["State"] == (
            "Wake word is armed " + chr(0x2014) + ' say "jarvis" to start.'
            " What you say goes to whatever you're typing in.")
        assert not p._stop_btn.isEnabled(), "an armed wake listener is ambient, nothing to stop"
        assert p._pause_act.isEnabled() and p._pause_act.text() == "Pause hands-free"
        app.recording = True
        p.refresh()
        assert p.state_texts["State"].startswith("Listening now.")
        assert p._stop_btn.isEnabled() and not p._stop_reason.isVisibleTo(p)

    def test_absent_mic_is_said_in_error_colour(self, page):
        app = _app(available_mics=[{'id': 1, 'name': 'other'}])   # configured id 7 is gone
        p = page(app)
        assert p.state_texts["State"].startswith("Microphone not found.")
        assert theme.ERROR in p._state_line.styleSheet()
        app.available_mics = [{'id': 7, 'name': 'USB mic'}]
        p.refresh()
        assert "Microphone not found" not in p.state_texts["State"]
        assert theme.ERROR not in p._state_line.styleSheet()

    def test_no_devices_at_all_is_absent(self):
        assert home_qt.mic_present(_app(available_mics=[])) is False

    def test_session_target_is_the_focus_locked_window_title(self, page, monkeypatch):
        monkeypatch.setattr(home_qt, "_window_title", lambda hwnd: f"Notepad ({hwnd})")
        app = _app(command_mode_active=True,
                   _session_mode_manager=types.SimpleNamespace(_dictate_target_hwnd=4242))
        p = page(app)
        assert p.state_texts["State"] == "Hands-free is on. What you say goes to Notepad (4242)."
        app.command_mode_active = False
        p.refresh()
        assert p.state_texts["State"].endswith("What you say goes to whatever you're typing in.")

    @pytest.mark.parametrize("flags,mode,expected", [
        ({"command_mode_active": True}, "command", "What you say is taken as a command."),
        ({"command_mode_active": True}, "ava", "What you say goes to Ava."),
        ({"ava_command_session_active": True}, None, "What you say goes to Ava."),
        ({"command_mode_active": True}, "dictate", "What you say goes to whatever you're typing in."),
    ])
    def test_session_modes_render_in_plain_words(self, page, flags, mode, expected):
        from samsara.session_modes import SessionMode
        manager = types.SimpleNamespace(mode=SessionMode(mode) if mode else None,
                                        _dictate_target_hwnd=None)
        p = page(_app(_session_mode_manager=manager, **flags))
        assert p.state_texts["State"].endswith(expected)

    def test_instruction_line_is_generated_from_config_and_catalog(self, page):
        app = _app()
        p = page(app)
        assert p.state_texts["Instruction"] == (
            'Hold Ctrl+Shift and speak. Say "what can I say" for commands.')
        app.config.update({'hotkey': 'mouse4'})
        p.refresh()
        assert p.state_texts["Instruction"] == 'Hold Mouse4 and speak. Say "what can I say" for commands.'
        app.config.update({'hotkey': 'ctrl+alt', 'mode': 'toggle', 'wake_word_enabled': False})
        p.refresh()
        assert p.state_texts["Instruction"] == (
            'Press Ctrl+Alt to start and again to stop. Say "what can I say" for commands.')

    def test_help_phrase_comes_from_the_catalog_only(self, monkeypatch):
        assert home_qt.help_phrase(None) is None
        assert "for commands" not in home_qt.instruction_line({'mode': 'hold'}, None)

    @pytest.mark.parametrize("flag,method", [
        ("recording", "stop_recording"),
        ("continuous_active", "stop_continuous_mode"),
        ("command_mode_active", "exit_command_mode"),
        ("ava_command_session_active", "exit_ava_command_session"),
    ])
    def test_stop_listening_stops_the_current_capture_never_snoozes(self, page, qapp, flag, method):
        """38: as found, the button called snooze_listening (home_qt.stop_listening).
        Now it calls the app's own per-lane stop and leaves the wake listener alone."""
        from PySide6.QtTest import QTest

        app = _app(**{flag: True})
        p = page(app)
        assert p._stop_btn.isEnabled()
        p._stop_btn.click()
        assert app.stop_calls == [method] and app.snooze_calls == []
        assert app.snoozed is False and app.wake_word_active is True
        # feedback within 200 ms: the button is disabled at once (reading
        # "Stopping" while the app's stop path is still running, or already
        # "Stop listening" when it returned synchronously) and the state
        # line has changed; everything settles on the 150 ms refresh.
        assert not p._stop_btn.isEnabled() and p._stop_btn.text() in ("Stopping", "Stop listening")
        assert p.state_texts["State"].startswith("Wake word is armed")
        QTest.qWait(200)
        assert p._stop_btn.text() == "Stop listening" and not p._stop_btn.isEnabled()
        assert p.state_texts["State"].startswith("Wake word is armed")

    def test_stop_button_is_disabled_not_dead_when_nothing_captures(self, page):
        app = _app()
        p = page(app)
        assert not p._stop_btn.isEnabled() and p._stop_btn.text() == "Stop listening"
        p._on_stop()                              # even called directly, it touches nothing
        assert app.stop_calls == [] and app.snooze_calls == []

    def test_pause_hands_free_lives_in_the_more_menu_with_a_way_back(self, page, qapp):
        from PySide6.QtTest import QTest

        app = _app()
        p = page(app)
        assert p._more_btn.menu() is p._more_menu
        assert p._more_menu.accessibleName() == "Listening options"
        assert [a.text() for a in p._more_menu.actions()] == ["Pause hands-free"]
        p._pause_act.trigger()
        assert app.snooze_calls == [None] and app.snoozed
        QTest.qWait(200)
        assert p._pause_act.text() == "Resume hands-free" and p._pause_act.isEnabled()
        assert p.state_texts["State"].startswith("Hands-free is paused.")
        p._pause_act.trigger()
        QTest.qWait(200)
        assert not app.snoozed and p._pause_act.text() == "Pause hands-free"
        assert p.state_texts["State"].startswith("Wake word is armed")

    def test_stop_capture_helper_never_reaches_the_snooze(self):
        app = _app(recording=True)
        assert home_qt.stop_capture(app) == "stop_recording"
        assert home_qt.stop_capture(app) is None and app.snooze_calls == []

    def test_tagline_slot_exists_and_is_hidden(self, page):
        p = page()
        slot = p.findChild(QWidget, "home.tagline")
        assert slot is not None and slot.text() == "" and not slot.isVisibleTo(p)

    def test_state_mark_follows_the_header_frame(self, page):
        from samsara.ui.tray_qt import MarkFrame
        p = page()
        assert p._mark.width() == p._mark.height() == home_qt.STATE_MARK_PX == 44
        p.set_mark_frame(MarkFrame("listening", "armed"))
        assert (p._mark.frame.capture, p._mark.frame.eye) == ("listening", "armed")


# ---------------------------------------------------------------------------
# Capability cards from the live catalog
# ---------------------------------------------------------------------------

class TestCapabilityCards:
    def test_cards_in_order_with_live_values(self, page):
        p = page(_app())
        titles = [c.text() for c in p.cards]
        assert titles == list(home_qt.CAPABILITY_TITLES)
        values = dict(zip(titles, (c.value_text for c in p.cards)))
        records = command_catalog.guidance_catalog(None)
        n = len(home_qt.matching_records(records, "window"))
        assert n > 0 and values["Control windows"] == f"{n} commands"
        assert values["Run it hands-free"] == 'say "jarvis"'
        assert values["Dictate anywhere"] == "hold Ctrl+Shift"
        assert values["Ask Ava"] in ("on", "off")
        assert values["Teach it your words"].endswith(" words") or values["Teach it your words"] == "your dictionary"
        assert not p._catalog_note.isVisibleTo(p)

    def test_degrades_honestly_without_a_catalog(self, page, monkeypatch):
        monkeypatch.setattr(command_catalog, "load_catalog_json", lambda path=None: None)
        p = page(_app())
        assert p.cards[0].value_text == "command list unavailable"
        assert p._catalog_note.isVisibleTo(p)
        assert "unavailable" in p._catalog_note.text() and 'href="' in p._catalog_note.text()
        assert p.last_action_kind == "Nothing yet." and p.last_action_text == ""
        assert "for commands" not in p.state_texts["Instruction"]

    def test_cards_open_the_reference_or_settings_or_the_dictionary(self, page, monkeypatch):
        opened = []
        monkeypatch.setattr(home_qt, "open_cheatsheet_filtered", lambda app, n: opened.append(("sheet", n)))
        monkeypatch.setattr(home_qt, "open_settings_tab", lambda app, t: opened.append(("settings", t)))
        app = _app()
        p = home_qt.HomePage(app, open_page=lambda name: opened.append(("page", name)))
        for c in p.cards:
            c.click()
        assert opened == [("sheet", "window"), ("settings", "Modes"), ("settings", "Modes"),
                          ("settings", "Ava / Cloud"), ("page", "Dictionary")]
        # the sixth card opens the guides row on the page itself
        assert p.cards[5].text() == "Guides & help" and p._guides_row.isVisibleTo(p)

    def test_cheatsheet_filter_path_sets_category_and_text(self, monkeypatch):
        posted = []
        from samsara.ui import qt_runtime
        monkeypatch.setattr(qt_runtime, "post", lambda fn: posted.append(fn))
        calls = []
        window = types.SimpleNamespace(
            _set_category=lambda c: calls.append(("cat", c)),
            _filter=types.SimpleNamespace(setText=lambda t: calls.append(("text", t))))
        sheet = types.SimpleNamespace(show=lambda: calls.append(("show",)), _window=window)
        home_qt.open_cheatsheet_filtered(types.SimpleNamespace(cheat_sheet=sheet), "window")
        for fn in posted:
            fn()
        assert calls == [("show",), ("cat", "All"), ("text", "window")]


# ---------------------------------------------------------------------------
# Words today: typed outcomes since local midnight only
# ---------------------------------------------------------------------------

class TestWordsToday:
    def test_counts_only_dictation_since_local_midnight(self, tmp_path):
        from samsara.history import HistoryManager
        from samsara.history_store import HistoryStore

        manager = HistoryManager(db_path=str(tmp_path / "history.db"))
        now = dt.datetime(2026, 9, 13, 14, 30, 0)
        rows = [
            ("one two three", "dictation", now - dt.timedelta(hours=1)),        # 3
            ("four five", "dictation", now.replace(hour=0, minute=0, second=1)),  # 2, just after midnight
            ("six seven eight nine", "dictation", now - dt.timedelta(days=1)),   # yesterday
            ("open chrome", "command", now - dt.timedelta(minutes=5)),           # not typed
            ("[FAILED] boom", "failed", now - dt.timedelta(minutes=5)),          # not typed
            ("ten", "dictation", now + dt.timedelta(minutes=1)),                 # later today still counts
        ]
        for text, entry_type, stamp in rows:
            row_id = manager.add(raw_text=text, display_text=text, entry_type=entry_type)
            with manager._lock:
                manager._conn.execute("UPDATE history SET timestamp = ? WHERE id = ?",
                                      (stamp.isoformat(), row_id))
                manager._conn.commit()
        store = HistoryStore(manager)
        app = types.SimpleNamespace(history_store=store)
        assert home_qt.words_today(app, now) == 6

    def test_unavailable_store_reads_zero(self):
        from samsara.history_store import HistoryStore
        assert home_qt.words_today(types.SimpleNamespace(history_store=HistoryStore(None))) == 0
        assert home_qt.words_today(types.SimpleNamespace()) == 0

    def test_identity_strip(self, page):
        p = page(_app(history_store=_Store(words=123)))
        assert p._words_val.text() == "123"
        assert home_qt.find_by_accessible_name(p, "words remaining value").text() == chr(0x221E)
        assert theme.ACCENT in home_qt.find_by_accessible_name(p, "words remaining value").styleSheet()
        assert p._words_card.size() == p._infinity_card.size()
        creed = p._creed
        assert creed.text() == "Free " + chr(0xB7) + " Open source " + chr(0xB7) + " Accessibility first"
        assert theme.FONT_FAMILY_DISPLAY in creed.styleSheet()
        assert creed.font().pixelSize() >= 14
        assert "letter-spacing" in creed.styleSheet()


# ---------------------------------------------------------------------------
# Accessibility: Label in Name, targets, tab order
# ---------------------------------------------------------------------------

class TestAccessibility:
    def test_every_button_label_equals_its_accessible_name(self, page):
        p = page()
        buttons = p.findChildren(QAbstractButton)
        assert len(buttons) >= 9
        for b in buttons:
            assert b.text() and b.accessibleName() == b.text(), b.text()

    def test_targets_are_at_least_44px(self, page):
        p = page()
        for b in p.findChildren(QAbstractButton):
            assert b.height() >= 44 and b.width() >= 44, (b.text(), b.size())

    def test_tab_order_runs_top_to_bottom(self, page):
        p = page()
        chain = p.tab_chain
        ys = [w.mapTo(p, w.rect().topLeft()).y() for w in chain]
        assert ys == sorted(ys), [(w.text(), y) for w, y in zip(chain, ys)]
        # And Qt's own focus chain agrees with the declared order.
        w = chain[0]
        seen = [w]
        for _ in range(len(chain) - 1):
            w = w.nextInFocusChain()
            while w not in chain:
                w = w.nextInFocusChain()
            seen.append(w)
        assert seen == chain


# ---------------------------------------------------------------------------
# Hub wiring: landing, walkthrough, header mark
# ---------------------------------------------------------------------------

@pytest.fixture
def hub(qapp, monkeypatch):
    from unittest.mock import MagicMock

    class _History(QWidget):
        def __init__(self, *a, **k):
            super().__init__()
            self.setAccessibleName("History list")
            self.refreshed = 0

        def refresh(self):
            self.refreshed += 1

    monkeypatch.setattr(main_window_qt, "HistoryView", _History)
    monkeypatch.setattr(main_window_qt.QTimer, "singleShot", MagicMock())
    app = _app()
    app.create_icon_image = lambda rotation=0.0, opacity=1.0: main_window_qt.MarkFrame("listening", "armed", 0.0, 1.0)
    win = main_window_qt._MainWindow(app)
    win.show()
    qapp.processEvents()
    yield win, app
    win._poll_timer.stop()
    win._header_mark.stop()
    win.hide()
    win.deleteLater()


class TestHubWiring:
    def test_home_is_the_landing_and_first_in_nav(self, hub):
        win, _ = hub
        assert list(win._nav_btns) == ["Home", "History", "Dictionary", "Settings"]
        assert isinstance(win._stack.currentWidget(), home_qt.HomePage)
        assert win._nav_btns["Home"].isChecked()
        for name, btn in win._nav_btns.items():
            assert btn.accessibleName() == name == btn.text() and btn.height() >= 44

    def test_hands_free_walkthrough_by_accessible_names(self, hub, qapp):
        """Open Home -> read the state -> Stop listening -> open History ->
        return Home, every step through an accessible name."""
        win, app = hub
        find = lambda name: home_qt.find_by_accessible_name(win, name)

        find("Home").click(); qapp.processEvents()
        home = win._stack.currentWidget()
        assert isinstance(home, home_qt.HomePage)

        assert find("State line").text().startswith("Wake word is armed")
        assert find("Instruction").text().startswith("Hold Ctrl+Shift and speak.")

        app.recording = True                      # a capture is running
        home.refresh()
        assert find("State line").text().startswith("Listening now.")
        find("Stop listening").click(); qapp.processEvents()
        assert app.stop_calls == ["stop_recording"] and app.snooze_calls == []
        from PySide6.QtTest import QTest
        QTest.qWait(200)
        assert find("State line").text().startswith("Wake word is armed")
        assert not find("Stop listening").isEnabled()

        find("History").click(); qapp.processEvents()
        assert win._stack.currentWidget().accessibleName() == "History list"

        find("Home").click(); qapp.processEvents()
        assert win._stack.currentWidget() is home
        assert win._nav_btns["Home"].isChecked()

    def test_header_mark_feeds_homes_mark_and_polls_refresh(self, hub, qapp):
        win, app = hub
        home = win._panel_cache["Home"]
        win._header_mark.refresh()
        assert (home._mark.frame.capture, home._mark.frame.eye) == ("listening", "armed")
        app.recording = True
        win._refresh_status()
        assert home.state_texts["State"].startswith("Listening now.")
        app._outcome_ring.append(("typed", "success", time.time()))
        win._on_dictation("hello world")
        assert home.last_action_kind == "Dictated" and home.last_action_text == "hello world"

    def test_the_newest_outcome_is_shown_once_not_again_in_the_status_bar(self, hub, qapp):
        """41 (4c): the status bar's "Last: ..." preview is gone; Home's row
        is the one place the newest outcome appears."""
        from PySide6.QtWidgets import QLabel
        win, app = hub
        app._outcome_ring.append(("typed", "success", time.time()))
        win._on_dictation("hello world")
        qapp.processEvents()
        assert not hasattr(win, "_lbl_prev")
        shown = [l for l in win.findChildren(QLabel) if l.isVisibleTo(win) and "hello world" in l.text()]
        assert shown == [win._panel_cache["Home"]._action_text]


# ---------------------------------------------------------------------------
# Scaling gate: 125%, 150%, and a 20% larger default font -- nothing clips
# ---------------------------------------------------------------------------

_SCALE_SCRIPT = r'''
import json, sys, types, collections, time
sys.path.insert(0, sys.argv[1])
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget
app = QApplication([])
if sys.argv[2] != "1":
    f = app.font(); f.setPointSizeF(f.pointSizeF() * float(sys.argv[2])); app.setFont(f)
from samsara.ui import main_window_qt, home_qt
main_window_qt.HistoryView = lambda *a, **k: QWidget()
fake = types.SimpleNamespace(
    config={'mode': 'hold', 'hotkey': 'ctrl+shift', 'microphone': 7, 'wake_word_enabled': True,
            'wake_word_config': {'phrase': 'jarvis'}},
    available_mics=[{'id': 1, 'name': 'x'}], recording=False, snoozed=False, wake_word_active=True,
    continuous_active=False, command_mode_active=False, toggle_active=False,
    _outcome_ring=collections.deque([(chr(0x2717) + " no mic", "error", time.time())], maxlen=8))
win = main_window_qt._MainWindow(fake)
win.resize(900, 650)
win.show()
for _ in range(40):
    app.processEvents()
home = win._panel_cache["Home"]
home._why_btn.click()
for _ in range(20):
    app.processEvents()
out = {
    "overflow": home_qt.overflowing_widgets(home),
    "content_w": home.content_widget.width(),
    "viewport_w": home._scroll.viewport().width(),
    "scale": home_qt.text_scale(),
    "buttons_under_44": [b.text() for b in home.findChildren(main_window_qt.QPushButton)
                         if b.height() < 44 or b.width() < 44],
}
print("RESULT " + json.dumps(out))
'''


@pytest.mark.parametrize("scale_factor,font_factor", [("1", "1"), ("1.25", "1"), ("1.5", "1"), ("1", "1.2"), ("1.5", "1.2")])
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
    assert result["overflow"] == [], result
    assert result["content_w"] <= result["viewport_w"], result
    assert result["buttons_under_44"] == [], result
    if font_factor != "1":
        assert result["scale"] > 1.1


# ---------------------------------------------------------------------------
# 40: every Home action has a concrete destination
# ---------------------------------------------------------------------------

class _Wrapper:
    """A settings / cheat-sheet wrapper the way the real ones behave: show()
    posts its window's construction, so `_window` is None at call time and
    only exists once the posted init has run."""

    def __init__(self, posted, window_factory):
        self._window = None
        self._posted = posted
        self._factory = window_factory

    def show(self):
        if self._window is None:
            self._posted.append(self._init)
        else:
            self._posted.append(self._window.show)

    def _init(self):
        self._window = self._factory()


class _SettingsWindow:
    def __init__(self):
        self.shown_tabs = []
        self.shows = 0

    def show(self):
        self.shows += 1

    def show_tab(self, name):
        self.shown_tabs.append(name)


class _SheetWindow:
    def __init__(self):
        self.calls = []
        self._filter = types.SimpleNamespace(setText=lambda t: self.calls.append(("text", t)))

    def show(self):
        self.calls.append(("show",))

    def _set_category(self, c):
        self.calls.append(("cat", c))


class TestDestinations:
    def test_every_action_resolves_to_a_concrete_destination(self, page, monkeypatch):
        """Table-driven: each button and card on the page maps to an app
        method that exists, a settings page in the registry, the cheat
        sheet, a hub page, the guides row, or an inline change -- never a
        bare pass or a generic landing."""
        from samsara.ui import settings_qt
        from PySide6.QtWidgets import QAbstractButton

        app = _app(history_store=_Store("hello world"))
        app._outcome_ring.append((f"{CHIP_CROSS} no mic", "error", time.time()))
        app.open_correction_capture = lambda: None
        app.open_quick_reference = lambda: None
        app.show_tutorial = lambda: None
        app.open_settings = lambda: None
        app.cheat_sheet = types.SimpleNamespace(show=lambda: None, _window=None)
        p = page(app)
        table = p.destinations()
        labels = {b.text() for b in p.findChildren(QAbstractButton)}
        assert labels <= set(table) | {"Stopping"}, labels - set(table)
        for action in p._more_menu.actions():
            assert action.text() in table
        for label, (kind, target) in table.items():
            if kind == "app":
                for name in str(target).split(" | "):
                    assert callable(getattr(app, name, None)), f"{label}: app has no {name}"
            elif kind == "settings":
                assert target in settings_qt._TAB_NAMES, f"{label}: {target!r} is not a settings page"
            elif kind == "page":
                assert target in ("History", "Dictionary", "Home")
            elif kind == "cheatsheet":
                assert app.cheat_sheet is not None
            elif kind == "guides":
                assert target is None
            elif kind == "inline":
                assert home_qt.find_by_accessible_name(p, target) is not None
            else:
                raise AssertionError(f"{label}: unknown destination kind {kind!r}")
        # every card's action is one of the handled kinds (no silent else)
        for spec in home_qt.capability_cards(app, p._records):
            assert spec["action"] in ("cheatsheet", "settings", "page", "guides")

    def test_ava_card_lands_on_the_ava_settings_page_after_the_window_is_built(self, monkeypatch):
        """40: the page was selected against the window read at CALL time,
        which is None on the first open, so Settings opened on page one.
        Now the id is checked against the registry and the selection runs
        after the window's own init post."""
        from samsara.ui import qt_runtime, settings_qt

        posted = []
        monkeypatch.setattr(qt_runtime, "post", lambda fn: posted.append(fn))
        app = _app()
        app._settings_qt = _Wrapper(posted, _SettingsWindow)
        app.open_settings = app._settings_qt.show
        assert "Ava / Cloud" in settings_qt._TAB_NAMES
        assert home_qt.open_settings_tab(app, "Ava / Cloud") is True
        assert app._settings_qt._window is None, "nothing built yet: the init is still queued"
        for fn in list(posted):                    # the Qt thread drains FIFO
            fn()
        assert app._settings_qt._window.shown_tabs == ["Ava / Cloud"]
        # a second press on an existing window still selects the page
        posted.clear()
        assert home_qt.open_settings_tab(app, "Ava / Cloud") is True
        for fn in list(posted):
            fn()
        assert app._settings_qt._window.shown_tabs == ["Ava / Cloud", "Ava / Cloud"]

    def test_unknown_settings_page_is_refused_loudly(self, monkeypatch, caplog):
        import logging

        app = _app()
        opened = []
        app.open_settings = lambda: opened.append(True)
        with caplog.at_level(logging.ERROR):
            assert home_qt.open_settings_tab(app, "Ava") is False
        assert opened == [] and any("not in the registry" in r.getMessage() for r in caplog.records)

    def test_command_reference_filter_applies_on_the_first_open(self, monkeypatch):
        from samsara.ui import qt_runtime

        posted = []
        monkeypatch.setattr(qt_runtime, "post", lambda fn: posted.append(fn))
        app = _app()
        app.cheat_sheet = _Wrapper(posted, _SheetWindow)
        assert home_qt.open_cheatsheet_filtered(app, "window") is True
        for fn in list(posted):
            fn()
        assert app.cheat_sheet._window.calls == [("cat", "All"), ("text", "window")]

    def test_guides_card_opens_the_row_and_each_guide_has_its_surface(self, page, monkeypatch, qapp):
        opened = []
        monkeypatch.setattr(home_qt, "open_cheatsheet_filtered", lambda app, n: opened.append(("sheet", n)) or True)
        monkeypatch.setattr(home_qt, "open_settings_tab", lambda app, t: opened.append(("settings", t)) or True)
        app = _app()
        app.open_quick_reference = lambda: opened.append(("app", "open_quick_reference"))
        app.show_tutorial = lambda: opened.append(("app", "show_tutorial"))
        p = page(app)
        assert not p._guides_row.isVisibleTo(p)
        card = p.cards[5]
        assert card.text() == card.accessibleName() == "Guides & help" and card.value_text == "4 guides"
        card.click(); qapp.processEvents()
        assert p._guides_row.isVisibleTo(p)
        for label, btn in p._guide_btns.items():
            assert btn.text() == btn.accessibleName() == label and btn.minimumHeight() >= 44
            btn.click()
        assert opened == [("sheet", ""), ("app", "open_quick_reference"), ("app", "show_tutorial"),
                          ("settings", "Help & Support")]
        assert [l for l, _k, _t in home_qt.GUIDES] == ["Command reference", "Quick reference", "Tutorial", "Help & support"]

    def test_teach_a_word_is_disabled_with_a_reason_when_nothing_to_correct(self, page):
        app = _app(history_store=_Store(""))
        app._outcome_ring.append(("typed", "success", time.time()))
        calls = []
        app.open_correction_capture = lambda: calls.append(True)
        p = page(app)
        assert p._teach_btn.isVisibleTo(p) and not p._teach_btn.isEnabled()
        assert p._teach_note.isVisibleTo(p) and p._teach_note.text() == "Nothing to correct yet"
        p._on_teach()                              # even called directly: nothing opens
        assert calls == []
        app.history_store = _Store("hello world")
        p.refresh()
        assert p._teach_btn.isEnabled() and not p._teach_note.isVisibleTo(p)
        p._teach_btn.click()
        assert calls == [True]

    def test_why_stays_inline_and_real(self, page):
        app = _app(history_store=_Store("hello world"))
        app._outcome_ring.append(("refused: focus lock", "warning", time.time()))
        p = page(app)
        assert p.destinations()["Why?"] == ("inline", "Reason")
        p._why_btn.click()
        assert p._reason.text() == "focus lock"


# ---------------------------------------------------------------------------
# 41 (4d): no Undo on Home, and no Home path reaches a keystroke undo
# ---------------------------------------------------------------------------

_SCRATCH_OR_KEYS = ("_handle_unified_scratch_that", "_do_scratch_that", "scratch", "undo",
                    "SendInput", "keybd_event", "send_keys", "press_keys", "pyautogui",
                    "ctrl+z", "backspace", "hotkey_send", "keyboard")


def _home_code_identifiers():
    """Every name, attribute and non-docstring string constant in home_qt.py
    (comments and docstrings may explain the decision; code may not act on it)."""
    import ast
    tree = ast.parse(Path(home_qt.__file__).read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.alias):
            out.append(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.append(node.value)
    return out


class TestNoUndoOnHome:
    def test_no_undo_control_or_destination(self, page):
        app = _app(history_store=_Store("hello world"))
        app._outcome_ring.append(("typed", "success", time.time()))
        p = page(app)
        assert not hasattr(p, "_undo_btn") and not hasattr(p, "_on_undo")
        assert "Undo" not in p.destinations()
        assert not any("undo" in t.lower() for t in p.visible_texts())

    def test_home_code_names_no_scratch_or_synthetic_key_path(self):
        hits = [n for n in _home_code_identifiers()
                if any(bad.lower() in str(n).lower() for bad in _SCRATCH_OR_KEYS)]
        assert hits == []

    def test_pressing_everything_on_home_never_reaches_the_session_undo(self, page, monkeypatch, qapp):
        from PySide6.QtWidgets import QAbstractButton
        monkeypatch.setattr(home_qt, "open_cheatsheet_filtered", lambda app, n: True)
        monkeypatch.setattr(home_qt, "open_settings_tab", lambda app, t: True)
        monkeypatch.setattr(home_qt, "open_guide", lambda app, l: True)
        popped = []
        manager = types.SimpleNamespace(mode=None, _dictate_target_hwnd=None,
                                        _do_scratch_that=lambda: popped.append("stack") or True)
        app = _app(history_store=_Store("hello world"), _session_mode_manager=manager, recording=True)
        app._handle_unified_scratch_that = lambda: popped.append("unified")
        app._outcome_ring.append(("typed", "success", time.time()))
        p = page(app)
        p.show_guides(True)
        qapp.processEvents()
        for b in p.findChildren(QAbstractButton):
            if b is p._more_btn:                   # opens a modal menu; its actions are triggered below
                continue
            b.click()
            qapp.processEvents()
        for action in p._more_menu.actions():
            action.trigger()
        assert popped == []
        assert app.scratch_calls == 0


# ---------------------------------------------------------------------------
# 41: type scale, plain language, quoted phrases, descriptions, one primary
# control, a shorter state row
# ---------------------------------------------------------------------------

_BODY_ROLES = ("instruction", "stop reason", "menu item", "button", "outcome text", "note",
               "card description", "card value", "usage label", "status value")


class TestTypeScale:
    def test_token_table_floors(self):
        table = theme.HOME_TYPE_SCALE
        assert min(table.values()) >= theme.TYPE_MIN == 12
        assert table["nav"] >= 15
        assert table["card title"] >= 15 and table["outcome kind"] >= 15
        assert table["state line"] >= 15
        for role in _BODY_ROLES:
            assert table[role] >= 14, role
        assert table["section label"] >= 12 and theme.LETTER_SPACING_SECTION
        assert table["usage figure"] >= 24

    def test_every_home_text_has_a_role_from_the_table(self, page):
        from PySide6.QtWidgets import QAbstractButton, QLabel
        app = _app(history_store=_Store("hello world"))
        app._outcome_ring.append(("refused: focus lock", "warning", time.time()))
        p = page(app)
        for w in p.findChildren(QLabel) + p.findChildren(QAbstractButton):
            if isinstance(w, QLabel) and not w.text():
                continue                           # icon pixmaps, empty slots
            role = w.property("typeRole")
            assert role in theme.HOME_TYPE_SCALE, (w.accessibleName() or w.text(), role)

    def test_no_home_text_renders_below_12px(self, hub, qapp):
        """Every stylesheet on the hub window (Home, header, nav, status bar)
        asks for at least TYPE_MIN px -- scaled by the text scale in force."""
        import re
        from PySide6.QtWidgets import QMenu
        win, _ = hub
        floor = home_qt._px(theme.TYPE_MIN) if home_qt.text_scale() >= 1 else theme.TYPE_MIN
        sizes = []
        for w in [win, *win.findChildren(QWidget), *win.findChildren(QMenu)]:
            for m in re.finditer(r"font-size:\s*(\d+)px", w.styleSheet() or ""):
                sizes.append((int(m.group(1)), w.accessibleName() or w.objectName() or type(w).__name__))
        assert sizes and all(px >= min(floor, theme.TYPE_MIN) for px, _ in sizes), \
            [s for s in sizes if s[0] < theme.TYPE_MIN]

    def test_nav_uses_the_nav_token(self):
        assert f"font-size: {theme.TYPE_NAV}px" in main_window_qt._MainWindow._nav_style(True)
        assert f"font-size: {theme.TYPE_NAV}px" in main_window_qt._MainWindow._nav_style(False)


def _states():
    """App states covering every state line and destination."""
    from samsara.session_modes import SessionMode
    mgr = lambda mode, hwnd=None: types.SimpleNamespace(mode=mode, _dictate_target_hwnd=hwnd)
    return [
        _app(), _app(wake_word_active=False), _app(recording=True), _app(snoozed=True, wake_word_active=False),
        _app(continuous_active=True), _app(toggle_active=True),
        _app(command_mode_active=True, _session_mode_manager=mgr(SessionMode.COMMAND)),
        _app(command_mode_active=True, _session_mode_manager=mgr(SessionMode.DICTATE)),
        _app(command_mode_active=True, _session_mode_manager=mgr(SessionMode.AVA)),
        _app(ava_command_session_active=True), _app(ava_mode_active=True),
        _app(available_mics=[]),
        _app(wake_word_active=False, config={'mode': 'continuous', 'hotkey': 'ctrl+shift'}),
    ]


class TestPlainLanguage:
    def test_lane_never_appears_in_any_user_visible_string(self, page):
        import re
        for app in _states():
            app._outcome_ring.append(("typed", "success", time.time()))
            p = page(app)
            bad = [t for t in p.visible_texts() if re.search(r"\blanes?\b", t, re.I)]
            assert bad == [], bad
            for name in ("command", "dictate"):
                assert not any(t.strip().lower() == name for t in p.visible_texts())

    def test_lane_never_appears_on_the_hub_chrome(self, hub):
        import re
        from PySide6.QtWidgets import QAbstractButton, QLabel
        win, _ = hub
        texts = []
        for w in win.findChildren(QWidget):
            if isinstance(w, (QLabel, QAbstractButton)):
                texts.append(w.text())
            texts += [w.accessibleName(), w.accessibleDescription(), w.toolTip()]
        assert not [t for t in texts if t and re.search(r"\blanes?\b", t, re.I)]

    def test_every_spoken_phrase_is_quoted_everywhere(self, page, hub, qapp):
        """The wake phrase, the catalog's help phrase and the example it offers
        appear on Home only inside double quotes -- state line, instruction,
        cards, the outcome row and the hub's status bar."""
        import re
        from PySide6.QtWidgets import QAbstractButton, QLabel
        records = command_catalog.guidance_catalog(None)
        phrases = {"jarvis", home_qt.help_phrase(records),
                   command_catalog.canonical_phrase(command_catalog.pick_examples(records, 1)[0])}
        texts = []
        for app in _states():
            texts += page(app).visible_texts()
        win, _ = hub
        win._refresh_status()
        qapp.processEvents()
        texts += [w.text() for w in win.findChildren(QLabel)]
        found = 0
        for text in texts:
            for phrase in phrases:
                for m in re.finditer(re.escape(phrase), text, re.I):
                    found += 1
                    before = text[m.start() - 1] if m.start() else ""
                    after = text[m.end()] if m.end() < len(text) else ""
                    assert before == '"' and after == '"', (phrase, text)
        assert found >= 4


class TestCardsDescribed:
    def test_each_card_has_a_real_one_line_description(self, page):
        p = page(_app())
        descriptions = [c.description_text for c in p.cards]
        assert all(d and d.endswith(".") and len(d) <= 80 for d in descriptions), descriptions
        assert len(set(descriptions)) == len(descriptions)
        for c in p.cards:
            assert c.accessibleDescription() == c.description_text
            assert home_qt.find_by_accessible_name(p, f"{c.text()} description").isVisibleTo(p)

    @pytest.mark.parametrize("local,cloud,where", [
        (True, True, "Runs on your own machine, or your own API key."),
        (True, False, "Runs on your own machine."),
        (False, True, "Runs on your own API key."),
        (False, False, "Turned off; set it up in Settings."),
    ])
    def test_ask_ava_says_where_it_runs(self, local, cloud, where):
        app = _app()
        app.config.update({'ollama': {'enabled': local}, 'cloud_llm': {'enabled': cloud}})
        ava = [c for c in home_qt.capability_cards(app, None) if c["title"] == "Ask Ava"][0]
        assert ava["description"] == f"Ask questions out loud. {where}"
        assert ava["value"] == ("on" if (local or cloud) else "off")


class TestStateRow:
    def test_exactly_one_primary_control_in_the_state_row(self, page):
        from PySide6.QtWidgets import QAbstractButton
        for app in (_app(), _app(recording=True), _app(snoozed=True, wake_word_active=False)):
            p = page(app)
            buttons = p.state_row.findChildren(QAbstractButton)
            primaries = [b for b in buttons if b.property("primary")]
            assert primaries == [p._stop_btn]
            assert [b for b in buttons if b not in primaries] == [p._more_btn]
            assert not any(b.text() in ("Pause hands-free", "Resume hands-free")
                           for b in p.findChildren(QAbstractButton))

    def test_state_row_is_one_state_line_and_one_instruction_line(self, page):
        from PySide6.QtWidgets import QLabel
        p = page(_app())
        visible = [l for l in p.state_row.findChildren(QLabel) if l.isVisibleTo(p) and l.text()]
        assert [l.accessibleName() for l in visible] == ["State line", "Instruction", "Stop listening note"]


# Measured at b429907 (the three-fact state block) with this same script: 216 px.
STATE_ROW_HEIGHT_BEFORE = 216

_HEIGHT_SCRIPT = r'''
import collections, json, sys, types
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication([])
from samsara.ui.home_qt import HomePage, find_by_accessible_name
fake = types.SimpleNamespace(
    config={'mode': 'hold', 'hotkey': 'mouse4', 'microphone': 7, 'wake_word_enabled': True,
            'wake_word_config': {'phrase': 'jarvis'}},
    available_mics=[{'id': 7, 'name': 'USB mic'}], recording=False, snoozed=False,
    wake_word_active=True, continuous_active=False, command_mode_active=False, toggle_active=False,
    _outcome_ring=collections.deque(maxlen=8))
page = HomePage(fake)
page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
page.resize(720, 900)
page.show()
for _ in range(5):
    app.processEvents()
print("RESULT " + json.dumps({"height": find_by_accessible_name(page, "State").height()}))
'''


@pytest.mark.skipif(sys.platform != "win32", reason="measured on the real Windows platform, not offscreen")
def test_state_row_is_shorter_than_before(tmp_path):
    """Real platform (offscreen has no fonts and wraps differently), never on screen."""
    script = tmp_path / "height_check.py"
    script.write_text(_HEIGHT_SCRIPT, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("QT_QPA_PLATFORM", "QT_SCALE_FACTOR")}
    proc = subprocess.run([PY, str(script), str(REPO)], env=env, capture_output=True, text=True,
                          timeout=110, cwd=str(REPO))
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")]
    assert line, proc.stdout[-2000:] + proc.stderr[-2000:]
    height = json.loads(line[-1][len("RESULT "):])["height"]
    assert height < STATE_ROW_HEIGHT_BEFORE, height
    assert height <= STATE_ROW_HEIGHT_BEFORE * 0.6, height
