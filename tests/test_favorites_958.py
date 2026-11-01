"""958: real store, matcher/policy and Qt HUD; native reads/launches stay fake."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import favorites as fav
from samsara import favorites_store as store, plugin_commands, execution_policy as policy
from samsara.command_registry import DispatchState
from samsara.command_scope import MatchContext


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    monkeypatch.setattr(fav, '_visible', False)
    monkeypatch.setattr(fav, '_app', None)
    monkeypatch.setattr(fav, '_rows', [])


@pytest.fixture
def app():
    return SimpleNamespace(config={}, save_config=Mock(), _show_outcome_chip=Mock(),
                           audio_coordinator=SimpleNamespace(speak=Mock()),
                           command_matching_enabled=True, command_mode_active=True,
                           _ava_cmd_generation=0)


@pytest.fixture
def panel(monkeypatch):
    panel = Mock()
    monkeypatch.setattr(fav, '_panel', lambda: panel)
    monkeypatch.setattr('samsara.ui.qt_runtime.is_alive', lambda: True)
    return panel


@pytest.fixture(scope='module')
def production():
    from tools.dump_command_metadata import build_executor
    executor = build_executor()
    return executor, dict(plugin_commands._REGISTRY)


@pytest.fixture
def executor(production, monkeypatch, app):
    executor, registry = production
    monkeypatch.setattr(plugin_commands, '_REGISTRY', dict(registry))
    monkeypatch.setattr(executor, '_app', app)
    monkeypatch.setattr('samsara.command_scope.capture_context', lambda: MatchContext(tags=frozenset(
        {'window_cube.visible', 'numbered_list.visible'} if fav.active(app) else set())))
    app.command_executor = executor
    return executor


def test_profile_and_crud_survive_reload(tmp_path):
    saved = store.FavoritesStore()
    assert saved.path == tmp_path / 'favorites.json'
    assert saved.load() == []
    assert saved.add('example.com') == (1, True)
    assert saved.add(r'C:\Example\one.lnk') == (2, True)
    assert saved.add('example.com') == (1, False)
    saved.edit(2, name='Notes & résumé')
    assert store.FavoritesStore().load()[1]['name'] == 'Notes & résumé'
    saved.edit(1)
    assert saved.load() == [dict(name='Notes & résumé', target=r'C:\Example\one.lnk')]


@pytest.mark.parametrize('raw', ['{bad json', '{"version":1,"items":null}',
                                '{"version":1,"items":[{}]}', '{"version":99,"items":[]}'])
def test_bad_store_is_preserved_on_add(tmp_path, raw):
    path = tmp_path / 'favorites.json'
    path.write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError, match='preserved'):
        store.FavoritesStore().add('example.com')
    assert path.read_text(encoding='utf-8') == raw


def test_failed_atomic_replace_keeps_previous_list(monkeypatch):
    saved = store.FavoritesStore()
    saved.add('example.com')
    original = saved.path.read_bytes()
    monkeypatch.setattr(store.os, 'replace', Mock(side_effect=OSError('synthetic failure')))
    with pytest.raises(OSError):
        saved.add('https://example.org')
    assert saved.path.read_bytes() == original
    assert not list(saved.path.parent.glob('favorites-*.tmp'))


@pytest.mark.parametrize('target', ['javascript:alert(1)', 'file:///C:/tmp', 'cmd /c calc',
                                   'https://user:pass@example.com', 'https://example.com/\nsecret',
                                   'relative-folder', 'https:///missing-host'])
def test_non_targets_rejected(target):
    with pytest.raises(ValueError):
        store.normalize_target(target)


def test_list_cap_and_invalid_number_do_not_change_file():
    saved = store.FavoritesStore()
    saved._save([dict(name=f'Item {n}', target=f'https://example.com/{n}') for n in range(99)])
    original = saved.path.read_bytes()
    with pytest.raises(ValueError, match='99'):
        saved.add('example.org')
    with pytest.raises(ValueError):
        saved.edit(0)
    assert saved.path.read_bytes() == original


@pytest.mark.parametrize('kind', ['file.txt', 'shortcut.lnk', 'folder'])
def test_local_targets_use_existing_default_open(tmp_path, monkeypatch, kind):
    from samsara import platform
    target = tmp_path / kind
    target.mkdir() if kind == 'folder' else target.touch()
    opened = Mock(return_value=True)
    monkeypatch.setattr(platform, 'open_file_or_folder', opened)
    store.open_target(str(target))
    opened.assert_called_once_with(target)


def test_missing_and_browser_failures_are_explicit(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='missing'):
        store.open_target(str(tmp_path / 'missing.lnk'))
    import webbrowser
    opened = Mock(return_value=True)
    monkeypatch.setattr(webbrowser, 'open', opened)
    store.open_target('example.com')
    opened.assert_called_once_with('https://example.com')
    opened.return_value = False
    with pytest.raises(ValueError, match='browser'):
        store.open_target('example.org')


@pytest.mark.parametrize('phrase,canonical,remainder', [
    ('add this to favorites', 'add favorite', ''),
    ('add this to quick list', 'add favorite', ''),
    (r'add "C:\Example\Mixed Case.lnk" to quick list', 'add favorite', r'"C:\Example\Mixed Case.lnk" to quick list'),
    ('add https://example.com/Case?q=A.B to favorites', 'add favorite', 'https://example.com/Case?q=A.B to favorites'),
    ('add https://example.com/Case?q=A.B to favorites.', 'add favorite', 'https://example.com/Case?q=A.B to favorites.'),
    ('remove three from favorites', 'remove favorite', 'three from favorites'),
    ('show favorites', 'show favorites', ''),
    ('quick list', 'show favorites', ''),
    ('favorites font size thirty', 'favorites font size', 'thirty'),
    ('rename favorite 2 to My Notes.', 'rename favorite', '2 to My Notes.'),
])
def test_real_matcher_phrases(production, phrase, canonical, remainder):
    entry, rest = production[0]._matcher.match(phrase, MatchContext())
    assert entry is not None and entry.phrase == canonical
    assert rest == remainder


@pytest.mark.parametrize('phrase', ['add milk to shopping list', 'remove three from the shelf',
                                   'rename 2 to Notes', 'add', 'remove', 'rename'])
def test_generic_prose_does_not_become_favorites(production, phrase):
    entry, _ = production[0]._matcher.match(phrase, MatchContext())
    assert entry is None or entry.handler.__module__ != fav.__name__


def test_spoken_lifecycle_through_real_executor(executor, app, panel, monkeypatch):
    assert executor.process_text('add example.com to quick list.', force_commands=True).succeeded
    assert executor.process_text('add https://example.org to favorites', force_commands=True).succeeded
    assert executor.process_text('show favorites', force_commands=True).succeeded
    assert panel.show.call_args.args[0] == store.FavoritesStore().load()
    assert executor.process_text('rename two to My Work', force_commands=True).succeeded
    assert store.FavoritesStore().load()[1]['name'] == 'My Work'
    assert executor.process_text('remove one from favorites', force_commands=True).succeeded
    opened = Mock()
    monkeypatch.setattr(fav, 'open_target', opened)
    assert executor.process_text('one', force_commands=True).succeeded
    opened.assert_called_once_with('https://example.org')
    assert not fav.active()


def test_add_this_passes_real_policy_with_no_argument(executor, app, panel, monkeypatch):
    monkeypatch.setattr(fav, 'current_target', lambda: 'https://example.com/ActiveTab')
    assert executor.process_text('add this to favorites', force_commands=True).succeeded
    assert store.FavoritesStore().load()[0]['target'] == 'https://example.com/ActiveTab'


@pytest.mark.parametrize('spoken,number', [('3', 3), ('twelve', 12), ('ninety nine', 99)])
def test_bare_numbers_use_same_snapshot_and_close(executor, app, panel, monkeypatch, spoken, number):
    saved = store.FavoritesStore()
    saved._save([dict(name=f'Item {n}', target=f'https://example.com/{n}') for n in range(1, 100)])
    fav.show_favorites(app)
    saved.edit(1)  # External edit must never reassign an already displayed number.
    opened = Mock()
    monkeypatch.setattr(fav, 'open_target', opened)
    result = executor.process_text(spoken, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    opened.assert_called_once_with(f'https://example.com/{number}')
    assert not fav.active()


def test_failed_pick_keeps_hud_and_visible_feedback(app, panel, monkeypatch):
    store.FavoritesStore().add(r'C:\Example\missing.lnk')
    fav.show_favorites(app)
    monkeypatch.setattr(fav, 'open_target', Mock(side_effect=ValueError('This favorite is missing.')))
    assert fav.pick(app, 1).state is DispatchState.FAILED
    assert fav.active(app)
    assert 'missing' in app._show_outcome_chip.call_args.args[0]
    panel.hide.assert_not_called()


def test_close_window_numbers_do_not_gain_favorites_scope(executor, app, panel):
    fav.show_favorites(app)
    context = executor._matcher.current_context()
    entry, _ = executor._matcher.match('close 12', context)
    assert entry is None or entry.phrase != 'close 12'


def test_ava_writes_remain_confirmed_and_can_execute(executor, app):
    args = {'remainder': 'https://example.com/FromAva'}
    inv = policy.Invocation('add favorite', args, policy.Route.MODEL, 0)
    assert isinstance(policy.authorize(inv, app=app, executor=executor), policy.NeedsConfirmation)
    assert store.FavoritesStore().load() == []
    result = executor.execute_canonical('add favorite', app, route=policy.Route.MODEL,
                                        generation=0, args=args, confirmed=True)
    assert result.succeeded
    assert store.FavoritesStore().load()[0]['target'] == args['remainder']
    for command, remainder in [('rename favorite', '1 to Ava name'), ('remove favorite', '1')]:
        inv = policy.Invocation(command, {'remainder': remainder}, policy.Route.MODEL, 0)
        assert isinstance(policy.authorize(inv, app=app, executor=executor), policy.NeedsConfirmation)


def test_ava_can_show_list(executor, app, panel):
    result = executor.execute_canonical('show favorites', app, route=policy.Route.MODEL,
                                        generation=0, args={})
    assert result.succeeded and fav.active(app)


def test_size_font_voice_persists_and_bounds(app, panel):
    assert fav.font_size(app, 'thirty') is True
    assert fav.window_size(app, 'nine hundred by seven hundred') is True
    assert app.config['favorites'] == dict(font_size=30, width=900, height=700)
    assert fav.font_size(app, '100').state is DispatchState.FAILED
    assert fav.window_size(app, '5 by 2').state is DispatchState.FAILED


def test_foreground_change_refuses_this(monkeypatch):
    from samsara import handlers
    import win32gui
    monkeypatch.setattr(handlers, '_get_foreground_hwnd', Mock(side_effect=[100, 200]))
    monkeypatch.setattr(handlers, '_get_foreground_exe_lower', lambda: 'explorer.exe')
    monkeypatch.setattr(win32gui, 'GetClassName', lambda hwnd: 'CabinetWClass')
    monkeypatch.setattr(fav, '_explorer_target', lambda hwnd: r'C:\Example\Notes')
    with pytest.raises(ValueError, match='changed'):
        fav.current_target()


@pytest.mark.parametrize('selected', [True, False])
def test_explorer_reads_selection_or_folder_without_keys(monkeypatch, selected):
    import pythoncom
    import win32com.client
    monkeypatch.setattr(pythoncom, 'CoInitialize', Mock())
    monkeypatch.setattr(pythoncom, 'CoUninitialize', Mock())
    selection = SimpleNamespace(Count=int(selected), Item=lambda n: SimpleNamespace(Path=r'C:\Example\selected.txt'))
    doc = SimpleNamespace(SelectedItems=lambda: selection,
                          Folder=SimpleNamespace(Self=SimpleNamespace(Path=r'C:\Example\Folder')))
    shell = SimpleNamespace(Windows=lambda: [SimpleNamespace(HWND=100, Document=doc)])
    monkeypatch.setattr(win32com.client, 'Dispatch', lambda name: shell)
    assert fav._explorer_target(100) == (r'C:\Example\selected.txt' if selected else r'C:\Example\Folder')


def test_browser_reads_chrome_and_ignores_page_url_field(monkeypatch):
    from samsara import uia
    def node(in_document):
        return SimpleNamespace(control_type='EditControl', in_document=in_document,
                               enabled=True, offscreen=False, name='Address and search bar')
    page, chrome = node(True), node(False)
    service = SimpleNamespace(snapshot=lambda **kw: SimpleNamespace(hwnd=100, nodes=[page, chrome]),
                              act=Mock(return_value='https://example.com/VisibleTab'))
    monkeypatch.setattr(uia, 'get_service', lambda: service)
    assert fav._browser_target(100) == 'https://example.com/VisibleTab'
    service.act.assert_called_once_with(chrome, 'read')
    with pytest.raises(ValueError, match='changed'):
        fav._browser_target(200)


def test_hud_cards_click_resize_font_escape(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from samsara.ui.favorites_overlay_qt import FavoritesWindow
    picked, closed, settings = Mock(), Mock(), Mock()
    rows = [dict(name=f'Item {n}', target=f'https://example.com/{n}') for n in range(12)]
    window = FavoritesWindow(rows, picked, closed, on_settings=settings)
    window.show()
    for _ in range(5):
        qapp.processEvents()
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert len(window.buttons) == 12
    assert window.scroll.verticalScrollBar().maximum() > 0
    QTest.mouseClick(window.buttons[2], Qt.MouseButton.LeftButton)
    picked.assert_called_once_with(3)
    window.set_font_size(30)
    window.resize(900, 700)
    window.save_settings()
    assert settings.call_args.args[0] == dict(font_size=30, width=900, height=700)
    QTest.keyClick(window, Qt.Key.Key_Escape)
    qapp.processEvents()
    closed.assert_called_once()
    assert not window.isVisible()


def test_late_render_cannot_reopen_after_hide(monkeypatch, qapp):
    from samsara.ui.favorites_overlay_qt import FavoritesPanel
    jobs = []
    monkeypatch.setattr('samsara.ui.qt_runtime.post', jobs.append)
    panel = FavoritesPanel()
    panel.show([], Mock(), Mock())
    panel.hide()
    for job in jobs:
        job()
    assert panel.window is None


def test_long_names_and_urls_fit_small_window_at_large_font(qapp):
    from samsara.ui.favorites_overlay_qt import FavoritesWindow
    row = dict(name='Long title ' * 18, target='https://example.com/' + 'x' * 200)
    window = FavoritesWindow([row], Mock(), Mock(), dict(width=480, height=320, font_size=32))
    window.show()
    for _ in range(5):
        qapp.processEvents()
    assert window.width() == 480
    assert window.host.width() <= window.scroll.viewport().width()
    assert window.names[0].text().endswith('…')
    assert window.names[0].toolTip() == row['name']
    assert window.targets[0].text().endswith('…')
    assert window.targets[0].toolTip() == row['target']
    assert window.buttons[0].text() == 'Open'
    assert window.buttons[0].accessibleName().endswith(row['name'])
    assert window.names[0].geometry().center().y() == window.targets[0].geometry().center().y()
    assert window.names[0].geometry().right() < window.targets[0].geometry().left()
    assert window.targets[0].geometry().right() < window.buttons[0].geometry().left()
    assert window.names[0].parentWidget().height() <= 80
    window.close()


def test_icons_use_local_shell_and_cache_without_site_or_unc_reads(qapp, tmp_path, monkeypatch):
    from samsara.ui import favorites_overlay_qt as hud
    executable = tmp_path / 'program.exe'
    executable.touch()
    provider = Mock()
    provider.icon.return_value.isNull.return_value = False
    monkeypatch.setattr(hud, 'QFileIconProvider', Mock(return_value=provider,
        Option=hud.QFileIconProvider.Option, IconType=hud.QFileIconProvider.IconType))
    real_info = hud.QFileInfo
    info = Mock(side_effect=real_info)
    monkeypatch.setattr(hud, 'QFileInfo', info)
    hud._favorite_icon.cache_clear()
    try:
        hud._favorite_icon(str(executable))
        hud._favorite_icon(str(executable))
        info.assert_called_once_with(str(executable))
        assert provider.icon.call_count == 1
        assert isinstance(provider.icon.call_args.args[0], real_info)
        assert not hud._favorite_icon('https://example.com/page').isNull()
        assert provider.icon.call_count == 1  # Site globe is painted locally.
        hud._favorite_icon(r'\\server\share\program.exe')
        assert info.call_count == 1  # Neither site nor UNC path probes the filesystem.
        assert provider.icon.call_args_list[-1].args[0] == hud.QFileIconProvider.IconType.File
    finally:
        hud._favorite_icon.cache_clear()
