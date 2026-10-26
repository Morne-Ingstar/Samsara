"""929: real number conversion; synthetic clock/thread/notification boundaries."""
from types import SimpleNamespace as NS

import pytest

from plugins.commands import timer


@pytest.fixture(autouse=True)
def isolated_boundaries(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('unexpected timer thread or subprocess')

    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(timer.thread_registry, 'spawn', forbidden)
    monkeypatch.setattr(timer, 'time', NS(sleep=forbidden, time=lambda: 123))
    monkeypatch.setattr(timer, '_active_timers', [])


@pytest.mark.parametrize('text,seconds', [
    ('1 and a half hours', 5400), ('one and a half hours', 5400),
    ('1 and a half minutes', 90), ('one and a half minutes', 90),
    ('2 and half hours', 9000), ('half an hour', 1800),
    ('half a minute', 30), ('a half hour', 1800), ('half hour', 1800),
    ('half a second', .5), ('1 and a half seconds', 1.5),
    ('1 second', 1), ('one second', 1),
    ('2 minutes 30 seconds', 150), ('two minutes thirty seconds', 150),
    ('1 hour and 2 minutes', 3720), ('1h, 2m and 3s', 3723),
    ('1 hour 0 minutes', 3600), ('0 hours 2 minutes', 120),
    ('1.5 minutes', 90), ('.5 minutes', 30), ('0.25 seconds', .25),
    ('for twenty one minutes', 1260), ('one hundred and two seconds', 102),
    ('5', 300), ('five', 300), ('for 5', 300), ('5 tea', 300),
    ('5 minutes tea', 300), ('5 minutes tea 2 minutes later', 300),
    ('5 min', 300), ('10 sec', 10), ('1 hr', 3600),
    ('168 hours', 604800), ('604800 seconds', 604800),
    ('5 minutes "tea"; $(Write-Output "fixture")', 300),
])
def test_exact_seconds_with_real_converter(text, seconds):
    assert timer._parse_duration(text) == (seconds, None)


INVALID = [None, '', ' ', 'tea', 'tea 5 minutes', '-5 minutes', '+5 minutes',
           'minus five minutes', 'negative five', '0', 'zero minutes',
           '0 seconds', 'NaN minutes', 'inf hours', 'Infinity', '1e3 seconds',
           '1..5 minutes', '1.5', '5 minutes -2 seconds',
           '5 minutes +2 seconds', '5 minutes 2', '5 minutes and',
           '5 minutes and a half', '5 minutes NaN seconds', '5 minutes hours',
           '5 minutes 2 parsecs', '604801 seconds', '169 hours',
           '9' * 400 + ' hours', '5 minutes ' + 'x' * 503,
           ' '.join(['one'] * 65)]


@pytest.mark.parametrize('text', INVALID)
def test_rejection_never_schedules(text):
    seconds, error = timer._parse_duration(text)
    assert seconds is None and error
    # No app is needed for a rejected request: every effect must follow parse.
    assert timer.handle_timer(NS(), text) is True
    assert timer._active_timers == []


def test_input_bound_precedes_converter(monkeypatch):
    import samsara.spoken_numbers as numbers
    monkeypatch.setattr(numbers, 'number_words_to_digits',
                        lambda _: pytest.fail('oversized input reached converter'))
    assert timer._parse_duration('x' * 513)[0] is None
    assert timer._parse_duration(' '.join(['one'] * 65))[0] is None


@pytest.mark.parametrize('seconds,display', [(.5, '0.5 seconds'), (1.5, '1.5 seconds'),
                                          (90, '1m 30s'), (5400, '1h 30m')])
def test_fractional_formatting(seconds, display):
    assert timer._format_duration(seconds) == display


@pytest.mark.parametrize('display', ['Compact', 'Full'])
def test_handler_spawns_once_and_start_ui_is_scheduled(monkeypatch, display):
    spawned, queued, modes = [], [], []
    thread = object()
    monkeypatch.setattr(timer.thread_registry, 'spawn',
                        lambda *args, **kwargs: spawned.append((args, kwargs)) or thread)
    indicator = NS(set_mode=lambda text: modes.append(text))
    app = NS(config={'ui': {'live_surface': {'enabled': display == 'Full'}}},
             listening_indicator=indicator,
             _schedule_ui=lambda *args: queued.append(args))
    label = 'for One and a half hours Tea "two" $fixture'
    assert timer.handle_timer(app, label) is True
    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert args == ('timer-5400s', timer._timer_thread)
    assert kwargs == {'args': (5400, label, app), 'daemon': True}
    assert timer._active_timers == [dict(thread=thread, seconds=5400,
                                        label=label, started=123)]
    assert modes == []
    assert len(queued) == 1
    fn, *args = queued.pop()
    fn(*args)
    assert modes == ['Timer: 1h 30m']


@pytest.mark.parametrize('label', [
    '5 minutes "Tea"; $(Write-Output "fixture"); $env:FIXTURE',
    '5 minutes <b>Tea & coffee</b>',
    '5 minutes <img src="https://invalid.example/fixture">',
    '5 minutes `fixture` \'single quotes\'',
])
def test_completion_posts_once_via_real_notification_chain(monkeypatch, label):
    from samsara import notifications
    from samsara.ui import reminder_toast
    from html import escape

    posts, rows, sleeps, sounds = [], [], [], []
    toast = reminder_toast.ReminderToast()
    toast._window = NS(add_row=lambda *args: rows.append(args))
    monkeypatch.setattr(reminder_toast.qt_runtime, 'post', lambda fn: posts.append(fn))
    monkeypatch.setattr(reminder_toast, '_is_show_numbers_active', lambda: False)
    monkeypatch.setattr(notifications, 'get_toast', lambda: toast)
    manager = notifications.NotificationManager.__new__(notifications.NotificationManager)
    manager.on_notification = None
    monkeypatch.setattr(timer, 'time', NS(sleep=lambda n: sleeps.append(n)))
    app = NS(notification_manager=manager, play_sound=lambda sound: sounds.append(sound))
    timer._timer_thread(300, label, app)
    assert sleeps == [300, .5]
    assert sounds == ['start', 'start']
    assert len(posts) == 1 and rows == []
    posts.pop()()
    assert rows == [('Samsara Timer',
                     '<span>' + escape(f'Timer done ({label}): 5 minutes') + '</span>',
                     None, None)]


def test_sound_failure_does_not_skip_or_duplicate_completion(monkeypatch):
    calls = []
    monkeypatch.setattr(timer, 'time', NS(sleep=lambda _: None))
    def broken_sound(_):
        raise RuntimeError('synthetic sound unavailable')
    app = NS(play_sound=broken_sound,
             notification_manager=NS(show_notification=lambda *args: calls.append(args)))
    timer._timer_thread(1, '', app)
    assert calls == [('Samsara Timer', '<span>Timer done: 1 second</span>')]
