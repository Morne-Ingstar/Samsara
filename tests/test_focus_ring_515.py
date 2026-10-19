"""Real widgets and rendered rings, in-process and with synthetic app state."""
import pytest
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAccessible, QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

from samsara.ui import home_qt, theme
from samsara.ui.focus_ring import install, keyboard_stylesheet
from tests.test_home_qt import _app, _FakeSettings
from tests.test_settings_pages import _StubApp, _schema_defaults


def settle(qapp):
    for _ in range(20):
        qapp.processEvents()


@pytest.fixture(params=('home', 'settings'))
def window(request, qapp, monkeypatch, qt_widget_scope):
    from samsara import autostart
    from samsara.ui.first_run_qt import ComponentsPage
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    _FakeSettings.values = {}
    if request.param == 'home':
        from samsara.ui import main_window_qt
        monkeypatch.setattr(main_window_qt, 'HistoryView', lambda *a, **k: QWidget())
        result = main_window_qt._MainWindow(_app())
        result._poll_timer.stop()
    else:
        from samsara.ui.settings_qt import _SettingsWindow
        result = _SettingsWindow(_StubApp(_schema_defaults()))
    with qt_widget_scope(result):
        result.resize(1160, 900)
        result.show()
        settle(qapp)
        yield result


def rings(window):
    return [w for w in window.findChildren(QWidget)
            if w.hasFocus() and w.property('keyboardFocus')]


def next_control(window, current=None):
    control = (current or window).nextInFocusChain()
    while control is not window:
        if control.isVisibleTo(window) and control.isEnabled() and control.focusPolicy() & Qt.FocusPolicy.TabFocus:
            return control
        control = control.nextInFocusChain()
    raise AssertionError('no keyboard control')


def test_open_tab_mouse_and_programmatic_focus(window, qapp):
    assert rings(window) == []
    current = qapp.focusWidget()
    first = next_control(window, current)
    QTest.keyClick(current or window, Qt.Key.Key_Tab)
    settle(qapp)
    assert qapp.focusWidget() is first
    assert rings(window) == [first]
    if first is getattr(window, '_search_edit', None):
        assert first.property('settingsKeyboardFocus') is True
    accessible = QAccessible.queryAccessibleInterface(first)
    assert accessible is not None and accessible.state().focused
    button = next(b for b in window.findChildren(QPushButton)
                  if b.isVisibleTo(window) and b.isEnabled() and b is not first
                  and b.accessibleName().startswith('Help:')) if hasattr(window, '_pages') else window._reset_btn
    # Focus-only button action: avoid routing a real app callback.
    button.blockSignals(True)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    button.blockSignals(False)
    settle(qapp)
    assert button.hasFocus()
    assert rings(window) == []
    QTest.keyClick(button, Qt.Key.Key_Backtab)
    settle(qapp)
    assert len(rings(window)) == 1
    button.setFocus(Qt.FocusReason.OtherFocusReason)
    settle(qapp)
    assert rings(window) == []


@pytest.mark.parametrize('origin', ('none', 'program', 'mouse'))
@pytest.mark.parametrize('backwards', (False, True))
def test_first_tab_preserves_native_focus_traversal(qapp, origin, backwards):
    install()
    window = QWidget()
    layout = QVBoxLayout(window)
    buttons = [QPushButton(f'Control {index}') for index in range(3)]
    for button in buttons:
        layout.addWidget(button)
    window.show()
    window.activateWindow()
    settle(qapp)
    try:
        if origin == 'none':
            qapp.focusWidget().clearFocus()
            assert qapp.focusWidget() is None
            target = window
            expected = buttons[-1] if backwards else buttons[0]
        else:
            target = buttons[0]
            if origin == 'program':
                target.setFocus(Qt.FocusReason.OtherFocusReason)
            else:
                QTest.mouseClick(target, Qt.MouseButton.LeftButton)
            settle(qapp)
            assert qapp.focusWidget() is target
            expected = buttons[-1] if backwards else buttons[1]
        assert rings(window) == []
        QTest.keyClick(target, Qt.Key.Key_Backtab if backwards else Qt.Key.Key_Tab)
        settle(qapp)
        assert qapp.focusWidget() is expected
        assert rings(window) == [expected]
    finally:
        window.hide()
        window.deleteLater()
        settle(qapp)


def test_guides_mouse_keeps_clicked_control(window, qapp):
    if not hasattr(window, '_panel_cache'):
        pytest.skip('Home disclosure only')
    home = window._panel_cache['Home']
    button = next(b for b in home._cards if b.text() == 'Guides & tutorials' or b.accessibleName() == 'Guides & tutorials')
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    settle(qapp)
    assert button.hasFocus()
    assert not next(iter(home._guide_btns.values())).hasFocus()
    assert rings(window) == []


