"""819: registered phrases reach current surfaces without stealing app commands."""
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import command_catalog as cc
from samsara import samsara_navigation as nav
from samsara.command_registry import CommandMatcher
from samsara.handlers import CommandContext, HotkeyHandler, MethodHandler
from samsara.plugin_commands import load_plugins as load_shipped_plugins

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = json.loads((ROOT / 'commands.json').read_text(encoding='utf-8'))['commands']
NAV_COMMANDS = {phrase: cmd for phrase, cmd in COMMANDS.items()
                if cmd.get('method', '').startswith(('open_samsara_', 'show_samsara_places'))}
PHRASES = [(alias, cmd) for phrase, cmd in NAV_COMMANDS.items()
           for alias in [phrase, *cmd.get('aliases', [])]]


@pytest.fixture
def queue(monkeypatch):
    from samsara.ui import qt_runtime
    pending = deque()
    monkeypatch.setattr(qt_runtime, 'post', pending.append)

    def drain():
        for _ in range(100):
            if not pending:
                return
            pending.popleft()()
        pytest.fail('navigation did not finish posting')
    return pending, drain


def test_current_inventory_has_one_command_per_settings_page_and_tool():
    from samsara.ui.settings_qt import _TAB_NAMES
    from samsara.ui.settings.tools_page import TOOLS
    settings = {nav.PLACES[c['samsara_destination']].target for c in NAV_COMMANDS.values()
                if c.get('samsara_destination') and nav.PLACES[c['samsara_destination']].kind == 'settings'}
    tools = {nav.PLACES[c['samsara_destination']].target for c in NAV_COMMANDS.values()
             if c.get('samsara_destination') and nav.PLACES[c['samsara_destination']].kind == 'tool'}
    assert settings == set(_TAB_NAMES)
    assert tools == set(TOOLS)
    assert set(nav.PLACES) == {c['samsara_destination'] for c in NAV_COMMANDS.values()
                               if 'samsara_destination' in c}


def test_current_hub_inventory_is_covered():
    from samsara.ui.main_window_qt import NAV_ORDER
    hub = {p.target for p in nav.PLACES.values() if p.kind == 'hub'}
    assert set(NAV_ORDER) - {'Settings'} <= hub


@pytest.fixture(scope='module')
def matcher():
    matcher = CommandMatcher()
    matcher.load_builtins(COMMANDS)
    matcher.freeze()
    return matcher


@pytest.mark.parametrize('phrase,cmd', PHRASES, ids=[p for p, _ in PHRASES])
def test_every_phrase_matches_and_dispatches_to_its_destination(phrase, cmd, matcher, monkeypatch):
    entry, remainder = matcher.match(phrase)
    assert entry is not None and not remainder
    assert entry.data == cmd
    assert entry.pack == 'core' and not entry.scope
    app = nav.SamsaraNavigationMixin()
    opened = []
    monkeypatch.setattr(nav, 'open_place', lambda app, place_id: opened.append(place_id))
    monkeypatch.setattr(nav, 'show_places', lambda app: opened.append('places'))
    assert MethodHandler().execute(entry.data, CommandContext(app=app))
    assert opened == [cmd.get('samsara_destination', 'places')]


def test_dictation_app_inherits_navigation():
    # The maintained gate adapter supplies this class without app bootstrap.
    from dictation import DictationApp
    assert issubclass(DictationApp, nav.SamsaraNavigationMixin)
    assert all(hasattr(DictationApp, c['method']) for c in NAV_COMMANDS.values())


@pytest.mark.parametrize('phrase,keys', [('open settings', ['win', 'i']), ('open history', ['ctrl', 'h'])])
def test_existing_settings_and_browser_history_keys_are_unchanged(phrase, keys, monkeypatch, matcher):
    from samsara import handlers
    from samsara.command_scope import MatchContext
    monkeypatch.setattr(handlers, '_get_foreground_exe_lower', lambda: 'chrome.exe')
    entry, remainder = matcher.match(phrase, context=MatchContext.for_app('chrome.exe'))
    assert not remainder and entry.data['type'] == 'hotkey'
    keyboard = Mock()
    assert HotkeyHandler().execute(entry.data, CommandContext(keyboard_controller=keyboard))
    assert [call.args[0] for call in keyboard.press.call_args_list] == keys


