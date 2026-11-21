"""Commands and Vocabulary in their real Qt editors, using synthetic data."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QToolButton

from samsara.command_packs import PACKS
from samsara.dictionary_store import DictionaryStore
from samsara.ui.settings import agents_section, tools_page
from samsara.ui.settings_qt import SettingsQt
from tests.test_builtin_vocabulary_801 import builtin_panel
from tests.test_friendlier_settings_595 import window


def _visible_tab_stops(scope, qapp):
    scope.window().activateWindow()
    qapp.processEvents()
    start = qapp.focusWidget()
    assert start is not None
    stops = []
    for _ in range(120):
        current = qapp.focusWidget()
        if current is scope or scope.isAncestorOf(current):
            stops.append(current)
        scope.window().focusNextChild()
        if qapp.focusWidget() is start:
            return stops
        assert qapp.focusWidget() is not current
    pytest.fail("Tab did not return to its starting point")


def _y(widget, parent):
    return widget.mapTo(parent, QPoint()).y()


def _visible_in_scroll(widget, scroll):
    return scroll.viewport().rect().contains(widget.mapTo(scroll.viewport(), widget.rect().center()))


def test_commands_order_tab_and_pack_save(window, qapp, monkeypatch):
    window.resize(1160, 680)
    window._select_tool("Command library")
    page = window._tool_pages["Command library"]
    widgets = window._widgets
    packs = widgets["_pack_scroll"]
    controls = widgets["_pack_checkboxes"]
    positions = [next(label for label in page.findChildren(QLabel) if label.text() == "Commands"),
                 widgets["cmd_search"], widgets["cmd_table"], page.findChild(QPushButton, "addCommandButton"),
                 next(label for label in page.findChildren(QLabel)
                      if label.text() == "Changes to voice commands apply immediately."),
                 packs, widgets["_command_profiles_entry"]]
    ys = [_y(widget, page.widget()) for widget in positions]
    assert ys == sorted(set(ys))
    order = ["accessibility", "text-editing", "window-management"]
    order += [key for key in PACKS if key not in (*order, "experimental", "ai", "agents", "hermes")]
    order.append("experimental")
    assert [key for key in order if packs.isAncestorOf(controls[key])] == order
    assert sorted(order, key=lambda key: controls[key].parentWidget().y()) == order
    for key in ("core", "session"):
        assert controls[key].isChecked() and controls[key].isHidden()
        assert not controls[key].isEnabled()

    widgets["cmd_search"].setFocus()
    stops = _visible_tab_stops(page, qapp)
    assert stops.index(widgets["cmd_table"]) < stops.index(packs)
    assert [stop for stop in stops if stop is packs or packs.isAncestorOf(stop)] == [packs]
    widgets["cmd_table"].setFocus()
    QTest.keyClick(widgets["cmd_table"], Qt.Key.Key_Tab)
    assert qapp.focusWidget() is page.findChild(QPushButton, "addCommandButton")
    reload_button = page.findChild(QPushButton, "reloadCommandsButton")
    reload_button.setFocus()
    QTest.keyClick(reload_button, Qt.Key.Key_Tab)
    assert qapp.focusWidget() is packs
    QTest.keyClick(packs, Qt.Key.Key_Tab)
    assert qapp.focusWidget() is widgets["_command_profiles_button"]

    packs.setFocus()
    first, second, last = (controls[key] for key in ("accessibility", "text-editing", "experimental"))
    checked = [control.isChecked() for control in (first, second, last)]
    QTest.keyClick(packs, Qt.Key.Key_Home)
    QTest.keyClick(packs, Qt.Key.Key_Space)
    QTest.keyClick(packs, Qt.Key.Key_Down)
    QTest.keyClick(packs, Qt.Key.Key_Space)
    QTest.keyClick(packs, Qt.Key.Key_End)
    QTest.keyClick(packs, Qt.Key.Key_Space)
    assert [control.isChecked() for control in (first, second, last)] == [not value for value in checked]
    assert qapp.focusWidget() is packs
    assert _visible_in_scroll(last, packs)
    notice = widgets["_pack_restart_lbl"]
    assert notice.isVisible() and notice.text() == "Restart Samsara to apply pack changes."

    # Exercise the actual Apply path without reaching runtime services or devices.
    monkeypatch.setattr("samsara.listening_display.run_display", lambda app: None)
    monkeypatch.setattr("samsara.listening_display.request_restart_once", lambda *args: None)
    monkeypatch.setattr("samsara.ui.command_pack_actions.refresh_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr("samsara.wake_setup.calibration_needed", lambda config: False)
    monkeypatch.setattr(window, "_replace_tts_runtime", lambda *args: None)
    before = dict(window.app.config.get("command_packs", {}))
    before["unknown-legacy-pack"] = True
    window.app.config["command_packs"] = before.copy()
    saved = []
    monkeypatch.setattr(window.app, "save_config", lambda: saved.append(window.app.config["command_packs"].copy()))
    window._apply_button.click()
    assert saved
    assert saved[-1]["accessibility"] == (not checked[0])
    assert saved[-1]["text-editing"] == (not checked[1])
    assert saved[-1]["experimental"] == (not checked[2])
    for key in ("unknown-legacy-pack", "core", "session", "ai", "hermes"):
        assert saved[-1].get(key) == before.get(key)


@pytest.mark.parametrize("first", ["Command library", "Integrations"])
def test_agents_checkbox_is_shared_in_both_lazy_orders(window, qapp, monkeypatch, first):
    synced = []
    server = SimpleNamespace(status="off", port=47832, connection_details=lambda: None,
                             sync=lambda: synced.append(True))
    monkeypatch.setattr(agents_section, "for_app", lambda app: server)
    second = "Integrations" if first == "Command library" else "Command library"
    window._select_tool(first)
    qapp.processEvents()
    assert set(window._tool_pages) == {first}
    cb = window._widgets["_pack_checkboxes"]["agents"]
    window._select_tool(second)
    qapp.processEvents()
    commands = window._tool_pages["Command library"]
    integrations = window._tool_pages["Integrations"]
    (agents,) = integrations.findChildren(agents_section.AgentsControls)
    assert agents.enabled is cb
    assert [control for control in integrations.findChildren(QCheckBox)
            if control.accessibleName() == "Let AI apps use Samsara"] == [cb]
    window._select_tool("Command library")
    assert not commands.isAncestorOf(cb)
    assert not window._widgets["_pack_checkboxes"]["ai"].isVisible()
    assert window._widgets["_pack_scroll"].isVisible()
    window._select_tool("Integrations")
    qapp.processEvents()
    assert cb.isVisible() and cb.focusPolicy() & Qt.FocusPolicy.TabFocus
    original = cb.isChecked()
    cb.click()
    assert window.app.config["command_packs"]["agents"] is not original
    assert synced == [True]
    assert window._widgets["_pack_restart_lbl"].isHidden()


def test_search_and_open_packs_land_on_bounded_list(window, qapp, monkeypatch):
    from samsara.ui import qt_runtime

    window.resize(1160, 680)
    assert not window._tool_pages
    search = window._search_edit
    search.setFocus()
    search.setText("Spotify")
    window._search_debounce.stop()
    window._apply_search_filter()
    qapp.processEvents()
    assert window._tool_name == "Command library"
    assert qapp.focusWidget() is search
    page = window._tool_pages["Command library"]
    packs = window._widgets["_pack_scroll"]
    spotify = window._widgets["_pack_checkboxes"]["spotify"]
    assert page.verticalScrollBar().value() > 0
    assert packs.verticalScrollBar().value() > 0
    assert _visible_in_scroll(packs, page) and _visible_in_scroll(spotify, packs)

    search.clear()
    window._search_debounce.stop()
    window._select_tool("Command library")
    qapp.processEvents()
    assert page.verticalScrollBar().value() == 0
    monkeypatch.setattr(qt_runtime, "post", lambda action: action())
    controller = SettingsQt(window.app)
    controller._window = window
    tools_page.open_packs(controller)
    qapp.processEvents()
    assert qapp.focusWidget() is packs
    assert page.verticalScrollBar().value() > 0
    assert _visible_in_scroll(packs, page)


def test_vocabulary_order_tab_and_word_save(builtin_panel, qapp, monkeypatch):
    from samsara.ui import dictionary_panel_qt

    panel, dictionary = builtin_panel
    panel.resize(820, 720)
    panel.show()
    panel.activateWindow()
    qapp.processEvents()
    page = panel._tabs.currentWidget()
    buttons = {button.text(): button for button in page.findChildren(QPushButton)}
    more = next(button for button in page.findChildren(QToolButton)
                if button.accessibleName() == "More dictionary actions")
    ys = [_y(widget, page) for widget in (
        panel._voice_expander, panel._vocab_input, panel._vocab_list, buttons["Remove Selected"],
        panel._builtin_expander, panel._builtin_enabled)]
    assert ys == sorted(set(ys))
    assert _y(buttons["Import"], page) == _y(buttons["Export"], page) == _y(more, page)
    for label in ("Import", "Export"):
        accessible = QAccessible.queryAccessibleInterface(buttons[label])
        assert accessible.text(QAccessible.Text.Name) == label
        assert accessible.text(QAccessible.Text.Description) == f"{label} words"
    assert [action.text() for action in more.menu().actions()] == ["Export dictionary…", "Import dictionary…"]
    panel._vocab_input.setFocus()
    collapsed = _visible_tab_stops(page, qapp)
    panel._builtin_expander.click()
    qapp.processEvents()
    expanded = _visible_tab_stops(page, qapp)
    words = panel._builtin_scroll
    insert = collapsed.index(panel._builtin_enabled) + 1
    assert expanded == [*collapsed[:insert], words, *collapsed[insert:]]
    panel._builtin_enabled.setFocus()
    QTest.keyClick(panel._builtin_enabled, Qt.Key.Key_Tab)
    assert qapp.focusWidget() is words
    QTest.keyClick(words, Qt.Key.Key_Tab)
    assert qapp.focusWidget() is not words and not words.isAncestorOf(qapp.focusWidget())
    assert words.height() == 220 and words.verticalScrollBar().maximum() > 0
    first, second, *_ = list(panel._builtin_switches.values())
    interface = QAccessible.queryAccessibleInterface(second)
    assert interface.role() == QAccessible.Role.CheckBox
    assert interface.text(QAccessible.Text.Name) == f"Use built-in word {list(panel._builtin_switches)[1]}"
    assert first.isChecked() and second.isChecked()

    # Leave the simulated disk write pending long enough to check focus and serialization.
    pending = []
    monkeypatch.setattr(dictionary_panel_qt.thread_registry, "spawn",
                        lambda name, target, daemon=True: pending.append(target))
    words.setFocus()
    QTest.keyClick(words, Qt.Key.Key_Home)
    QTest.keyClick(words, Qt.Key.Key_Down)
    QTest.keyClick(words, Qt.Key.Key_Space)
    assert qapp.focusWidget() is words
    QTest.keyClick(words, Qt.Key.Key_Space)
    assert len(pending) == 1
    pending.pop()()
    qapp.processEvents()
    assert first.isChecked() and not second.isChecked() and qapp.focusWidget() is words
    second_word = list(panel._builtin_switches)[1]
    entries = {entry["word"]: entry["enabled"] for entry in DictionaryStore(dictionary.app).get_builtin_words()}
    assert entries[second_word] is False
    assert dictionary.app.config.get("builtin_vocabulary_enabled", True) is True
