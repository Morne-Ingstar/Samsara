"""Synthetic T10 evidence tests. No installed process or Cockpit connection."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tools.build_receipts import common, daily, runner


IDENTITY = dict(build_ts='2026-11-25T20:00:00-08:00', commit='a' * 40,
                timezone_source='build timestamp')
OBSERVATIONS = dict(lookup_success=True, has_id=True, has_checkpoint=True)
COMMAND = dict(dispatch_state='queued', ticket_answer=True, ticket_success=True)
DIAGNOSTIC = dict(reason='api_unavailable', detail='connection_refused', http_status=None,
                  elapsed_ms=3, body_bytes=None, ticket_count=None)
PRIVATE = 'Synthetic private ticket content'


def fake_channel(*, lookup=True, diagnostic=DIAGNOSTIC):
    calls = []

    def call(operation):
        calls.append(operation)
        if operation == 'ticket_lookup':
            return dict.fromkeys(OBSERVATIONS, lookup)
        if operation == 'ticket':
            return COMMAND
        assert operation == 'ticket_lookup_diagnostic'
        if isinstance(diagnostic, Exception):
            raise diagnostic
        return diagnostic

    return SimpleNamespace(call=call), calls


@pytest.fixture(params=[runner.Harness, daily.DailyHarness])
def make_harness(request, tmp_path):
    def make(identity=IDENTITY):
        args = (tmp_path, tmp_path, tmp_path / 'report', identity)
        if request.param is daily.DailyHarness:
            args += (None, None, None)
        return request.param(*args, root=tmp_path)

    return make


@pytest.mark.parametrize('fresh', [True, False])
def test_no_diagnostic_for_pass_or_old_build_only(make_harness, fresh):
    identity = IDENTITY if fresh else dict(IDENTITY, build_ts='2026-01-01T00:00:00-08:00')
    harness = make_harness(identity)
    harness.channel, calls = fake_channel()

    harness.check('T10', 'ticket recall', harness.ticket, '')

    row, = harness.rows
    assert row['result'] == ('PASS' if fresh else 'FAIL')
    assert row['observations'] == dict(OBSERVATIONS, command_admitted=True, ticket_reply_observed=True,
                                       command_reply_queued=True, build_newer_than_landing=fresh)
    assert calls == ['ticket_lookup', 'ticket']
    assert 'ticket_lookup_diagnostic' not in row
    assert not (harness.artifacts / 'T10_diagnostic.json').exists()


@pytest.mark.parametrize('diagnostic,expected', [
    (dict(DIAGNOSTIC, ticket_text=PRIVATE), DIAGNOSTIC),
    (dict(DIAGNOSTIC, detail=PRIVATE), dict(failure_code='InvalidProbeReply')),
    (dict(DIAGNOSTIC, reason=PRIVATE), dict(failure_code='InvalidProbeReply')),
    (dict(DIAGNOSTIC, body_bytes={'text': PRIVATE}), dict(failure_code='InvalidProbeReply')),
    (dict(DIAGNOSTIC, elapsed_ms=True), dict(failure_code='InvalidProbeReply')),
    (runner.ReceiptFailure('transaction', 'ticket_lookup_diagnostic', 'dispatch', PRIVATE,
                           dict(code='AttributeError')), dict(failure_code='AttributeError')),
])
def test_failure_persists_safe_diagnostic_and_continues(make_harness, diagnostic, expected):
    harness = make_harness()
    harness.channel, calls = fake_channel(lookup=False, diagnostic=diagnostic)

    harness.check('T10', 'ticket recall', harness.ticket, '')
    harness.check('T29', 'pace', lambda: {'observed': True}, '')

    assert calls == ['ticket_lookup', 'ticket', 'ticket_lookup_diagnostic']
    assert [row['result'] for row in harness.rows] == ['FAIL', 'PASS']
    row = harness.rows[0]
    assert row['observations'] == dict.fromkeys(OBSERVATIONS, False) | dict(
        command_admitted=True, ticket_reply_observed=True, command_reply_queued=True,
        build_newer_than_landing=True)
    assert row['ticket_lookup_diagnostic'] == expected
    sidecar = harness.artifacts / 'T10_diagnostic.json'
    assert Path(row['ticket_lookup_diagnostic_evidence']) == sidecar
    assert json.loads(sidecar.read_text(encoding='utf-8')) == expected
    assert json.loads((harness.artifacts / 'T10.json').read_text(encoding='utf-8')) == row
    assert json.loads((harness.report / 'receipts.json').read_text(encoding='utf-8')) == harness.rows
    assert PRIVATE not in json.dumps(row)


def test_daily_final_write_keeps_diagnostic_and_shutdown_fields(monkeypatch, tmp_path):
    root = tmp_path / 'scratch'
    app = root / 'app'
    app.mkdir(parents=True)
    (app / 'Samsara.exe').touch()
    take = tmp_path / 'take.wav'
    take.write_bytes(b'fixture')
    report_root = tmp_path / 'reports'
    diagnostic = dict(reason='schema_error', detail='invalid_json', http_status=200,
                      elapsed_ms=3, body_bytes=18, ticket_count=None)

    class FakeHarness(daily.DailyHarness):
        def run(self):
            self.channel, calls = fake_channel(lookup=False, diagnostic=dict(diagnostic, text=PRIVATE))
            self.check('T10', 'ticket recall', self.ticket, '')
            assert calls == ['ticket_lookup', 'ticket', 'ticket_lookup_diagnostic']
            self.check('T29', 'pace', lambda: {'observed': True}, '')

        def quit(self):
            self.shutdown_verified = True
            self.cleanup_outcome = 'exited'

    monkeypatch.setattr(daily, 'DailyHarness', FakeHarness)
    monkeypatch.setattr(common, 'build_identity', lambda _: IDENTITY)
    monkeypatch.setattr(daily.replay_input, 'choose_take', lambda _: take)
    monkeypatch.setattr(daily, 'local_model_name', lambda _: 'base')
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(
        EnumDisplayMonitors=lambda: [(1,)], GetMonitorInfo=lambda _: dict(Device=daily.desktop.DEVICE,
                                                                        Work=(0, 0, 1000, 1000))))
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace())

    assert daily.main(['--scratch-root', str(root), '--corpus', str(tmp_path), '--model', str(tmp_path),
                       '--report-root', str(report_root)]) == 1

    report, = report_root.iterdir()
    ticket, pace = json.loads((report / 'receipts.json').read_text(encoding='utf-8'))
    assert ticket['result'] == 'FAIL'
    assert pace['result'] == 'PASS'
    assert ticket['normal_shutdown_verified'] is True
    assert ticket['cleanup_outcome'] == 'exited'
    assert ticket['ticket_lookup_diagnostic'] == diagnostic
    assert json.loads(Path(ticket['evidence']).read_text(encoding='utf-8')) == ticket
    summary = (report / 'RECEIPTS.md').read_text(encoding='utf-8')
    for key, value in diagnostic.items():
        assert f'{key}={value}' in summary
    assert 'T10_diagnostic.json)' in summary
    assert 'Run failed:' not in summary
    assert PRIVATE not in summary
