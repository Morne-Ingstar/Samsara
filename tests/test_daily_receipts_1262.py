"""1262: offline receipt plumbing; no app, processes, hardware or live files."""
import importlib.util
import json
import logging
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from tools.build_receipts import common, daily, desktop, installed_probe, replay_input
from tools.build_receipts.runner import ReceiptFailure


@pytest.fixture
def identity():
    return dict(commit='a' * 40, build_ts='2026-11-18T12:00:00-08:00', timezone_source='build timestamp')


def wav(path, *, frames=16000, channels=1, width=2, rate=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as out:
        out.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        out.writeframes((np.ones(frames * channels, dtype='<i2') * 1000).tobytes())
    return path


def test_daily_profile_is_fresh_and_previous_run_is_preserved(tmp_path):
    app, home = tmp_path / 'app', tmp_path / 'runs' / 'run-1' / 'home'
    app.mkdir()
    (app / 'Samsara.exe').touch()
    assert daily.validate_daily(app, home, root=tmp_path) == (app, home)
    home.mkdir(parents=True)
    marker = home / 'keep.json'
    marker.write_text('preserve')
    with pytest.raises(ValueError, match='Preserve'):
        daily.validate_daily(app, home, root=tmp_path)
    assert marker.read_text() == 'preserve'
    fresh = tmp_path / 'runs' / 'run-2' / 'home'
    assert daily.validate_daily(app, fresh, root=tmp_path)[1] == fresh


@pytest.mark.parametrize('home', ['home', 'runs/home', 'runs/a/nested/home', 'runs/a/profile'])
def test_daily_profile_cannot_fall_back_to_owner_or_legacy_home(tmp_path, home):
    with pytest.raises(ValueError):
        daily.validate_daily(tmp_path / 'app', tmp_path / home, root=tmp_path)


@pytest.mark.parametrize('monitors', [[], [dict(Device=r'\\.\DISPLAY1', Work=(0, 0, 1, 1))]])
def test_monitor3_missing_is_refused(monitors):
    with pytest.raises(ValueError, match='Monitor3'):
        desktop.monitor_rect(monitors)


def test_exact_device_name_and_negative_monitor_coordinates():
    rect = (-1920, -200, 0, 880)
    assert desktop.monitor_rect([dict(Device=r'\\.\DISPLAY30', Work=(0, 0, 1, 1)),
                                 dict(Device=r'\\.\display3', Work=rect)]) == rect


def test_startup_has_no_activation_and_places_at_monitor_origin(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, 'STARTUPINFO', SimpleNamespace, raising=False)
    monkeypatch.setattr(subprocess, 'STARTF_USESHOWWINDOW', 1, raising=False)
    monkeypatch.setattr(subprocess, 'CREATE_NO_WINDOW', 0x8000000, raising=False)
    options = desktop.startup_options((-1920, 20, 0, 1000))
    info = options['startupinfo']
    assert (info.dwX, info.dwY, info.wShowWindow, info.dwFlags) == (-1920, 20, 0, 5)
    assert options['creationflags'] == 0x8000000


@pytest.mark.parametrize('changes', [dict(channels=2), dict(rate=44100), dict(frames=400), dict(frames=128000)])
def test_replay_requires_whole_bounded_corpus_format(tmp_path, changes):
    with pytest.raises(ValueError):
        replay_input.read_take(wav(tmp_path / 'a.wav', **changes))


def test_choose_take_reads_wav_only_and_skips_corrupt_and_silent_takes(tmp_path):
    takes = tmp_path / 'takes'
    takes.mkdir()
    (takes / '0.wav').write_bytes(b'corrupt')
    wav(takes / '1.wav')
    with wave.open(str(takes / '1.wav'), 'wb') as out:
        out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        out.writeframes(bytes(32000))
    (takes / '2.json').write_text('must never be read')
    expected = wav(takes / '2.wav')
    assert replay_input.choose_take(tmp_path) == expected
    assert replay_input.read_take(expected).shape == (16000,)


def test_truncated_pcm_payload_is_not_a_complete_recorded_take(tmp_path):
    path = wav(tmp_path / 'short.wav')
    path.write_bytes(path.read_bytes()[:-8000])
    with pytest.raises(ValueError, match='Incomplete'):
        replay_input.read_take(path)


def test_replay_delay_resampling_and_silence_never_repeats_take(monkeypatch):
    now = [0.]
    monkeypatch.setattr(replay_input.time, 'monotonic', lambda: now[0])
    replay = replay_input.Replay(np.array([.1, .2, .3, .4], dtype='float32'))
    replay.queue(delay=1)
    assert not replay.block(2, 16000, 1).any()
    now[0] = 1
    assert np.allclose(replay.block(4, 32000, 2)[:, 0], [.1, .15, .2, .25])
    assert np.allclose(replay.block(4, 32000, 2)[:, 1], [.3, .35, .4, .4])
    assert not replay.block(4, 32000, 1).any()
    replay.silence()
    assert not replay.block(2, 16000, 1).any()


def test_input_adapter_delivers_callback_and_blocks_raw_device_fallback(monkeypatch):
    class Event:
        def __init__(self): self.stopped = False
        def clear(self): self.stopped = False
        def is_set(self): return self.stopped
        def set(self): self.stopped = True
        def wait(self, _): self.set()
    class Thread:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): self.target()
        def join(self, _): pass
    monkeypatch.setattr(replay_input.threading, 'Event', Event)
    monkeypatch.setattr(replay_input.threading, 'Thread', Thread)
    forbidden = lambda **_: pytest.fail('physical input opened')
    sd = SimpleNamespace(InputStream=forbidden, RawInputStream=forbidden, CallbackFlags=lambda: False)
    replay = replay_input.Replay(np.ones(1600, dtype='float32'))
    replay.queue()
    replay_input.install(sd, replay)
    calls, finished = [], []
    stream = sd.InputStream(samplerate=16000, callback=lambda *args: calls.append(args),
                            blocksize=1600, finished_callback=lambda: finished.append(True))
    with stream:
        pass
    assert calls[0][0].shape == (1600, 1)
    assert calls[0][0].mean() == 1
    assert calls[0][1] == 1600 and calls[0][3] is False
    assert finished == [True] and stream.closed
    with pytest.raises(RuntimeError, match='RawInputNotCovered'):
        sd.RawInputStream()


