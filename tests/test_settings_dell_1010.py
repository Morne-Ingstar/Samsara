"""Dell findings 23/26: guide-first Ava and a prepared Settings first paint."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt

from samsara import ai_preferences, ava_readiness
from samsara.ui import settings_qt
from tests.test_ava_page_guide_669 import _App


def _configured(policy='local'):
    config = {'ollama': {'host': 'http://127.0.0.1:11434'}}
    if policy == 'cloud':
        config['cloud_llm'] = {'provider': 'deepseek', 'api_key': 'test-only'}
    provider = ai_preferences.provider_for(config, policy)
    consent = ai_preferences.accept_consent(
        config, provider=provider, cloud=policy == 'cloud',
        accepted_at='2026-11-01T00:00:00Z')
    return ai_preferences.apply_accepted_ava(config, policy=policy, consent=consent)


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda *a, **kw: None)
    monkeypatch.setattr('samsara.cuda_detect.request_cuda_status', lambda cb: None)
    monkeypatch.setattr(settings_qt.window_activation, 'confirm_foreground', lambda *a: None)
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda app: SimpleNamespace(ready=False))
    win = settings_qt._SettingsWindow(_App())
    yield win
    win.hide()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _open_ava(window, qapp):
    window.show_tab('Ava')
    qapp.processEvents()
    return window._pages['Ava']


@pytest.mark.parametrize('policy', ['off', 'local', 'cloud', 'missing-consent', 'missing-key'])
def test_single_guide_uses_header_right_iff_provider_configuration_is_incomplete(window, qapp, policy):
    config = {} if policy == 'off' else _configured('cloud' if policy == 'missing-key' else
                                                   policy if policy in ('local', 'cloud') else 'local')
    if policy == 'missing-consent':
        config['ava'].pop('consent')
    if policy == 'missing-key':
        config['cloud_llm']['api_key'] = ''
    window.app.config = config
    page = _open_ava(window, qapp)
    pending = policy not in ('local', 'cloud')
    header = page.ava_header
    button = window._widgets['ava_setup_guide_button']
    assert header.isVisible()
    # 1015: setup never changes access to settings or hides the header guide.
    assert page.advanced.toggle.accessibleName() == 'Advanced'
    assert not page.advanced.toggle.isChecked()
    route = window._rows_by_widget['providerRoute']
    assert route.isVisible()
    assert button.isVisible()
    from PySide6.QtWidgets import QLabel, QPushButton
    assert [b for b in page.findChildren(QPushButton) if b.text() == 'Ava guide'] == [
        button]
    assert page.base.itemAt(0).widget() is route is page.base_rows[0]
    assert not window._widgets['ava_conversation_log_enabled'].isVisible()
    assert header.isAncestorOf(button)
    assert header.isAncestorOf(window._widgets['ava_setup_guide_line'])
    assert not any(label.text() in ('Set up Ava', 'Ava setup')
                   for label in page.findChildren(QLabel))
    assert page.column.indexOf(header) < page.column.indexOf(page.base)
    header_bottom = header.mapTo(page.container, header.rect().bottomRight()).y()
    route_top = route.mapTo(page.container, route.rect().topLeft()).y()
    assert header_bottom < route_top
    if pending:
        assert button.x() >= header.width() / 2
        assert 32 <= button.height() <= 48
        assert button.width() < 200
    else:
        from samsara.ui.settings.ava_page import SUBTITLE
        assert window._widgets['ava_setup_guide_line'].text() == SUBTITLE


def test_top_guide_opens_and_more_settings_reveals_existing_controls(window, qapp):
    page = _open_ava(window, qapp)
    window._widgets['ava_setup_guide_button'].click()
    assert window.app.guide_opened is True
    assert window._rows_by_widget['providerRoute'].isVisible()
    assert not window._widgets['ava_conversation_log_enabled'].isVisible()
    page.advanced.toggle.click()
    qapp.processEvents()
    # 1015: retain the ordinary Advanced disclosure in every setup state.
    assert page.advanced.toggle.text().startswith('Advanced')
    # The combo is intentionally hidden behind the route's segmented control.
    assert window._rows_by_widget['providerRoute'].isVisible()
    assert window._widgets['ava_memory_mode'].isVisible()
    assert window._widgets['ava_conversation_log_enabled'].isVisible()


def test_search_reveals_settings_hidden_below_the_guide(window, qapp):
    page = _open_ava(window, qapp)
    window._search_edit.setText('Keep a conversation log')
    window._apply_search_filter()
    qapp.processEvents()
    assert page.advanced.toggle.isChecked()
    assert window._widgets['ava_conversation_log_enabled'].isVisible()


def test_configuration_changes_restore_layout_without_rebuilding_or_readiness_probe(window, qapp):
    page = _open_ava(window, qapp)
    route = window._rows_by_widget['providerRoute']
    control = window._widgets['ava_memory_mode']
    header = page.ava_header
    button = window._widgets['ava_setup_guide_button']
    base_rows = tuple(page.base_rows)
    for config, pending in ((_configured(), False), ({}, True), (_configured('cloud'), False)):
        window.app.config = config
        window._refresh_conditions()
        qapp.processEvents()
        assert window._pages['Ava'] is page
        assert window._widgets['ava_memory_mode'] is control
        assert page.ava_header is header
        assert window._widgets['ava_setup_guide_button'] is button
        assert button.isVisible()  # 1015: the guide stays available for reconfiguration.
        assert tuple(page.base_rows) == base_rows
        assert page.base.itemAt(0).widget() is route
        assert route.disclosure is None
        assert route.isVisible()


def test_settings_copy_points_to_controls_on_the_current_page(window, qapp):
    _open_ava(window, qapp)
    assert window._widgets['ava_mode_key_off_note'].text() == (
        'Ava is off - this key does nothing until you choose where Ava runs.')
    window.open_tool('Integrations')
    qapp.processEvents()
    from PySide6.QtWidgets import QLabel
    notes = [label.text() for label in window._tool_pages['Integrations'].findChildren(QLabel)]
    assert "Use Hermes Agent's MCP connection." in notes
    assert not any('Settings > Integrations' in note for note in notes)


@pytest.mark.parametrize('entry', ['show', 'showNormal', 'show_tab', 'open_tool'])
def test_every_hidden_open_is_polished_laid_out_and_on_requested_page(window, qapp, monkeypatch, entry):
    prepared = []
    original = window._prepare_first_paint

    def prepare():
        assert not window.isVisible()
        assert window.styleSheet()
        original()
        assert window.testAttribute(Qt.WidgetAttribute.WA_WState_Polished)
        assert window.centralWidget().geometry().width() > 0
        prepared.append(window._stack.currentWidget())

    monkeypatch.setattr(window, '_prepare_first_paint', prepare)
    for _ in range(2):
        if entry == 'show_tab':
            window.show_tab('Ava')
            expected = window._pages['Ava']
        elif entry == 'open_tool':
            window.open_tool('Health')
            expected = window._tool_pages['Health']
        else:
            getattr(window, entry)()
            expected = window._pages['General']
        qapp.processEvents()
        assert window.isVisible()
        assert prepared[-1] is expected
        assert expected.isVisible()
        window.close()
        qapp.processEvents()
    assert len(prepared) == 2


def test_show_event_sees_the_prepared_requested_page(window, qapp):
    from PySide6.QtCore import QObject

    observed = []

    class Watch(QObject):
        def eventFilter(self, obj, event):
            if obj is window and event.type() == QEvent.Type.Show:
                observed.append((window._stack.currentWidget(), bool(window.styleSheet()),
                                 window.testAttribute(Qt.WidgetAttribute.WA_WState_Polished)))
            return False

    watch = Watch(window)
    window.installEventFilter(watch)
    page = _open_ava(window, qapp)
    assert observed == [(page, True, True)]


def test_settings_open_does_not_start_a_process(window, qapp, monkeypatch):
    import os
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail('Settings opening started a process')

    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    window.show_tab('Ava')
    window.close()
    window.show_tab('General')
    qapp.processEvents()


def test_static_settings_open_process_calls_are_hidden_on_windows():
    """Scan Settings and its opening helpers, including nested worker bodies."""
    repo = Path(__file__).resolve().parents[1]
    paths = [repo / 'samsara/ui/settings_qt.py', repo / 'samsara/cuda_detect.py',
             repo / 'samsara/ui/window_activation.py']
    paths.extend((repo / 'samsara/ui/settings').glob('*.py'))
    checked = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        stack = [tree]
        while stack:
            node = stack.pop()
            # This Explorer action is connected to Open folder; opening
            # Settings never invokes it. Keep that exception narrowly named.
            if (path.name == 'health_qt.py' and isinstance(node, ast.FunctionDef)
                    and node.name == '_open_health_folder'):
                continue
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in {'subprocess.Popen', 'subprocess.run', 'subprocess.call',
                            'subprocess.check_output', 'subprocess.check_call', 'os.system',
                            'Popen', 'system'}:
                    flags = next((ast.unparse(k.value) for k in node.keywords
                                  if k.arg == 'creationflags'), '')
                    assert 'CREATE_NO_WINDOW' in flags, (path.name, node.lineno)
            stack.extend(ast.iter_child_nodes(node))
        checked.append(path.name)
    assert {'settings_qt.py', 'cuda_detect.py', 'window_activation.py'} <= set(checked)
