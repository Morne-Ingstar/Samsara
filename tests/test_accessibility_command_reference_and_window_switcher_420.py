"""420: native names, roles, keyboard actions, and passive cube focus.

Synthetic commands and window slots only; no desktop bootstrap, enumeration,
command execution, or owner-profile reads.
"""
import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from samsara.ui import command_cheatsheet_qt as sheet, window_cube_qt as cube
from samsara import command_stats


def _interface(widget, role):
    interface = QAccessible.queryAccessibleInterface(widget)
    name = interface.text(QAccessible.Text.Name).strip()
    assert name and name != type(widget).__name__
    assert interface.role() == role
    return interface


def _tab_walk(qapp, start, targets):
    start.window().activateWindow()
    start.setFocus()
    qapp.processEvents()
    seen = []
    for _ in range(40):
        focused = QApplication.focusWidget()
        if focused not in seen:
            seen.append(focused)
        if all(target in seen for target in targets):
            return seen
        assert focused is not None, [type(widget).__name__ for widget in seen]
        QTest.keyClick(focused, Qt.Key.Key_Tab)
        qapp.processEvents()
    pytest.fail('Tab did not reach every action: seen=' + str([
        (type(widget).__name__, getattr(widget, '_number', None)) for widget in seen
    ]) + '; targets=' + str([
        (getattr(widget, '_number', None), widget.focusPolicy().name,
         widget.isVisible(), widget.isEnabled()) for widget in targets
    ]) + '; reviewing=' + str(getattr(start.window(), '_reviewing', None)))


def _dispose(qapp, *widgets):
    for widget in widgets:
        widget.hide()
        widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.fixture
def reference(qapp, tmp_path, monkeypatch):
    app = SimpleNamespace(executed=[], commands=[
        dict(phrase=phrase, canonical_id='synthetic.' + str(index), plugin='builtin',
             pack='core', aliases=[phrase], shown_aliases=[], description='Synthetic command')
        for index, phrase in enumerate(('sample alpha', 'sample beta', 'sample gamma'))
    ])
    monkeypatch.setattr(sheet, 'catalog_rows', lambda commands: list(commands))
    monkeypatch.setattr(sheet, '_disabled_packs', lambda: set())
    monkeypatch.setattr(sheet, '_scope_context', lambda: None)
    monkeypatch.setattr(sheet._CheatSheetWindow, '_foreground_screen', lambda self: None)
    monkeypatch.setattr(command_stats, 'get_top_commands',
                        lambda limit: [('sample alpha', 4), ('sample beta', 3)])
    path = tmp_path / 'palette.json'
    path.write_text(json.dumps(dict(pinned=['sample beta'], most_used_expanded=True)),
                    encoding='utf-8')
    win = sheet._CheatSheetWindow(app.executed.append, lambda: app.commands, path)
    win.show()
    win.activateWindow()
    qapp.processEvents()
    yield win, app, path
    _dispose(qapp, win)


def _rows(win):
    return [row for row in win.findChildren(sheet._StaticRow) if row.isVisibleTo(win)]


def test_reference_row_names_roles_and_pin_states(reference):
    """F042/F045 execute; F043/F046/F103/F104 pin target and real state."""
    win, _, _ = reference
    rows = _rows(win)
    assert len(rows) == 3
    for row in rows:
        execute = _interface(row, QAccessible.Role.Button)
        assert execute.text(QAccessible.Text.Name) == row._phrase.title()
        pin = _interface(row._pin_lbl, QAccessible.Role.CheckBox)
        assert pin.text(QAccessible.Text.Name) == 'Pin ' + row._phrase.title()
        assert pin.state().checkable
        assert bool(pin.state().checked) == (row._phrase in win._pinned)
        assert any(execute.child(index).object() is row._pin_lbl
                   for index in range(execute.childCount()))


def test_command_list_has_a_purpose_and_native_list_role(reference):
    """F044: the list name identifies its purpose, independent of item Value."""
    win, _, _ = reference
    interface = _interface(win._list, QAccessible.Role.List)
    assert interface.text(QAccessible.Text.Name) == 'Command list'


def test_reference_tab_reaches_execute_and_pin_in_both_sections(reference, qapp):
    win, _, _ = reference
    rows = _rows(win)
    targets = [action for row in rows for action in (row, row._pin_lbl)]
    seen = _tab_walk(qapp, win._filter, targets)
    assert [action for action in seen if action in targets] == targets


@pytest.mark.parametrize('phrase', ['sample alpha', 'sample beta'])
@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Space])
def test_execute_keys_and_mouse_use_the_same_callback(reference, qapp, phrase, key):
    win, app, _ = reference
    row = next(row for row in _rows(win) if row._phrase == phrase)
    row.setFocus()
    QTest.keyClick(row, key)
    qapp.processEvents()
    assert app.executed == [phrase]
    QTest.mouseClick(row, Qt.MouseButton.LeftButton)
    assert app.executed == [phrase, phrase]


