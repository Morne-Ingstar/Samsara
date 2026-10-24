"""820: hermetic overview tests. No live enumeration, speech or window actions."""
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QPushButton

from plugins.commands import window_switcher as ws, window_cube as cube, windows as mover
from samsara import plugin_commands
from samsara.command_registry import CommandMatcher
from samsara.command_scope import MatchContext
from samsara.ui import window_overview_qt as ui
from samsara.ui.history_source import row_context


@pytest.fixture
def overview(monkeypatch):
    app = SimpleNamespace(config={}, audio_coordinator=Mock(), cancel_recording=Mock())
    rows = [dict(number=1, hwnd=101, pid=201, app='Brave', title='Private full browser title - Brave',
                 short_title='Private full browser title'),
            dict(number=2, hwnd=102, pid=202, app='File Explorer', title='Many tabs and folders',
                 short_title='Many tabs and folders'),
            dict(number=3, hwnd=103, pid=201, app='Brave', title='YouTube - Brave',
                 short_title='YouTube')]
    panel = Mock()
    monkeypatch.setattr(ws, '_overview_rows', rows)
    monkeypatch.setattr(ws, '_overview_app', app)
    monkeypatch.setattr(ws, '_overview_visible', True)
    monkeypatch.setattr(ws, '_overview_keep', False)
    monkeypatch.setattr(ws, '_overview_pending_close', None)
    monkeypatch.setattr(ws, '_overview_panel', lambda: panel)
    monkeypatch.setattr(ws, '_overview_is_live', lambda row: True)
    monkeypatch.setattr(ws, '_speak', Mock())
    monkeypatch.setattr(ws, '_force_focus', Mock(return_value=True))
    monkeypatch.setattr(mover, 'snap_window', Mock(return_value=True))
    monkeypatch.setattr(mover, 'move_window_to_monitor', Mock(return_value=True))
    import win32gui
    monkeypatch.setattr(win32gui, 'ShowWindow', Mock())
    monkeypatch.setattr(win32gui, 'PostMessage', Mock())
    monkeypatch.setattr(cube, 'handle_show_cube', Mock(return_value=True))
    return app, rows, panel


@pytest.mark.parametrize('changes, expected', [
    ({}, True), ({'visible': False, 'minimized': True}, True),
    ({'visible': False}, False), ({'cloaked': True}, False), ({'title': ''}, False),
    ({'owner': 8}, False), ({'exstyle': ws.WS_EX_TOOLWINDOW}, False),
    ({'owner': 8, 'exstyle': ws.WS_EX_APPWINDOW}, True),
])
def test_shared_switchable_filter(changes, expected):
    window = dict(visible=True, minimized=False, title='Document', owner=0, exstyle=0, cloaked=False)
    window.update(changes)
    assert ws._alt_tab_eligible(window) is expected


def test_monitor_order_preserves_z_order_including_minimized():
    left = dict(rect=(-1920, 0, 0, 1080))
    right = dict(rect=(0, 0, 1920, 1080))
    windows = [(1, 'Right top', None, False), (2, 'Left top', None, False),
               (3, 'Left minimized', None, True), (4, 'Right underneath', None, False)]
    result = ws.order_overview_windows(windows, [right, left],
                                      lambda hwnd, monitors: left if hwnd in (2, 3) else right)
    assert [row[0] for row in result] == [2, 3, 1, 4]


def test_short_names_and_titles(monkeypatch):
    import psutil
    monkeypatch.setattr(cube, '_get_pid', lambda hwnd: 55)
    for process, expected in [('explorer.exe', 'File Explorer'), ('brave.exe', 'Brave'),
                              ('Obsidian.exe', 'Obsidian')]:
        monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(name=lambda: process))
        assert cube._friendly_app_name(101) == expected
    assert ws.short_window_title('YouTube - Brave', 'Brave') == 'YouTube'
    assert len(ws.short_window_title('x' * 200)) == 40
    assert ws.short_window_title('  Folder\n  name ') == 'Folder name'


def test_app_file_description_fallback(monkeypatch):
    import psutil
    import win32api
    monkeypatch.setattr(cube, '_get_pid', lambda hwnd: 55)
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(
        name=lambda: 'synthetic.exe', exe=lambda: 'synthetic.exe'))
    monkeypatch.setattr(win32api, 'GetFileVersionInfo', lambda path, key:
                        [(1033, 1200)] if key.endswith('Translation') else 'Friendly Editor')
    assert cube._friendly_app_name(101) == 'Friendly Editor'


