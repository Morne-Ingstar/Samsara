"""765: number parsing through real command seams; all effects are stubbed."""
from types import SimpleNamespace as NS

import pytest

from samsara.spoken_numbers import (
    number_words_to_digits, parse_spoken_number, spoken_number_matches,
)


@pytest.mark.parametrize('text,expected', [
    ('0', 0), ('2', 2), ('12', 12), ('002', 2), ('two', 2), ('twelve', 12),
    ('twenty one', 21), ('twenty-one', 21), ('ninety nine', 99),
    ('second', 2), ('2nd', 2), ('twenty second', 22), ('twelfth', 12),
    ('one hundred', 100), ('four hundred and two', 402), ('two thousand', 2000),
    ('', None), ('junk', None), ('someone', None), ('two windows', None),
    ('two two', None), ('twenty twelve', None), ('-2', None), ('2.5', None),
    ('one hundred and', None), ('for', None), ('to', None), ('too', None),
])
def test_helper_table(text, expected):
    assert parse_spoken_number(text) == expected


def test_homophones_are_explicit():
    for text, value in {'won': 1, 'to': 2, 'too': 2, 'for': 4}.items():
        assert parse_spoken_number(text) is None
        assert parse_spoken_number(text, homophones={text: value}) == value


def test_extraction_keeps_boundaries_and_separate_numbers():
    assert [v for _, _, v in spoken_number_matches('someone two into twenty one, 4')] == [2, 21, 4]
    assert number_words_to_digits('two minutes thirty seconds') == '2 minutes 30 seconds'
    assert number_words_to_digits('2:00 pm 1.5 minutes') == '2:00 pm 1.5 minutes'


@pytest.mark.parametrize('text', ['2', 'two', 'second', '2nd'])
def test_cube_page_through_registry(monkeypatch, text):
    from plugins.commands import window_cube as cube
    from samsara import plugin_commands
    plugin_commands._reinstall_module_commands(cube)
    from samsara.command_registry import CommandMatcher
    matcher = CommandMatcher()
    matcher.load_plugins(plugin_commands._REGISTRY)
    matcher.freeze()
    app = NS(config={'window_cube': {'max_rows': 1}})
    monkeypatch.setattr(cube, '_slots', [{'number': 1}, {'number': 2}])
    monkeypatch.setattr(cube, '_page', 1)
    rendered = []
    monkeypatch.setattr(cube, '_render', lambda app: rendered.append(cube._page))
    monkeypatch.setattr(cube, '_speak', lambda *args: pytest.fail('valid page refused'))
    entry, remainder = matcher.match('cube page ' + text)
    assert remainder == text
    assert entry.handler(app, remainder) is True
    assert rendered == [2]


def test_cube_bare_advances_and_wraps_invalid_stays(monkeypatch):
    from plugins.commands import window_cube as cube
    app = NS(config={'window_cube': {'max_rows': 1}})
    monkeypatch.setattr(cube, '_slots', [{'number': 1}, {'number': 2}])
    monkeypatch.setattr(cube, '_page', 1)
    monkeypatch.setattr(cube, '_render', lambda app: None)
    spoken = []
    monkeypatch.setattr(cube, '_speak', lambda app, text: spoken.append(text))
    cube.handle_cube_page(app, '')
    assert cube._page == 2
    cube.handle_cube_page(app, '')
    assert cube._page == 1
    for text in ('someone', '0', '3', 'two two'):
        cube.handle_cube_page(app, text)
        assert cube._page == 1
    monkeypatch.setattr(cube, '_slots', [{'number': 1}])
    cube.handle_cube_page(app, '')
    assert spoken[-1] == 'There is only one cube page.'


@pytest.mark.parametrize('text', ['2', 'two'])
def test_cube_choose_copy_tile_and_desk(monkeypatch, text):
    from plugins.commands import window_cube as cube, app_verbs
    app = NS()
    monkeypatch.setattr(cube, '_app_ref', app)
    monkeypatch.setattr(cube, '_selection_available', lambda: True)
    monkeypatch.setattr(cube, '_slot_for_number', lambda n: {'hwnd': 22, 'title': 'Test', 'closed': False})
    monkeypatch.setattr(cube, '_build_rows', lambda app: [(22, 'App', 'Test')])
    switched = []
    monkeypatch.setattr(cube, '_switch_to_number', lambda app, n: switched.append(n) or True)
    assert cube.handle_choose(app, text) is True
    assert switched == [2]
    assert cube._number_from('window', text) == 2
    numbers = []
    monkeypatch.setattr(cube, '_letters_for_numbers', lambda app, nums: numbers.append(nums) or ['B', 'D'])
    monkeypatch.setattr(cube, 'handle_window_copy', lambda app, r: r)
    monkeypatch.setattr(cube, 'handle_window_tile', lambda app, r: r)
    assert cube.handle_cube_copy(app, text + ' into four') == 'B into D'
    assert cube.handle_cube_tile(app, text + ' and four') == 'B and D'
    assert numbers == [[2, 4], [2, 4]]
    monkeypatch.setattr(app_verbs, '_switch_desktop', lambda n: switched.append(n) or True)
    assert app_verbs.handle_desk(app, text) is True
    assert switched == [2, 2]


