"""Daily reply locks and ticket recall, with disposable files and fake peers."""
import errno
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tools.build_receipts import common, daily, installed_probe, runner


IDENTITY = dict(build_ts='2026-11-25T06:13:57-08:00', commit='a' * 40)


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    clock.monotonic = lambda: clock.now
    clock.sleep = sleep
    monkeypatch.setattr(common, 'time', clock)
    return clock


@pytest.fixture
def peer(tmp_path, monkeypatch):
    mailbox = tmp_path / 'mailbox'
    mailbox.mkdir()
    channel = runner.Channel(mailbox, SimpleNamespace(poll=lambda: None))
    handled, dispatched = set(), []

    def dispatch(operation):
        dispatched.append(operation)
        return {'observed': True}

    def wait(probe, **kwargs):
        def tick():
            installed_probe.service_requests(mailbox, handled, dispatch)
            return probe()

        return common.wait_for(tick, **kwargs)

    monkeypatch.setattr(runner, 'wait_for', wait)
    return channel, dispatched


def lock_replies(monkeypatch, clock, *, until, winerror, operation):
    original = Path.read_text
    attempts = []

    def read(path, *args, **kwargs):
        if path.name.startswith('response-') and path.is_file():
            request = path.with_name(path.name.replace('response-', 'request-', 1))
            if json.loads(original(request, encoding='utf-8'))['operation'] == operation:
                attempts.append(clock.now)
                if clock.now < until:
                    error = PermissionError(errno.EACCES, 'private error detail', 'private path')
                    if winerror is not None:
                        error.winerror = winerror
                    raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    return attempts


@pytest.mark.parametrize('winerror', [None, 32, 33])
def test_reply_lock_retries_same_transaction(monkeypatch, clock, peer, winerror):
    channel, dispatched = peer
    attempts = lock_replies(monkeypatch, clock, until=.2, winerror=winerror, operation='state')

    assert channel.call('state', timeout=1) == {'observed': True}
    assert attempts == pytest.approx([0, .1, .2])
    assert dispatched == ['state']


def test_permanent_reply_lock_fails_one_check_and_finishes_daily(monkeypatch, tmp_path, clock, peer):
    channel, dispatched = peer
    attempts = lock_replies(monkeypatch, clock, until=float('inf'), winerror=32, operation='silence_state')
    harness = daily.DailyHarness(tmp_path, tmp_path, tmp_path / 'report', IDENTITY, None, None, None,
                                 root=tmp_path)
    harness.channel = channel
    monkeypatch.setattr(harness, 'launch', lambda: None)
    for name in ('pace', 'ticket', 'game', 'timing', 'mic', 'mouse', 'reread'):
        monkeypatch.setattr(harness, name, lambda: {'observed': True})
    monkeypatch.setattr(harness, 'silent', lambda: channel.call('silence_state', timeout=.25))

    harness.run()

    assert attempts == pytest.approx([0, .1, .2])
    assert dispatched == ['silence_state', 'help', 'desktop_state']
    assert len(harness.rows) == 13
    assert [row['ticket'] for row in harness.rows if row['result'] == 'FAIL'] == ['1252']
    row = json.loads((harness.artifacts / '1252.json').read_text(encoding='utf-8'))
    assert row['failure_code'] == 'PermissionError'
    diagnostic, = row['failure_diagnostics']
    assert diagnostic == dict(
        phase='transaction', operation='silence_state', action='read_reply',
        path=diagnostic['path'],
        code='PermissionError', winerror=32, errno=errno.EACCES,
    )
    folder, filename = diagnostic['path'].split('/')
    assert folder == 'receipt_channel' and filename.startswith('response-')
    request = channel.directory / filename.replace('response-', 'request-', 1)
    assert json.loads(request.read_text(encoding='utf-8'))['operation'] == 'silence_state'
    status = daily.write_summary(harness.report, IDENTITY, harness.rows, elapsed=clock.now, lifecycle_ok=True)
    summary = (harness.report / 'RECEIPTS.md').read_text(encoding='utf-8')
    assert status == 1
    assert 'The reply file stayed locked until the read deadline.' in summary
    assert 'Run failed:' not in summary
    assert 'private' not in json.dumps(row)


def test_explicit_access_denial_is_an_immediate_nonsharing_failure(monkeypatch, clock, peer):
    channel, dispatched = peer
    attempts = lock_replies(monkeypatch, clock, until=float('inf'), winerror=5, operation='state')

    with pytest.raises(runner.ReceiptFailure) as caught:
        channel.call('state', timeout=1)

    assert not isinstance(caught.value, runner.ReplyReadLocked)
    assert caught.value.diagnostic['winerror'] == 5
    assert attempts == [0] and dispatched == ['state']


