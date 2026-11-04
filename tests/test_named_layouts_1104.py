"""Offline layout restore receipts: no real processes, windows or owner profiles."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from samsara import plugin_commands, window_layouts as layouts
from samsara.command_registry import CommandMatcher, DispatchState
from plugins.commands import windows as wm


PRIMARY = dict(index=1, device='primary', primary=True, rect=(0, 0, 1920, 1040))
SECONDARY = dict(index=2, device='second', primary=False, rect=(1920, 0, 3840, 1040))


def entry(**changes):
    return dict(app='editor.exe', exe_path=r'C:\Apps\editor.exe', monitor_device='primary',
                rect=[100, 100, 900, 700], maximized=False, **changes)


class FakeDesktop:
    def __init__(self, *, present=(), appears=(), running=False, monitors=None, fail_launch=False):
        self.present, self.appears = list(present), list(appears)
        self.is_running, self.fail_launch = running, fail_launch
        self.monitors = monitors if monitors is not None else [PRIMARY]
        self.placed, self.launched, self.tabs = [], [], []
        self.now = 0.

    def get_monitors(self): return self.monitors
    def find(self, _entry): return self.present
    def running(self, _entry): return self.is_running
    def title(self, hwnd): return {1: 'one', 2: 'two'}.get(hwnd, '')
    def place(self, hwnd, rect, maximized): self.placed.append((hwnd, rect, maximized))
    def open_tabs(self, urls): self.tabs.append(urls)
    def clock(self): return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.now >= .2 and self.appears:
            self.present = self.appears

    def launch(self, value):
        self.launched.append(value)
        if self.fail_launch:
            raise OSError('not installed')

    def restore(self, windows, **extra):
        return layouts.restore_profile(dict(windows=windows, **extra), self,
                                       clock=self.clock, sleep=self.sleep)


def test_missing_app_launched_waited_and_placed():
    desktop = FakeDesktop(appears=[42])
    result = desktop.restore([entry()])
    assert len(desktop.launched) == 1 and .2 <= desktop.now <= 10
    assert desktop.placed == [(42, (100, 100, 800, 600), False)]
    assert result['restored'] == result['launched'] == 1 and result['failed'] == 0


def test_present_app_placed_only():
    desktop = FakeDesktop(present=[42])
    desktop.restore([entry()])
    assert not desktop.launched and desktop.now == 0
    assert len(desktop.placed) == 1


def test_running_process_without_window_waits_without_relaunch():
    desktop = FakeDesktop(running=True, appears=[42])
    assert desktop.restore([entry()])['restored'] == 1
    assert not desktop.launched


def test_wait_is_bounded_once_per_app_and_other_apps_continue():
    desktop = FakeDesktop()
    a, b = entry(), {**entry(), 'app': 'other.exe', 'exe_path': r'C:\Apps\other.exe'}
    result = desktop.restore([a, a, b])
    assert 19.9 <= desktop.now <= 20.01
    assert len(desktop.launched) == 2
    assert result['failed'] == 3 and not desktop.placed


def test_launch_failure_does_not_prevent_browser_tabs():
    desktop = FakeDesktop(fail_launch=True)
    result = desktop.restore([entry()], browser_tabs=['https://example.com'])
    assert result['failed'] == 1 and desktop.now == 0
    assert desktop.tabs == [['https://example.com']]


def test_same_app_windows_assigned_once_with_title_preference():
    desktop = FakeDesktop(present=[1, 2])
    desktop.restore([{**entry(), 'title_pattern': 'two'},
                     {**entry(), 'rect': [900, 100, 1700, 700], 'title_pattern': 'one'}])
    assert desktop.placed == [(2, (100, 100, 800, 600), False),
                              (1, (900, 100, 800, 600), False)]
    assert not desktop.launched


def test_removed_device_uses_primary_even_if_its_old_index_is_reused():
    saved = {**entry(), 'monitor_device': 'unplugged', 'monitor_index': 2,
             'rect': [-4000, -2000, 4000, 3000]}
    assert layouts.restore_rect(saved, [PRIMARY, SECONDARY]) == (0, 0, 1920, 1040)


def test_reordered_device_and_shrunken_work_area_are_clamped():
    saved = {**entry(), 'monitor_device': 'second', 'monitor_index': 1,
             'rect': [1900, -100, 3900, 1400]}
    assert layouts.restore_rect(saved, [SECONDARY, PRIMARY]) == (1920, 0, 1920, 1040)


def test_legacy_profile_without_device_or_launch_path_still_places():
    desktop = FakeDesktop(present=[1])
    old = dict(app='editor.exe', monitor_index=1, rect=[100, 100, 900, 700])
    assert desktop.restore([old])['restored'] == 1
    assert not desktop.launched


def test_no_monitors_does_not_launch_apps():
    desktop = FakeDesktop(monitors=[])
    assert desktop.restore([entry()])['failed'] == 1
    assert not desktop.launched


def test_browser_tabs_opened_once_as_one_new_window():
    calls = []
    urls = ['https://example.com/a', 'https://example.com/b', 'https://example.com/a']
    layouts.open_browser_tabs(urls, resolve=lambda: r'C:\Browser\msedge.exe',
                             popen=lambda *a, **kw: calls.append((a, kw)))
    assert calls == [(([r'C:\Browser\msedge.exe', '--new-window', *urls[:2]],), {'shell': False})]
    desktop = FakeDesktop(present=[1, 2])
    result = desktop.restore([entry(), entry()], browser_tabs=urls)
    assert desktop.tabs == [urls[:2]] and result['tabs'] == 2


@pytest.mark.parametrize('urls', [['file:///C:/secret'], ['javascript:alert(1)'], ['--new-window'],
                                 ['https://example.com bad'], 'https://example.com'])
def test_invalid_tab_sets_never_open(urls):
    desktop = FakeDesktop()
    assert desktop.restore([], browser_tabs=urls)['tab_failed']
    assert not desktop.tabs


def test_unsupported_default_browser_is_reported_without_fallback_launch():
    launch = MagicMock()
    with pytest.raises(ValueError):
        layouts.open_browser_tabs(['https://example.com'], resolve=lambda: 'unknown.exe', popen=launch)
    launch.assert_not_called()


def test_saved_executable_is_one_literal_argument(monkeypatch):
    launch = MagicMock()
    monkeypatch.setattr(layouts.os.path, 'isfile', lambda _path: True)
    monkeypatch.setattr(layouts.subprocess, 'Popen', launch)
    layouts.launch_app(entry())
    launch.assert_called_once_with([r'C:\Apps\editor.exe'], shell=False)
    for value in ('editor.exe', 'https://example.com', r'C:\Apps\editor.cmd', None):
        with pytest.raises(ValueError):
            layouts.launch_app({**entry(), 'exe_path': value})


@pytest.fixture
def saved_profiles(tmp_path, monkeypatch):
    path = tmp_path / 'window_layouts.json'
    path.write_text(json.dumps({'work': {'windows': [], 'browser_tabs': ['https://example.com']},
                                'movie night': {'windows': []}}), encoding='utf-8')
    monkeypatch.setattr(wm, '_get_layouts_path', lambda: path)
    return path


@pytest.mark.parametrize('shared', [False, True])
def test_exact_named_phrases_and_legacy_phrases_use_both_matchers(saved_profiles, monkeypatch, shared):
    registry = {}
    for value in plugin_commands._MODULE_ENTRIES[wm.__name__].values():
        if value['phrase'] in {'save layout', 'restore layout', 'save this tab to layout', 'clear layout tabs'}:
            for phrase in (value['phrase'], *value['aliases']):
                registry[phrase] = value
    monkeypatch.setattr(plugin_commands, '_REGISTRY', registry)
    matcher = CommandMatcher()
    matcher.load_plugins(registry)
    matcher.freeze()
    monkeypatch.setattr(plugin_commands, '_shared_matcher', matcher if shared else None)
    for text in ('WORK layout!', 'restore work layout', 'restore layout work', 'load layout work',
                 'restore window layout the work', 'movie night layout'):
        hit, name = plugin_commands.find_command(text)
        assert hit['func'] is wm.handle_restore_layout
        assert name == ('movie night' if text.startswith('movie') else 'work')
    for text in ('I said work layout yesterday', 'work layout after lunch',
                 'restore work layout after lunch', 'please restore layout work',
                 'restore layout work after lunch', 'unrecorded layout',
                 'save this tab to layout work after lunch'):
        assert plugin_commands.find_command(text) == (None, '')
    hit, name = plugin_commands.find_command('Save layout as morning routine.')
    assert hit['func'] is wm.handle_save_layout and name == 'morning routine'
    for text, handler in [('save this tab to layout work', wm.handle_save_layout_tab),
                          ('clear layout tabs work', wm.handle_clear_layout_tabs)]:
        hit, name = plugin_commands.find_command(text)
        assert hit['func'] is handler and name == 'work'
    if shared:
        matcher.set_enabled_packs({'core'})
        assert matcher.match('work layout') == (None, '')


def test_save_records_launch_identity_normal_rect_and_preserves_tab_set(saved_profiles, monkeypatch):
    gui = MagicMock()
    gui.GetWindowRect.return_value = (-8, -8, 1928, 1048)
    gui.GetWindowPlacement.return_value = (0, 3, (0, 0), (0, 0), (100, 100, 900, 700))
    monkeypatch.setattr(wm, 'win32gui', gui)
    monkeypatch.setattr(wm, 'win32con', SimpleNamespace(SW_SHOWMAXIMIZED=3))
    monkeypatch.setattr(wm, 'get_monitors', lambda: [PRIMARY])
    monkeypatch.setattr(wm, 'get_all_movable_windows', lambda: [(42, 'editor', 123)])
    monkeypatch.setattr(wm.psutil, 'Process', lambda _pid: SimpleNamespace(name=lambda: 'editor.exe',
                                                                    exe=lambda: r'C:\Apps\editor.exe'))
    wm._save_current_layout('work')
    profile = json.loads(saved_profiles.read_text())['work']
    assert profile['browser_tabs'] == ['https://example.com']
    assert profile['windows'][0] == dict(app='editor.exe', exe_path=r'C:\Apps\editor.exe',
                                       title_pattern='editor', monitor_device='primary', monitor_index=1,
                                       rect=[100, 100, 900, 700], maximized=True)


def test_tab_set_can_be_added_by_voice_and_cleared(saved_profiles, monkeypatch):
    from plugins.commands import favorites
    monkeypatch.setattr(favorites, 'current_target', lambda: 'https://example.com/second')
    assert wm.handle_save_layout_tab(None, 'work') is True
    assert wm.handle_save_layout_tab(None, 'work') is True
    assert json.loads(saved_profiles.read_text())['work']['browser_tabs'] == [
        'https://example.com', 'https://example.com/second']
    assert wm.handle_clear_layout_tabs(None, 'work') is True
    assert json.loads(saved_profiles.read_text())['work']['browser_tabs'] == []


def test_partial_restore_is_failed_instead_of_false_success(saved_profiles, monkeypatch):
    monkeypatch.setattr(wm, '_WINDOWS_AVAILABLE', True)
    monkeypatch.setattr(wm, '_restore_layout', lambda _name: {'failed': 1, 'tab_failed': False})
    assert wm.handle_restore_layout(None, 'work').state == DispatchState.FAILED


def test_maximized_current_window_is_restored_before_placing(monkeypatch):
    gui = MagicMock()
    gui.IsIconic.return_value = False
    gui.GetWindowPlacement.return_value = (0, 3)
    monkeypatch.setattr(wm, 'win32gui', gui)
    monkeypatch.setattr(wm, 'win32con', SimpleNamespace(SW_SHOWMAXIMIZED=3, SW_RESTORE=9))
    raised = MagicMock(return_value=True)
    monkeypatch.setattr(wm, 'raise_window', raised)
    wm._LayoutAdapter().place(42, (100, 100, 800, 600), False)
    gui.ShowWindow.assert_called_once_with(42, 9)
    gui.SetWindowPos.assert_called_once_with(42, wm.HWND_TOP, 100, 100, 800, 600, wm.SWP_SHOWWINDOW)
    raised.assert_called_once_with(42, activate=False)


def test_layout_moves_and_app_launches_remain_immediate(monkeypatch):
    from samsara import effect_policy, execution_policy
    declaration = plugin_commands._MODULE_ENTRIES[wm.__name__]['restore layout']
    monkeypatch.setattr(plugin_commands, '_REGISTRY', {'restore layout': declaration})
    handling = effect_policy.handling_for(execution_policy.Invocation('windows.restore_layout'))
    assert handling.tier == 'T0' and handling.wait_s == 0


@pytest.mark.parametrize('text', [
    'save layout as work and close window', 'save layout work then paste',
    'save layout as work after lunch', 'save layout work before the meeting',
    'save layout as work; paste', 'save layout work. New tab',
    'save layout work\npaste', 'please save layout as work',
])
def test_save_grammar_rejects_chains_and_prose(text):
    assert layouts.save_name(text) is None


def test_frozen_layout_grammar_survives_effect_handler_replacement(saved_profiles, monkeypatch):
    registry = {key: value for key, value in plugin_commands._MODULE_ENTRIES[wm.__name__].items()
                if key in {'save layout', 'restore layout'}}
    matcher = CommandMatcher()
    matcher.load_plugins(registry)
    matcher.freeze()
    for declaration in matcher._sorted:
        monkeypatch.setattr(declaration, 'handler', lambda _app, _remainder: True)
    assert matcher.match('new tab then scroll down') == (None, '')
    hit, name = matcher.match('work layout')
    assert hit.phrase == 'restore layout' and name == 'work'
    hit, name = matcher.match('save layout as morning routine')
    assert hit.phrase == 'save layout' and name == 'morning routine'
    matcher.set_enabled_packs({'core'})
    assert matcher.match('work layout') == (None, '')


def test_unmarked_handler_cannot_install_a_whole_utterance_grammar(monkeypatch):
    def effect(_app, _remainder):
        return True
    effect.match_utterance = lambda _text: 'would capture anything'
    registry = {'ordinary': {'phrase': 'ordinary', 'aliases': [], 'func': effect}}
    matcher = CommandMatcher()
    matcher.load_plugins(registry)
    matcher.freeze()
    assert matcher.match('new tab then scroll down') == (None, '')
    monkeypatch.setattr(plugin_commands, '_REGISTRY', registry)
    monkeypatch.setattr(plugin_commands, '_shared_matcher', None)
    assert plugin_commands.find_command('new tab then scroll down') == (None, '')


def test_unavailable_placement_metadata_still_moves_and_raises(monkeypatch):
    gui = MagicMock()
    gui.GetWindowPlacement.side_effect = OSError('window placement unavailable')
    gui.IsIconic.return_value = False
    monkeypatch.setattr(wm, 'win32gui', gui)
    raised = MagicMock(return_value=True)
    monkeypatch.setattr(wm, 'raise_window', raised)
    wm._LayoutAdapter().place(42, (100, 100, 800, 600), False)
    gui.ShowWindow.assert_not_called()
    gui.SetWindowPos.assert_called_once_with(42, wm.HWND_TOP, 100, 100, 800, 600, wm.SWP_SHOWWINDOW)
    raised.assert_called_once_with(42, activate=False)