@pytest.mark.parametrize('observed', [False, None, 'PASS', 1])
def test_new_feature_observations_must_be_boolean_true(identity, tmp_path, observed):
    row = daily.make_row('1252', identity, 'silence', tmp_path / 'proof.json', {'shown': observed})
    assert row['result'] == 'FAIL'
    assert not row['acceptance_eligible']


def test_new_features_reject_build_before_their_landing(identity, tmp_path):
    old = dict(identity, build_ts='2026-11-16T12:00:00-08:00')
    row = daily.make_row('1254', old, 'mouse callback', tmp_path / 'proof.json', {'started': True})
    assert row['result'] == 'FAIL'


def complete_rows(identity, tmp_path):
    return [daily.make_row(f, identity, 'Exact synthetic trigger', tmp_path / (f + '.json'),
                           {'observed': True}, result='NOT COVERED' if f in daily.NOT_COVERED else None,
                           limitation=daily.NOT_COVERED.get(f, 'Scoped proof only')) for f in daily.FEATURES]


@pytest.mark.parametrize('lifecycle_ok,error,expected', [(True, None, 0), (False, None, 1), (True, 'TimeoutError', 1)])
def test_summary_has_every_feature_evidence_and_head(identity, tmp_path, lifecycle_ok, error, expected):
    rows = complete_rows(identity, tmp_path)
    assert daily.write_summary(tmp_path, identity, rows, elapsed=12.3, lifecycle_ok=lifecycle_ok, error=error) == expected
    text = (tmp_path / 'RECEIPTS.md').read_text(encoding='utf-8')
    assert text.count('\n- ') == len(daily.FEATURES) == 13
    assert identity['commit'] in text
    assert all(row['evidence'].replace('\\', '/') in text for row in rows)
    assert all(not r['acceptance_eligible'] for r in rows)
    assert json.loads((tmp_path / 'completion.json').read_text())['exit_code'] == expected


