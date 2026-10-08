"""387: decoder verdicts protect streaming delivery, including quiet speech."""
import ast
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import streaming, transcript_gates
from samsara import hallucination_filter


ROOT = Path(__file__).resolve().parents[1]
FLOOR = 0.008


def _segment(**changes):
    values = dict(text="A complete ordinary sentence.", avg_logprob=-0.3,
                  no_speech_prob=0.1, compression_ratio=1.2, temperature=0.0)
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def make_worker(monkeypatch):
    monkeypatch.setattr(streaming.diagnostics, 'record', Mock())
    monkeypatch.setattr(streaming, 'clean_text', Mock(side_effect=lambda text, **kw: text))
    monkeypatch.setattr(streaming, 'smart_correct', Mock(side_effect=lambda text, app: text))
    monkeypatch.setattr(streaming.pyautogui, 'hotkey', Mock())
    if streaming.pyperclip is not None:
        monkeypatch.setattr(streaming.pyperclip, 'copy', Mock())

    def build(segments, speech_level=0.1, params=None):
        audio = np.zeros(16000, dtype=np.float32)
        profile = dict(language='en', temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                       no_speech_threshold=0.6, log_prob_threshold=-1.0,
                       without_timestamps=False)
        profile.update(params or {})
        app = SimpleNamespace(
            model_rate=16000, model_lock=threading.Lock(),
            model=SimpleNamespace(transcribe=Mock(side_effect=lambda *a, **kw:
                (iter(segments), SimpleNamespace(language='en')))),
            get_transcription_params=Mock(return_value=profile),
            config={'cleanup_mode': 'clean', 'add_trailing_space': False},
            voice_training_window=SimpleNamespace(
                apply_corrections=Mock(side_effect=lambda text: text),
                get_initial_prompt=Mock(return_value=None)),
            process_transcription=Mock(side_effect=lambda text: text),
            _apply_formatting_tokens=Mock(side_effect=lambda text: text),
            _dictation_consumer=SimpleNamespace(
                stop_streaming=Mock(), snapshot_streaming_audio=Mock(return_value=audio)),
        )
        session = SimpleNamespace(app=app, stop_event=threading.Event(),
                                  cancel_event=threading.Event(), on_final=Mock(),
                                  on_partial=Mock(), start_time=streaming.time.time(),
                                  _stop_capture=Mock(return_value=audio))
        if speech_level is not None:
            session._release_tail_signals = dict(speech_level=speech_level, speech_floor=FLOOR)
        return streaming.StreamingWorker(session), session
    return build


@pytest.mark.parametrize('segment,level', [
    (_segment(temperature=1.0, avg_logprob=-4.77), FLOOR),
    (_segment(temperature=0.0, compression_ratio=18.4), 0.1),
    (_segment(temperature=0.2, avg_logprob=-2.4), 0.1),
    (_segment(temperature=0.0, avg_logprob=-2.4), FLOOR),
])
def test_failed_final_never_reaches_corrections_or_delivery(make_worker, segment, level):
    worker, session = make_worker([segment], level)
    worker._final_pass()
    assert session.on_final.call_args.args == (None,)
    session.app.voice_training_window.apply_corrections.assert_not_called()
    session.app.process_transcription.assert_not_called()
    streaming.clean_text.assert_not_called()
    streaming.smart_correct.assert_not_called()
    session.app._apply_formatting_tokens.assert_not_called()
    streaming.pyautogui.hotkey.assert_not_called()
    if streaming.pyperclip is not None:
        streaming.pyperclip.copy.assert_not_called()


@pytest.mark.parametrize('temperature,logprob,level', [
    (0.0, -0.3, 0.10), (0.0, -0.5, 0.009), (0.0, -0.5, FLOOR),
    (0.0, -0.5, 0.001), (0.2, -0.9, 0.05), (0.0, -0.5, None),
])
def test_clean_final_including_whispers_is_delivered(make_worker, temperature, logprob, level):
    segment = _segment(temperature=temperature, avg_logprob=logprob)
    worker, session = make_worker([segment], level)
    worker._final_pass()
    assert session.on_final.call_args.args == (segment.text,)
    session.app.voice_training_window.apply_corrections.assert_called_once_with(segment.text)