@pytest.mark.parametrize('phrase, expected', [
    ('3', (3, 'go', False)), ('three left', (3, 'left', False)),
    ('3 top left', (3, 'top left', False)), ('two top right', (2, 'top right', False)),
    ('three bottom left', (3, 'bottom left', False)),
    ('twenty one bottom right', (21, 'bottom right', False)),
    ('3 maximize', (3, 'maximize', False)), ('3 minimize', (3, 'minimize', False)),
    ('3 next monitor', (3, 'next monitor', False)), ('3 cube', (3, 'cube', False)),
    ('close 2', (2, 'close', False)), ('close two', (2, 'close', False)),
    ('3 right keep it open', (3, 'right', True)), ('three things', None),
    ('0 left', None), ('-1 left', None), ('3 left garbage', None),
])
def test_number_action_grammar(phrase, expected):
    assert ws.parse_overview_action(phrase) == expected


def _matcher():
    registry = CommandMatcher()
    builtins = json.loads((Path(__file__).parents[1] / 'commands.json').read_text(encoding='utf-8'))
    registry.load_builtins(builtins['commands'])
    entries = {}
    for module in (ws, cube):
        for entry in plugin_commands._MODULE_ENTRIES[module.__name__].values():
            entries[entry['phrase']] = entry
    registry.load_plugins(entries)
    registry.freeze()
    return registry


@pytest.mark.parametrize('phrase, number, action', [
    ('3 top left', 3, 'top left'), ('three left', 3, 'left'),
    ('two top right', 2, 'top right'), ('close 2', 2, 'close'),
    ('twenty one right', 21, 'right'), ('21', 21, 'go'), ('3', 3, 'go'),
])
def test_real_registry_routes_scoped_followups(overview, monkeypatch, phrase, number, action):
    app, _, _ = overview
    invoke = Mock(return_value=True)
    monkeypatch.setattr(ws, 'overview_action', invoke)
    registry = _matcher()
    context = MatchContext(tags=frozenset({'window_overview.visible', 'window_cube.visible'}))
    entry, remainder = registry.match(phrase, context=context)
    assert entry.handler(app, remainder)
    assert invoke.call_args.args[:3] == (app, number, action)


