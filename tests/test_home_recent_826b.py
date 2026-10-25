"""Privacy and responsive Home regressions; invented text, isolated SQLite."""
from datetime import datetime
from types import SimpleNamespace
import sqlite3
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from samsara import recent_privacy as privacy
from samsara.config_schema import SETTINGS_SCHEMA
from samsara.history import HistoryManager
from samsara.ui import home_qt, home_signals, main_window_qt, theme
from samsara.ui.home_recent_qt import RecentActivity, presentation, row_budget, relative_time
from tests.test_home_qt import _app, _hub, _FakeSettings


def fixture_rows():
    return [
        dict(id=3, timestamp=datetime.now().isoformat(), raw_text="The blue kite flies over the meadow",
             display_text="The blue kite flies over the meadow.", status="success", entry_type="dictation",
             app_context="PRIVATE DOCUMENT TITLE"),
        dict(id=2, timestamp=datetime.now().isoformat(), raw_text="snap right", display_text="snap right",
             status="success", entry_type="command", matched_command="snap right"),
        dict(id=1, timestamp=datetime.now().isoformat(), raw_text="", display_text="[No speech]",
             status="empty", entry_type="failed"),
    ]


@pytest.fixture(autouse=True)
def isolate(monkeypatch, qapp):
    old_font = QFont(qapp.font())
    qapp.setFont(QFont("Segoe UI", 9))
    monkeypatch.setattr(home_qt, "QSettings", _FakeSettings)
    _FakeSettings.values = {home_qt.WHATS_NEW_SEEN_KEY: "999.0.0"}
    monkeypatch.setattr(home_signals, "corrections_waiting", lambda app: 0)
    monkeypatch.setattr(home_signals, "_user_words_exist", lambda: True)
    monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
    monkeypatch.setattr(home_qt, "whats_new_note", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_window_qt._MainWindow, "_geometry_screens",
                        lambda self: ([(0, 0, 1920, 1040)], (0, 0, 1920, 1040)))
    monkeypatch.setattr(privacy, "sharing_detected", lambda *args, **kw: False)
    yield
    qapp.setFont(old_font)


@pytest.fixture
def manager(tmp_path):
    db = HistoryManager(tmp_path / "history.db")
    yield db
    db._conn.close()


@pytest.mark.parametrize("reason", ["password_field", "unverified_target"])
def test_password_and_unknown_row_store_no_text(manager, monkeypatch, reason):
    monkeypatch.setattr(privacy, "focused_target", lambda: privacy.TargetPrivacy(reason, "notepad.exe"))
    app = SimpleNamespace(history_db=manager)
    row_id = privacy.write_history(app, raw_text="invented violet castle", display_text="invented violet castle",
                                   app_context="private title", entry_type="dictation")
    row = dict(manager.recent()[0])
    assert row["id"] == row_id
    assert row["raw_text"] == row["display_text"] == row["app_context"] == ""
    assert row["privacy_reason"] == reason
    assert privacy.legacy_text(app, "invented violet castle") == ""
    manager.update_status(row_id, "success", "must stay concealed")
    assert manager.recent()[0]["display_text"] == ""
    assert presentation(row, True)[0] == "hidden"


def test_known_nonpassword_preserves_history(manager, monkeypatch):
    monkeypatch.setattr(privacy, "focused_target", lambda: privacy.TargetPrivacy("", "notepad.exe"))
    privacy.write_history(SimpleNamespace(history_db=manager), raw_text="invented meadow", entry_type="dictation")
    row = manager.recent()[0]
    assert row["raw_text"] == "invented meadow"
    assert row["target_app"] == "notepad.exe"


def test_target_changes_to_password_between_history_and_injection(manager, monkeypatch):
    manager.add("invented meadow", display_text="Invented meadow.")
    saves = []
    app = SimpleNamespace(history_db=manager, history=[("fixture", "Invented meadow.", False)],
                          save_history=lambda: saves.append(True))
    monkeypatch.setattr(privacy, "focused_target", lambda: privacy.TargetPrivacy("password_field"))
    assert privacy.before_delivery(app, "Invented meadow. ")
    row = dict(manager.recent()[0])
    assert row["raw_text"] == row["display_text"] == ""
    assert row["privacy_reason"] == "password_field"
    assert app.history == [("fixture", "", False)]
    assert saves
    # Focus has moved away after delivery: the later session History write is still hidden.
    monkeypatch.setattr(privacy, "focused_target", lambda: privacy.TargetPrivacy(""))
    privacy.write_history(app, raw_text="invented meadow", entry_type="dictation")
    assert manager.recent()[0]["raw_text"] == ""