def test_missing_or_failed_feature_never_returns_zero(identity, tmp_path):
    rows = complete_rows(identity, tmp_path)
    assert daily.write_summary(tmp_path, identity, rows[:-1], elapsed=1, lifecycle_ok=True) == 1
    rows[0]['result'] = 'FAIL'
    assert daily.write_summary(tmp_path, identity, rows, elapsed=1, lifecycle_ok=True) == 1


def test_check_redacts_exception_and_keeps_following_feature(identity, tmp_path):
    harness = daily.DailyHarness(tmp_path, tmp_path, tmp_path, identity, tmp_path, tmp_path, (0, 0, 1, 1), root=tmp_path)
    def failed(): raise ValueError('do not retain this exception text')
    harness.check('1252', 'silent input', failed, 'Scoped')
    harness.check('1135', 'help', lambda: {'correct': True}, 'Scoped')
    output = (tmp_path / 'receipts.json').read_text()
    assert 'do not retain' not in output
    assert [r['result'] for r in harness.rows] == ['FAIL', 'PASS']
    assert all(Path(r['evidence']).is_file() for r in harness.rows)


@pytest.fixture
def daily_args(tmp_path):
    # 1276 passes runner paths through the CLI using a prepared synthetic copy.
    root = tmp_path / 'scratch root'
    app = root / 'app'
    app.mkdir(parents=True)
    (app / 'Samsara.exe').touch()
    return ['--scratch-root', str(root), '--corpus', str(tmp_path / 'corpus'), '--report-root', str(tmp_path)]


def test_daily_requires_explicit_scratch_root(tmp_path):
    with pytest.raises(SystemExit) as error:
        daily.main(['--corpus', str(tmp_path), '--report-root', str(tmp_path)])
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_daily_requires_prepared_scratch_root(tmp_path, monkeypatch):
    monkeypatch.setattr(common, 'build_identity', lambda _: pytest.fail('must validate the copy first'))
    root = tmp_path / 'unprepared'
    report_root = tmp_path / 'reports'
    assert daily.main(['--scratch-root', str(root), '--corpus', str(tmp_path),
                       '--report-root', str(report_root)]) == 1
    assert not root.exists()
    # 1351: The summary records one run failure and links the preflight evidence.
    summary = next(report_root.glob('*/RECEIPTS.md')).read_text(encoding='utf-8')
    assert '0 of 9 checks ran' in summary and 'Run failure code: ValueError' in summary


def test_once_daily_cache_does_not_launch_or_overwrite(identity, tmp_path, monkeypatch, daily_args):
    report = tmp_path / daily.datetime.now().astimezone().date().isoformat()
    report.mkdir()
    completion = report / 'completion.json'
    completion.write_text(json.dumps(dict(build=identity, exit_code=0)))
    before = completion.read_bytes()
    monkeypatch.setattr(common, 'build_identity', lambda _: identity)
    monkeypatch.setattr(daily, 'validate_daily', lambda *_: pytest.fail('cached run must not prepare a profile'))
    assert daily.main(daily_args) == 0
    assert completion.read_bytes() == before
    assert not (report / 'RUNNING.lock').exists()
    changed = dict(identity, commit='b' * 40)
    monkeypatch.setattr(common, 'build_identity', lambda _: changed)
    assert daily.main(daily_args) == 1
    assert completion.read_bytes() == before


