from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from samsara.ui import settings_qt
from tests.test_settings_pages import _StubApp, _schema_defaults


def _window(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / "samsara-home"))
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    monkeypatch.setattr(autostart, "sync_run_entry", lambda *a, **kw: None)
    monkeypatch.setattr(ComponentsPage, "refresh_async", lambda self: None)
    return settings_qt._SettingsWindow(_StubApp(_schema_defaults()))


def test_filtered_advanced_disclosure_keeps_a_visible_empty_state(qapp, monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    proof = Path(r"C:\Users\Morne\Documents\Claude\ui_proof\312")
    proof.mkdir(parents=True, exist_ok=True)
    try:
        window.show_tab("Dictation")
        page = window._pages["Dictation"]
        window._search_edit.setText("312-no-advanced-match")
        window._apply_search_filter()
        QTest.mouseClick(page.advanced.toggle, Qt.MouseButton.LeftButton)
        qapp.processEvents()

        assert page.advanced.toggle.isChecked()
        assert not any(
            row.isVisible() for row in window._destination_rows
            if row.page_name == "Dictation" and row.disclosure is page.advanced
        )
        # Capture the pre-fix state by hiding the new empty-state label after
        # the same synthetic click. QWidget.grab() captures the whole window.
        page.advanced.no_match.hide()
        QWidget.grab(window).save(str(proof / "312-advanced-before.png"))
        page.advanced.refresh_filter_state("312-no-advanced-match")
        qapp.processEvents()
        QWidget.grab(window).save(str(proof / "312-advanced-after.png"))
        assert page.advanced.no_match.isVisible()
        assert page.isVisible()
    finally:
        window.deleteLater()


def test_search_matches_collapsed_advanced_wake_profile(qapp, monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    try:
        window.show_tab("Dictation")
        page = window._pages["Dictation"]
        assert not page.advanced.toggle.isChecked()
        window._search_edit.setText("claude")
        window._apply_search_filter()
        assert page.advanced.toggle.isChecked()
        row = next(row for row in window._destination_rows if row.title == "Wake profiles")
        assert row.isVisible()
        assert window._stack.currentWidget() is page
    finally:
        window.deleteLater()


def test_wake_filter_and_advanced_click_keep_dictation_page_visible(qapp, monkeypatch, tmp_path):
    window = _window(monkeypatch, tmp_path)
    proof = Path(r"C:\Users\Morne\Documents\Claude\ui_proof\312")
    try:
        window.show_tab("Dictation")
        page = window._pages["Dictation"]
        window._search_edit.setText("wake")
        window._apply_search_filter()
        assert page.advanced.toggle.isChecked()
        advanced_matches = [
            row.title for row in window._destination_rows
            if row.page_name == "Dictation" and row.disclosure is page.advanced and row.isVisible()
        ]
        assert "Wake profiles" in advanced_matches
        QTest.mouseClick(page.advanced.toggle, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert page.isVisible()
        assert any(row.title == "Wake activation" and row.isVisible() for row in window._destination_rows)
        QWidget.grab(window).save(str(proof / "312-wake-filter-click.png"))
        (Path(r"C:\Users\Morne\Documents\Claude\reports\312\artifacts") / "wake_filter_layout.txt").write_text(
            f"query=wake\nauto_expanded_before_click=True\nadvanced_matches={advanced_matches}\n"
            f"page_visible_after_click={page.isVisible()}\nbase_wake_activation_visible="
            f"{any(row.title == 'Wake activation' and row.isVisible() for row in window._destination_rows)}\n",
            encoding="utf-8",
        )
    finally:
        window.deleteLater()