def test_delivery_redaction_invalidates_already_loaded_home_snapshot(manager, monkeypatch, qapp):
    row_id = manager.add("invented meadow", display_text="Invented meadow.")
    app = _app()
    app.history_db = manager
    widget = RecentActivity(app, lambda page: None, lambda: "Hold Alt to talk")
    try:
        widget.rows = [dict(manager.recent()[0])]
        widget._sharing = False
        widget.resize(760, 100)
        widget.fit(250)
        assert "invented meadow" in widget._slots[0][1].text()
        app._notify_main_window = lambda text: widget._paint_rows()
        monkeypatch.setattr(privacy, "focused_target", lambda: privacy.TargetPrivacy("password_field"))
        assert privacy.before_delivery(app, "Invented meadow.")
        assert app._recent_sensitive_ids[row_id] == "password_field"
        assert widget._slots[0][1].text() == "hidden"
        assert widget.rows[0]["raw_text"] == ""
        assert widget._slots[0][3].text() == "Open in History"
    finally:
        widget.deleteLater()


def test_redaction_failure_cancels_injection(monkeypatch):
    def fail(*args):
        raise OSError("fixture")
    app = SimpleNamespace(history_db=SimpleNamespace(conceal_delivery=fail))
    monkeypatch.setattr(privacy, "focused_target", lambda: privacy.TargetPrivacy("password_field"))
    assert not privacy.before_delivery(app, "invented meadow")


@pytest.mark.parametrize("typed", [True, False])
def test_real_delivery_seams_redact_before_injecting(manager, monkeypatch, typed):
    from samsara import paste
    manager.add("invented meadow", display_text="Invented meadow.")
    app = SimpleNamespace(config={}, history_db=manager,
                          _foreground_wants_typed_injection=lambda: typed,
                          _flight_foreground_process_name=lambda: "notepad.exe",
                          _record_undoable_paste=lambda *a, **kw: None,
                          adaptive_learner=SimpleNamespace(record_transcription=lambda text: None))
    monkeypatch.setattr(privacy, "focused_target", lambda: privacy.TargetPrivacy("password_field"))
    monkeypatch.setattr(paste.injection_safety, "window_integrity", lambda: None)
    monkeypatch.setattr(paste, "_editor_snapshot", lambda: None)
    monkeypatch.setattr(paste.delivery_verify, "snapshot", lambda: None)
    monkeypatch.setattr(paste.flight_recorder, "record", lambda *a, **kw: None)
    monkeypatch.setattr(paste.recovery, "current_attempt", lambda: None)
    injected = []

    def inject(text, **kwargs):
        assert manager.recent()[0]["raw_text"] == ""
        injected.append(True)
        return True

    def clipboard(text, before_paste, delivery_observer, **kwargs):
        assert before_paste()
        delivery_observer(paste.DeliveryOutcome.CONFIRMED)
        return inject(text)

    result = paste.paste_preserving_clipboard(
        app, "Invented meadow. ", return_delivery_confirmation=True,
        type_text_unicode_fn=inject, paste_with_preservation_fn=clipboard,
        get_foreground_hwnd_fn=lambda: 7)
    assert result[0] and injected


def test_probe_timeout_and_provider_failure_conceal(monkeypatch):
    def fail():
        raise RuntimeError("fixture provider failure")
    monkeypatch.setattr(privacy, "_read_target", fail)
    assert privacy.focused_target().sensitive
    release = threading.Event()
    monkeypatch.setattr(privacy, "_read_target", lambda: (release.wait(1), privacy.TargetPrivacy(""))[1])
    try:
        assert privacy.focused_target(timeout=.001).sensitive
        assert privacy.focused_target(timeout=.001).sensitive
    finally:
        release.set()