def test_overlapping_run_does_not_remove_another_runs_lock(tmp_path, daily_args):
    report = tmp_path / daily.datetime.now().astimezone().date().isoformat()
    report.mkdir()
    lock = report / 'RUNNING.lock'
    lock.write_text('another-run')
    assert daily.main(daily_args) == 1
    assert lock.read_text() == 'another-run'


def test_preflight_failure_emits_thirteen_fail_rows_without_launch(tmp_path, monkeypatch, daily_args):
    def failed(_): raise ValueError('private details must not be copied')
    monkeypatch.setattr(common, 'build_identity', failed)
    assert daily.main(daily_args) == 1
    report = tmp_path / daily.datetime.now().astimezone().date().isoformat()
    rows = json.loads((report / 'receipts.json').read_text())
    # 1351: Preserve the existing test ID but require a single incident in place of fabricated rows.
    assert rows == []
    summary = (report / 'RECEIPTS.md').read_text(encoding='utf-8')
    assert summary.count('0 of 9 checks ran') == 1
    assert not any(line.startswith('- FAIL') for line in summary.splitlines())
    evidence = next((report / 'artifacts' / 'claude').glob('preflight-*.json')).read_text(encoding='utf-8')
    assert 'private details' not in evidence + summary
    diagnostic = json.loads(evidence)
    assert diagnostic['cleanup'] == 'not_started'
    assert diagnostic['failures'][0]['operation'] == 'read_build_identity'


def test_shared_deadline_bounds_even_a_truthy_probe(monkeypatch):
    monkeypatch.setattr(common, 'DEADLINE', 1)
    monkeypatch.setattr(common.time, 'monotonic', lambda: 2)
    with pytest.raises(TimeoutError):
        common.wait_for(lambda: True, timeout=1000)


