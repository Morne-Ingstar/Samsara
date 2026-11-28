"""Exercise the Daily receipt order using a simulated clock and the installed probe."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from samsara.tts import coordinator
from tools.build_receipts import common, daily, desktop, installed_probe, replay_input


@pytest.fixture
def probe(monkeypatch):
    # Resolve the aliases used by the installed scratch profile without launching it.
    monkeypatch.setitem(sys.modules, 'receipt_probe', installed_probe)
    monkeypatch.setitem(sys.modules, 'receipt_replay', replay_input)
    monkeypatch.setitem(sys.modules, 'receipt_desktop', desktop)
    path = Path(daily.__file__).with_name('daily_probe.py')
    spec = importlib.util.spec_from_file_location('receipt_daily_probe_1404', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'on_ui', lambda fn: fn())
    return module


@pytest.mark.parametrize('speech_end_ms, tts, hold_start_ms', [
    (300, {}, 700),
    (300, {'self_hear_grace_ms': 0}, 300),
    (9800, {}, None),
    (None, {}, None),
], ids=['default-grace', 'zero-grace', 'grace-outlasts-deadline', 'speech-never-clears'])
def test_t10_speech_window_gates_the_1252_hold(
        monkeypatch, tmp_path, probe, speech_end_ms, tts, hold_start_ms):
    identity = dict(commit='a' * 40, build_ts='2026-11-27T06:00:00-08:00')
    harness = daily.DailyHarness(tmp_path, tmp_path, tmp_path / 'report', identity, None, None, None,
                                 root=tmp_path)
    monkeypatch.setattr(harness, 'launch', lambda: None)
    for name in ('pace', 'game', 'timing', 'mic', 'reread'):
        monkeypatch.setattr(harness, name, lambda: dict(verified=True))

    app = SimpleNamespace(config={'tts': dict(volume=0.0, **tts)}, recording=False,
                          _receipt_replay=SimpleNamespace(silence=lambda: None))
    audio = app.audio_coordinator = coordinator.AudioCoordinator(app, engine=None)
    monkeypatch.setattr(audio, '_on_transition', lambda *args: None)
    now_ns = 0

    def sleep(seconds):
        nonlocal now_ns
        now_ns += round(seconds * 1e9)
        if speech_end_ms is not None and now_ns >= speech_end_ms * 1_000_000 and audio.is_speaking:
            audio.transition_to(coordinator.IDLE)

    clock = SimpleNamespace(perf_counter_ns=lambda: now_ns, monotonic=lambda: now_ns / 1e9, sleep=sleep)
    for module in (common, coordinator, daily, probe):
        monkeypatch.setattr(module, 'time', clock)

    operations = []
    holds = []

    def hold(pressed):
        app.recording = pressed
        if any(row['ticket'] == '1252' for row in harness.rows):
            return  # The later 1254 hold has its own receipt.
        holds.append((pressed, now_ns))
        if not pressed:
            app._mic_silence_notice_1252 = SimpleNamespace(
                isVisible=lambda: True, windowTitle=lambda: 'No sound from your microphone')

    app._on_main_hotkey_mouse = hold

    def call(operation, *, timeout=None):
        operations.append(operation)
        if operation == 'ticket_lookup':
            return dict(lookup_success=True, has_id=True, has_checkpoint=True)
        if operation == 'ticket':
            audio.transition_to(coordinator.SPEAKING)
            return dict(dispatch_state='queued', ticket_answer=True, ticket_success=True)
        if operation in {'help', 'desktop_state'}:
            return dict(verified=True)
        return probe.perform(app, harness.home, operation)

    harness.channel = SimpleNamespace(call=call)
    harness.run()

    rows = {row['ticket']: row for row in harness.rows}
    assert rows['T10']['result'] == rows['1254']['result'] == 'PASS'
    assert operations.index('ticket') < operations.index('wait_for_speech_clear') < operations.index('mouse_down')
    row = json.loads((harness.artifacts / '1252.json').read_text(encoding='utf-8'))
    if hold_start_ms is None:
        assert holds == []
        assert 'silence_state' not in operations
        assert row['result'] == 'NOT COVERED'
        assert 'grace period' in row['limitation'] and '10 seconds' in row['limitation']
        assert row['observations'] == {}
        assert now_ns == 10_300_000_000  # Ten seconds plus the later 1254 hold.
    else:
        start_ns = hold_start_ms * 1_000_000
        assert holds == [(True, start_ns), (False, start_ns + 1_000_000_000)]
        assert row['result'] == 'PASS'
        assert row['observations'] == dict(capture_started=True, capture_stopped=True, notice_visible=True,
                                           notice_title_correct=True, build_newer_than_landing=True)
