"""544: mechanical evidence, whole finals and wake cleanup, no app bootstrap."""
import ast
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import config_defaults, speech_presence as presence
from samsara.constants import HEAD_GRACE_CLICK_PAD_MS
from samsara.log import safe_exception
from tests.test_silent_hold_387 import make_worker, _segment


@pytest.fixture
def detector(monkeypatch):
    detector = presence.SpeechPresence()
    detector._model = Mock(side_effect=lambda audio: np.zeros(len(audio)//512, np.float32))
    # Override conftest's decoder-fixture unknown stub with our own detector.
    monkeypatch.setattr(presence, '_presence', detector)
    return detector


def use_scores(detector, pairs):
    def score(audio):
        result = np.zeros(len(audio)//512, np.float32)
        for frame, probability in pairs:
            result[frame] = probability
        return result
    detector._model.side_effect = score


def app(earcon_ms=120):
    return SimpleNamespace(model_rate=16000, config={}, _last_recording_earcon_ms=earcon_ms,
                           _speech_presence_capture=presence.HoldEvidence(
                               0.0, earcon_ms / 1000 if earcon_ms else None, 0.0))


@pytest.mark.parametrize('peak', [.1001, .16, .18, .3, .5])
def test_one_weak_frame_after_grace_is_not_speech(detector, peak):
    use_scores(detector, [(20, peak)])
    state = SimpleNamespace()
    assert presence.skip_decode(np.zeros(16000), app(), final=False, path='streaming', state=state)
    assert not hasattr(state, '_speech_presence_seen')
    assert presence.skip_decode(np.zeros(64000), app(), path='streaming', state=state)


@pytest.mark.parametrize('pairs', [[(20,.11),(21,.11)], [(20,.5001)]])
def test_minimal_quiet_or_confident_evidence_passes(detector, pairs):
    use_scores(detector, pairs)
    assert not presence.skip_decode(np.zeros(16000), app(), path='streaming')


@pytest.mark.parametrize('path', ['streaming', 'batch'])
def test_press_head_cannot_establish_cached_speech(detector, path, caplog):
    use_scores(detector, [(0,.18),(1,.18),(2,.18),(3,.18),(4,.18)])
    state = SimpleNamespace()
    assert presence.skip_decode(np.zeros(16000), app(), final=False, path=path, state=state)
    assert not hasattr(state, '_speech_presence_seen')
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert presence.skip_decode(np.zeros(80000), app(), path=path, state=state)
    line = caplog.records[-1].getMessage()
    assert 'frames=157' in line and 'head_grace_ms=180.000' in line
    assert 'peak=0.180000' in line and 'grace_peak=0.000000' in line
    assert 'cached=0 cached_at_s=-1.000' in line


@pytest.mark.parametrize('path', ['handsfree', 'handsfree_preview', 'continuous', 'wake',
                                  'ava_session', 'commit_redecode'])
def test_utterance_paths_do_not_inherit_stale_press_grace(detector, path, caplog):
    use_scores(detector, [(0,.51)])
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(np.zeros(16000), app(), path=path)
    assert 'head_grace_ms=0.000' in caplog.records[-1].getMessage()


def test_shared_pad_and_earcon_off(detector, caplog):
    tree = ast.parse(Path('dictation.py').read_text(encoding='utf-8'))
    assign = next(n for n in tree.body if isinstance(n, ast.Assign) and
                  any(isinstance(t, ast.Name) and t.id=='_GATE_HEAD_GRACE_CLICK_PAD_MS' for t in n.targets))
    assert isinstance(assign.value, ast.Name) and assign.value.id == 'HEAD_GRACE_CLICK_PAD_MS'
    assert presence.HEAD_GRACE_CLICK_PAD_MS == HEAD_GRACE_CLICK_PAD_MS
    use_scores(detector, [(0,.18)])
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert presence.skip_decode(np.zeros(16000), app(0), path='streaming')
    assert 'head_grace_ms=60.000' in caplog.records[-1].getMessage()


def test_statistics_clip_grace_and_unpadded_tail(detector):
    use_scores(detector, [(0,.2),(1,.4),(2,.6)])
    result = detector.measure(np.ones(1100), 16000)
    whole, peak = result.statistics()
    grace, grace_peak = result.statistics(40)
    assert peak == pytest.approx(.6) and grace_peak == pytest.approx(.6)
    np.testing.assert_allclose(whole, ((1100/16000,1100/16000),(588/16000,588/16000),(76/16000,76/16000)))
    np.testing.assert_allclose(grace, ((460/16000,460/16000),(460/16000,460/16000),(76/16000,76/16000)))


def test_disconnected_weak_frames_and_padded_tail_do_not_make_a_run(detector):
    use_scores(detector, [(0,.2),(2,.2),(4,.2)])
    assert presence.skip_decode(np.zeros(5*512), 16000)
    use_scores(detector, [(0,.2),(1,.2)])
    assert presence.skip_decode(np.zeros(700), 16000)  # only 43.75 ms of real PCM


def test_resampling_scores_copy_without_modifying_pcm(detector):
    detector._model.side_effect = lambda audio: np.full(len(audio)//512,.2,np.float32)
    audio = np.ones(4800, np.float32)
    result = detector.measure(audio,48000)
    assert result.samples == 1600 and result.frames == 4
    assert result.total_s == .1
    assert np.all(audio == 1)


def test_final_remeasures_tail_metrics_and_ignores_early_verdict(detector, caplog):
    state = SimpleNamespace()
    use_scores(detector, [(20,.2),(21,.2)])
    assert not presence.skip_decode(np.zeros(16000),app(),final=False,path='streaming',state=state)
    detector._model.reset_mock()
    assert not presence.skip_decode(np.zeros(24000),app(),final=False,path='streaming',state=state)
    detector._model.assert_not_called()
    use_scores(detector, [(100,.8),(101,.8),(102,.8)])
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(np.zeros(64000),app(),path='streaming',state=state)
    detector._model.assert_called_once()
    line = caplog.records[-1].getMessage()
    assert 'duration_s=4.000 frames=125' in line and 'peak=0.800000' in line
    for prefix in ('','grace_'):
        for threshold in ('01','03','05'):
            assert f'{prefix}total_p{threshold}_s=0.096' in line
            assert f'{prefix}run_p{threshold}_s=0.096' in line
    assert 'cached=1 cached_at_s=1.000' in line
    use_scores(detector, [])
    assert presence.skip_decode(np.zeros(64000),app(),path='streaming',state=state)


def test_cache_from_old_gate_or_changed_grace_is_not_reused(detector):
    state = SimpleNamespace(_speech_presence_seen=presence.Presence(True,.032,.032,.18,25))
    assert presence.skip_decode(np.zeros(16000),app(),final=False,path='streaming',state=state)
    use_scores(detector,[(3,.2),(4,.2)])
    assert not presence.skip_decode(np.zeros(16000),app(0),final=False,path='streaming',state=state)
    assert presence.skip_decode(np.zeros(16000),app(120),final=False,path='streaming',state=state)


def test_shorter_snapshot_cannot_reuse_early_evidence(detector):
    state = SimpleNamespace()
    use_scores(detector,[(20,.2),(21,.2)])
    assert not presence.skip_decode(np.zeros(16000),app(),final=False,path='streaming',state=state)
    use_scores(detector,[])
    assert presence.skip_decode(np.zeros(12000),app(),final=False,path='streaming',state=state)


def test_cached_final_failure_decodes_and_reports_unknown(detector, caplog):
    state = SimpleNamespace()
    use_scores(detector,[(20,.6)])
    assert not presence.skip_decode(np.zeros(16000),app(),final=False,path='streaming',state=state)
    detector._model.side_effect = RuntimeError('unavailable')
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(np.zeros(32000),app(),path='streaming',state=state)
    line = caplog.records[-1].getMessage()
    assert 'verdict=unknown cached=1' in line and 'grace_peak=-1.000000' in line


@pytest.mark.parametrize('verdict', [False, True])
@pytest.mark.parametrize('batch_path', ['short', 'chunked', 'loud'])
def test_batch_final_measured_while_existing_scans_keep_their_decision(detector, verdict, batch_path, caplog):
    class Decision:
        path = batch_path
        head_grace_ms = 180
        def __bool__(self):
            return verdict
    use_scores(detector, [(0, .99)])
    with caplog.at_level(logging.INFO, logger='Samsara'):
        # Even a loud bypass preserves strong speech within grace.
        assert not presence.skip_decode(np.zeros(16000), app(), path='batch', existing_gate=Decision())
    line = caplog.records[-1].getMessage()
    if batch_path != 'loud' or verdict:
        detector._model.assert_not_called()
        assert 'verdict=skipped reason=batch_decided' in line and 'peak=' not in line
    else:
        detector._model.assert_called_once()
        assert 'verdict=pass' in line and 'frames=32' in line


def test_legacy_batch_pass_with_no_post_grace_evidence_stays_pass(detector, caplog):
    class Pass:
        path = 'short'
        def __bool__(self):
            return False
    use_scores(detector, [])
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(np.zeros(16000), app(), path='batch', existing_gate=Pass())
    detector._model.assert_not_called()
    assert 'verdict=skipped reason=batch_decided' in caplog.records[-1].getMessage()


def test_disabled_final_does_no_inference_and_reports_skip(detector, caplog):
    config = {'dictation': {'speech_presence_gate': False}}
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(np.zeros(16000), 16000, config)
    detector._model.assert_not_called()
    line = caplog.records[-1].getMessage()
    assert 'verdict=skipped reason=switch_off' in line and 'peak=' not in line


def test_click_partial_then_empty_streaming_final_never_decodes(make_worker,detector,caplog):
    worker,session = make_worker([_segment()])
    session.app._speech_presence_capture = app()._speech_presence_capture
    worker._speech_presence_capture = session.app._speech_presence_capture
    use_scores(detector,[(0,.18)])
    assert worker._transcribe_partial() is None
    with caplog.at_level(logging.INFO,logger='Samsara'):
        worker._final_pass()
    session.app.model.transcribe.assert_not_called()
    assert session.on_final.call_args.args == (None,)
    assert not session.app.model_lock.locked()
    assert len([r for r in caplog.records if 'speech_presence path=' in r.getMessage()]) == 1


@pytest.mark.parametrize('audio', [None, np.zeros(0,np.float32)])
def test_zero_length_streaming_final_has_one_measured_line(make_worker,detector,caplog,audio):
    worker,session = make_worker([])
    session._stop_capture.return_value = audio
    with caplog.at_level(logging.INFO,logger='Samsara'):
        worker._final_pass()
    session.app.model.transcribe.assert_not_called()
    lines = [r.getMessage() for r in caplog.records if 'speech_presence path=' in r.getMessage()]
    assert len(lines)==1 and 'duration_s=0.000 verdict=skipped reason=not_decoded' in lines[0]


@pytest.mark.parametrize('rms_reject', [False,True])
def test_wake_gate_rejects_without_chip_or_history_and_releases_owner(detector,caplog,rms_reject):
    tree = ast.parse(Path('dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='DictationApp')
    method = next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='_decode_wake_word_buffer')
    ns = dict(np=np,time=time,safe_exception=safe_exception,
              logger=logging.getLogger('544.wake'),resample_audio=lambda a,*args:a,
              config_defaults=config_defaults)
    exec(compile(ast.Module(body=[method],type_ignores=[]),'<544-wake>','exec'),ns)
    fake = app()
    fake.app_state = 'wake_session'
    fake._transcription_owners = SimpleNamespace(claim=Mock(return_value='owned'),release=Mock())
    fake._wake_audio_is_below_gate = Mock(return_value=rms_reject)
    fake._vad_available = True
    fake.get_transcription_params = Mock(return_value={})
    fake._show_outcome_chip = Mock()
    fake._log_history = Mock()
    fake._vad_reset = Mock()
    fake.model = SimpleNamespace(transcribe=Mock())
    timing = SimpleNamespace(mark=Mock())
    with caplog.at_level(logging.INFO,logger='Samsara'):
        ns['_decode_wake_word_buffer'](fake,[np.zeros(16000)],16000,stage_timing=timing)
    fake.model.transcribe.assert_not_called()
    fake._show_outcome_chip.assert_not_called()
    fake._log_history.assert_not_called()
    fake._transcription_owners.release.assert_called_once_with('wake','owned')
    fake._vad_reset.assert_called_once()
    if rms_reject:
        timing.mark.assert_any_call('capture_finalised',outcome='gated')
    else:
        timing.mark.assert_any_call('transcribe_end',outcome='empty')
    line = caplog.records[-1].getMessage()
    if rms_reject:
        detector._model.assert_not_called()
        assert 'verdict=skipped reason=rms_dropped' in line and 'peak=' not in line
    else:
        detector._model.assert_called_once()
        assert 'verdict=reject' in line


def test_invalid_press_grace_fails_open_with_one_unknown_line(detector,caplog):
    with caplog.at_level(logging.INFO,logger='Samsara'):
        assert not presence.skip_decode(np.zeros(16000),app(float('nan')),path='streaming')
    assert len(caplog.records)==1 and 'verdict=unknown' in caplog.records[0].getMessage()


@pytest.mark.parametrize('grace_ms', [60, 180, 430, 1680])
def test_quiet_run_crossing_grace_counts_its_full_onset(detector, grace_ms):
    boundary = int(grace_ms * 16 / 512)
    use_scores(detector, [(boundary - 1, .11), (boundary, .11)])
    result = detector.measure(np.zeros(32000), 16000)
    assert result.statistics(grace_ms)[0][0][1] < presence.MIN_RUN_S
    assert result.passes(grace_ms)


def test_strong_word_inside_grace_counts_but_weak_run_ending_inside_does_not(detector):
    use_scores(detector, [(0, .51)])
    assert detector.measure(np.zeros(16000), 16000).passes(430)
    use_scores(detector, [(0, .18), (1, .18), (2, .18)])
    assert not detector.measure(np.zeros(16000), 16000).passes(430)


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('policy', ['eligible', 'strict', 'none', 'unconfirmed'])
def test_actual_retained_preroll_anchors_press_and_earcon(monkeypatch, streaming, policy, detector):
    from tests.audio_engine.test_dictation_consumer import (
        _onset_app, _onset_write, _onset_activate, _onset_pump, _onset_audio,
        _FakeEngine, FrameBus, DictationSessionConsumer)
    fake = _onset_app(eligible=policy == 'eligible', request=10.0, confirmation=10.1)
    if policy in ('none', 'unconfirmed'):
        fake._hold_capture_duck_confirmed_at = None
        fake._hold_capture_onset = None
    context = presence.HoldEvidence(10.0, 10.25)
    fake._speech_presence_capture = context
    bus = FrameBus()
    consumer = DictationSessionConsumer(_FakeEngine(bus), fake)
    # Activation at 10.4; actual 1.5 s rewind begins at 8.9, not at press.
    _onset_write(bus, [8.9 + i * .1 for i in range(1, 16)])
    _onset_activate(consumer, streaming, monkeypatch)
    _onset_pump(consumer, streaming, monkeypatch)
    audio = _onset_audio(consumer, streaming)
    assert audio is not None
    start = context.first_kept_block_start
    expected = {'eligible': 9.8, 'strict': 10.1, 'none': 8.9, 'unconfirmed': 8.9}[policy]
    assert start == pytest.approx(expected)
    grace, press, earcon_end = context.offsets()
    assert press == pytest.approx((10.0 - start) * 1000)
    assert grace == pytest.approx((10.25 - start) * 1000 + HEAD_GRACE_CLICK_PAD_MS)
    assert earcon_end == pytest.approx((10.25 - start) * 1000)
    # A weak earcon after duck confirmation cannot bind a positive partial.
    frame = int((10.13 - start) * 16000 / 512)
    use_scores(detector, [(frame, .18), (frame + 1, .18), (frame + 2, .18)])
    fake.model_rate = 16000
    fake.config = {}
    assert presence.skip_decode(audio, fake, path='streaming')


@pytest.mark.parametrize('path,seconds', [('batch', .50), ('continuous', .50),
                                          ('handsfree', .29), ('ava_session', .29)])
def test_short_captures_skip_without_inference(detector, caplog, path, seconds):
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(np.zeros(int(seconds * 16000)), app(), path=path)
    detector._model.assert_not_called()
    line = caplog.records[-1].getMessage()
    assert 'verdict=skipped reason=not_decoded' in line and 'peak=' not in line


def test_start_playback_timing_includes_duck_delay_and_resets_per_hold(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(presence, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(presence, 'sleep', Mock())
    fake = app()
    fake._sound_cache = {'start': np.zeros(1920)}
    fake._sound_stream_sr = 16000
    fake._hotkey_recording = True
    def queue_start(*args, **kwargs):
        clock[0] = 10.13
        presence.note_hold_earcon(fake, 'start', fake._sound_cache['start'])
    fake.play_sound = Mock(side_effect=queue_start)
    presence.begin_hold(fake)
    context = presence.capture_context(fake)
    context.observe(9.75)
    presence.play_hold_start(fake, True)
    assert context.offsets() == pytest.approx((560, 250, 500))
    fake.play_sound.assert_called_once_with('start', use_winsound=True)
    presence.sleep.assert_called_once_with(.15)
    old = context
    presence.begin_hold(fake)
    presence.play_hold_start(fake, False)
    assert presence.capture_context(fake) is not old
    assert presence.capture_context(fake).earcon_end_at is None
    assert fake._last_recording_earcon_ms == 0


def test_worker_keeps_own_context_when_a_new_hold_is_prepared(make_worker, detector):
    from samsara.streaming import StreamingWorker
    old, session = make_worker([_segment()])
    context = presence.HoldEvidence(10.0, 10.12, 9.75)
    session.app._speech_presence_capture = context
    worker = StreamingWorker(session)
    session.app._speech_presence_capture = presence.HoldEvidence(20.0, 20.12, 19.75)
    assert worker._speech_presence_capture is context
    use_scores(detector, [(8, .18), (9, .18)])
    worker._final_pass()
    session.app.model.transcribe.assert_not_called()


def test_missing_actual_capture_timestamp_fails_open(detector, caplog):
    fake = app()
    fake._speech_presence_capture.first_kept_block_start = None
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(np.zeros(16000), fake, path='streaming')
    detector._model.assert_not_called()
    assert 'verdict=unknown' in caplog.records[-1].getMessage()


def test_delayed_hold_start_sound_updates_bound_context_without_changing_batch_gate(monkeypatch):
    import threading
    tree = ast.parse(Path('dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'play_sound')
    timing = SimpleNamespace(feedback_started=Mock(), feedback_finished=Mock())
    ns = dict(speech_presence=presence, np=np, current_stage_timing=lambda: timing)
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<544-play>', 'exec'), ns)
    fake = app(0)
    fake._hotkey_recording = True
    fake._sound_stream_sr = 16000
    fake._sound_cache = {'start': np.zeros((1920, 1))}
    fake._resolve_playback_output = lambda: 7
    fake._sound_stream_device = 7
    fake._buffer_lock = threading.Lock()
    monkeypatch.setattr(presence, 'perf_counter', lambda: .2)
    ns['play_sound'](fake, 'start')
    assert fake._speech_presence_capture.earcon_end_at == pytest.approx(.32)
    assert fake._last_recording_earcon_ms == 0  # legacy decision stays intact
    assert fake._speech_presence_capture.offsets()[0] == pytest.approx(380)
    fake._hotkey_recording = False
    fake._speech_presence_capture.earcon_end_at = None
    ns['play_sound'](fake, 'start')
    assert fake._speech_presence_capture.earcon_end_at is None