def test_daily_probe_opt_in_cannot_target_normal_process(monkeypatch):
    monkeypatch.setenv('SAMSARA_RECEIPTS_1262', '1')
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(Path.cwd() / 'fake-owner'))
    assert installed_probe.start_services(object()) is None


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setitem(sys.modules, 'receipt_probe', installed_probe)
    monkeypatch.setitem(sys.modules, 'receipt_replay', replay_input)
    monkeypatch.setitem(sys.modules, 'receipt_desktop', desktop)
    path = Path(daily.__file__).with_name('daily_probe.py')
    spec = importlib.util.spec_from_file_location('receipt_daily_probe_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mouse_probe_uses_owned_callback_without_global_input(probe, tmp_path):
    events = []
    app = SimpleNamespace(_receipt_replay=SimpleNamespace(silence=lambda: events.append('silence')),
                          recording=True, _on_main_hotkey_mouse=lambda pressed: events.append(pressed))
    assert probe.perform(app, tmp_path, 'mouse_down') == {'recording': True}
    assert probe.perform(app, tmp_path, 'mouse_up') == {'release_dispatched': True}
    assert events == ['silence', True, False]
    with pytest.raises(ValueError, match='UnknownOperation'):
        probe.perform(app, tmp_path, 'arbitrary command')


def test_help_observer_forwards_and_restores_real_speech_call(probe, tmp_path):
    spoken = []
    coordinator = SimpleNamespace(speak=lambda text: spoken.append(text))
    original = coordinator.speak
    def process(*args, **kwargs):
        coordinator.speak('A phrase that tells Samsara to listen for a command without holding a key.')
        return SimpleNamespace(state=SimpleNamespace(value='completed'))
    app = SimpleNamespace(_receipt_replay=None, audio_coordinator=coordinator,
                          command_executor=SimpleNamespace(process_text=process))
    assert probe.perform(app, tmp_path, 'help') == {'command_completed': True, 'correct_answer_to_speech': True}
    assert coordinator.speak is original and len(spoken) == 1


@pytest.mark.parametrize('crash,quit_error,expected', [(False, False, 0), (True, False, 1), (False, True, 1), (True, True, 1)])
def test_fake_daily_run_preserves_old_artifacts_and_finishes_all_rows(identity, tmp_path, monkeypatch,
                                                                    crash, quit_error, expected):
    root = tmp_path / 'scratch'
    app = root / 'app'
    app.mkdir(parents=True)
    (app / 'Samsara.exe').touch()
    model = tmp_path / 'hub' / 'models--Systran--faster-whisper-base' / 'snapshots' / 'fixture'
    model.mkdir(parents=True)
    (model / 'model.bin').touch()
    take = wav(tmp_path / 'take.wav')
    report_root = tmp_path / 'reports'
    report = report_root / daily.datetime.now().astimezone().date().isoformat()
    old = report / 'artifacts' / 'claude' / 'previous-run.json'
    old.parent.mkdir(parents=True)
    old.write_text('preserve')
    # 1276 exercises the real CLI-to-validator binding.
    monkeypatch.setattr(common, 'build_identity', lambda _: identity)
    monkeypatch.setattr(replay_input, 'choose_take', lambda _: take)
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(
        EnumDisplayMonitors=lambda: [(1,)], GetMonitorInfo=lambda _: dict(Device=desktop.DEVICE, Work=(0, 0, 1000, 1000))))
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace())
    instances = []
    class FakeHarness(daily.DailyHarness):
        def run(self):
            instances.append(self)
            if crash:
                raise ReceiptFailure('startup', 'state', 'publish_request', 'receipt_channel/request.json',
                                     dict(code='PermissionError', winerror=5, errno=13))
            for feature in daily.FEATURES:
                if feature in daily.NOT_COVERED:
                    self.uncovered(feature, daily.NOT_COVERED[feature])
                else:
                    self.check(feature, 'fake observation', lambda: {'observed': True}, 'Fakes only')
        def quit(self):
            if quit_error:
                raise ReceiptFailure('shutdown', 'quit', 'wait_exit', 'logs/session.running',
                                     dict(code='TimeoutExpired'))
            self.shutdown_verified = True
            self.lifecycle.append(dict(event='normal_exit', verified=True))
    monkeypatch.setattr(daily, 'DailyHarness', FakeHarness)
    assert daily.main(['--scratch-root', str(root), '--app', str(app), '--corpus', str(tmp_path),
                       '--model', str(model), '--report-root', str(report_root), '--force']) == expected
    assert instances[0].root == root
    assert instances[0].home.parent.parent == root / 'runs'
    assert old.read_text() == 'preserve'
    rows = json.loads((report / 'receipts.json').read_text())
    # 1351: Give the supervisor observed rows only and report cleanup independently.
    assert len(rows) == (0 if crash else 13)
    if not crash:
        assert {row['ticket'] for row in rows if row['result'] == 'NOT COVERED'} == daily.NOT_COVERED.keys()
    if crash or quit_error:
        summary = (report / 'RECEIPTS.md').read_text(encoding='utf-8')
        assert summary.count('Run failed:') == 1
        failure = json.loads((instances[0].artifacts / 'run-failure.json').read_text())
        assert failure['cleanup'] == ('normal_exit_unverified' if quit_error else 'normal_exit_verified')
        assert [item['phase'] for item in failure['failures']] == (['startup'] if crash else []) + (['shutdown'] if quit_error else [])
        assert set(failure['process']) == {'pid', 'account', 'session_id'}
        assert ('0 of 9 checks ran' if crash else '9 of 9 checks ran') in summary
    config = json.loads((instances[0].home / 'config.json').read_text())
    assert config['ducking']['enabled'] is False
    assert config['command_mode']['enabled'] is False
    assert config['hotkey'] == 'f24'
    assert config['model_size'] == config['game_mode']['cpu_model'] == 'base'
    assert config['game_mode']['detect'] == 'manual'
    assert (instances[0].home / 'personal_plugins' / 'receipt_daily_probe.py').is_file()
    assert not (report / 'RUNNING.lock').exists()