def test_repo_collision_checker_finds_no_new_claims_or_orphans(monkeypatch):
    from tools.gen_command_catalog import generate
    from samsara import plugin_commands
    # conftest's registry isolation blocks discovery by default. This check
    # deliberately loads shipped declarations, with no app/service startup.
    monkeypatch.setattr(plugin_commands, 'load_plugins', load_shipped_plugins)
    specs, claims = generate()
    # raw_phrase_claims currently counts builtin keys only. Include builtin
    # aliases too, so a new standalone alias cannot hide a plugin phrase.
    for phrase, cmd in COMMANDS.items():
        cid = 'builtin.' + cc.slug(phrase)
        for alias in cmd.get('aliases', []):
            claims.setdefault(cc.normalize_phrase(alias), set()).add(cid)
    added_ids = {'builtin.' + cc.slug(phrase) for phrase in NAV_COMMANDS}
    collisions = cc.collisions(claims)
    orphans = cc.orphans(claims, specs)
    assert not [(p, ids) for p, ids in collisions if added_ids.intersection(ids)]
    assert not [(p, ids) for p, ids in orphans if added_ids.intersection(ids)]
    print(f'819 repository collision checker: {len(collisions)} existing collisions, '
          f'{len(orphans)} existing orphans; none involve navigation')
    by_id = {s.canonical_id: s for s in specs}
    assert added_ids <= set(by_id)
    assert any(s.kind == 'plugin' for s in specs), 'collision check must load shipped plugins'
    assert all(by_id[cid].risk == 'ui' for cid in added_ids)


@pytest.mark.parametrize('page', ['General', 'Dictation', 'Audio', 'Ava', 'Shortcuts', 'Help'])
def test_settings_page_is_selected_on_first_and_repeated_open(page, monkeypatch, queue):
    from samsara.ui import settings_qt as settings
    pending, drain = queue
    window = SimpleNamespace(show_tab=Mock())
    factory = Mock(return_value=window)
    monkeypatch.setattr(settings, '_SettingsWindow', factory)
    controller = settings.SettingsQt(SimpleNamespace())
    controller.show_page(page)
    assert controller._window is None
    drain()
    window.show_tab.assert_called_once_with(page)
    controller.show_page('Help')
    drain()
    assert factory.call_count == 1
    window.show_tab.assert_called_with('Help')
    with pytest.raises(ValueError):
        controller.show_page('missing page')


@pytest.mark.parametrize('place_id', [k for k, p in nav.PLACES.items()
                                    if p.kind in ('settings', 'tool', 'setting')])
def test_preferences_tools_and_ai_setup_route_to_current_ids(place_id, monkeypatch, queue):
    from samsara.ui import settings_qt as settings
    pending, drain = queue
    controller = Mock()
    factory = Mock(return_value=controller)
    monkeypatch.setattr(settings, 'SettingsQt', factory)
    app = SimpleNamespace(_schedule_ui=pending.append)
    nav.open_place(app, place_id)
    assert not factory.called
    drain()
    place = nav.PLACES[place_id]
    method = {'settings': 'show_page', 'tool': 'show_tool', 'setting': 'show_setting'}[place.kind]
    getattr(controller, method).assert_called_once_with(place.target)
    assert app._settings_qt is controller


