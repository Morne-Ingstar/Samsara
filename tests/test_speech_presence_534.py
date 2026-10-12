"""534 routing contracts; real CPU corpus evidence is a separate developer run.

No application import, audio device, model download, or subprocess. Injected
Silero scores keep these contracts runnable in the guarded plain export.
"""
import ast
import logging
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import config_defaults, speech_presence as presence, streaming
from tests.test_silent_hold_387 import make_worker, _segment


ROOT = Path(__file__).resolve().parents[1]
PHANTOMS = [pytest.param('1,2,3,4,5,6,7,8,9 done!', id='counting'),
            pytest.param("thank you for watching, and I'll see you in the next video!", id='signoff')]
NON_SPEECH = ['silence', 'noise_low', 'noise_mid', 'noise_high', 'fan', 'clicks', 'breath']


def pcm(kind, seconds=5):
    rng = np.random.default_rng(534)
    t = np.arange(seconds * 16000) / 16000
    audio = np.zeros(t.size, dtype=np.float32)
    if kind.startswith('noise_'):
        noise = rng.standard_normal(t.size)
        rms = {'noise_low': .001, 'noise_mid': .004, 'noise_high': .008}[kind]
        audio[:] = noise * rms / np.sqrt(np.mean(noise ** 2))
    elif kind == 'fan':
        audio[:] = .004 * np.sin(2*np.pi*60*t) + .002 * np.sin(2*np.pi*120*t)
    elif kind == 'clicks':
        n = 640
        audio[16000:16000+n] = .3 * np.exp(-np.arange(n)/80) * rng.standard_normal(n)
    elif kind == 'breath':
        n = 12800
        audio[16000:16000+n] = .02 * np.sin(np.linspace(0, np.pi, n))**2 * rng.standard_normal(n)
    return audio