@pytest.mark.parametrize('text', ['2', 'two'])
def test_overlay_and_named_selection(monkeypatch, text):
    from plugins.commands import show_numbers as overlay, show_numbers_targets as targets, click_by_name as named
    app = NS()
    monkeypatch.setattr(named, 'active', lambda app: True)
    assert named.selection_number(text, app) == 2
    fake_overlay = NS(grid_active=lambda: False, is_overlay_active=lambda: True)
    assert targets.selection_number(text, fake_overlay) == 2
    assert overlay._parse_spoken_number(text) == 2
    assert overlay._parse_click_target('click ' + text) == (2, 'single', frozenset())
    assert overlay._is_label_number(text)
    monkeypatch.setattr(named, 'active', lambda app: False)
    assert named.selection_number(text, app) is None
    fake_overlay.grid_active = lambda: True
    assert targets.selection_number(text, fake_overlay) is None


def test_overlay_rejects_prose_and_out_of_range():
    from plugins.commands import show_numbers as overlay
    for text in ('someone', 'two buttons', '100', 'two two'):
        assert overlay._parse_spoken_number(text) is None
        assert not overlay._is_label_number(text)


@pytest.mark.parametrize('text', ['2', 'two'])
def test_tab_and_monitors(monkeypatch, text):
    from plugins.commands import tab_finder, window_switcher as switcher, windows
    assert tab_finder._browser_tab_number(text) == 2
    assert switcher._parse_monitor_index('monitor ' + text) == 2
    monitors = [{'index': 1}, {'index': 2}]
    assert windows._parse_destination('monitor ' + text, NS(), monitors) is monitors[1]
    assert windows._is_destination_text('monitor ' + text)
    assert windows._parse_send_remainder('chrome to monitor ' + text) == ('chrome', 'monitor ' + text)
    assert tab_finder._browser_tab_number('twelve') is None
    assert switcher._parse_monitor_index('monitor 12') == 12


@pytest.mark.parametrize('text', ['2', 'two'])
@pytest.mark.parametrize('handler,store_method', [('handle_complete_task', 'complete_task'), ('handle_remove_task', 'remove_task')])
def test_task_handlers(monkeypatch, text, handler, store_method):
    from plugins.commands import tasks
    items = [{'id': 'a', 'text': 'A'}, {'id': 'b', 'text': 'B'}]
    monkeypatch.setattr(tasks.tasks_store, 'get_active', lambda: items)
    monkeypatch.setattr(tasks.tasks_store, 'get_all', lambda: items)
    called = []
    monkeypatch.setattr(tasks.tasks_store, store_method, lambda task_id: called.append(task_id))
    monkeypatch.setattr(tasks, '_refresh_overlay', lambda: None)
    monkeypatch.setattr(tasks, '_speak', lambda *args: None)
    assert getattr(tasks, handler)(NS(), text) is True
    assert called == ['b']


@pytest.mark.parametrize('text', ['2', 'two'])
def test_duration_parsers(text):
    from plugins.commands import timer, screen_gif
    assert timer._parse_duration(text + ' minutes') == (120, None)
    assert timer._parse_duration(text + ' minutes thirty seconds') == (150, None)
    assert timer._parse_duration(text) == (120, None)
    assert timer._parse_duration('1.5 minutes') == (90, None)
    assert screen_gif._parse_duration('for ' + text + ' seconds') == 2
    assert screen_gif._parse_duration('for ' + text + ' minutes') == 120
    assert screen_gif._parse_duration('for ' + text) == 2