def test_transaction_failure_stops_rows_and_retains_cleanup_error(identity, tmp_path):
    harness = daily.DailyHarness(tmp_path, tmp_path, tmp_path, identity, tmp_path, tmp_path, (0, 0, 1, 1), root=tmp_path)

    def transaction():
        try:
            raise ReceiptFailure('transaction', 'state', 'read_reply', 'receipt_channel/response.json',
                                 dict(code='PermissionError', winerror=32))
        finally:
            raise ReceiptFailure('transaction', 'mic_check_close', 'publish_request', 'receipt_channel/request.json',
                                 dict(code='PermissionError', winerror=5))

    with pytest.raises(ReceiptFailure) as failure:
        harness.check('T80', 'fixture', transaction, 'fixture')
    assert harness.rows == []
    details = daily.run_diagnostics(failure.value, phase='transaction', operation='run_checks', path=None)
    assert [(item['operation'], item['winerror']) for item in details] == [('state', 32), ('mic_check_close', 5)]


def test_daily_mic_receipt_runs_both_actual_checklist_paths(probe, identity, tmp_path, qapp, qt_widget_scope,
                                                       monkeypatch):
    from samsara import mic_check
    from samsara.ui.mic_check_qt import MicCheckWindow

    take = wav(tmp_path / 'take.wav', frames=3 * mic_check.SAMPLE_RATE)
    replay = replay_input.Replay(replay_input.read_take(take))
    app = SimpleNamespace(config={'microphone': 3, 'microphone_name': 'Fixture microphone'},
                          _receipt_replay=replay, recording=False)

    source = SimpleNamespace(pull=lambda: replay.block(1600, mic_check.SAMPLE_RATE, 1)[:, 0], close=lambda: None)
    work = []
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda _name, fn, **_: work.append(fn))
    monkeypatch.setattr(probe, 'on_ui', lambda fn: fn())
    window = MicCheckWindow(app, source_factory=lambda _: source, inspect=lambda _: (.7, False),
                            save=lambda result, name: mic_check.save_result(result, name, tmp_path / 'diagnostics' / 'mic_check.json'))
    app._mic_check_window = window
    harness = daily.DailyHarness(tmp_path, tmp_path, tmp_path / 'report', identity, take, tmp_path, (0, 0, 1, 1),
                                 root=tmp_path)

    def call(operation):
        if operation == 'mic':
            window.show()
            return {'dispatched': True}
        result = probe.perform(app, tmp_path, operation)
        if work:
            work.pop(0)()
        return result

    harness.channel = SimpleNamespace(call=call)
    with qt_widget_scope(window):
        assert all(value is True for value in harness.mic().values())
        assert window.transcriber is None and window.take is None
    evidence = json.loads((harness.artifacts / 'mic-check.json').read_text())
    assert set(evidence) == {'match', 'difference'}
    assert not evidence['match']['transcript_visible'] and evidence['difference']['transcript_visible']
    for result in evidence.values():
        assert result['phases'] == ['quiet', 'noise', 'speak', 'decoding', 'result']
        assert result['measurements']['duration_s'] == 8 and result['phrase_samples'] == 128000
        assert result['checklist_rows'] == list(mic_check.ROWS)
    assert mic_check.SENTENCE not in json.dumps(evidence)
    assert 'Fixture microphone' not in json.dumps(evidence)