def test_old_schema_migrates_without_exposing_tagged_content(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, timestamp TEXT, app_context TEXT, "
                       "raw_text TEXT, display_text TEXT, duration_ms INTEGER, mode TEXT, status TEXT, "
                       "audio_path TEXT, correction_source TEXT)")
    connection.commit()
    connection.close()
    db = HistoryManager(path)
    try:
        db.add("fixture", privacy_reason="password_field", audio_path="fixture.wav")
        row = db.recent()[0]
        assert row["raw_text"] == row["display_text"] == ""
        assert row["audio_path"] is None
    finally:
        db._conn.close()


@pytest.mark.parametrize("foreground,running,expected", [
    ("Zoom.exe", [], True), ("ms-teams.exe", [], True), ("Teams.exe", [], True),
    ("Discord.exe", [], True), ("notepad.exe", ["obs64.exe"], True),
    ("notepad.exe", ["discord.exe"], False), ("notepad.exe", [], False),
])
def test_share_guard_list(foreground, running, expected, monkeypatch):
    # Restore the actual pure list check, with no process enumeration.
    monkeypatch.undo()
    assert privacy.sharing_detected(foreground, running) is expected


@pytest.mark.parametrize("status,expected", [
    ("failed", "Not typed"), ("refused", "Not typed"), ("buffered", "Saved to clipboard"),
    ("transcribing", "Transcribing"), ("unknown", "Outcome unavailable"),
])
def test_outcomes_do_not_turn_unknown_or_failed_into_success(status, expected):
    row = {**fixture_rows()[0], "status": status}
    assert presentation(row, True)[1] == expected


def test_hidden_mode_conceals_command_names_and_private_titles():
    for row in fixture_rows():
        text = " ".join(map(str, presentation(row, False)))
        assert row.get("raw_text", "") not in text if row.get("raw_text") else True
        assert "PRIVATE DOCUMENT TITLE" not in text
    assert presentation(fixture_rows()[0], False)[1] == "Typed 7 words"


def settle(qapp):
    for _ in range(20):
        qapp.processEvents()
    QTest.qWait(20)
    qapp.processEvents()


@pytest.mark.parametrize("width,height,count", [(1065, 906, 3), (900, 661, 2), (700, 500, 0)])
def test_strip_rows_fit_real_host_and_stats_never_move(qapp, monkeypatch, width, height, count):
    app = _app()
    app.history_store.query = lambda **kw: fixture_rows()[:kw.get("limit", 3)]
    win = _hub(qapp, width=width, height=height, app=app)
    try:
        win.apply_theme()
        settle(qapp)
        home = win._panel_cache["Home"]
        recent = home._recent
        # 843 / 861: actual reserved cards/footer determine capacity, not old stretch.
        assert 0 <= recent.capacity <= 3
        if width == 700:
            assert recent.capacity == 0
        if width == 1065:
            assert recent.capacity == 3
        assert recent.isVisibleTo(home) is bool(recent.capacity)
        for slot in recent._slots[:recent.capacity]:
            assert slot[3] in home.tab_chain
        stats = home._identity.mapTo(home, home._identity.rect().topLeft()).y()
        scrollbar = home._scroll.verticalScrollBar().maximum()
        # 843 / 861: changing visible activity changes preceding content; resizing spare height does not.
        recent_height = recent.height() if recent.capacity else 0
        monkeypatch.setattr(recent, "fit", lambda free: recent.hide())
        recent.hide()
        settle(qapp)
        expected = stats - (recent_height + 24 if recent_height else 0)
        assert home._identity.mapTo(home, home._identity.rect().topLeft()).y() == expected
        assert home._scroll.verticalScrollBar().maximum() == scrollbar
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_resize_drops_rows_and_large_text_uses_scaled_budget(qapp):
    app = _app()
    app.history_store.query = lambda **kw: fixture_rows()
    win = _hub(qapp, app=app)
    try:
        settle(qapp)
        home = win._panel_cache["Home"]
        assert home._recent.capacity == 3
        win.resize(900, 661)
        settle(qapp)
        # 843 / 861: no optional preview may force required content off-screen.
        assert 0 <= home._recent.capacity <= 2
        if home._recent.capacity:
            assert home._scroll.verticalScrollBar().maximum() == 0
        win.resize(700, 500)
        settle(qapp)
        assert home._recent.capacity == 0
        assert all(not heard.text() for _, heard, _, _ in home._recent._slots)
        assert row_budget(233) == 3
        assert row_budget(232) == 2
        assert row_budget(177) == 2
        assert row_budget(121) == 1
        assert row_budget(120) == 0
        assert row_budget(233 * 1.5, 1.5) == 3
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_empty_state_uses_effective_activation_and_no_false_words(qapp):
    widget = RecentActivity(_app(), lambda page: None, lambda: "Tap Alt to start and stop")
    try:
        widget.resize(760, 100)
        widget.fit(200)
        assert widget.empty.text() == "Nothing heard yet. Tap Alt to start and stop."
        assert not any(slot[1].text() for slot in widget._slots)
    finally:
        widget.deleteLater()