def test_shared_batch_backstop_runs_after_decoder_guard(make_worker, monkeypatch):
    worker, session = make_worker([_segment()])
    backstop = Mock(return_value=True)
    monkeypatch.setattr(transcript_gates, '_is_hallucinated_segments', backstop)
    worker._final_pass()
    backstop.assert_called_once()
    assert session.on_final.call_args.args == (None,)
    session.app.process_transcription.assert_not_called()


def test_one_guard_line_contains_signals_without_transcript(make_worker, caplog):
    worker, _ = make_worker([_segment(avg_logprob=-4.77, temperature=1.0)], FLOOR)
    with caplog.at_level(logging.INFO):
        worker._final_pass()
    lines = [record.getMessage() for record in caplog.records
             if '[GUARD] streaming final discarded:' in record.getMessage()]
    assert len(lines) == 1
    for signal in ('avg_logprob=', 'compression_ratio=', 'temperature=', 'speech_level='):
        assert signal in lines[0]
    assert 'A complete ordinary sentence' not in lines[0]


@pytest.mark.parametrize('logprob,expected', [(-2.4, None), (-3.2, None),
                                            (-0.4, 'A complete ordinary sentence.')])
def test_partial_uses_logprob_before_cleanup(make_worker, monkeypatch, logprob, expected):
    worker, session = make_worker([_segment(avg_logprob=logprob)], None)
    cleanup = Mock(wraps=worker._partial_cleanup)
    monkeypatch.setattr(worker, '_partial_cleanup', cleanup)
    assert worker._transcribe_partial() == expected
    assert cleanup.call_count == (expected is not None)
    assert not session.app.model_lock.locked()


def test_bad_partial_is_never_emitted(make_worker):
    worker, session = make_worker([_segment(avg_logprob=-2.4)], None)
    worker._stop_event = Mock()
    worker._stop_event.is_set.return_value = False
    worker._stop_event.wait.side_effect = [False, True]
    worker._loop_partials()
    session.on_partial.assert_not_called()


@pytest.mark.parametrize('profile_temp,expected', [(0.0, 0.0),
    ((0.0, 0.2, 0.4, 0.6, 0.8, 1.0), (0.0, 0.2))])
def test_only_streaming_final_ladder_changes(make_worker, profile_temp, expected):
    worker, session = make_worker([], params={'temperature': profile_temp})
    original_profile = dict(session.app.get_transcription_params())
    final = worker._final_params()
    assert final['temperature'] == expected
    assert final['vad_filter'] is False
    assert final['without_timestamps'] is False
    assert session.app.get_transcription_params() == original_profile
    partial = worker._partial_params()
    assert partial['temperature'] == 0.0
    assert partial['vad_filter'] is False
    assert partial['log_prob_threshold'] == streaming.LOG_PROB_THRESHOLD


def test_missing_profile_temperature_has_two_rungs(make_worker):
    worker, session = make_worker([])
    profile = dict(session.app.get_transcription_params())
    profile.pop('temperature')
    session.app.get_transcription_params.return_value = profile
    assert worker._final_params()['temperature'] == (0.0, 0.2)


def test_suppressed_direct_partial_is_undone_and_notice_posted_once(make_worker, monkeypatch):
    worker, fake = make_worker([_segment(avg_logprob=-4.77, temperature=1.0)], FLOOR)
    session = streaming.StreamingSession.__new__(streaming.StreamingSession)
    session.app = fake.app
    session._state_lock = threading.Lock()
    session._paste_lock = threading.Lock()
    session._parking_enabled = False
    session._direct_paste = True
    session._last_pasted = 'previous partial'
    session._mark_done = Mock()
    session.app._log_history = Mock()
    overlay = streaming._LiveSurfaceStreamingOverlay.__new__(streaming._LiveSurfaceStreamingOverlay)
    overlay._partials = SimpleNamespace(sequence=0, capture_id=7, lane='dictate',
                                       controller=SimpleNamespace(post=Mock()))
    overlay.flash_done_and_fade = Mock()
    session._overlay = overlay
    monkeypatch.setattr(streaming, '_release_held_modifiers', Mock(return_value=[]))
    monkeypatch.setattr(streaming.thread_registry, 'spawn',
                        lambda name, target, **kw: target())
    fake.on_final.side_effect = lambda text, **kw: session._deliver_final(
        text, kw.get('raw_text'), kw.get('duration_s', 0), kw.get('elapsed_ms', 0))
    worker._final_pass()
    assert session._last_pasted is False
    streaming.pyautogui.hotkey.assert_called_once_with('ctrl', 'z')
    overlay._partials.controller.post.assert_called_once()
    assert overlay._partials.controller.post.call_args.args[0].message == 'No speech'
    overlay.flash_done_and_fade.assert_called_once_with(session._mark_done)
    if streaming.pyperclip is not None:
        streaming.pyperclip.copy.assert_not_called()