def test_ai_setup_reveals_agents_after_tool_creation(monkeypatch, queue):
    from samsara.ui.settings_qt import SettingsQt
    pending, drain = queue
    controller = SettingsQt(SimpleNamespace())
    def build(tool):
        assert tool == 'Integrations'  # 827c: AI setup moved with its controls.
        controller._window = SimpleNamespace(focus_setting=Mock())
    monkeypatch.setattr(controller, '_show_tool', build)
    controller.show_setting('agents')
    drain()
    controller._window.focus_setting.assert_called_once_with('agents')


@pytest.mark.parametrize('place_id', [k for k, p in nav.PLACES.items() if p.kind in ('hub', 'guides')])
def test_cold_hub_build_precedes_selection_and_guides_reveal(place_id, queue):
    pending, drain = queue
    events = []
    main = SimpleNamespace(_window=None)
    home = SimpleNamespace(show_guides=lambda value: events.append(('guides', value)))
    def build():
        events.append('build')
        main._window = SimpleNamespace(_panel_cache={'Home': home})
    def select(page):
        assert main._window is not None
        pending.append(lambda: events.append(('page', page)))
        return True
    app = SimpleNamespace(main_window=main, _schedule_ui=pending.append,
                          show_main_window=lambda: pending.append(build), open_hub_page=select)
    nav.open_place(app, place_id)
    drain()
    assert events[:2] == ['build', ('page', nav.PLACES[place_id].target)]
    if place_id == 'guides':
        assert events[2] == ('guides', True)


@pytest.mark.parametrize('place_id', [k for k, p in nav.PLACES.items() if p.kind == 'method'])
def test_existing_window_methods_are_called_on_ui_thread(place_id, monkeypatch, queue):
    pending, drain = queue
    place = nav.PLACES[place_id]
    app = SimpleNamespace(_schedule_ui=pending.append)
    window = object()
    called = []
    def show():
        called.append(place.target)
        if place.wrapper:
            pending.append(lambda: setattr(app, place.wrapper, window))
    setattr(app, place.target, show)
    surfaced = []
    monkeypatch.setattr(nav, '_surface', surfaced.append)
    nav.open_place(app, place_id)
    assert not called
    drain()
    assert called == [place.target]
    assert surfaced == ([window] if place.wrapper else [])


@pytest.mark.parametrize('choice', ['compact', 'full'])
def test_show_listening_display_is_idempotent_for_both_choices(choice, queue):
    from samsara.listening_display import DisplayChoice
    pending, drain = queue
    app = SimpleNamespace(_schedule_ui=pending.append, _listening_display_visible=False,
                          _listening_display_choice=DisplayChoice(choice, False),
                          apply_listening_indicator_settings=Mock())
    for _ in range(2):
        nav.open_place(app, 'listening_display')
        drain()
        assert app._listening_display_visible is True
    assert app.apply_listening_indicator_settings.call_count == 2
    assert app._listening_display_choice.value == choice


def test_places_help_is_short_spoken_and_shown(queue, monkeypatch):
    pending, drain = queue
    app = SimpleNamespace(_schedule_ui=pending.append, audio_coordinator=Mock(), cheat_sheet=Mock())
    monkeypatch.setattr(nav, '_surface', Mock())
    nav.show_places(app)
    drain()
    app.cheat_sheet.show_filtered.assert_called_once_with('samsara')
    app.audio_coordinator.speak.assert_called_once_with(nav.PLACES_HELP, category='readback', interruptible=False)
    assert len(nav.PLACES_HELP.split()) < 50


def test_reference_group_and_run_phrase_preserve_execution_identity():
    from samsara.ui.command_cheatsheet_qt import catalog_rows, plugin_label
    matcher = CommandMatcher()
    matcher.load_builtins(COMMANDS)
    matcher.freeze()
    rows = catalog_rows(matcher.list_commands())
    grouped = [r for r in rows if r['plugin'] == 'samsara']
    assert {r['phrase'] for r in grouped} == set(NAV_COMMANDS)
    assert plugin_label('samsara') == 'Samsara'
    assert all(r['canonical_id'] == 'builtin.' + cc.slug(r['phrase']) for r in grouped)
    assert all(r['pack'] == 'core' and r['description'] for r in grouped)