@pytest.mark.parametrize('palette', ('dark', 'light'))
@pytest.mark.parametrize('origin', ('local', 'application'))
def test_rendered_border_tracks_keyboard_and_same_widget_mouse(
        qapp, palette, origin, request, qt_widget_scope):
    old_theme = theme.active_theme()
    theme.set_theme(palette, refresh=False)
    install()
    button = QPushButton('Focus fixture')
    old_sheet = qapp.styleSheet()
    def cleanup():
        # Deliver this owner's DeferredDelete BEFORE replacing the shared QSS.
        # Global processEvents does not reliably deliver deferred deletion.
        with qt_widget_scope(button):
            pass
        qapp.setStyleSheet(old_sheet)
        theme.set_theme(old_theme, refresh=False)
    request.addfinalizer(cleanup)
    sheet = (f'QPushButton {{ background: {theme.BG1}; border: 2px solid transparent; }}'
             f'QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}')
    if origin == 'local':
        button.setStyleSheet(sheet)
    else:
        qapp.setStyleSheet(sheet)
    button.resize(200, 60)
    button.show()
    settle(qapp)
    def border():
        return button.grab().toImage().pixelColor(100, 0)
    assert border() != theme.qcolor(theme.ACCENT)
    QTest.keyClick(button, Qt.Key.Key_Tab)
    settle(qapp)
    assert border() == theme.qcolor(theme.ACCENT)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    settle(qapp)
    assert button.hasFocus() and border() != theme.qcolor(theme.ACCENT)
    QTest.keyClick(button, Qt.Key.Key_Right)
    settle(qapp)
    assert border() == theme.qcolor(theme.ACCENT)


def test_subcontrol_selector_and_theme_reapplication_are_idempotent():
    source = 'QSlider::handle:horizontal:focus { border-color: accent; }'
    revised = keyboard_stylesheet(source)
    assert revised == 'QSlider[keyboardFocus="true"]::handle:horizontal:focus { border-color: accent; }'
    assert keyboard_stylesheet(revised) == revised
    link = 'QLabel:focus { border: 1px solid accent; text-decoration: none; }'
    guarded = keyboard_stylesheet(link)
    assert 'QLabel:focus {text-decoration: none;}' in guarded
    assert 'QLabel[keyboardFocus="true"]:focus' in guarded
    assert keyboard_stylesheet(guarded) == guarded


def test_quit_dialog_keeps_code_focus_quiet_and_keyboard_focus_visible(qapp):
    from samsara.ui.quit_draft_dialog_qt import QuitDraftDialog
    install()
    dialog = QuitDraftDialog()
    dialog.show()
    settle(qapp)
    assert rings(dialog) == []
    assert dialog.action_buttons[-1].hasFocus()
    QTest.keyClick(dialog, Qt.Key.Key_Tab)
    settle(qapp)
    assert rings(dialog) == [dialog.action_buttons[0]]
    button = dialog.action_buttons[-1]
    button.blockSignals(True)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    button.blockSignals(False)
    settle(qapp)
    assert button.hasFocus() and rings(dialog) == []
    dialog.hide()
    dialog.deleteLater()
    settle(qapp)


def test_shortcut_opened_window_keeps_startup_focus_quiet(qapp):
    from samsara.ui.quit_draft_dialog_qt import QuitDraftDialog
    install()
    host = QPushButton('Shortcut fixture')
    opened = []
    def show_dialog():
        dialog = QuitDraftDialog()
        opened.append(dialog)
        dialog.show()
    shortcut = QShortcut(QKeySequence('F6'), host)
    shortcut.activated.connect(show_dialog)
    host.show()
    host.activateWindow()
    host.setFocus()
    settle(qapp)
    QTest.keyClick(host, Qt.Key.Key_F6)
    settle(qapp)
    assert len(opened) == 1
    assert rings(opened[0]) == []
    for widget in (*opened, host):
        widget.hide()
        widget.deleteLater()
    settle(qapp)


def test_tall_controls_keep_title_and_description_together(window, qapp):
    if not hasattr(window, '_pages'):
        pytest.skip('Settings rows only')
    from samsara.ui.settings.presentation import Disclosure
    for name, page in {**window._pages, **window._tool_pages}.items():
        window._stack.setCurrentWidget(page)
        if hasattr(page, 'advanced'):
            page.advanced.reveal()
        for disclosure in page.findChildren(Disclosure):
            disclosure.reveal()
        settle(qapp)
        for row in page.findChildren(QWidget):
            if not hasattr(row, '_calm_layout') or not row.isVisibleTo(page):
                continue
            layout = row.text.layout()
            if row.detail.isVisibleTo(row) and row.heading.isVisibleTo(row):
                title = layout.itemAt(0).widget()
                gap = row.detail.y() - title.y() - title.height()
                assert 0 <= gap <= theme.SPACE_1 + 1, (name, row.title, gap)
                if row.help:
                    assert title.height() <= row.heading.height() + 1, (name, row.title)
                    assert row.rect().contains(row.help.geometry()), (name, row.title)
        sheet = page.verticalScrollBar().styleSheet()
        assert 'width: 10px' in sheet and 'height: 0' in sheet


def test_settings_input_geometry_updates_do_not_restyle_forever(window, qapp):
    if not hasattr(window, '_pages'):
        pytest.skip('Settings input geometry only')
    field = window._widgets['theme_combo']
    class Changes(QObject):
        count = 0
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.StyleChange:
                self.count += 1
            return False
    changes = Changes(field)
    field.installEventFilter(changes)
    for _ in range(3):
        window._settings_input_layout.style_field(field)
    settle(qapp)
    assert changes.count == 0
