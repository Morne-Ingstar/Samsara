"""1070: render-matched playback prefix; preserve uncertain onset speech.

Public-corpus audio only. No devices, app bootstrap, profile or Git access.
The native bench exercises actual decoder/history delivery at both levels.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
from samsara.audio_engine.frame import FRAME_SIZE
from samsara.audio_engine.ring import FrameBus
from samsara.hold_capture_onset import HoldOnset
from samsara.speech_presence import HoldEvidence


spec = importlib.util.spec_from_file_location(
    'bench_1070', Path(__file__).parents[1] / 'tools' / 'hold_e2e_bench.py')
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_fixture_pairs_have_identical_pcm_but_different_speaker_provenance(tmp_path):
    cases = bench.build_preduck_fixtures(tmp_path)
    assert len(cases) == 6
    for variant in ('peak0995', 'peak_minus12'):
        stream, quiet, overlap = [next(c for c in cases if c['id'] == case+'_'+variant)
                                  for case in ('1070', '1070_firstword', '1070_overlap')]
        assert stream['sha256'] == quiet['sha256'] == overlap['sha256']
        assert stream['media_ducked'] and overlap['media_ducked']
        assert not quiet['media_ducked']
        assert not stream['user_onset'] and quiet['user_onset'] and overlap['user_onset']
        assert stream['press_s'] < stream['duck_effective_s']
        assert 'gospel' not in bench.words(stream['reference'])
        assert bench.words(quiet['reference'])[0] == 'gospel'
        assert bench.rms(bench.read_audio(stream['render_wav'])[:8000]) > 0
        assert bench.rms(bench.read_audio(quiet['render_wav'])) == 0
        assert stream['playback_only_blocks'] == 5
        assert overlap['playback_only_blocks'] == 0


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('eligible', [False, True])
@pytest.mark.parametrize('setting', [None, False, True])
def test_early_user_word_survives_even_when_another_app_was_ducked(monkeypatch, streaming, eligible, setting):
    # A lowered volume lease cannot identify the speaker. In particular,
    # eligible=False must not authorize deleting this genuine onset word.
    from samsara.audio_engine import dictation_consumer
    monkeypatch.setattr(dictation_consumer.thread_registry, 'spawn', lambda *a, **kw: None)
    bus = FrameBus()
    app = NS(config={'mode': 'hold'}, _hold_capture_duck_token=-1,
             _hold_capture_duck_confirmed_at=10.5,
             _speech_presence_capture=HoldEvidence(10.4),
             _hold_capture_onset=HoldOnset(-1, 10.4, 10.5, eligible, lambda *a, **kw: None))
    if setting is not None:
        app.config['dictation'] = {'drop_playback_preroll': setting}
    consumer = DictationSessionConsumer(NS(register_consumer=bus.new_reader, _overflow_count=0), app)
    onset = bench.peak(bench.source('speech_0')[round(4.8*bench.RATE):round(5.3*bench.RATE)], .6)
    pcm = np.round(onset * 32767).astype(np.int16)
    for index in range(5):
        bus.write(pcm[index*FRAME_SIZE:(index+1)*FRAME_SIZE], 10.+(index+1)*.1+1e-6, 0)
    assert (consumer.activate_streaming() if streaming else consumer.activate())
    # The background reader is intentionally starved; release must flush it.
    consumer._capture_not_after = 10.500002
    captured = consumer.stop_streaming() if streaming else consumer.drain()
    np.testing.assert_array_equal(captured, pcm.astype(np.float32)/32767.)
    assert not consumer.capture_loss
    assert app._speech_presence_capture.first_kept_block_start < 10.4


def test_preduck_pcm_contract_does_not_weaken_existing_bench(tmp_path):
    cases = bench.build_fixtures(tmp_path)
    assert len(cases) == 8
    assert {c['case'] for c in cases} == {'1026', '1043', '1042', '1051'}


@pytest.mark.slow
def test_native_preduck_bench_uses_real_delivery(tmp_path, monkeypatch):
    import os
    model_path = os.environ.get('SAMSARA_HOLD_BENCH_MODEL')
    if not model_path:
        pytest.skip('Set SAMSARA_HOLD_BENCH_MODEL to a local faster-whisper model directory')
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    from samsara import speech_presence
    assert getattr(speech_presence._presence.measure, '__self__', None) is speech_presence._presence
    model = bench.load_model(Path(model_path))
    results = [(case, bench.run_case(case, model, tmp_path/'replay'/case['id']))
               for case in bench.build_preduck_fixtures(tmp_path/'wav')]
    for case, result in results:
        assert result['native_calls'] and any(c['segments'] for c in result['native_calls'])
        assert result['history'] and result['passed'], (case['id'], result['checks'])
        if case['user_onset']:
            assert result['checks']['user_first_word_retained']
        else:
            assert result['checks']['no_stream_word']
        assert result['checks']['selected_input_pcm_preserved']


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('setting', [None, False, True])
def test_render_prefix_removed_but_first_unexplained_onset_and_later_matches_kept(monkeypatch, streaming, setting):
    from samsara.audio_engine import dictation_consumer
    monkeypatch.setattr(dictation_consumer.thread_registry, 'spawn', lambda *a, **kw: None)
    bus = FrameBus()
    media = bench.peak(bench.source('speech_0')[76800:84800], .6)
    user = bench.peak(bench.source('speech_3')[1600:3200], .08)
    pcm = np.round(media*32767).astype(np.int16)
    # Short, quiet speech mixed into a strongly correlated stream block.
    pcm[1800:2120] += np.round(user[:320]*32767).astype(np.int16)
    app = NS(config={'mode': 'hold'}, _speech_presence_capture=HoldEvidence(10.4),
             _hold_capture_duck_confirmed_at=10.5)
    if setting is not None:
        app.config['dictation'] = {'drop_playback_preroll': setting}
    engine = NS(register_consumer=bus.new_reader, _overflow_count=0,
                _hold_render_reference=NS(snapshot=lambda: (10.500001, media)))
    consumer = DictationSessionConsumer(engine, app)
    for i in range(5):
        bus.write(pcm[i*FRAME_SIZE:(i+1)*FRAME_SIZE], 10.+(i+1)*.1+1e-6, 0)
    assert (consumer.activate_streaming() if streaming else consumer.activate())
    consumer._capture_not_after = 10.500002
    captured = consumer.stop_streaming() if streaming else consumer.drain()
    expected = pcm.astype(np.float32)/32767.
    if setting is True:
        expected[:FRAME_SIZE] = 0
    np.testing.assert_array_equal(captured, expected)
    # Suppression must not overwrite the shared microphone ring.
    np.testing.assert_array_equal(bus._pcm[:5].ravel(), pcm)
    if setting is True:
        assert consumer._playback_prefix.removed == 1
        assert consumer._playback_prefix.preserve
    else:
        assert consumer._playback_prefix is None
    assert not consumer.capture_loss


@pytest.mark.parametrize('lag', [0, .04, .15])
@pytest.mark.parametrize('gain', [.2, .8, 1.5])
def test_matching_tolerates_acoustic_lag_gain_and_room_noise(lag, gain):
    from samsara.audio_engine.preduck import playback_match
    ref = bench.peak(bench.source('speech_0')[76800:84800], .5)
    block = ref[3200:4800].astype(np.float64)*gain
    block += np.random.default_rng(1070).normal(0, .00002, len(block))
    matched, score, residual = playback_match(block, 10.3+lag, (10.5, ref))
    assert matched and score >= .995 and residual < .0005


@pytest.mark.parametrize('condition', ['missing', 'stale', 'silent', 'tone', 'unrelated', 'quiet_user', 'nan'])
def test_uncertain_or_mixed_audio_is_never_excluded(condition):
    from samsara.audio_engine.preduck import playback_match
    ref = bench.peak(bench.source('speech_0')[76800:84800], .6)
    mic = ref[1600:3200].astype(np.float64)
    snapshot = (10.5, ref.copy())
    if condition == 'missing':
        snapshot = None
    elif condition == 'stale':
        snapshot = (8., ref)
    elif condition == 'silent':
        snapshot = (10.5, np.zeros_like(ref))
    elif condition == 'tone':
        ref = np.sin(np.arange(8000)*2*np.pi*440/16000)*.5
        mic, snapshot = ref[1600:3200], (10.5, ref)
    elif condition == 'unrelated':
        mic = bench.source('speech_3')[1600:3200].astype(np.float64)
    elif condition == 'quiet_user':
        mic[200:520] += bench.peak(bench.source('speech_3')[1600:1920], .01)
    elif condition == 'nan':
        snapshot[1][:] = np.nan
    assert not playback_match(mic, 10.2, snapshot)[0]


def test_live_audio_and_device_changes_are_preserved():
    from samsara.audio_engine.preduck import PlaybackPrefix
    from samsara.audio_engine.frame import Frame
    ref = bench.peak(bench.source('speech_0')[76800:84800], .6)
    pcm = np.round(ref[:1600]*32767).astype(np.int16)
    prefix = PlaybackPrefix((10.5, ref), 10.5, 0)
    assert not prefix.excludes(Frame(0, 10.6, pcm, 0))
    assert not prefix.excludes(Frame(1, 10.1, pcm, 1))
    assert not prefix.excludes(Frame(2, 10.1, pcm, 0))


def test_loopback_failure_logs_once_and_keeps_current_behavior(monkeypatch, caplog):
    from samsara.audio_engine import preduck
    from samsara.echo_cancel import LoopbackCapture
    engine = NS(_config={'dictation': {'drop_playback_preroll': True}})
    monkeypatch.setattr(LoopbackCapture, 'start', lambda self: False)
    monkeypatch.setattr(LoopbackCapture, 'stop', lambda self: None)
    with caplog.at_level('INFO', logger=preduck.__name__):
        preduck.start_reference(engine)
        preduck.start_reference(engine)
        preduck.log_unavailable(engine)
    assert engine._hold_render_reference is None
    assert caplog.text.count('Render loopback unavailable; preserving hold pre-roll') == 1


@pytest.mark.parametrize('setting', [None, False, True])
def test_engine_owns_one_render_reference_and_closes_it(monkeypatch, setting):
    from samsara.audio_engine import AudioCaptureEngine, preduck
    events = []
    config = {} if setting is None else {'dictation': {'drop_playback_preroll': setting}}
    engine = AudioCaptureEngine(FrameBus(), config=config)
    monkeypatch.setattr(engine, '_open_stream', lambda device: None)
    monkeypatch.setattr(preduck, 'start_reference', lambda owner: events.append(('start', owner)))
    monkeypatch.setattr(preduck, 'stop_reference', lambda owner: events.append(('stop', owner)))
    engine.start()
    engine.start()
    engine.stop()
    assert events == ([('start', engine)] if setting is True else []) + [('stop', engine)]


def test_render_callback_buffers_stereo_resamples_and_invalidates_gaps(monkeypatch):
    from samsara.audio_engine import preduck
    from samsara import echo_cancel
    tick = [10.]
    monkeypatch.setattr(preduck.time, 'perf_counter', lambda: tick[0])
    monkeypatch.setattr(echo_cancel, 'pyaudio', NS(paContinue=0))
    def start(capture):
        capture._device_channels, capture._device_rate = 2, 48000
        # The real transport invokes the first callback before _running=True.
        stereo = np.tile(np.array([.2, .4], np.float32), (2400, 1))
        assert capture._stream_callback(stereo.tobytes(), 2400, {}, 0) == (None, 0)
        capture._running = True
        return True
    monkeypatch.setattr(echo_cancel.LoopbackCapture, 'start', start)
    monkeypatch.setattr(echo_cancel.LoopbackCapture, 'stop', lambda c: setattr(c, '_running', False))
    engine = NS(_config={'dictation': {'drop_playback_preroll': True}})
    preduck.start_reference(engine)
    reference = engine._hold_render_reference
    end, pcm = reference.snapshot()
    assert end == 10. and len(pcm) == 800
    np.testing.assert_allclose(pcm, .3)
    reference._stream_callback(b'', 0, {}, 1)
    assert reference.snapshot() is None  # overrun is never usable evidence
    tick[0] = 10.05
    reference._stream_callback(np.zeros(4800, np.float32).tobytes(), 2400, {}, 0)
    tick[0] = 11.
    reference._stream_callback(np.ones(4800, np.float32).tobytes(), 2400, {}, 0)
    end, pcm = reference.snapshot()
    assert end == 11. and len(pcm) == 800  # gap cleared the stale prefix
    np.testing.assert_array_equal(pcm, np.ones(800))
    preduck.stop_reference(engine)
    assert engine._hold_render_reference is None
    assert reference.snapshot() is None


def test_prepress_matching_never_suppresses_press_to_duck_audio(monkeypatch):
    from samsara.audio_engine import dictation_consumer
    monkeypatch.setattr(dictation_consumer.thread_registry, 'spawn', lambda *a, **kw: None)
    bus = FrameBus()
    media = bench.peak(bench.source('speech_0')[76800:84800], .6)
    pcm = np.round(media*32767).astype(np.int16)
    app = NS(config={'mode': 'hold', 'dictation': {'drop_playback_preroll': True}},
             _speech_presence_capture=HoldEvidence(10.4),
             _hold_capture_duck_confirmed_at=10.5)
    engine = NS(register_consumer=bus.new_reader, _overflow_count=0,
                _hold_render_reference=NS(snapshot=lambda: (10.500001, media)))
    consumer = DictationSessionConsumer(engine, app)
    for i in range(5):
        bus.write(pcm[i*FRAME_SIZE:(i+1)*FRAME_SIZE], 10.+(i+1)*.1+1e-6, 0)
    consumer.activate_streaming()
    consumer._capture_not_after = 10.500002
    captured = consumer.stop_streaming()
    expected = pcm.astype(np.float32)/32767.
    expected[:4*FRAME_SIZE] = 0
    np.testing.assert_array_equal(captured, expected)
    assert consumer._playback_prefix.removed == 4
    assert consumer._playback_prefix.snapshot is None


@pytest.mark.parametrize('setting', [None, False])
def test_off_never_opens_loopback_or_logs_unavailable(monkeypatch, caplog, setting):
    from samsara.audio_engine import preduck
    from samsara.echo_cancel import LoopbackCapture
    config = {} if setting is None else {'dictation': {'drop_playback_preroll': setting}}
    engine = NS(_config=config)
    def unexpected(*args, **kwargs):
        pytest.fail('Disabled feature must not touch the render transport')
    monkeypatch.setattr(LoopbackCapture, '__init__', unexpected)
    with caplog.at_level('INFO', logger=preduck.__name__):
        preduck.start_reference(engine)
    assert not hasattr(engine, '_hold_render_reference')
    assert not caplog.records


@pytest.mark.parametrize('streaming', [False, True])
def test_enabled_missing_loopback_preserves_pcm_and_logs_once(monkeypatch, caplog, streaming):
    from samsara.audio_engine import dictation_consumer, preduck
    monkeypatch.setattr(dictation_consumer.thread_registry, 'spawn', lambda *a, **kw: None)
    bus = FrameBus()
    app = NS(config={'mode': 'hold', 'dictation': {'drop_playback_preroll': True}},
             _speech_presence_capture=HoldEvidence(10.4))
    engine = NS(register_consumer=bus.new_reader, _overflow_count=0)
    consumer = DictationSessionConsumer(engine, app)
    pcm = np.round(bench.source('speech_0')[76800:78400]*32767).astype(np.int16)
    bus.write(pcm, 10.3, 0)
    with caplog.at_level('INFO', logger=preduck.__name__):
        assert (consumer.activate_streaming() if streaming else consumer.activate())
        preduck.begin(consumer)  # Repeated holds/failure log only once per engine.
    consumer._capture_not_after = 10.4
    captured = consumer.stop_streaming() if streaming else consumer.drain()
    np.testing.assert_array_equal(captured, pcm.astype(np.float32)/32767.)
    assert consumer._playback_prefix is None
    assert caplog.text.count('Render loopback unavailable; preserving hold pre-roll') == 1


def test_config_defaults_off_and_has_no_settings_row():
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.config_defaults import cfg_get
    key = 'dictation.drop_playback_preroll'
    assert SETTINGS_SCHEMA[key]['type'] == 'bool'
    assert SETTINGS_SCHEMA[key]['default'] is False
    assert SETTINGS_SCHEMA[key]['settings_ui'] is False
    assert cfg_get({}, key) is False
    assert cfg_get({'dictation': {'drop_playback_preroll': True}}, key) is True


@pytest.fixture
def _speech_presence_unknown_for_decoder_fixtures():
    # Exercise the real detector instead of conftest's synthetic-PCM stub.
    yield