def test_setting_off_and_share_guard_remove_all_label_text_and_disable_fix(qapp, monkeypatch):
    app = _app()
    saved = []
    app.save_config = lambda: saved.append(True)
    widget = RecentActivity(app, lambda page: None, lambda: "Hold Alt to talk")
    try:
        widget.rows = fixture_rows()
        widget._sharing = False
        widget.resize(760, 212)
        widget.fit(300)
        assert "blue kite" in widget._slots[0][1].text()
        widget.hide_words()
        assert saved and app.config["home"]["show_recent_words"] is False
        assert widget._slots[0][3].text() == "Open in History"
        for label in widget.findChildren(QLabel):
            assert "blue kite" not in label.text()
            assert "blue kite" not in label.accessibleName()
            assert not label.toolTip()
            assert not label.accessibleDescription()
        app.config["home"]["show_recent_words"] = True
        monkeypatch.setattr(privacy, "sharing_detected", lambda: True)
        widget.refresh()
        assert widget._slots[0][1].text() == "hidden"
        assert "snap right" not in widget._slots[1][2].text()
    finally:
        widget.deleteLater()


def test_fix_word_uses_selected_saved_capture_and_existing_ui(qapp, monkeypatch):
    from samsara.ui import correction_capture_qt
    opened, pages = [], []
    monkeypatch.setattr(correction_capture_qt.CorrectionCaptureQt, "open", lambda self, text: opened.append(text))
    widget = RecentActivity(_app(), pages.append, lambda: "Hold Alt to talk")
    try:
        widget.rows = fixture_rows()
        widget._activate(0)
        assert opened == [fixture_rows()[0]["display_text"]]
        widget._activate(1)
        widget._activate(2)
        assert pages == ["History", "History"]
    finally:
        widget.deleteLater()


def test_query_failure_is_not_fresh_install(qapp):
    widget = RecentActivity(_app(), lambda page: None, lambda: "Hold Alt to talk")
    try:
        widget.fit(250)
        widget._accept(None, widget._generation)
        assert widget.empty.text() == "Recent history unavailable"
    finally:
        widget.deleteLater()


def test_schema_default_and_general_settings_roundtrip(qapp, monkeypatch):
    from samsara.ui import settings_qt
    from samsara.ui.settings import general_page
    from tests.test_settings_pages import _StubApp, _schema_defaults
    assert SETTINGS_SCHEMA["home.show_recent_words"]["default"] is True
    app = _StubApp()
    app.config = _schema_defaults()
    app.config["home"] = {"show_recent_words": False, "fixture_neighbor": 17}
    win = settings_qt._SettingsWindow(app)
    try:
        win.show_tab("General")
        control = win._widgets["home_recent_words"]
        page = win._pages["General"]
        row = win._rows_by_widget["home_recent_words"]
        assert len(general_page.BASE) == 7
        assert len(general_page.ADVANCED) == 5
        assert row.disclosure is page.advanced
        page.advanced.toggle.setChecked(False)
        assert not control.isVisibleTo(page)
        page.advanced.toggle.setChecked(True)
        assert control.isVisibleTo(page)
        assert not control.isChecked()
        control.setChecked(True)
        updates = {}
        # The source save callback only collects updates; no app/lifecycle call.
        monkeypatch.setattr("samsara.autostart.sync_run_entry", lambda enabled: None)
        for save in win._source_saves["general"]:
            updates.update(save(updates))
        assert updates["home"] == {"show_recent_words": True, "fixture_neighbor": 17}
    finally:
        win.deleteLater()


def test_invalid_times_and_clock_changes_do_not_break_preview():
    assert relative_time("bad date") == ""
    assert relative_time("2030-01-01", now=datetime(2020, 1, 1)) == "0s ago"