@pytest.mark.parametrize('text', ['2', 'two'])
def test_reminder_time_and_cancel(monkeypatch, text):
    from plugins.commands import reminders
    assert reminders._parse_time(text + ' pm') == '14:00'
    assert reminders._parse_time('2:00 pm') == '14:00'
    called = []
    manager = NS(get_all_reminders=lambda: [{'id': 'a'}, {'id': 'b'}], remove_reminder=called.append)
    monkeypatch.setattr(reminders, '_get_manager', lambda app: manager)
    monkeypatch.setattr(reminders, '_speak', lambda *args: None)
    assert reminders.handle_cancel_reminder(NS(), text) is True
    assert called == ['b']
    manager.add_reminder = lambda **kw: called.append(kw)
    assert reminders.handle_remind_me_to(NS(), 'fixture at ' + text + ' pm') is True
    assert called[-1]['schedule'] == {'type': 'times', 'times': ['14:00']}


@pytest.mark.parametrize('text', ['2', 'two'])
def test_pain_and_medication_parsers(text):
    from plugins.commands import health_tracker as health
    assert health._parse_pain_level(text + ' knees') == (2, 'knees')
    assert health._parse_pain_level('level ' + text + ' knees') == (2, 'knees')
    assert health._parse_medication('fixture ' + text + ' tablets') == ('fixture', '2 tablets', None)
    assert health._parse_pain_level('eleven knees') == (None, 'eleven knees')


@pytest.mark.parametrize('text', ['2', 'two'])
def test_word_and_volume(monkeypatch, text):
    from plugins.commands import word, volume
    values = NS(Font=NS(Size=None), ParagraphFormat=NS(LineSpacingRule=None))
    monkeypatch.setattr(word, '_selection_call', lambda app, call: (True, call(values)))
    monkeypatch.setattr(word, '_report', lambda *args: None)
    assert word.line_spacing(NS(), text) is True
    assert values.ParagraphFormat.LineSpacingRule == 2
    assert word.font_size(NS(), text) is False  # Preserve 6--72 font-size range.
    assert word.font_size(NS(), 'twelve' if text == 'two' else '12') is True
    assert values.Font.Size == 12
    levels = []
    monkeypatch.setattr(volume._audio, 'set_volume', lambda level: levels.append(level) or True)
    assert volume.handle_set_volume(NS(), text) is True
    assert levels == [.02]


@pytest.mark.parametrize('text', ['2', 'two', 'second', '2nd'])
def test_shared_grammar_interact_policy_and_grid(text):
    from samsara import grammar, interact, execution_policy
    from samsara.intent import normalize
    from samsara.ui import mouse_grid
    assert grammar._number(text.split()) == 2
    assert grammar._monitor(['monitor', text]) == 'monitor ' + text
    assert normalize.parse_number(text.split()) == 2
    assert interact.parse_action('move to monitor ' + text) == ('move', {'monitor': 2}, False)
    assert execution_policy._spoken_number(text) == 2
    assert mouse_grid.word_to_digit(text) == 2
    for bad in ('someone', 'two windows', '100'):
        assert grammar._number(bad.split()) is None
    assert normalize.parse_ordinal('2nd') == 2
    assert normalize.parse_ordinal('2') is None
    assert normalize.parse_ordinal('seventh') is None


def test_existing_homophones_stay_scoped():
    from samsara import execution_policy
    from samsara.intent import normalize
    from samsara.ui import mouse_grid
    from plugins.commands import window_cube
    assert execution_policy._spoken_number('for') == 4
    assert normalize.parse_number(['too']) == 2
    assert mouse_grid.word_to_digit('tree') == 3
    assert window_cube._numbers_in('two into four') == [2, 4]
    assert window_cube._numbers_in('to for') == []


def test_settings_help_and_dictation_formatting_unchanged():
    from pathlib import Path
    from samsara.number_format import format_spoken_numbers
    root = Path(__file__).resolve().parents[1]
    for relative in ('samsara/ui/settings/dictation_page.py', 'samsara/ui/settings/general_qt.py'):
        assert 'Writes numbers you dictate as digits (twenty -> 20). Commands understand both either way.' in (root / relative).read_text(encoding='utf-8')
    assert format_spoken_numbers('twenty') == '20'
    assert format_spoken_numbers('one thing') == 'one thing'


@pytest.mark.parametrize('text', ['2', 'two'])
def test_notification_parser(text):
    from samsara.notifications import NotificationManager
    manager = NotificationManager.__new__(NotificationManager)
    for phrase in ('remind me in ' + text + ' minutes to fixture',
                   'set a reminder for ' + text + ' minutes to fixture',
                   text + ' minute reminder to fixture'):
        assert manager.parse_remind_command(phrase) == (2, 'fixture')


def test_label_number_prefix_is_preserved():
    from plugins.commands import show_numbers as overlay
    assert overlay._parse_spoken_number('number two') == 2
    assert overlay._parse_click_target('click number two') == (2, 'single', frozenset())