@pytest.mark.parametrize('phrase', ['sample alpha', 'sample beta'])
@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Space])
def test_pin_keys_and_mouse_toggle_real_state_without_execution(
        reference, qapp, phrase, key):
    win, app, path = reference
    initially_pinned = phrase in win._pinned
    row = next(row for row in _rows(win) if row._phrase == phrase)
    row._pin_lbl.setFocus()
    QTest.keyClick(row._pin_lbl, key)
    qapp.processEvents()
    assert (phrase in win._pinned) != initially_pinned
    assert (phrase in json.loads(path.read_text(encoding='utf-8'))['pinned']) != initially_pinned
    row = next(row for row in _rows(win) if row._phrase == phrase)
    pin = _interface(row._pin_lbl, QAccessible.Role.CheckBox)
    assert bool(pin.state().checked) != initially_pinned
    assert QApplication.focusWidget() in (row._pin_lbl, win._list)
    QTest.mouseClick(row._pin_lbl, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert (phrase in win._pinned) == initially_pinned
    assert app.executed == []


@pytest.fixture
def panel(qapp, monkeypatch):
    native_styles = []
    monkeypatch.setattr(cube, 'apply_toolwindow_exstyle',
                        lambda hwnd, **kw: native_styles.append(kw.get('review', False)) or True)
    editor = QLineEdit()
    editor.show()
    editor.activateWindow()
    editor.setFocus()
    qapp.processEvents()
    switched = []
    win = cube._CubeWindow(switched.append, 1.0, (20, 20))
    win._on_render([cube.CubeRow(1, 'Test editor'),
                    cube.CubeRow(2, 'Test browser', 'Synthetic document'),
                    cube.CubeRow(3, 'Closed editor', closed=True)])
    yield win, editor, switched, native_styles
    _dispose(qapp, win, editor)


def test_passive_cube_show_refresh_and_announcements_preserve_focus(panel, qapp, monkeypatch):
    win, editor, _, _ = panel
    announcements = []
    monkeypatch.setattr(QAccessible, 'updateAccessibility',
                        lambda event: announcements.append(event.message()))
    focused = QApplication.focusWidget()
    assert focused is editor
    win.show()
    qapp.processEvents()
    assert QApplication.focusWidget() is focused
    assert win.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert win.accessibleName() and win.accessibleDescription()
    assert announcements and 'Test editor' in announcements[-1]
    count = len(announcements)
    win._on_render(list(win._announced_rows))
    qapp.processEvents()
    assert len(announcements) == count
    win._on_render([cube.CubeRow(1, 'Updated editor')])
    qapp.processEvents()
    assert QApplication.focusWidget() is focused
    assert len(announcements) == count + 1


def test_cube_shortcut_enters_review_tab_reaches_rows_and_escape_restores_focus(panel, qapp):
    """F088 has an actual app-local keyboard entry, separate from passive show."""
    win, editor, _, native_styles = panel
    win.show()
    qapp.processEvents()
    QTest.keyClick(editor, Qt.Key.Key_F6,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    qapp.processEvents()
    assert win._reviewing
    assert native_styles[-1] is True
    rows = win.findChildren(cube._Row)
    for row in rows:
        interface = _interface(row, QAccessible.Role.Button)
        assert str(row._number) in interface.text(QAccessible.Text.Description)
    assert _interface(rows[-1], QAccessible.Role.Button).state().disabled
    assert 'Synthetic document' in rows[1].accessibleDescription()
    _tab_walk(qapp, rows[0], rows[:2])
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Escape)
    qapp.processEvents()
    assert not win._reviewing and native_styles[-1] is False
    assert win.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert QApplication.focusWidget() is editor


@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Space])
def test_cube_keyboard_and_mouse_use_existing_switch_slot(panel, qapp, key):
    win, _, switched, native_styles = panel
    win.show()
    win.focus_review()
    qapp.processEvents()
    row = win.findChildren(cube._Row)[1]
    row.setFocus()
    QTest.keyClick(row, key)
    assert switched == [2]
    assert not win._reviewing and native_styles[-1] is False
    QTest.mouseClick(row, Qt.MouseButton.LeftButton)
    assert switched == [2, 2]


def test_cube_public_review_is_posted_to_shared_runtime(monkeypatch):
    posted = []
    marker = lambda: None
    wrapper = cube.CubePanel()
    wrapper._window = SimpleNamespace(focus_review=marker)
    monkeypatch.setattr(cube.qt_runtime, 'post', posted.append)
    wrapper.focus_review()
    assert posted == [marker]