def test_daily_manual_game_probe_restores_original_with_auto_detector_disabled(probe, identity, tmp_path, monkeypatch):
    from plugins.commands import game_mode as commands
    from samsara import game_mode

    app = SimpleNamespace(config={'game_mode': {'detect': 'manual', 'cpu_model': 'base'}},
                          model=object(), model_lock=threading.Lock(), device_type='cpu', compute_type='int8',
                          _receipt_replay=None)
    original, resident = app.model, object()
    model_args, calls = [], []

    def create_model(*args, **kwargs):
        model_args.append((args, kwargs))
        return resident

    policy = app._game_mode = game_mode.GameMode(app, create_model,
        probe=lambda: pytest.fail('automatic detection was invoked'), notify=lambda *_: None)
    policy.sample = game_mode.Sample(fullscreen=True, gpu_busy=True)
    assert not policy.wanted and not policy.automatic

    def state(_):
        return dict(game_active=policy.active, game_runtime=policy.runtime_text(),
                    device=app.device_type, model_id=id(app.model))

    def execute(text, target, *, force_commands):
        assert target is app and force_commands
        calls.append(text)
        handler = {'game mode on': commands.game_mode_on, 'game mode off': commands.game_mode_off}[text]
        assert handler(app)
        if policy.wanted:
            assert policy.prepare()
            assert policy.reconcile()
        return SimpleNamespace(state=SimpleNamespace(value='completed'))

    monkeypatch.setattr(probe, 'state', state)
    monkeypatch.setattr(installed_probe, 'state', state)
    app.command_executor = SimpleNamespace(process_text=execute)
    harness = daily.DailyHarness(tmp_path, tmp_path, tmp_path / 'report', identity, tmp_path, tmp_path, (0, 0, 1, 1),
                                 root=tmp_path)
    harness.channel = SimpleNamespace(call=lambda op: probe.perform(app, tmp_path, op))
    logs = tmp_path / 'logs'
    logs.mkdir()
    handler = logging.FileHandler(logs / 'samsara.log', encoding='utf-8')
    logger = logging.Logger(game_mode.log.name, level=logging.INFO)
    logger.addHandler(handler)
    monkeypatch.setattr(game_mode, 'log', logger)
    try:
        observations = harness.game()
        assert {key for key, value in observations.items() if value is not True} == set()
    finally:
        handler.close()
    assert calls == ['game mode on', 'game mode off'] and model_args[0][1]['device'] == 'cpu'
    assert app.model is original and not policy.active and not policy.automatic


def test_qt_guard_prepares_splash_before_show_and_rejects_activation(monkeypatch):
    moved, shown = [], []
    rect = SimpleNamespace(x=lambda: -1920, y=lambda: 0, contains=lambda _: True)
    class Widget:
        def __init__(self): self.flags = 0
        def isWindow(self): return True
        def setAttribute(self, value): pass
        def windowFlags(self): return self.flags
        def setWindowFlag(self, value, enabled): self.flags |= value
        def move(self, x, y): moved.append((x, y))
        def objectName(self): return 'samsaraStartupSplash'
        def show(self): shown.append((self.flags, moved[-1]))
        def activateWindow(self): pytest.fail('activation must be suppressed')
        def raise_(self): pytest.fail('raising must be suppressed')
        def setFocus(self, *_): pytest.fail('focus must be suppressed')
    window = Widget()
    app = SimpleNamespace(installEventFilter=lambda guard: None)
    application = SimpleNamespace(screens=lambda: [SimpleNamespace(name=lambda: desktop.DEVICE,
                                   availableGeometry=lambda: rect)], instance=lambda: app,
                                   topLevelWidgets=lambda: [window])
    qt = SimpleNamespace(WidgetAttribute=SimpleNamespace(WA_ShowWithoutActivating=10),
                         WindowType=SimpleNamespace(WindowDoesNotAcceptFocus=4))
    events = SimpleNamespace(Type=SimpleNamespace(Polish=1, Show=2, Move=3))
    class Object:
        def __init__(self, parent): pass
    # Restore before the repository's automatic Qt cleanup fixture runs.
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, 'PySide6.QtCore', SimpleNamespace(QObject=Object, QEvent=events, Qt=qt))
        patch.setitem(sys.modules, 'PySide6.QtWidgets', SimpleNamespace(QApplication=application, QWidget=Widget))
        guard = desktop.install()
        assert shown == [(4, (-1896, 24))]
        window.activateWindow()
        window.raise_()
        window.setFocus()
        assert guard.eventFilter(window, SimpleNamespace(type=lambda: 2)) is False
