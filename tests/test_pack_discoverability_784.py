"""784: discover off packs, preserve consent and activate the frozen matcher live."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from samsara.command_packs import get_enabled_packs
from samsara.ui import command_cheatsheet_qt as cs, command_pack_actions as actions
from tests.test_tools_routing_785 import tools, _settle


@pytest.fixture
def owner(monkeypatch):
    from samsara.agent_channel import hermes
    monkeypatch.setattr(hermes, 'installed', lambda: True)
    app = SimpleNamespace(config={'command_packs': {'macros': False, 'hermes': False},
                                  'hermes': {'notice_accepted': False}},
                          _config_lock=threading.Lock(), save_config=Mock(),
                          command_executor=SimpleNamespace(rebuild_matcher=Mock()),
                          cheat_sheet=SimpleNamespace(refresh=Mock()))
    return app


@pytest.fixture
def reference(owner, qapp, monkeypatch, tmp_path):
    rows = [dict(phrase='ask hermes', canonical_id='hermes.ask', kind='plugin',
                 aliases=['ask hermes'], description='Ask your Hermes a question.',
                 examples=['ask Hermes what is on my calendar'], args=[], plugin='ask_agent', pack='hermes'),
            dict(phrase='focus mode', canonical_id='macro.focus', kind='plugin',
                 aliases=['focus mode'], description='Run your focus macro.', args=[], plugin='macros', pack='macros')]
    monkeypatch.setattr(cs, 'catalog_rows', lambda _: [dict(row) for row in rows])
    monkeypatch.setattr(cs, '_scope_context', lambda: None)
    monkeypatch.setattr(cs._CheatSheetWindow, '_foreground_screen', lambda _: None)
    win = cs._CheatSheetWindow(Mock(), lambda: [], tmp_path / 'palette.json',
                              open_packs_cb=lambda: actions.open_packs(owner),
                              enable_pack_cb=lambda pack, parent: actions.enable_pack(owner, pack, parent),
                              config_cb=lambda: owner.config)
    win.show()
    _settle(qapp)
    yield win
    win.hide()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def show_detail(win, phrase, qapp):
    win._filter.setText(phrase)
    _settle(qapp)
    target = next(win._list.item(i) for i in range(win._list.count())
                  if win._list.item(i).data(Qt.ItemDataRole.UserRole) == phrase)
    QTest.mouseClick(win._list.viewport(), Qt.MouseButton.LeftButton,
                     pos=win._list.visualItemRect(target).center())
    return win._detail_card


def test_home_and_reference_open_visible_packs_at_top(tools, qapp, monkeypatch, tmp_path):
    from samsara.ui import home_qt
    from tests.test_home_qt import _FakeSettings, _app
    app = _app()
    app._settings_qt = tools._settings_tools
    monkeypatch.setattr(home_qt, 'QSettings', _FakeSettings)
    home = home_qt.HomePage(app)
    home.resize(900, 900)
    home.show()
    try:
        card = next(c for c in home._cards if c.text() == 'Commands')
        button = next(b for b in card.findChildren(QPushButton) if b.text() == 'Command packs')
        interface = QAccessible.queryAccessibleInterface(card)
        assert any(interface.child(i).text(QAccessible.Text.Name) == 'Command packs'
                   and interface.child(i).role() == QAccessible.Role.Button for i in range(interface.childCount()))
        assert button in home.tab_chain
        button.click()
        window = app._settings_qt._window
        packs = window._widgets['_pack_scroll']
        page = window._tool_pages['Command library']
        _settle(qapp)
        assert window._stack.currentWidget() is page
        assert packs.isVisible()
        assert page.verticalScrollBar().value() == 0
        packs.verticalScrollBar().setValue(packs.verticalScrollBar().maximum())
        window.open_tool('Music')
        monkeypatch.setattr(cs, 'catalog_rows', lambda _: [])
        monkeypatch.setattr(cs, '_disabled_packs', lambda: set())
        ref = cs._CheatSheetWindow(Mock(), lambda: [], tmp_path / 'palette.json',
                                   open_packs_cb=lambda: actions.open_packs(app))
        try:
            ref._title_bar._packs_button.click()
            _settle(qapp)
            assert window._stack.currentWidget() is page
            assert page.verticalScrollBar().value() == 0
            assert packs.verticalScrollBar().value() == 0
            assert not ref._title_bar._packs_button.font().underline()
        finally:
            ref.deleteLater()
    finally:
        home.hide()
        home.deleteLater()


def test_ordinary_card_enables_same_key_and_updates_in_place(reference, owner, qapp):
    card = show_detail(reference, 'focus mode', qapp)
    assert card.pack_status.text() == "This command's pack is off"
    assert card.enable_button.text() == 'Turn on Custom Macros'
    assert not card.run_button.isVisible()
    assert card.geometry().top() >= reference._list.mapTo(card.parentWidget(), reference._list.rect().topLeft()).y()
    card.enable_button.click()
    _settle(qapp)
    assert owner.config['command_packs']['macros'] is True
    owner.save_config.assert_called_once()
    owner.command_executor.rebuild_matcher.assert_called_once()
    assert card.isVisible() and card.phrase == 'focus mode'
    assert not card.pack_status.isVisible()
    assert not card.enable_button.isVisible()
    assert card.run_button.isVisible()
    reference._execute_cb.assert_not_called()


@pytest.mark.parametrize('accept', [False, True])
def test_hermes_card_requires_notice_and_retains_result(reference, owner, qapp, monkeypatch, accept):
    notice = Mock(return_value=accept)
    monkeypatch.setattr('samsara.ui.hermes_notice.request_acceptance', notice)
    card = show_detail(reference, 'ask hermes', qapp)
    assert card.isVisible()
    card.enable_button.click()
    _settle(qapp)
    notice.assert_called_once_with(reference)
    assert owner.config['command_packs']['hermes'] is accept
    assert owner.config['hermes']['notice_accepted'] is accept
    assert card.isVisible() and card.phrase == 'ask hermes'
    assert card.enable_button.isVisible() is not accept
    assert owner.command_executor.rebuild_matcher.call_count == int(accept)


def test_accepted_hermes_does_not_ask_again(owner, monkeypatch):
    owner.config['hermes']['notice_accepted'] = True
    notice = Mock(side_effect=AssertionError('repeat notice'))
    monkeypatch.setattr('samsara.ui.hermes_notice.request_acceptance', notice)
    assert actions.enable_pack(owner, 'hermes')
    notice.assert_not_called()


@pytest.mark.parametrize('unaccepted', [None, 'true', 1])
def test_non_boolean_notice_values_never_bypass_consent(owner, monkeypatch, unaccepted):
    owner.config['hermes']['notice_accepted'] = unaccepted
    notice = Mock(return_value=False)
    monkeypatch.setattr('samsara.ui.hermes_notice.request_acceptance', notice)
    assert not actions.enable_pack(owner, 'hermes')
    notice.assert_called_once()
    owner.save_config.assert_not_called()


@pytest.mark.parametrize('accept', [False, True])
def test_real_hermes_notice_returns_to_updated_card(reference, owner, qapp, accept):
    from samsara.ui.hermes_notice import HermesNotice
    card = show_detail(reference, 'ask hermes', qapp)
    seen = []
    def answer_notice():
        dialog = QApplication.activeModalWidget()
        seen.append(isinstance(dialog, HermesNotice))
        (dialog.accept if accept else dialog.reject)()
    QTimer.singleShot(0, answer_notice)
    card.enable_button.click()
    _settle(qapp)
    assert seen == [True]
    assert owner.config['command_packs']['hermes'] is accept
    assert card.isVisible() and card.phrase == 'ask hermes'
    assert card.run_button.isVisible() is accept


def test_save_failure_retains_off_state_and_shows_recovery(reference, owner, qapp):
    owner.save_config.side_effect = OSError('test storage unavailable')
    card = show_detail(reference, 'focus mode', qapp)
    card.enable_button.click()
    assert owner.config['command_packs']['macros'] is False
    assert card.isVisible()
    assert 'Could not turn on' in card.pack_status.text()
    owner.command_executor.rebuild_matcher.assert_not_called()


def test_missing_hermes_never_enables(owner, monkeypatch):
    monkeypatch.setattr('samsara.agent_channel.hermes.installed', lambda: False)
    monkeypatch.setattr('PySide6.QtWidgets.QMessageBox.information', Mock())
    assert not actions.enable_pack(owner, 'hermes')
    assert not owner.config['command_packs']['hermes']
    owner.save_config.assert_not_called()


@pytest.mark.parametrize('accept', [False, True])
def test_ai_uses_existing_consent_transaction(owner, monkeypatch, accept):
    from samsara.ui import ava_consent_qt as consent
    from samsara import ava_readiness
    monkeypatch.setattr(ava_readiness, 'recheck_after_settings', lambda *a: None)
    monkeypatch.setattr(consent, 'request_consent', Mock(return_value={'accepted': True} if accept else None))
    apply = Mock(return_value={key: {} for key in consent._PREFERENCE_KEYS})
    apply.return_value['command_packs'] = {'ai': True}
    monkeypatch.setattr(consent, 'apply_accepted_ava', apply)
    owner.update_config = lambda updates, save=True: owner.config.update(updates)
    assert actions.enable_pack(owner, 'ai') is accept
    assert apply.call_count == int(accept)
    if accept:
        assert apply.call_args.kwargs['persist'] is False


@pytest.mark.parametrize('pack', ['macros', 'hermes'])
@pytest.mark.parametrize('enabled', [False, True])
def test_runtime_replaces_frozen_matcher_without_restart(owner, monkeypatch, pack, enabled):
    from samsara.commands import CommandExecutor
    from samsara import plugin_commands
    owner.config['hermes']['notice_accepted'] = True
    owner.config['command_packs'][pack] = not enabled
    executor = CommandExecutor.__new__(CommandExecutor)
    executor._matcher_lock = threading.RLock()
    executor._app = owner
    executor.commands = {}
    monkeypatch.setattr(plugin_commands, '_REGISTRY', {
        'test pack action': {'func': lambda **k: None, 'phrase': 'test pack action',
                             'pack': pack, 'aliases': ['test pack action']}})
    monkeypatch.setattr(plugin_commands, 'set_shared_matcher', lambda _: None)
    executor.rebuild_matcher()
    old = executor._matcher
    owner.command_executor = executor
    before = get_enabled_packs(owner.config)
    owner.config['command_packs'][pack] = enabled
    actions.refresh_runtime(owner, before)
    assert executor._matcher is not old
    assert (executor._matcher.match('test pack action')[0] is not None) is enabled
    owner.cheat_sheet.refresh.assert_called_once()


@pytest.mark.parametrize('destination,pack', [('Commands', 'media'), ('Integrations', 'hermes')])
def test_settings_apply_rebuilds_and_refreshes(tools, qapp, monkeypatch, destination, pack):
    monkeypatch.setattr('samsara.agent_channel.hermes.installed', lambda: True)
    app = tools._settings_tools.app
    app.command_executor.rebuild_matcher = Mock()
    app.cheat_sheet = SimpleNamespace(refresh=Mock())
    tools._settings_tools.show_tool(destination)
    window = tools._settings_tools._window
    checkbox = window._widgets['_pack_checkboxes'][pack]
    expected = not checkbox.isChecked()
    checkbox.setChecked(expected)
    window._apply_and_close()
    assert app.config['command_packs'][pack] is expected
    app.command_executor.rebuild_matcher.assert_called_once()
    app.cheat_sheet.refresh.assert_called_once()


def test_no_effective_change_does_not_rebuild(owner):
    actions.refresh_runtime(owner, get_enabled_packs(owner.config))
    owner.command_executor.rebuild_matcher.assert_not_called()
    assert not actions.enable_pack(owner, 'unknown')