def test_unknown_destination_fails_before_scheduling():
    app = SimpleNamespace(_schedule_ui=Mock())
    with pytest.raises(KeyError):
        nav.open_place(app, 'no such page')
    app._schedule_ui.assert_not_called()


@pytest.mark.parametrize('place_id', ['reminders', 'tasks'])
def test_existing_overviews_are_shown_instead_of_toggled(place_id, monkeypatch, queue):
    pending, drain = queue
    overlay = SimpleNamespace(show=Mock())
    app = SimpleNamespace(_schedule_ui=pending.append, notification_manager=object(), alarm_manager=object())
    if place_id == 'reminders':
        from samsara.ui import status_overlay
        monkeypatch.setattr(status_overlay, 'get_overlay', lambda: overlay)
    else:
        from plugins.commands import tasks
        from samsara import tasks_store
        monkeypatch.setattr(tasks, '_get_overlay', lambda: overlay)
        monkeypatch.setattr(tasks_store, 'get_all', lambda: [])
    surfaced = []
    monkeypatch.setattr(nav, '_surface', surfaced.append)
    for _ in range(2):
        nav.open_place(app, place_id)
        drain()
    assert overlay.show.call_count == 2
    assert surfaced == [overlay, overlay]
    if place_id == 'reminders':
        overlay.show.assert_called_with(notification_manager=app.notification_manager, alarm_manager=app.alarm_manager)
    else:
        overlay.show.assert_called_with([])


def test_tutorial_is_retained_and_raised_on_repeated_open(monkeypatch, queue):
    from samsara.ui import tutorial_qt, window_activation
    pending, drain = queue
    factory = Mock()
    monkeypatch.setattr(tutorial_qt, 'TutorialWindow', factory)
    raised = Mock()
    monkeypatch.setattr(window_activation, 'surface_requested', raised)
    app = SimpleNamespace(_schedule_ui=pending.append)
    for _ in range(2):
        nav.open_place(app, 'tutorial')
        drain()
    factory.assert_called_once_with(app)
    assert raised.call_count == 2
    raised.assert_called_with(app._samsara_tutorial)


def test_updates_only_opens_dialog_without_starting_check(monkeypatch, queue):
    from samsara.ui import update_qt, window_activation
    pending, drain = queue
    opener = Mock()
    raised = Mock()
    monkeypatch.setattr(update_qt, 'show_update_dialog', opener)
    monkeypatch.setattr(window_activation, 'surface_requested', raised)
    app = SimpleNamespace(_schedule_ui=pending.append)
    nav.open_place(app, 'updates')
    drain()
    opener.assert_called_once_with(app)
    raised.assert_called_once_with(opener.return_value)


@pytest.mark.parametrize('place_id', ['documentation', 'discord', 'support'])
def test_links_use_existing_url_constants(place_id, monkeypatch, queue):
    from PySide6.QtGui import QDesktopServices
    from samsara.support_feedback import DOCUMENTATION_URL, SUPPORT_URL
    from samsara.ui.community_links import DISCORD_URL
    pending, drain = queue
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()) or True)
    app = SimpleNamespace(_schedule_ui=pending.append)
    nav.open_place(app, place_id)
    drain()
    assert opened == [{'documentation': DOCUMENTATION_URL, 'discord': DISCORD_URL, 'support': SUPPORT_URL}[place_id]]


def test_wrapper_and_direct_qwidget_activation_share_existing_policy(qapp, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from samsara.ui import window_activation
    window = QWidget()
    raised = Mock()
    monkeypatch.setattr(window_activation, 'surface_requested', raised)
    for wrapper in (SimpleNamespace(_window=window), SimpleNamespace(window=window), window):
        nav._surface(wrapper)
    assert raised.call_count == 3
    assert all(c.args == (window,) for c in raised.call_args_list)
    window.deleteLater()
