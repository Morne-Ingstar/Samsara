"""Daily checks with a fake clock and an in-process probe peer."""
import json
from types import SimpleNamespace

import pytest

from tools.build_receipts import common, daily, installed_probe, runner


IDENTITY = dict(build_ts='2026-11-25T13:33:26-08:00', commit='a' * 40)


@pytest.fixture
def harness(tmp_path):
    return daily.DailyHarness(tmp_path, tmp_path, tmp_path / 'report', IDENTITY, None, None, None,
                              root=tmp_path)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    clock.monotonic = lambda: clock.now
    clock.sleep = sleep
    monkeypatch.setattr(common, 'time', clock)
    monkeypatch.setattr(daily, 'time', clock)
    return clock


@pytest.mark.parametrize('visible,title_correct,expected', [
    (True, True, 'PASS'),
    (True, False, 'FAIL'),
    (False, True, 'FAIL'),
    (False, False, 'FAIL'),
])
def test_silent_hold_requires_visible_correct_notice_and_keeps_hold_evidence(
        harness, clock, visible, title_correct, expected):
    calls = []

    def call(operation, *, timeout=None):
        calls.append(operation)
        if operation == 'wait_for_speech_clear':  # 1404b adds the pre-hold speech check.
            return dict(speech_clear=True)
        released = 'mouse_up' in calls
        if operation in ('mouse_down', 'mouse_state'):
            return dict(recording=not released, stopping=False)
        if operation == 'mouse_up':
            assert clock.now == 1.0
            return dict(release_dispatched=True)
        assert operation == 'silence_state' and released
        return dict(notice_visible=visible, notice_title_correct=title_correct)

    harness.channel = SimpleNamespace(call=call)
    harness.check('1252', 'Silent hold', harness.silent, 'Visible notice')

    row = json.loads((harness.artifacts / '1252.json').read_text(encoding='utf-8'))
    assert row['result'] == expected
    assert row['observations'] == dict(capture_started=True, capture_stopped=True,
                                       notice_visible=visible, notice_title_correct=title_correct,
                                       build_newer_than_landing=True)
    assert calls[:6] == ['wait_for_speech_clear', 'mouse_down', 'mouse_state', 'mouse_up',
                         'mouse_state', 'silence_state']
    if visible:
        assert clock.now == 1.0
    else:
        assert 26 <= clock.now < 26.1


def test_reread_dispatch_attributeerror_is_one_failed_row_and_help_runs(harness, clock, monkeypatch):
    mailbox = harness.home / 'mailbox'
    mailbox.mkdir()
    harness.channel = runner.Channel(mailbox, SimpleNamespace(poll=lambda: None))
    handled, operations = set(), []

    def dispatch(operation):
        operations.append(operation)
        if operation == 'reread_stage':
            raise AttributeError('private diagnostic message')
        return {'observed': True}

    def wait(probe, **kwargs):
        def tick():
            installed_probe.service_requests(mailbox, handled, dispatch)
            return probe()

        return common.wait_for(tick, **kwargs)

    monkeypatch.setattr(runner, 'wait_for', wait)
    monkeypatch.setattr(harness, 'launch', lambda: None)
    for name in ('pace', 'ticket', 'game', 'timing', 'mic', 'silent', 'mouse'):
        monkeypatch.setattr(harness, name, lambda: {'observed': True})

    harness.run()

    assert operations == ['silence_start', 'reread_stage', 'continuous_stop', 'help', 'desktop_state']
    attempted = [row for row in harness.rows if row['result'] != 'NOT COVERED']
    assert len(attempted) == 9
    assert [(row['ticket'], row['result']) for row in attempted[-2:]] == [('1251', 'FAIL'), ('1135', 'PASS')]
    assert len(harness.rows) == 13
    payload = (harness.artifacts / '1251.json').read_text(encoding='utf-8')
    assert 'private diagnostic message' not in payload
    row = json.loads(payload)
    assert row['failure_code'] == 'AttributeError'
    failure, = row['failure_diagnostics']
    assert failure == dict(phase='transaction', operation='reread_stage', action='dispatch',
                           path=failure['path'], code='AttributeError', winerror=None, errno=None)
    assert failure['path'].startswith('receipt_channel/response-')
    assert daily.write_summary(harness.report, IDENTITY, harness.rows,
                               elapsed=clock.now, lifecycle_ok=True) == 1
    assert 'Run failed:' not in (harness.report / 'RECEIPTS.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('phase,action,code', [
    ('startup', 'wait_reply', 'TimeoutError'),
    ('shutdown', 'verify_exit', 'NormalShutdownUnverified'),
    ('transaction', 'before_publish', 'ProbeExited'),
    ('transaction', 'wait_reply', 'ProbeExited'),
])
def test_daily_check_propagates_lifecycle_failure(harness, phase, action, code):
    def fail():
        raise runner.ReceiptFailure(phase, 'state', action, None, dict(code=code))

    with pytest.raises(runner.ReceiptFailure, match=code):
        harness.check('T29', 'Verify pace', fail, 'Persistence')

    assert harness.rows == []
