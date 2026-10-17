"""Queue 417: real Qt Name/role/state for each Settings tools audit row."""

from types import SimpleNamespace

import pytest
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QLabel, QLineEdit

from samsara.command_packs import PACKS


# F008-F029, in the audit's order; AI is edited on Ava, not in this tool.
PACK_ROWS = tuple(zip(
    (f"F{i:03}" for i in range(8, 30)),
    (key for key, meta in PACKS.items() if not meta.get("always_on") and key != "ai"),
))
FIELD_ROWS = (
    ("F001", "Music", "music_library_name", "Music item name", QAccessible.Role.EditableText),
    ("F002", "Music", "music_library_link", "Spotify link", QAccessible.Role.EditableText),
    ("F003", "Music", "music_library_table", "Music library", QAccessible.Role.Table),
    ("F004", "Health", "health_log", "Today's Log", QAccessible.Role.Table),
    ("F005", "Alarms", "alarms_table", "Your Alarms", QAccessible.Role.Table),
    ("F006", "Command library", "cmd_search", "Search commands", QAccessible.Role.EditableText),
    ("F007", "Command library", "cmd_table", "Voice Commands", QAccessible.Role.Table),
    ("F030", "Integrations", "todoist_token", "Todoist API token", QAccessible.Role.EditableText),
)


@pytest.fixture(scope="module")
def tools_window(qapp, qt_deferred_delete):
    from tests.test_settings_pages import _StubApp
    from samsara import health_store
    from samsara.ui.settings_qt import _SettingsWindow
    from samsara.runtime import thread_registry

    with pytest.MonkeyPatch.context() as patch:
        # No product workers, devices, private entries, or external actions.
        patch.setattr(thread_registry, "spawn", lambda *args, **kwargs: None)
        patch.setattr(health_store, "get_today", lambda: [])
        patch.setattr(health_store, "get_pain_average", lambda **kwargs: None)
        patch.setattr(health_store, "get_by_type", lambda *args, **kwargs: [])
        app = _StubApp({
            "tasks": {"todoist": {"enabled": True}},
            "music_library": {"Morning": {"type": "playlist", "uri": "spotify:playlist:synthetic"}},
            "alarms": {"items": [{"id": "water", "name": "Water", "enabled": True}]},
        })
        app.command_executor = SimpleNamespace(commands={
            "pause music": {"type": "hotkey", "pack": "media", "description": "Pause music"},
        }, find_command=lambda phrase: None)
        window = _SettingsWindow(app, tool="Music")
        window.resize(1160, 920)
        window.show()
        qapp.processEvents()
        yield window
        window.hide()
        window.deleteLater()
        qt_deferred_delete()


@pytest.mark.parametrize("row,tool,key,name,role", FIELD_ROWS, ids=[r[0] for r in FIELD_ROWS])
def test_audit_field_name_and_native_role(tools_window, qapp, row, tool, key, name, role):
    window = tools_window
    window._select_tool(tool)
    qapp.processEvents()
    widget = window._health_log_table if key == "health_log" else window._widgets[key]
    assert widget.isVisible(), row
    interface = QAccessible.queryAccessibleInterface(widget)
    assert interface is not None
    assert interface.text(QAccessible.Text.Name) == name
    assert name and name != widget.metaObject().className()
    assert interface.role() == role
    if isinstance(widget, QLineEdit):
        widget.setText("synthetic")
        assert interface.text(QAccessible.Text.Name) == name
        if key == "todoist_token":
            assert widget.echoMode() == QLineEdit.EchoMode.Password
            assert interface.state().passwordEdit
        else:
            assert interface.text(QAccessible.Text.Value) == "synthetic"
        widget.clear()


@pytest.mark.parametrize("row,pack", PACK_ROWS, ids=[r[0] for r in PACK_ROWS])
def test_audit_pack_name_role_and_checked_state(tools_window, qapp, row, pack):
    from samsara.ui.settings.presentation import Disclosure

    window = tools_window
    window._select_tool("Command library")
    disclosure = next(d for d in window._tool_pages["Command library"].findChildren(Disclosure)
                      if d.toggle.accessibleName() == "Command packs")
    disclosure.toggle.setChecked(True)
    qapp.processEvents()
    checkbox = window._widgets["_pack_checkboxes"][pack]
    assert checkbox.isVisible() and checkbox.isEnabled(), row
    label = next(label for label in checkbox.parentWidget().findChildren(QLabel)
                 if label.text().startswith(PACKS[pack]["label"]))
    interface = QAccessible.queryAccessibleInterface(checkbox)
    assert interface.text(QAccessible.Text.Name) == label.text()
    assert label.text() and label.text() != checkbox.metaObject().className()
    assert interface.role() == QAccessible.Role.CheckBox
    assert interface.state().checkable
    before = checkbox.isChecked()
    checkbox.setChecked(not before)
    assert interface.state().checked == (not before)
    checkbox.setChecked(before)
    assert interface.state().checked == before
