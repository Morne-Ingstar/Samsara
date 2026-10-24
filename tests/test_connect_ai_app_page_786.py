"""786: one connection flow, existing persisted controls and private Hermes setup."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QCheckBox, QLabel

from samsara.ui.settings import agents_section as ui
from samsara.ui.settings.calm_layout import RowLayout
from tests.test_agent_server_564 import memory_clipboard


@pytest.fixture
def page(qapp, monkeypatch, memory_clipboard):
    from samsara import ava_readiness

    saved = []
    app = SimpleNamespace(config={
        'command_packs': {'agents': True},
        'agent_server': {'allow_typing': False, 'command_key_only': False, 'port': 49123},
        'agent_voice': {'digest': 'auto'},
    })
    app.save_config = lambda: saved.append(deepcopy(app.config))
    state = SimpleNamespace(status='listening', port=49123, enabled=True, typing_enabled=True,
                            sync=lambda: None, cancel_typing=lambda: None)
    import secrets
    state.token = secrets.token_urlsafe(32)
    state.connection_details = lambda: (f'http://127.0.0.1:{state.port}/mcp\nAuthorization: Bearer {state.token}'
                                       if state.status == 'listening' else None)
    def rotate():
        state.token = secrets.token_urlsafe(32)
        return True
    state.new_token = rotate
    monkeypatch.setattr(ui, 'for_app', lambda _: state)
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda _: SimpleNamespace(ready=False))
    monkeypatch.setattr(ui.QTimer, 'singleShot', lambda *_: None)
    enabled = QCheckBox()
    enabled.setChecked(True)
    panel = ui.AgentsControls(SimpleNamespace(app=app), enabled)
    yield panel, app, state, saved, memory_clipboard
    panel.close()
    panel.deleteLater()


def test_all_existing_keys_are_bound_and_saved(page):
    panel, app, _, saved, _ = page
    for control, key in ((panel.enabled, 'command_packs.agents'),
                         (panel.typing, 'agent_server.allow_typing'),
                         (panel.approval_style, 'agent_server.command_key_only'),
                         (panel.digest_choice, 'agent_voice.digest')):
        assert control.objectName() == key
    assert hasattr(panel.enabled, '_calm_switch') and hasattr(panel.typing, '_calm_switch')
    panel.typing.click()
    assert saved[-1]['agent_server']['allow_typing'] is True
    panel.approval_style.setCurrentIndex(1)
    assert saved[-1]['agent_server']['command_key_only'] is True
    panel.digest_choice.setCurrentIndex(panel.digest_choice.findData('off'))
    assert saved[-1]['agent_voice']['digest'] == 'off'
    panel.enabled.click()
    assert saved[-1]['command_packs']['agents'] is False
    assert saved[-1]['agent_server']['allow_typing'] is False
    assert not panel.typing.isChecked() and not panel.typing.isEnabled()
    assert not panel.approval_style.isEnabled()
    assert app.config['agent_server']['command_key_only'] is True


@pytest.mark.parametrize('control,value', [('enabled', False), ('typing', True),
                                          ('approval_style', 1), ('digest_choice', 1)])
def test_failed_save_preserves_existing_values(page, control, value):
    panel, app, _, _, _ = page
    before = deepcopy(app.config)
    def fail():
        raise OSError('Synthetic save failure')
    app.save_config = fail
    widget = getattr(panel, control)
    if isinstance(widget, QCheckBox):
        widget.setChecked(value)
    else:
        widget.setCurrentIndex(value)
    assert app.config == before


@pytest.mark.parametrize('model,enabled,ready,guide', [
    (None, True, False, True), ('local', False, False, True),
    ('local', True, False, False), ('local', True, True, False),
])
def test_ava_guide_appears_once_only_without_configured_local_model(page, monkeypatch, qapp,
                                                                 model, enabled, ready, guide):
    from samsara import ava_readiness
    panel, app, _, _, _ = page
    app.config['ollama'] = {'model': model, 'enabled': enabled}
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda _: SimpleNamespace(ready=ready))
    monkeypatch.setattr(ava_readiness, 'configured_provider', lambda _: 'ollama')
    panel.refresh()
    panel.show()
    qapp.processEvents()
    assert panel.digest_guide.isVisible() is guide
    assert 'Ava guide' not in panel.digest_status.text()
    assert sum('Ava guide' in w.text() for w in panel.findChildren(QLabel) if w.isVisible()) == 0
    opened = []
    app.open_ava_guide = lambda: opened.append(True)
    panel.digest_guide.click()
    assert opened == [True]


def test_flow_and_app_specific_steps(page, qapp):
    panel, *_ = page
    # 858: turn the enabled fixture off, then open setup as a user would.
    panel.enabled.click()
    assert not panel.details.toggle.isChecked()
    panel.details.toggle.click()
    panel.resize(940, 1300)
    panel.show()
    qapp.processEvents()
    positions = [w.mapTo(panel, QPoint()).y() for w in
                 (panel.connection_heading, panel.enable_row, panel.status, panel.connect_heading,
                  panel.app_selector, panel.routes, panel.help, panel.capabilities_heading,
                  panel.digest_row, panel.typing_row, panel.approval_row)]
    assert positions == sorted(positions)
    assert [panel.app_selector.itemText(i) for i in range(3)] == ['Claude Code', 'Claude Desktop', 'Hermes']
    panel.help.reveal()
    for i in range(3):
        panel.app_selector.setCurrentIndex(i)
        qapp.processEvents()
        assert [p.isVisible() for p in panel.routes.panels] == [n == i for n in range(3)]
        assert [p.isVisible() for p in panel.help_routes.panels] == [n == i for n in range(3)]
        assert any(label.text().startswith('1.') for label in panel.routes.panels[i].findChildren(QLabel))
    assert not panel.findChildren(ui.SetupIllustration)
    assert all('<a ' not in label.text() for label in panel.findChildren(QLabel))
    destinations = []
    panel.owner.focus_setting = destinations.append
    panel.command_key_button.click()
    assert destinations == ['command_hotkey']


def test_hermes_copy_masks_token_and_tracks_rotation_and_port(page):
    panel, _, state, _, clipboard = page
    assert bool(all(state.token not in label.text() for label in panel.findChildren(QLabel)))
    assert '<token hidden>' in panel.hermes_env.text()
    panel.hermes_env.copy_button.click()
    assert bool(clipboard.text() == 'SAMSARA_MCP_TOKEN=' + state.token)
    assert clipboard.mimeData().hasFormat(ui._EXCLUDE_HISTORY)
    old = state.token
    panel.rotate.click()
    assert bool(state.token != old)
    panel.hermes_env.copy_button.click()
    assert bool(old not in clipboard.text() and state.token in clipboard.text())
    state.port = 49124
    panel.refresh()
    panel.hermes_preview.copy_button.click()
    config = yaml.safe_load(clipboard.text())['mcp_servers']['samsara']
    assert config == {'url': 'http://127.0.0.1:49124/mcp',
                      'headers': {'Authorization': 'Bearer ${SAMSARA_MCP_TOKEN}'}}
    state.status = 'off'
    panel.refresh()
    assert not panel.hermes_env.copy_button.isEnabled()
    clipboard.clear()
    assert panel.copy_hermes_env() is False and clipboard.text() == ''


@pytest.mark.parametrize('details', [None, '', 'http://example.org/mcp',
                                     'http://127.0.0.1:0/mcp\nAuthorization: Bearer bad'])
def test_hermes_rejects_invalid_connection_details(details):
    assert ui.hermes_profile_env(details) is None


@pytest.mark.parametrize('width', [560, 940])
def test_common_left_edge_and_compact_controls(page, qapp, width):
    panel, *_ = page
    panel.resize(width, 1600)
    for row in (panel.enable_row, panel.digest_row, panel.typing_row, panel.approval_row):
        row._calm_layout = RowLayout(row)
    panel.show()
    qapp.processEvents()
    for widget in (panel.connection_heading, panel.enable_row, panel.connect_heading,
                   panel.app_selector, panel.routes, panel.help, panel.capabilities_heading,
                   panel.digest_row, panel.typing_row, panel.approval_row):
        assert widget.mapTo(panel, QPoint()).x() == 0
    assert panel.app_selector.width() < width - 40
    assert panel.approval_style.width() < width - 40
    assert panel.code_copy.width() < 150