@pytest.mark.parametrize('lookup,dispatch,observed,success', [
    (True, 'queued', True, True),
    (False, 'queued', False, False),
    (True, 'queued', True, False),
    (True, 'rejected', False, False),
    (False, 'queued', True, True),
])
def test_ticket_receipt_records_reader_dispatch_and_reply_independently(
        monkeypatch, tmp_path, clock, lookup, dispatch, observed, success):
    harness = daily.DailyHarness(tmp_path, tmp_path, tmp_path / 'report', IDENTITY, None, None, None,
                                 root=tmp_path)
    calls = []

    def call(operation):
        calls.append(operation)
        if operation == 'ticket_lookup':
            return dict(lookup_success=lookup, has_id=lookup, has_checkpoint=lookup)
        if operation == 'ticket':
            return dict(dispatch_state=dispatch, dispatched=False, ticket_answer=False, ticket_success=False)
        assert operation == 'state'
        return dict(ticket_answer=observed, ticket_success=success)

    harness.channel = SimpleNamespace(call=call)
    monkeypatch.setattr(runner, 'wait_for', lambda probe: common.wait_for(probe, timeout=.2))

    harness.check('T10', 'installed ticket command', harness.ticket, 'Reply queue')

    row = harness.rows[0]
    admitted = dispatch == 'queued'
    assert row['observations'] == dict(lookup_success=lookup, has_id=lookup, has_checkpoint=lookup,
                                      command_admitted=admitted, ticket_reply_observed=observed,
                                      command_reply_queued=success, build_newer_than_landing=True)
    assert row['result'] == ('PASS' if lookup and success else 'FAIL')
    assert calls[:2] == ['ticket_lookup', 'ticket']
    assert calls[2:] == ['state'] * (1 if observed else 2 if admitted else 0)


def test_silence_receipt_checks_notice_after_the_hold_is_released(monkeypatch, tmp_path, clock):
    harness = daily.DailyHarness(tmp_path, tmp_path, tmp_path / 'report', IDENTITY, None, None, None,
                                 root=tmp_path)
    calls = []

    def call(operation):
        calls.append(operation)
        if operation in ('mouse_down', 'mouse_state'):
            return dict(recording='mouse_up' not in calls)
        if operation == 'mouse_up':
            assert clock.now == 1.0
            return dict(release_dispatched=True)
        assert operation == 'silence_state' and 'mouse_up' in calls
        return dict(notice_visible=True, notice_title_correct=True)

    harness.channel = SimpleNamespace(call=call)
    monkeypatch.setattr(daily, 'time', clock)

    assert harness.silent() == dict(capture_started=True, capture_stopped=True, notice_visible=True,
                                    notice_title_correct=True)
    assert calls == ['mouse_down', 'mouse_state', 'mouse_up', 'mouse_state', 'silence_state']


def test_daily_seeds_speech_queue_without_audible_playback(monkeypatch, tmp_path):
    from samsara.config_schema import migrate_removed_settings

    root = tmp_path / 'scratch'
    app = root / 'app'
    app.mkdir(parents=True)
    (app / 'Samsara.exe').touch()
    model = root / 'models--Systran--faster-whisper-base' / 'snapshots' / 'fixture'
    model.mkdir(parents=True)
    (model / 'model.bin').touch()
    take = root / 'take.wav'
    take.write_bytes(b'fixture')
    profiles = []

    class FakeHarness(daily.DailyHarness):
        def run(self):
            profiles.append(json.loads((self.home / 'config.json').read_text(encoding='utf-8')))

    monkeypatch.setattr(daily, 'DailyHarness', FakeHarness)
    monkeypatch.setattr(common, 'build_identity', lambda _: IDENTITY)
    monkeypatch.setattr(daily.replay_input, 'choose_take', lambda _: take)
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(
        EnumDisplayMonitors=lambda: [(1,)], GetMonitorInfo=lambda _: dict(Device=daily.desktop.DEVICE,
                                                                        Work=(0, 0, 1000, 1000))))
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace())

    daily.main(['--scratch-root', str(root), '--corpus', str(root), '--model', str(model),
                '--report-root', str(tmp_path / 'reports')])

    assert len(profiles) == 1
    migrate_removed_settings(profiles[0])
    assert profiles[0]['tts'] == dict(enabled=True, feedback_level='brief', engine='winrt', volume=0.0)
