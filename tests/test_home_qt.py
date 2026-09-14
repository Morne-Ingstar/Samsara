"""Queue 26: the Home page (samsara/ui/home_qt.py) and its wiring into the
hub window.

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
        snooze_calls=[], scratch_calls=0, teach_calls=0,
    )

    def snooze_listening(minutes=None):
        app.snooze_calls.append(minutes)
        app.recording = app.continuous_active = app.wake_word_active = False
        app.snoozed = True

    app.snooze_listening = snooze_listening
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
# Last action card: each kind renders, only its valid actions
# ---------------------------------------------------------------------------

class TestLastAction:
    def _with(self, page, label, kind):
        app = _app()
        app._outcome_ring.append((label, kind, time.time()))
        return page(app), app

    def test_typed_shows_the_text_and_enables_undo_only(self, page):
        p, app = self._with(page, "typed", "success")
        assert p.last_action_text == "hello world"
        assert p._undo_btn.isEnabled() and not p._why_btn.isEnabled()
        assert p._teach_btn.isEnabled()
        p._undo_btn.click()
        assert app.scratch_calls == 1
        p._teach_btn.click()
        assert app.teach_calls == 1

    def test_typed_text_is_one_line_and_elided(self, page):
        app = _app(history_store=_Store("line one\nline two " + "long " * 80))
        app._outcome_ring.append(("typed", "success", time.time()))
        p = page(app)
        assert "\n" not in p.last_action_text
        assert p.last_action_text.endswith(chr(0x2026))

    def test_command_renders_check_verb_and_no_undo(self, page):
        p, _ = self._with(page, f"{CHIP_CHECK} open chrome", "success")
        assert p.last_action_text == f"{CHIP_CHECK} open chrome"
        assert not p._undo_btn.isEnabled() and not p._why_btn.isEnabled()

    def test_miss_renders_cross_and_what_it_heard(self, page):
        app = _app(_last_miss_text="opun crome")
        app._outcome_ring.append(("MISS", "error", time.time()))
        p = page(app)
        assert p.last_action_text == f"{CHIP_CROSS} opun crome"
        assert not p._undo_btn.isEnabled() and not p._why_btn.isEnabled()

    def test_miss_without_the_heard_text_is_still_a_cross(self, page):
        p, _ = self._with(page, "MISS", "error")
        assert p.last_action_text == f"{CHIP_CROSS} MISS"

    @pytest.mark.parametrize("label,kind,reason", [
        (f"{CHIP_CROSS} no mic", "error", "no mic"),
        ("refused: focus lock", "warning", "focus lock"),
        ("cancelled: open chrome", "warning", "open chrome"),
    ])
    def test_why_shows_the_reason_when_there_is_one(self, page, qapp, label, kind, reason):
        p, _ = self._with(page, label, kind)
        assert p._why_btn.isEnabled()
        assert not p._reason.isVisibleTo(p)
        p._why_btn.click()
        qapp.processEvents()
        assert p._reason.isVisibleTo(p) and p._reason.text() == reason

    def test_empty_state_offers_a_catalog_example(self, page):
        p = page(_app())
        records = command_catalog.guidance_catalog(None)
        example = command_catalog.canonical_phrase(command_catalog.pick_examples(records, 1)[0])
        assert p.last_action_text == f"Nothing yet. Try: {example}"
        assert not p._undo_btn.isEnabled() and not p._why_btn.isEnabled()

    def test_newest_outcome_wins_and_refresh_follows_the_ring(self, page):
        app = _app()
        p = page(app)
        app._outcome_ring.append((f"{CHIP_CHECK} open chrome", "success", time.time()))
        app._outcome_ring.append(("typed", "success", time.time()))
        p.refresh()
        assert p.last_action_text == "hello world" and p._undo_btn.isEnabled()


# ---------------------------------------------------------------------------
# Runtime state block
# ---------------------------------------------------------------------------

class TestStateBlock:
    def test_facts_come_from_runtime_not_config(self, page):
        # Config says wake word enabled, runtime says the listener is not armed.
        app = _app(wake_word_active=False)
        p = page(app)
        assert p.state_texts["Listening"] == "Hold key"
        assert p.state_texts["Lane"] == "dictate"
        assert p.state_texts["Next utterance goes to"] == "any textbox"
        assert p._stop_btn.text() == "Listening is off"
        app.wake_word_active = True
        p.refresh()
        assert p.state_texts["Listening"] == "Wake word armed"
        assert p.state_texts["Lane"] == "command"
        assert p._stop_btn.text() == "Stop listening"
        app.recording = True
        p.refresh()
        assert p.state_texts["Listening"] == "Recording"

    def test_absent_mic_is_said_in_error_colour(self, page):
        app = _app(available_mics=[{'id': 1, 'name': 'other'}])   # configured id 7 is gone
        p = page(app)
        assert "microphone not found" in p.state_texts["Listening"]
        assert theme.ERROR in p._listening_val.styleSheet()
        app.available_mics = [{'id': 7, 'name': 'USB mic'}]
        p.refresh()
        assert "microphone not found" not in p.state_texts["Listening"]
        assert theme.ERROR not in p._listening_val.styleSheet()

    def test_no_devices_at_all_is_absent(self):
        assert home_qt.mic_present(_app(available_mics=[])) is False

    def test_session_target_is_the_focus_locked_window_title(self, page, monkeypatch):
        monkeypatch.setattr(home_qt, "_window_title", lambda hwnd: f"Notepad ({hwnd})")
        app = _app(command_mode_active=True,
                   _session_mode_manager=types.SimpleNamespace(_dictate_target_hwnd=4242))
        p = page(app)
        assert p.state_texts["Next utterance goes to"] == "Notepad (4242)"
        assert p.state_texts["Lane"] == "hands-free session"
        app.command_mode_active = False
        p.refresh()
        assert p.state_texts["Next utterance goes to"] == "any textbox"

    def test_instruction_line_is_generated_from_config_and_catalog(self, page):
        app = _app()
        p = page(app)
        assert p.state_texts["Instruction"] == (
            "Hold Ctrl+Shift and speak. Say jarvis for hands-free. Say what can i say for commands.")
        app.config.update({'hotkey': 'ctrl+alt', 'mode': 'toggle', 'wake_word_enabled': False})
        p.refresh()
        assert p.state_texts["Instruction"] == (
            "Press Ctrl+Alt to start and again to stop. Say what can i say for commands.")

    def test_help_phrase_comes_from_the_catalog_only(self, monkeypatch):
        assert home_qt.help_phrase(None) is None
        assert "for commands" not in home_qt.instruction_line({'mode': 'hold'}, None)

    def test_stop_listening_uses_the_tray_snooze_path(self, page):
        app = _app()
        p = page(app)
        p._stop_btn.click()
        assert app.snooze_calls == [None]
        assert p._stop_btn.text() == "Listening is off"
        p._stop_btn.click()                       # does nothing when off
        assert app.snooze_calls == [None]

    def test_tagline_slot_exists_and_is_hidden(self, page):
        p = page()
        slot = p.findChild(QWidget, "home.tagline")
        assert slot is not None and slot.text() == "" and not slot.isVisibleTo(p)

    def test_state_mark_follows_the_header_frame(self, page):
        from samsara.ui.tray_qt import MarkFrame
        p = page()
        assert p._mark.width() == p._mark.height() == 60
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
        assert values["Run it hands-free"] == "say jarvis"
        assert values["Dictate anywhere"] == "hold Ctrl+Shift"
        assert values["Ask Ava"] in ("local", "your key", "local or your key", "off")
        assert values["Teach it your words"].endswith(" words") or values["Teach it your words"] == "your dictionary"
        assert not p._catalog_note.isVisibleTo(p)

    def test_degrades_honestly_without_a_catalog(self, page, monkeypatch):
        monkeypatch.setattr(command_catalog, "load_catalog_json", lambda path=None: None)
        p = page(_app())
        assert p.cards[0].value_text == "command list unavailable"
        assert p._catalog_note.isVisibleTo(p)
        assert "unavailable" in p._catalog_note.text() and 'href="' in p._catalog_note.text()
        assert p.last_action_text == "Nothing yet."
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

        assert find("Listening").text() == "Wake word armed"
        assert find("Lane").text() == "command"
        assert find("Next utterance goes to").text() == "any textbox"
        assert find("Instruction").text().startswith("Hold Ctrl+Shift and speak.")

        find("Stop listening").click(); qapp.processEvents()
        assert app.snooze_calls == [None]
        assert find("Listening is off") is not None and find("Stop listening") is None
        assert find("Listening").text() == "Snoozed"

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
        assert home.state_texts["Listening"] == "Recording"
        app._outcome_ring.append(("typed", "success", time.time()))
        win._on_dictation("hello world")
        assert home.last_action_text == "hello world"


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