def test_reviewed_number_examples_reach_overview_actions(overview, monkeypatch):
    """820: all generated help examples must route and execute the stated layout."""
    from samsara import command_catalog as cc
    from samsara.spoken_numbers import NUMBER_WORDS
    app, _, _ = overview
    invoke = Mock(return_value=True)
    monkeypatch.setattr(ws, 'overview_action', invoke)
    registry = _matcher()
    context = MatchContext(tags=frozenset({'window_overview.visible', 'window_cube.visible'}))
    words = {value: word for word, value in NUMBER_WORDS.items()}
    for number in range(1, 100):
        word = words.get(number) or words[number // 10 * 10] + ' ' + words[number % 10]
        for phrase in ([word, f'window {word}'] if number < 10 else [f'window {word}']):
            examples, precondition = cc.spoken_help(phrase)
            assert 1 <= len(examples) <= 3 and precondition
            for sentence, action in zip(examples, ('go', 'left', 'top right'), strict=True):
                invoke.reset_mock()
                entry, remainder = registry.match(sentence, context=context)
                assert entry.phrase == phrase
                assert entry.handler(app, remainder)
                assert invoke.call_args.args[:3] == (app, number, action)


def test_followups_do_not_capture_numbers_when_hidden(overview):
    registry = _matcher()
    assert registry.match('three', context=MatchContext())[0] is None
    assert registry.match('21', context=MatchContext())[0] is None
    assert registry.match('close two', context=MatchContext())[0] is None
    assert registry.match('twelve left', context=MatchContext())[0] is None
    assert registry.match('window twelve', context=MatchContext())[0] is None


def test_new_number_commands_have_two_word_canonical_names(overview):
    registry = _matcher()
    context = MatchContext(tags=frozenset({'window_overview.visible'}))
    for text in ('12', 'twelve', 'window 12', 'window twelve'):
        entry, remainder = registry.match(text, context=context)
        assert entry.phrase == 'window twelve'
        assert not remainder
    assert not any(row['phrase'] in {'ten', 'twelve', 'twenty', 'sixty'}
                   for row in registry.list_commands())


def test_show_aliases_and_cancel_keep_recording_behavior(overview):
    app, _, panel = overview
    registry = _matcher()
    for phrase in ('show windows', 'window overview', 'show all windows'):
        entry, remainder = registry.match(phrase)
        assert entry.handler is ws.handle_show_windows
    entry, remainder = registry.match('cancel')
    from samsara.handlers import MethodHandler
    assert entry.source == 'builtin' and entry.data['method'] == 'cancel_recording'
    handler = MethodHandler()
    assert handler.execute(entry.data, SimpleNamespace(app=app))
    panel.hide.assert_called_once()
    app.cancel_recording.assert_not_called()
    assert handler.execute(entry.data, SimpleNamespace(app=app))
    app.cancel_recording.assert_called_once()


@pytest.mark.parametrize('direction', ['left', 'right', 'top left', 'top right',
                                      'bottom left', 'bottom right'])
def test_snap_uses_shared_exact_window_helper_and_closes(overview, direction):
    app, _, panel = overview
    assert ws.handle_overview_number(app, 3, direction)
    assert mover.snap_window.call_args.args == (103, direction)
    assert mover.snap_window.call_args.kwargs['preflight']()
    panel.hide.assert_called_once()


def test_keep_open_stays_for_next_action(overview):
    app, _, panel = overview
    assert ws.handle_overview_number(app, 3, 'left keep it open')
    assert ws.handle_overview_number(app, 2, 'right')
    panel.hide.assert_not_called()
    panel.keep_open.assert_called_with(True)


def test_close_requires_confirmation_revalidates_and_expires(overview, monkeypatch):
    import win32gui
    app, _, panel = overview
    assert ws.overview_action(app, 2, 'close')
    win32gui.PostMessage.assert_not_called()
    assert 'confirm close' in panel.confirmation.call_args.args[0]
    assert ws.handle_confirm_overview_close(app, '')
    win32gui.PostMessage.assert_called_once_with(102, 0x0010, 0, 0)
    assert not ws.overview_active()


def test_close_expired_or_reused_window_never_posts(overview, monkeypatch):
    import win32gui
    app, _, _ = overview
    ws.overview_action(app, 2, 'close')
    monkeypatch.setattr(ws, '_overview_pending_close', (2, 0))
    ws.handle_confirm_overview_close(app, '')
    win32gui.PostMessage.assert_not_called()
    ws.overview_action(app, 2, 'close')
    monkeypatch.setattr(ws, '_overview_is_live', lambda row: False)
    ws.handle_confirm_overview_close(app, '')
    win32gui.PostMessage.assert_not_called()


def test_other_actions_delegate(overview, monkeypatch):
    import win32gui
    app, _, _ = overview
    monkeypatch.setattr(ws, '_overview_keep', True)
    monitors = [dict(rect=(0, 0, 10, 10)), dict(rect=(10, 0, 20, 10))]
    monkeypatch.setattr(mover, 'get_monitors', lambda: monitors)
    monkeypatch.setattr(mover, '_get_monitor_for_window', lambda hwnd, monitors: monitors[0])
    for action in ('go', 'maximize', 'minimize', 'next monitor', 'cube'):
        assert ws.overview_action(app, 1, action)
    ws._force_focus.assert_called_once_with(101)
    assert win32gui.ShowWindow.call_count == 2
    assert mover.move_window_to_monitor.call_args.args == (101, monitors[1])
    cube.handle_show_cube.assert_called_once_with(app, '')


def test_read_chunks_duplicates_only_and_full_title_opt_in(overview):
    app, rows, _ = overview
    assert ws.handle_read_windows(app, '')
    chunks = [call.args[0] for call in app.audio_coordinator.speak.call_args_list]
    assert chunks == ['3 windows.', '1: Brave - Private full browser title.',
                      '2: File Explorer.', '3: Brave - YouTube.']
    assert rows[1]['title'] not in ' '.join(chunks)
    assert rows[0]['title'] not in ' '.join(chunks)
    assert all(call.kwargs['queue_mode'] == 'append'
               for call in app.audio_coordinator.speak.call_args_list)
    assert ws.handle_read_window(app, 'two')
    ws._speak.assert_called_with(app, rows[1]['title'])


def test_show_builds_once_read_reuses_numbers(overview, monkeypatch):
    app, _, panel = overview
    monkeypatch.setattr(ws, '_get_all_windows', Mock(return_value=[
        (103, 'YouTube - Brave', None, False), (102, 'Folders', None, True)]))
    monkeypatch.setattr(ws, '_get_pid', lambda hwnd: hwnd)
    monkeypatch.setattr(cube, '_friendly_app_name', lambda hwnd: 'Brave' if hwnd == 103 else 'File Explorer')
    monkeypatch.setattr(ws, '_assign_letters', lambda windows: {})
    monkeypatch.setattr(ws, '_mapping', {})
    monkeypatch.setattr(ws, '_manager', None)
    monkeypatch.setattr(ws, '_cancel_timer', lambda: None)
    monkeypatch.setattr(mover, 'get_monitors', lambda: [])
    monkeypatch.setattr(mover, '_get_monitor_for_window', lambda hwnd, monitors: None)
    assert ws.handle_show_windows(app, '')
    assert [row['hwnd'] for row in ws._overview_rows] == [103, 102]
    assert panel.show.call_args.args[0][0]['number'] == 1
    assert ws.handle_read_windows(app, '')
    ws._get_all_windows.assert_called_once()


def test_foreground_label_is_distinct_from_agent_channel():
    assert row_context(dict(entry_type='command', matched_command='show windows', app_context='Claude')) == 'in Claude'
    assert row_context(dict(entry_type='command', matched_command='ask claude', app_context='Notepad')) == 'Claude'
    assert row_context(dict(app_context='Unknown')) == 'Unknown'


class FakeDwm:
    def __init__(self):
        self.registered, self.released, self.updated = [], [], []
        self.fail = False

    def DwmRegisterThumbnail(self, destination, source, output):
        if self.fail:
            return -1
        output._obj.value = 0x100000000 + source
        self.registered.append((destination, source))
        return 0

    def DwmQueryThumbnailSourceSize(self, handle, output):
        output._obj.cx, output._obj.cy = 1920, 1080
        return 0

    def DwmUpdateThumbnailProperties(self, handle, props):
        self.updated.append((handle.value, bool(props._obj.visible)))
        return 0

    def DwmUnregisterThumbnail(self, handle):
        self.released.append(handle.value)
        return 0


def test_dwm_pointer_sized_handles_cleanup_and_failure():
    api = FakeDwm()
    thumbnails = ui.DwmThumbnails(api)
    assert thumbnails.register(77, 101)
    assert thumbnails.register(77, 101)
    assert len(api.registered) == 1
    assert thumbnails.update(101, (0, 0, 100, 100), True)
    thumbnails.close()
    thumbnails.close()
    assert api.released == [0x100000000 + 101]
    assert not thumbnails.handles
    api.fail = True
    assert not thumbnails.register(77, 102)
    assert not thumbnails.handles


def test_actual_widget_buttons_confirmation_and_thumbnail_lifecycle(qapp, overview, monkeypatch):
    app, rows, _ = overview
    api = FakeDwm()
    invoked = Mock()
    closed = Mock()
    window = ui.OverviewWindow(rows, invoked, closed, thumbnails=ui.DwmThumbnails(api))
    window.show()
    qapp.processEvents()
    buttons = {b.accessibleName(): b for b in window.findChildren(QPushButton)}
    assert 'Window 3: Top left' in buttons
    assert buttons['Window 3: Top left'].font().pixelSize() >= ui.theme.TYPE_BODY
    assert len([key for key in buttons if key.startswith('Window ')]) == 36
    buttons['Window 3: Top left'].click()
    invoked.assert_called_with(3, 'top left', False)
    window.show_confirmation('Close 2: File Explorer?')
    assert window.confirm.isVisible()
    window.confirm.click()
    invoked.assert_called_with(0, 'confirm close', False)
    assert len(api.registered) == 3
    window.close()
    assert len(api.released) == 3
    assert not window._thumbnails.handles
    closed.assert_called_once()


def test_dwm_failure_shows_icon_and_no_handles(qapp, overview, monkeypatch):
    from PySide6.QtGui import QIcon, QPixmap
    app, rows, _ = overview
    pixmap = QPixmap(32, 32)
    pixmap.fill()
    monkeypatch.setattr(ui, '_app_icon', lambda hwnd: QIcon(pixmap))
    api = FakeDwm()
    api.fail = True
    window = ui.OverviewWindow(rows[:1], Mock(), Mock(), thumbnails=ui.DwmThumbnails(api))
    window.show()
    qapp.processEvents()
    assert not window._previews[0][1].pixmap().isNull()
    window.close()
    assert not api.released


def test_queued_open_then_hide_never_builds_stale_panel(monkeypatch):
    callbacks = []
    monkeypatch.setattr(ui.qt_runtime, 'ensure_started', lambda: None)
    monkeypatch.setattr(ui.qt_runtime, 'post', callbacks.append)
    constructor = Mock()
    monkeypatch.setattr(ui, 'OverviewWindow', constructor)
    panel = ui.OverviewPanel()
    panel.show([], Mock(), Mock())
    panel.hide()
    for callback in callbacks:
        callback()
    constructor.assert_not_called()
