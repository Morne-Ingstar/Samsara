"""Deferred first-run and dictionary callbacks must recover safely."""

from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QLabel, QLineEdit, QListWidget, QListWidgetItem, QTableWidget, QWidget

from samsara.ui.command_cheatsheet_qt import _CheatSheetWindow
from samsara.ui import dictionary_panel_qt, first_run_qt
from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
from samsara.ui.first_run_qt import ComponentsPage
from samsara.ui.voice_training_qt import VoiceTrainingQt


@pytest.fixture
def component_page(qapp, tmp_path):
    page = ComponentsPage(QWidget(), on_later=lambda: None,
                          manifest_loader=lambda: {}, run_in_thread=True,
                          app_root=tmp_path / "app", downloads_dir=tmp_path / "downloads")
    callbacks = []
    page._post = callbacks.append
    return page, callbacks


def _component_page_with_row(page, tmp_path):
    page._manifest = {"components": [{
        "id": "extra", "name": "Extra model", "description": "Optional model",
        "kind": "model", "available": True, "filename": "extra.zip",
        "install_dir": "models", "requires": [], "default": False,
        "url": "https://example.invalid/extra.zip", "size_bytes": 10,
        "sha256": "0" * 64, "min_disk_bytes": 20,
    }]}
    page._rebuild()
    assert "extra" in page._rows


def test_manifest_check_is_user_started_and_failure_is_visible(
        component_page, monkeypatch):
    page, callbacks = component_page
    calls = []

    def offline():
        calls.append("manifest")
        raise OSError("offline")

    page._loader = offline
    monkeypatch.setattr(first_run_qt.thread_registry, "spawn",
                        lambda _name, work, daemon=True: work())
    assert calls == []
    assert page._check_btn.text() == "Check what's available"

    page._check_btn.click()
    assert calls == ["manifest"]
    assert not page._check_btn.isEnabled()
    assert len(callbacks) == 1
    callbacks.pop()()

    assert "unavailable" in page._status.text()
    assert page._check_btn.isEnabled()
    assert page._check_btn.text() == "Check what's available"
    assert not page._later_btn.isHidden()


@pytest.mark.parametrize("cancelled", [False, True], ids=["failed", "cancelled"])
def test_component_install_failure_and_cancel_restore_get(
        component_page, monkeypatch, tmp_path, cancelled):
    page, callbacks = component_page
    _component_page_with_row(page, tmp_path)

    def fail_or_cancel(_component, *_args, cancel, **_kwargs):
        if cancelled:
            page.cancel_fetch("extra")
            assert cancel()
            raise first_run_qt.FetchCancelled()
        raise OSError("offline")

    monkeypatch.setattr(first_run_qt, "fetch_and_install", fail_or_cancel)
    monkeypatch.setattr(first_run_qt.thread_registry, "spawn",
                        lambda _name, work, daemon=True: work())
    page.start_fetch("extra")
    assert len(callbacks) == 1
    callbacks.pop()()

    row = page._rows["extra"]
    assert row["get"].isEnabled()
    assert row["cancel"].isHidden()
    assert row["bar"].isHidden()
    assert not page._later_btn.isHidden()
    assert ("Cancelled" if cancelled else "Could not install") in row["result"].text()


def test_dictionary_save_failure_is_visible_and_callback_guards_children(
        qapp, tmp_path, monkeypatch):
    (tmp_path / "training_data.json").write_text(
        '{"vocabulary": [], "corrections": {}}', encoding="utf-8")
    app = SimpleNamespace(config_path=tmp_path / "config.json",
                          config={"initial_prompt": ""})
    app.voice_training_window = VoiceTrainingQt(app)
    panel = DictionaryPanelQt(app)
    panel._vt.add_correction = lambda *_args: (_ for _ in ()).throw(OSError("denied"))
    monkeypatch.setattr(dictionary_panel_qt.thread_registry, "spawn",
                        lambda _name, work, daemon=True: work())

    table, heard, replacement, status = QTableWidget(), QLineEdit(), QLineEdit(), QLabel()
    heard.setText("heard")
    replacement.setText("replacement")
    panel._kv_add(table, heard, replacement, "corrections", status)
    qapp.processEvents()

    assert status.text() == "Save failed: denied"


def test_dictionary_save_error_ignores_deleted_child(qapp, tmp_path, monkeypatch):
    (tmp_path / "training_data.json").write_text(
        '{"vocabulary": [], "corrections": {}}', encoding="utf-8")
    app = SimpleNamespace(config_path=tmp_path / "config.json",
                          config={"initial_prompt": ""})
    app.voice_training_window = VoiceTrainingQt(app)
    panel = DictionaryPanelQt(app)
    panel._vt.add_correction = lambda *_args: (_ for _ in ()).throw(OSError("denied"))
    monkeypatch.setattr(dictionary_panel_qt.thread_registry, "spawn",
                        lambda _name, work, daemon=True: work())
    callbacks = []
    monkeypatch.setattr(QTimer, "singleShot", lambda *args: callbacks.append(args[-1]))

    table, heard, replacement, status = QTableWidget(), QLineEdit(), QLineEdit(), QLabel()
    heard.setText("heard")
    replacement.setText("replacement")
    panel._kv_add(table, heard, replacement, "corrections", status)
    assert len(callbacks) == 1
    shiboken6.delete(status)
    callbacks[0]()


def test_cheatsheet_details_keep_no_removed_item_or_flash_timer(qapp, monkeypatch):
    owner = QWidget()
    owner._item_generation = 3
    owner._list = QListWidget(owner)
    executed, details = [], []
    owner._execute = executed.append
    owner._detail_card = SimpleNamespace(parentWidget=lambda: owner)
    owner._show_details = lambda *args: details.append(args)
    item = QListWidgetItem("Test command")
    owner._list.addItem(item)
    item.setData(Qt.ItemDataRole.UserRole, "test command")
    item.setData(Qt.ItemDataRole.UserRole + 2, owner._item_generation)
    scheduled = []
    monkeypatch.setattr(QTimer, "singleShot",
                        lambda delay, context, callback: scheduled.append((delay, context, callback)))

    _CheatSheetWindow._on_item_clicked(owner, item)
    assert not scheduled and not executed
    assert details[0][0] == "test command"
    assert not any(isinstance(value, QListWidgetItem) for value in details[0])
    owner._list.clear()
    assert details[0][0] == "test command"
