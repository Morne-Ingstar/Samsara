from __future__ import annotations

import types

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent

from samsara.ui import memos_qt


def _page(qapp, tmp_path, config=None):
    app = types.SimpleNamespace(config=config or {})
    page = memos_qt.MemosPage(app, home=tmp_path)
    page.resize(760, 560)
    page.show()
    qapp.processEvents()
    return page, app


def test_voice_button_starts_capture_and_reports_configured_stop_key(qapp, tmp_path):
    page, app = _page(qapp, tmp_path, {"memo_hotkey": "ctrl+shift+k"})
    started = []
    app.start_memo_capture = lambda: started.append(True) or True

    page._record_voice_btn.click()

    assert started == [True]
    assert "Ctrl+Shift+K" in page.status_text
    assert "finish" in page.status_text


def test_video_button_dispatches_through_command_executor(qapp, tmp_path):
    calls = []
    app = types.SimpleNamespace(
        config={"cancel_hotkey": "ctrl+shift+escape"},
        command_executor=types.SimpleNamespace(
            process_text=lambda *args, **kwargs: calls.append((args, kwargs)) or True))
    page = memos_qt.MemosPage(app, home=tmp_path)

    page._record_video_btn.click()

    assert calls == [(("record video memo", app), {"force_commands": True})]
    assert "Ctrl+Shift+Escape" in page.status_text


def test_key_line_refreshes_from_config_and_settings_opens_modes(qapp, tmp_path, monkeypatch):
    page, app = _page(qapp, tmp_path, {"memo_hotkey": "ctrl+alt+m"})
    app.config["memo_hotkey"] = "ctrl+shift+k"
    page.reload()
    assert "Ctrl+Shift+K" in page._memo_keys.text()
    assert 'say "record video memo"' in page._memo_keys.text()

    opened = []
    from samsara.ui import home_qt
    monkeypatch.setattr(home_qt, "open_settings_tab",
                        lambda target_app, tab: opened.append((target_app, tab)) or True)
    page._settings_link.click()
    assert opened == [(app, "Modes")]


def test_record_shortcuts_and_tab_order(qapp, tmp_path):
    page, _app = _page(qapp, tmp_path)
    called = []
    page.record_voice_memo = lambda: called.append("voice")
    page.record_video_memo = lambda: called.append("video")
    page.open_memo_settings = lambda: called.append("settings")
    for key in (Qt.Key.Key_R, Qt.Key.Key_V, Qt.Key.Key_S):
        event = QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.AltModifier)
        page.keyPressEvent(event)
        assert event.isAccepted()
    assert called == ["voice", "video", "settings"]
    assert page.tab_chain[:3] == [page._record_voice_btn, page._record_video_btn,
                                  page._settings_link]
    assert "Alt+S" in page._settings_link.accessibleName()
    assert "Alt+R" in page._record_voice_btn.accessibleName()
    assert "Alt+V" in page._record_video_btn.accessibleName()


def test_empty_state_points_to_record_buttons(qapp, tmp_path):
    page, _app = _page(qapp, tmp_path)
    assert page._empty.isVisible()
    assert page._empty.text() == memos_qt.EMPTY_MEMOS


def test_record_buttons_and_search_fit_their_contents(qapp, tmp_path):
    page, _app = _page(qapp, tmp_path)
    for button in (page._record_voice_btn, page._record_video_btn):
        assert button.width() == button.sizeHint().width()
    assert page._search.width() < page.width() // 2

