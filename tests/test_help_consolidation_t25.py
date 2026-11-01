"""Settings transfers must retire layout metadata before native reparenting."""
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDoubleSpinBox

from samsara.ui.settings.assembly import PageAssembly


@pytest.mark.parametrize('nested', [False, True])
def test_take_control_invalidates_source_item_before_reparenting(qapp, nested):
    source = QWidget()
    outer = QVBoxLayout(source)
    row = QHBoxLayout()
    outer.addLayout(row)
    label = QLabel('Quick help')
    control = QPushButton('Help')
    row.addWidget(label)
    host = control
    if nested:
        host = QWidget()
        QVBoxLayout(host).addWidget(control)
    row.addWidget(host)
    layout = host.layout() if nested else row
    # Retain the real item wrapper, as the source factories do. Implicit
    # setParent deletes its C++ item without invalidating the Python wrapper.
    item = layout.itemAt(layout.indexOf(control))
    assert item is not None
    owner = SimpleNamespace(_widgets={'help': control}, _placed_controls=set(),
                            _source_rows=[('Quick help', '', host, row)])
    destination = QWidget()
    target = QVBoxLayout(destination)
    try:
        moved = PageAssembly._take_control(owner, 'help')
        assert not shiboken6.isValid(item)
        assert moved is control
        assert moved.accessibleName() == 'Quick help'
        assert moved.parentWidget() is None
        assert label.isHidden()
        target.addWidget(moved)
        assert target.itemAt(0).widget() is control
        assert control.parentWidget() is destination
        with pytest.raises(ValueError, match='placed twice'):
            PageAssembly._take_control(owner, 'help')
    finally:
        shiboken6.delete(destination)
        shiboken6.delete(source)


def test_take_control_detaches_canonical_percent_editor_before_adapting(qapp):
    source = QWidget()
    row = QHBoxLayout(source)
    control = QDoubleSpinBox()
    control.setValue(0.5)
    control.setAccessibleName('Ducking level')
    row.addWidget(QLabel('Ducking level'))
    row.addWidget(control)
    item = row.itemAt(1)
    store = QWidget(source)
    owner = SimpleNamespace(_widgets={'adv_ducking_level': control}, _placed_controls=set(),
                            _source_store=store, _source_rows=[('Ducking level', '', control, row)])
    moved = None
    try:
        moved = PageAssembly._take_control(owner, 'adv_ducking_level')
        assert not shiboken6.isValid(item)
        assert moved._canonical_control is control
        assert moved.accessibleName() == 'Ducking level'
        assert control.parentWidget() is store
        moved.setValue(0.75)
        assert control.value() == 0.75
    finally:
        if moved is not None:
            shiboken6.delete(moved)
        shiboken6.delete(source)


@pytest.mark.parametrize('platform_name', ['offscreen', 'minimal'])
def test_passive_topmost_does_not_acquire_hwnd_outside_windows_qpa(qapp, monkeypatch, platform_name):
    from PySide6.QtGui import QGuiApplication
    from samsara.ui import passive_topmost
    monkeypatch.setattr(QGuiApplication, 'platformName', staticmethod(lambda: platform_name))
    def no_hwnd():
        pytest.fail('Non-Windows QPA must not acquire an HWND')
    passive_topmost.restore(SimpleNamespace(isVisible=lambda: True, winId=no_hwnd))


@pytest.mark.parametrize('destination,source_name,key', [
    ('Help', 'help', 'feedbackStatusLabel'),
    ('Dictation', 'advanced', 'cuda_status'),
    ('Ava', 'ava', 'cloud_test_status'),
])
def test_lazy_destination_invalidates_transferred_status_item(qapp, qt_widget_scope, destination, source_name, key):
    from PySide6.QtWidgets import QLayout
    from samsara.ui import settings_qt
    from tests.test_support_feedback_ui import _FeedbackApp
    with qt_widget_scope(settings_qt._SettingsWindow(_FeedbackApp())) as window:
        window._ensure_source(source_name)
        control = window._widgets.get(key) or window._source_store.findChild(QWidget, key)
        assert control is not None
        root = control.parentWidget().layout()
        layout = next(layout for layout in (root, *root.findChildren(QLayout)) if layout.indexOf(control) >= 0)
        item = layout.itemAt(layout.indexOf(control))
        assert item is not None
        page = window._pages[destination]
        assert not shiboken6.isValid(item)
        assert page.isAncestorOf(control)


@pytest.mark.parametrize('attempt', range(12))
def test_settings_reference_lifecycle_after_control_transfers(qapp, qt_widget_scope, monkeypatch, tmp_path, attempt):
    """Recreate slice 41/42's Settings -> native list allocation and teardown."""
    from samsara import command_stats
    from samsara.ui import settings_qt, command_cheatsheet_qt as reference, theme, window_activation
    from tests.test_support_feedback_ui import _FeedbackApp
    monkeypatch.setattr(reference, '_scope_context', lambda: None)
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _: [])
    monkeypatch.setattr(reference._CheatSheetWindow, '_foreground_screen', lambda _: None)
    # Navigation retains the real hosts; user desktop activation is an
    # external boundary, as in the original slice-41 Settings case.
    monkeypatch.setattr(window_activation, 'surface_requested', lambda _: None)
    previous = theme.active_theme()
    theme.set_theme('light' if attempt % 2 else 'dark', refresh=False)
    try:
        for reopening in range(2):
            with qt_widget_scope(settings_qt._SettingsWindow(_FeedbackApp())) as window:
                window.resize(1160, 920) if attempt % 2 else window.resize(780, 600)
                window.show()
                window._ensure_preferences()
                for name in tuple(window._pages):
                    window.show_tab(name)
                    qapp.processEvents()
                window.show_tab('Help & Support')
                support = window._widgets['supportChoices']
                assert support.menu().actions()
                with qt_widget_scope(reference._CheatSheetWindow(
                        lambda _: None, lambda: [], tmp_path / f'palette-{reopening}.json')) as sheet:
                    sheet.show()
                    qapp.processEvents()
                    assert sheet._list.count() > 0
                    sheet.close()
                    sheet.show()
                    qapp.processEvents()
                    assert sheet.isVisible()
                window.close()
    finally:
        theme.set_theme(previous, refresh=False)