def test_release_caller_passes_measured_signals_before_finalizing(make_worker):
    """Execute the actual owned release-tail block, without desktop bootstrap."""
    worker, session = make_worker([], None)
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    block = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                 and isinstance(node.test, ast.Name) and node.test.id == 'streaming_release_tail'
                 and any(isinstance(child, ast.Attribute) and child.attr == 'wait_for_streaming_release_tail'
                         for child in ast.walk(node)))
    fn = ast.parse('def release(self, sess, streaming_release_tail):\n    pass').body[0]
    fn.body = [block]
    namespace = dict(logger=logging.getLogger('387-release'),
                     HOLD_RELEASE_TAIL_SPEECH_THRESHOLD=FLOOR)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])),
                 str(ROOT / 'dictation.py'), 'exec'), namespace)
    app = SimpleNamespace(config={}, hotkey_pressed=False, play_sound=Mock(),
        _dictation_consumer=SimpleNamespace(finish_capture=Mock(),
            wait_for_streaming_release_tail=Mock(return_value=dict(reason='silence',
                elapsed_ms=358, frames=4, threshold=FLOOR, speech_level=FLOOR,
                last_frame_rms=0.0071))))
    namespace['release'](app, session, True)
    assert session._release_tail_signals['speech_level'] == FLOOR
    assert session._release_tail_signals['speech_floor'] == FLOOR
    app._dictation_consumer.finish_capture.assert_called_once_with(force=True)


@pytest.mark.parametrize('segments,kwargs,expected', [
    ([_segment(temperature=1.0, avg_logprob=-4.77)], {}, True),
    ([_segment(compression_ratio=18.4)], {}, True),
    ([_segment(compression_ratio=2.4)], {}, False),
    ([_segment(temperature=0.2, avg_logprob=-0.9)], {'top_temperature': 0.2}, False),
    ([_segment(avg_logprob=-0.5)], {'speech_level': FLOOR}, False),
    ([_segment(avg_logprob=-2.4, temperature=0.0)], {'speech_level': FLOOR}, True),
    ([_segment(avg_logprob=-2.4, temperature=0.0)], {}, False),
    ([_segment(avg_logprob=-1.0, temperature=1.0)], {}, False),
    ([SimpleNamespace(text='Missing telemetry')], {'speech_level': FLOOR}, False),
    ([], {'speech_level': FLOOR}, False),
    ([_segment(avg_logprob=-2.4), _segment()], {'partial': True}, True),
    ([_segment(avg_logprob=-2.4, temperature=1.0), _segment()], {}, False),
    ([_segment(avg_logprob=-2.4, no_speech_prob=0.7)], {}, True),
    ([_segment(avg_logprob=-0.4, no_speech_prob=0.9)], {}, False),
    ([_segment(avg_logprob=-0.9, temperature=0.2)],
     {'top_temperature': 0.2, 'log_prob_threshold': -0.8}, True),
    ([_segment(avg_logprob=float('nan'), temperature=1.0)],
     {'speech_level': FLOOR}, False),
    ([_segment(avg_logprob=-2.4, temperature=0.0)],
     {'speech_level': float('nan')}, False),
])
def test_pure_decoder_policy(segments, kwargs, expected):
    policy = getattr(hallucination_filter, 'decoder_failure_reason', None)
    assert callable(policy)
    assert bool(policy(segments, **kwargs)) is expected


def test_existing_batch_backstop_still_distinguishes_real_and_repetitive_speech():
    assert transcript_gates._is_hallucinated_segments([_segment()], 'An ordinary sentence.') is False
    assert transcript_gates._is_hallucinated_segments([_segment(compression_ratio=18.4)],
                                                     'Repeated output.') is True
    assert transcript_gates._is_hallucinated_segments([], 'Thank you for watching!') is True