@pytest.fixture
def detector(monkeypatch):
    obj = presence.SpeechPresence()
    obj._model = Mock(side_effect=lambda audio: np.full(audio.size // 512, .09, dtype=np.float32))
    monkeypatch.setattr(presence, '_presence', obj)
    return obj


def enable(session, audio):
    session.app.config['dictation'] = {'speech_presence_gate': True}
    session._stop_capture.return_value = audio
    session.app._dictation_consumer.snapshot_streaming_audio.return_value = audio


@pytest.mark.parametrize('kind', NON_SPEECH)
@pytest.mark.parametrize('phantom', PHANTOMS)
@pytest.mark.parametrize('path', ['final', 'partial'])
def test_empty_hold_never_decodes_or_delivers(make_worker, detector, monkeypatch, caplog, kind, phantom, path):
    worker, session = make_worker([_segment(text=phantom)])
    enable(session, pcm(kind))
    with caplog.at_level(logging.INFO, logger='Samsara'):
        if path == 'final':
            worker._final_pass()
            assert session.on_final.call_args.args == (None,)
            assert session.on_final.call_args.kwargs['duration_s'] == 5
        else:
            assert worker._transcribe_partial() is None
            session.on_partial.assert_not_called()
    session.app.model.transcribe.assert_not_called()
    session.app.process_transcription.assert_not_called()
    session.app.voice_training_window.apply_corrections.assert_not_called()
    streaming.pyautogui.hotkey.assert_not_called()
    if streaming.pyperclip is not None:
        streaming.pyperclip.copy.assert_not_called()
    drops = [r.getMessage() for r in caplog.records if 'no_speech_in_audio' in r.getMessage()]
    assert len(drops) == (path == 'final')
    assert all(phantom not in line for line in drops)
    assert not session.app.model_lock.locked()


@pytest.mark.parametrize('display', ['Full', 'Compact'])
def test_empty_result_uses_existing_display_and_history(make_worker, detector, display):
    worker, fake = make_worker([_segment()])
    enable(fake, pcm('silence'))
    session = streaming.StreamingSession.__new__(streaming.StreamingSession)
    session.app = fake.app
    session.cancel_event = threading.Event()
    session._state_lock = threading.Lock()
    session._direct_paste = True
    session._last_pasted = ''
    session._mark_done = Mock()
    session.app._log_history = Mock()
    if display == 'Full':
        overlay = streaming._LiveSurfaceStreamingOverlay.__new__(streaming._LiveSurfaceStreamingOverlay)
        overlay._partials = SimpleNamespace(sequence=0, capture_id=7, lane='dictate',
                                            controller=SimpleNamespace(post=Mock()))
        overlay.flash_done_and_fade = Mock()
    else:
        overlay = Mock(spec=streaming.StreamingOverlayQt)
    session._overlay = overlay
    fake.on_final.side_effect = lambda text, **kw: session._deliver_final(
        text, None, kw.get('duration_s', 0), 0)
    worker._final_pass()
    if display == 'Full':
        assert overlay._partials.controller.post.call_args.args[0].message == 'No speech'
    else:
        assert 'no speech' in overlay.update_text.call_args.args[0].lower()
    assert session.app._log_history.call_args.kwargs['status'] == 'empty'
    assert session.app._log_history.call_args.kwargs['raw_text'] == ''
    streaming.pyautogui.hotkey.assert_not_called()


@pytest.mark.parametrize('path', ['final', 'partial'])
@pytest.mark.parametrize('case', ['speech', 'one_frame_word', 'chair_creak', 'failure', 'off'])
def test_speech_uncertainty_failure_and_escape_hatch_decode_as_before(make_worker, detector, path, case):
    worker, session = make_worker([_segment()])
    enable(session, pcm('noise_low'))
    if case == 'off':
        session.app.config['dictation']['speech_presence_gate'] = False
    elif case == 'failure':
        detector._model.side_effect = RuntimeError('detector unavailable')
    else:
        def scores(audio):
            result = np.zeros(audio.size // 512, dtype=np.float32)
            result[0] = .11 if case == 'one_frame_word' else .23
            return result
        detector._model.side_effect = scores
    if path == 'final':
        worker._final_pass()
        assert session.on_final.call_args.args == (_segment().text,)
    else:
        assert worker._transcribe_partial() == _segment().text
    session.app.model.transcribe.assert_called_once()
    if case == 'off':
        detector._model.assert_not_called()


def test_first_partial_waits_then_full_pcm_is_preserved(make_worker, detector):
    worker, session = make_worker([_segment()])
    audio = pcm('silence')
    enable(session, audio)
    assert worker._transcribe_partial() is None
    session.app.model.transcribe.assert_not_called()
    detector._model.side_effect = lambda audio: np.full(audio.size // 512, .11)
    assert worker._transcribe_partial() == _segment().text
    assert session.app.model.transcribe.call_args.args[0] is audio


def test_commit_redecode_never_calls_decoder(detector):
    tree = ast.parse((ROOT/'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=='DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name=='_dictate_commit_redecode')
    ns = dict(np=np, logger=logging.getLogger('534.commit'))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), '<534-commit>', 'exec'), ns)
    app = SimpleNamespace(config={}, model_rate=16000, model=SimpleNamespace(transcribe=Mock()),
                          get_transcription_params=Mock(), model_lock=threading.Lock())
    assert ns['_dictate_commit_redecode'](app, '', [pcm('silence')]) is None
    app.model.transcribe.assert_not_called()
    app.get_transcription_params.assert_not_called()


@pytest.mark.parametrize('name', ['_handle_ava_command_utterance', '_handle_command_mode_utterance',
                                  'transcribe_continuous_buffer',
                                  pytest.param('_decode_wake_word_buffer', id='process_wake_word_buffer'),
                                  pytest.param('_stop_recording_impl', id='stop_recording')])
def test_other_final_call_sites_gate_before_decode(name):
    tree = ast.parse((ROOT/'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=='DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name==name)
    calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)]
    gates = [n for n in calls if isinstance(n.func, ast.Name) and n.func.id=='skip_decode']
    decodes = [n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr in {'transcribe', '_decode_hotkey_audio'}]
    assert gates and decodes and min(n.lineno for n in gates)<min(n.lineno for n in decodes)


def test_hands_free_preview_rejects_before_decode(detector):
    cls = streaming.DictatePreviewSession
    preview = cls.__new__(cls)
    preview.app = SimpleNamespace(config={}, model_rate=16000, model_lock=threading.Lock(),
                                  model=SimpleNamespace(transcribe=Mock()))
    preview._snapshot_audio = Mock(return_value=pcm('silence'))
    assert preview._transcribe_partial() is None
    preview.app.model.transcribe.assert_not_called()


def test_input_copy_tail_metrics_and_invalid_results(monkeypatch):
    detector = presence.SpeechPresence()
    audio = np.ones(700, dtype=np.float32)
    def model(copy):
        copy[:] = 0
        return np.array([.11, .11])
    detector._model = model
    result = detector.measure(audio, 16000)
    assert result.known and result.total_s == 700/16000 and result.longest_s == 700/16000
    assert np.all(audio == 1)
    for output in ([float('nan')], [1.1, .2], [.2], [-.1, .2]):
        detector._model = lambda _, output=output: np.array(output)
        assert not detector.measure(audio, 16000).known
    assert not detector.measure(audio, 0).known
    assert not detector.measure([float('nan')], 16000).known


def test_default_on_and_no_settings_row():
    from samsara.config_schema import SETTINGS_SCHEMA
    assert config_defaults.DEFAULTS[presence.CONFIG_KEY] is True
    assert SETTINGS_SCHEMA[presence.CONFIG_KEY]['settings_ui'] is False


def test_batch_empty_feedback_and_history(detector):
    app = SimpleNamespace(_show_outcome_chip=Mock(), _log_history=Mock())
    presence.show_empty_capture(app, 3)
    app._show_outcome_chip.assert_called_once_with('No speech', 'info')
    assert app._log_history.call_args.kwargs['raw_text'] == ''
    assert app._log_history.call_args.kwargs['status'] == 'empty'


def test_detector_load_and_inference_errors_are_unknown(monkeypatch):
    detector = presence.SpeechPresence()
    monkeypatch.setattr(detector, '_load_model', Mock(side_effect=ImportError('unavailable')))
    assert not detector.measure(np.zeros(16000), 16000).known
    assert not detector.measure(np.zeros(16000), 16000).known
    assert detector._model is None
    detector._load_model.assert_called_once()


def test_missing_config_defaults_on_and_raising_attributes_fail_open(detector, caplog):
    app = SimpleNamespace(model_rate=16000)
    assert presence.skip_decode(pcm('silence'), app)
    class Broken:
        model_rate = 16000
        @property
        def config(self):
            raise RuntimeError('unavailable')
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(pcm('silence'), Broken())
    assert 'verdict=unknown' in caplog.records[-1].getMessage()


def test_preview_without_config_still_decodes_when_presence_unknown(monkeypatch):
    monkeypatch.setattr(presence._presence, 'measure', lambda *a: presence.Presence(False))
    preview = streaming.DictatePreviewSession.__new__(streaming.DictatePreviewSession)
    preview.app = SimpleNamespace(model_rate=16000, model_lock=threading.Lock(),
        model=SimpleNamespace(transcribe=Mock(return_value=(iter([_segment()]), None))),
        _filter_dictation_language=lambda text, *a, **kw: text)
    preview._snapshot_audio = Mock(return_value=pcm('silence'))
    assert preview._transcribe_partial() == _segment().text
    preview.app.model.transcribe.assert_called_once()


def test_speech_cached_for_later_partials_and_final_only_in_same_hold(make_worker, detector, caplog):
    worker, session = make_worker([_segment()])
    enable(session, pcm('silence'))
    detector._model.side_effect = lambda audio: np.full(audio.size // 512, .8)
    assert worker._transcribe_partial() == _segment().text
    detector._model.reset_mock()
    detector._model.side_effect = RuntimeError('must not be rescored')
    assert worker._transcribe_partial() == _segment().text
    with caplog.at_level(logging.INFO, logger='Samsara'):
        worker._final_pass()
    detector._model.assert_not_called()
    lines = [r.getMessage() for r in caplog.records if 'speech_presence path=' in r.getMessage()]
    assert len(lines) == 1 and 'peak=0.80' in lines[0] and 'cached=1' in lines[0]
    other, other_session = make_worker([_segment()])
    enable(other_session, pcm('silence'))
    detector._model.side_effect = lambda audio: np.zeros(audio.size // 512)
    other._final_pass()
    other_session.app.model.transcribe.assert_not_called()


def test_load_error_logged_once_and_retried_only_after_minute(monkeypatch, caplog):
    detector = presence.SpeechPresence()
    clock = [0.0]
    monkeypatch.setattr(presence, 'monotonic', lambda: clock[0])
    loader = Mock(side_effect=ImportError('private exception contents'))
    monkeypatch.setattr(detector, '_load_model', loader)
    with caplog.at_level(logging.WARNING, logger='Samsara'):
        for instant in (0, 1, 59, 60, 61):
            clock[0] = instant
            assert not detector.measure(pcm('silence'), 16000).known
    assert loader.call_count == 2
    lines = [r.getMessage() for r in caplog.records if 'speech_presence_unavailable' in r.getMessage()]
    assert lines == ['[GATE] speech_presence_unavailable exception_type=ImportError']


@pytest.mark.parametrize('reject', [True, False])
def test_batch_scan_is_not_repeated_and_keeps_existing_result(detector, reject, caplog):
    class Decision:
        path = 'short'
        def __bool__(self):
            return reject
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert not presence.skip_decode(pcm('silence'), 16000, existing_gate=Decision(), path='batch')
    detector._model.assert_not_called()
    assert ('verdict=batch_reject' if reject else 'verdict=batch_pass') in caplog.records[-1].getMessage()


def test_batch_check_follows_existing_scan_before_decode():
    tree = ast.parse((ROOT/'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=='DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name=='_stop_recording_impl')
    calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)]
    old = next(n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr=='_buffer_should_skip_decode')
    new = next(n for n in calls if isinstance(n.func, ast.Name) and n.func.id=='skip_decode')
    assert old.lineno < new.lineno


def test_own_lock_serializes_context_mutation():
    from concurrent.futures import ThreadPoolExecutor
    detector = presence.SpeechPresence()
    inside = threading.Lock()
    def model(audio):
        assert inside.acquire(blocking=False)
        try:
            return np.ones(audio.size // 512)
        finally:
            inside.release()
    detector._model = model
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: detector.measure(np.ones(16000), 16000), range(8)))
    assert all(result.known and result.total_s == 1 for result in results)


def test_long_loud_clicks_have_no_bypass(make_worker, detector):
    worker, session = make_worker([_segment()])
    enable(session, np.tile(pcm('clicks'), 6))
    worker._final_pass()
    session.app.model.transcribe.assert_not_called()
    assert session.on_final.call_args.args == (None,)
    assert session.on_final.call_args.kwargs['duration_s'] == 30
