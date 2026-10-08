"""Qt Name/role contracts for audit 413 group C; no desktop bootstrap."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QTableWidget


@pytest.fixture(autouse=True)
def drain_widget_deletions(qapp):
    """processEvents alone does not deliver deleteLater without app.exec()."""
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def build_surface(kind, monkeypatch):
    """Construct real widgets with synthetic stores and no runtime facade."""
    app = SimpleNamespace(config={}, voice_training_window=SimpleNamespace(
        custom_vocab=["Example term"], corrections_dict={"heard": "intended"}))
    if kind == "ava":
        from samsara.ui.ava_search_panel_qt import _build_window
        window = _build_window()
        window.set_content("Example query", "Synthetic answer.",
                           [("Example source", "https://example.com/reference")])
    elif kind == "correction":
        from samsara.ui.correction_capture_qt import CorrectionCaptureWindow
        from samsara.correction_capture import extract_corrections
        window = CorrectionCaptureWindow(app, "the heard word")
        window._populate_review_queue(extract_corrections("the heard word", "the intended word"))
    elif kind == "dictionary":
        from samsara import wake_corrections
        from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
        monkeypatch.setattr(wake_corrections, "get_user_corrections", lambda: {"heard": "wake"})
        monkeypatch.setattr(wake_corrections, "get_default_corrections", lambda: {})
        window = DictionaryPanelQt(app)
        window.resize(760, 540)
    elif kind == "diagnostics":
        from samsara.ui import diagnostics_qt
        monkeypatch.setattr(diagnostics_qt.diagnostics, "recent", lambda _limit: [])
        window = diagnostics_qt.DiagnosticsWindow(app)
        window._timer.stop()
    elif kind == "benchmark":
        from samsara.ui import benchmark_review_qt
        monkeypatch.setattr(benchmark_review_qt.benchmark_store, "list_samples", lambda: [
            {"id": "synthetic", "final_text": "Synthetic transcript.", "duration_s": 1.0}])
        monkeypatch.setattr(benchmark_review_qt.benchmark_store, "stats",
                            lambda: {"gold_confirmed": 0, "total": 1})
        window = benchmark_review_qt.BenchmarkReviewWindow(app)
    elif kind == "log":
        from samsara.ui import log_viewer_qt
        monkeypatch.setattr(log_viewer_qt.LogTailer, "initial_tail", lambda _self: [])
        window = log_viewer_qt.LogViewerWindow(app)
        window._timer.stop()
        # Stable text, independent of the temporary log path.
        window._status_lbl.setText("Synthetic log")
    elif kind == "profile":
        from samsara.ui.profile_manager_qt import _ProfileManagerWindow
        pm = SimpleNamespace(get_active_profile_names=lambda: {},
                             list_dictionary_profiles=lambda: ["Example dictionary"],
                             list_command_profiles=lambda: ["Example commands"])
        window = _ProfileManagerWindow(pm)
    elif kind == "workflow":
        from samsara.ui.workflow_capture_qt import _ReviewWindow
        window = _ReviewWindow()
        window._events = [SimpleNamespace(kind="focus", label="Example editor")]
        window._rebuild_checkboxes()
    else:
        raise AssertionError(kind)
    return window


@pytest.fixture
def surface(request, qapp, monkeypatch):
    window = build_surface(request.param, monkeypatch)
    yield window
    window.hide()
    window.deleteLater()
    qapp.processEvents()


def control_for(window, row):
    attrs = {
        "F041": "sources", "F047": "_text_edit", "F054": "_vocab_input",
        "F055": "_vocab_list", "F056": "_table", "F057": "_list",
        "F058": "_text_edit", "F059": "_filter_edit", "F089": "_dict_combo",
        "F090": "_cmd_combo", "F095": "_provider",
    }
    if row in attrs:
        if row in ("F054", "F055"):
            window._tabs.setCurrentIndex(0)
        return getattr(window, attrs[row])
    index = 1 if row in ("F048", "F049", "F050") else 2
    window._tabs.setCurrentIndex(index)
    page = window._tabs.widget(index)
    if row in ("F050", "F053"):
        return page.findChild(QTableWidget)
    return page.findChildren(QLineEdit)[0 if row in ("F048", "F051") else 1]


CONTRACTS = [
    ("ava", "F041", "Web answer sources", QAccessible.Role.List),
    ("correction", "F047", "Correct Last Dictation", QAccessible.Role.EditableText),
    ("dictionary", "F048", "Heard", QAccessible.Role.EditableText),
    ("dictionary", "F049", "Should be", QAccessible.Role.EditableText),
    ("dictionary", "F050", "Corrections", QAccessible.Role.Table),
    ("dictionary", "F051", "Heard", QAccessible.Role.EditableText),
    ("dictionary", "F052", "Wake word", QAccessible.Role.EditableText),
    ("dictionary", "F053", "Wake Words", QAccessible.Role.Table),
    ("dictionary", "F054", "Add vocabulary word", QAccessible.Role.EditableText),
    ("dictionary", "F055", "Vocabulary", QAccessible.Role.List),
    ("diagnostics", "F056", "Dictation records", QAccessible.Role.Table),
    ("benchmark", "F057", "Dictation samples", QAccessible.Role.List),
    ("benchmark", "F058", "Sample transcript", QAccessible.Role.EditableText),
    ("log", "F059", "Filter log entries", QAccessible.Role.EditableText),
    ("profile", "F089", "Profile:", QAccessible.Role.ComboBox),
    ("profile", "F090", "Profile:", QAccessible.Role.ComboBox),
    ("workflow", "F095", "Analyze with:", QAccessible.Role.ComboBox),
]


@pytest.mark.parametrize("surface,row,name,role", CONTRACTS,
                         indirect=["surface"], ids=[item[1] for item in CONTRACTS])
def test_row_name_and_native_role(surface, row, name, role, qapp):
    control = control_for(surface, row)
    surface.show()
    qapp.processEvents()
    interface = QAccessible.queryAccessibleInterface(control)
    assert interface is not None
    assert interface.text(QAccessible.Text.Name) == name
    assert name != control.metaObject().className()
    assert interface.role() == role
    assert not interface.state().invisible
    # Table column text remains available in the native model.
    if row in ("F050", "F053"):
        expected = ["Heard", "Should be" if row == "F050" else "Wake word", "Source"]
        assert [control.horizontalHeaderItem(i).text() for i in range(3)] == expected


def test_buddies_use_visible_text(qapp, monkeypatch):
    for kind, attr, label in [("correction", "_text_edit", "Correct Last Dictation"),
                              ("profile", "_dict_combo", "Profile:"),
                              ("profile", "_cmd_combo", "Profile:"),
                              ("workflow", "_provider", "Analyze with:")]:
        window = build_surface(kind, monkeypatch)
        try:
            buddy = getattr(window, attr)
            assert any(item.buddy() is buddy and item.text() == label
                       for item in window.findChildren(QLabel))
        finally:
            window.hide()
            window.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("action", ["Always fix", "Just this once"])
def test_F110_pair_actions_have_context_and_native_keyboard_state(action, qapp):
    from samsara.ui.correction_capture_qt import _PairRow
    calls = []
    rows = [_PairRow(wrong, right,
                     lambda w, r: calls.append(("Always fix", w, r)),
                     lambda w, r: calls.append(("Just this once", w, r)))
            for wrong, right in [("heard", "intended"), ("other", "replacement")]]
    try:
        for row in rows:
            row.show()
        qapp.processEvents()
        names = []
        for row in rows:
            button = next(b for b in row.findChildren(QPushButton) if b.text() == action)
            interface = QAccessible.queryAccessibleInterface(button)
            assert interface.role() == QAccessible.Role.Button
            assert interface.state().focusable
            names.append(interface.text(QAccessible.Text.Name))
        assert names == [f'{action}: "heard"  →  "intended"',
                         f'{action}: "other"  →  "replacement"']
        # Space uses the same existing callback as a click, with native disabled state.
        button = next(b for b in rows[0].findChildren(QPushButton) if b.text() == action)
        rows[0].activateWindow()
        button.setFocus()
        qapp.processEvents()
        QTest.keyClick(button, Qt.Key.Key_Tab)
        assert qapp.focusWidget() in rows[0].findChildren(QPushButton)
        assert qapp.focusWidget() is not button
        QTest.keyClick(qapp.focusWidget(), Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        assert qapp.focusWidget() is button
        QTest.keyClick(button, Qt.Key.Key_Space)
        assert calls == [(action, "heard", "intended")]
        assert all(QAccessible.queryAccessibleInterface(b).state().disabled
                   for b in rows[0].findChildren(QPushButton))
        other = next(b for b in rows[1].findChildren(QPushButton) if b.text() == action)
        other.click()
        assert calls[-1] == (action, "other", "replacement")
    finally:
        for row in rows:
            row.hide()
            row.deleteLater()
        qapp.processEvents()


def test_passive_answer_preserves_focus_and_announces_once(qapp, monkeypatch):
    from samsara.ui import ava_search_panel_qt
    from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer
    events = []
    monkeypatch.setattr(QAccessible, "updateAccessibility", lambda event: events.append(event))
    editor = QLineEdit()
    panel = ava_search_panel_qt._build_window()
    try:
        assert panel.windowType() == Qt.WindowType.Tool
        assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        if qapp.platformName() == "offscreen":
            # Offscreen auto-activates Tool windows even with the production
            # show-without-activation attribute. ToolTip suppresses that QPA
            # artifact; an explicit activateWindow() still fails the focus
            # assertion below. Keep real-platform flags unchanged.
            panel.setWindowFlags(
                (panel.windowFlags() & ~Qt.WindowType.WindowType_Mask)
                | Qt.WindowType.ToolTip)
        editor.show()
        editor.activateWindow()
        editor.setFocus()
        qapp.processEvents()
        before = qapp.focusWidget()
        assert before is editor
        controller = ava_search_panel_qt._PanelController()
        controller._window = panel
        controller._show_on_qt_thread("Example query", "Synthetic answer.", [])
        qapp.processEvents()
        assert qapp.focusWidget() is before
        assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert panel.sources.accessibleDescription()
        assert panel.findChild(LiveSurfaceAnnouncer) is not None
        assert len(events) == 1
        assert events[0].message() == panel.windowTitle()
        controller._show_on_qt_thread("Example query", "Synthetic answer.", [])
        assert len(events) == 1
        controller._show_on_qt_thread("Another query", "Another answer.", [])
        assert len(events) == 2
        assert qapp.focusWidget() is before
    finally:
        panel.hide()
        editor.hide()
        panel.deleteLater()
        editor.deleteLater()
        qapp.processEvents()
