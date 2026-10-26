"""917b composition harness: synthetic audio/models, offscreen Qt, no bootstrap."""
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import decode_recovery as recovery, failure_reasons, speech_regions
from samsara.gpu_fallback import ResilientSpeechModel, paste_final
from samsara.streaming import StreamingSession


CUDA_ERROR = 'Library cublas64_12.dll is not found or cannot be loaded'


def segment(text=' A complete ordinary sentence.'):
    return NS(text=text, avg_logprob=-.3, no_speech_prob=.1, compression_ratio=1.2,
              temperature=0.0, start=0.0, end=1.0)


def harness(monkeypatch, *, gpu_delay=0, load_delay=0, timeout=1, cpu_segments=None):
    from samsara.ui import qt_runtime, speech_hardware_qt
    events, posted, params = [], [], []
    monkeypatch.setattr(qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(qt_runtime, 'post', lambda cb: (events.append('post'), posted.append(cb)))
    monkeypatch.setattr(speech_hardware_qt, 'show_fallback_notice', lambda: events.append('notice'))
    app = NS(config={'dictation': {'final_decode_timeout_s': timeout}}, device_type='cuda',
             model_rate=16000, model_lock=threading.Lock(), compute_type='float16',
             _filter_dictation_language=lambda text, info: True, _show_outcome_chip=Mock())

    def fail(*args, **kwargs):
        time.sleep(gpu_delay)
        raise RuntimeError(CUDA_ERROR)

    def transcribe(audio, **kwargs):
        params.append(kwargs)
        return (cpu_segments() if cpu_segments else iter([segment(), segment(' More words.')]),
                NS(language='en'))

    def factory():
        events.append('load')
        time.sleep(load_delay)
        return NS(transcribe=transcribe)

    app.model = ResilientSpeechModel(app, NS(transcribe=fail), factory, device='cuda')
    session = NS(app=app, cancel_event=threading.Event(), _last_partial='',
                 _state='finalizing', _tutorial_owner=None)
    worker = NS(_session=session, _decoder_guard=lambda *a, **k: None)
    audio = np.ones(32000, np.float32) * .1
    return app, session, worker, audio, events, posted, params


@pytest.mark.parametrize('entry', ['hold', 'stream'])
def test_first_utterance_recovers_after_two_second_gpu_and_two_second_cpu_load(monkeypatch, entry):
    app, session, worker, audio, events, posted, params = harness(
        monkeypatch, gpu_delay=2, load_delay=2, timeout=3)
    incoming = dict(beam_size=8, best_of=5, temperature=(0, .2, .4), max_new_tokens=448)
    if entry == 'hold':
        text = recovery.decode_hold(app, audio, incoming, 2).text
    else:
        text = recovery.stream_final(worker, audio, incoming)[2]
    assert 'ordinary sentence.' in text and 'More words.' in text
    assert params[0]['beam_size'] == 3 and params[0]['best_of'] == 1
    assert params[0]['temperature'] == 0 and params[0]['max_new_tokens'] <= 192
    assert not posted

    def insert(text, **kwargs):
        events.append('paste')
        assert not posted
        return True
    app._paste_preserving_clipboard = insert
    if entry == 'hold':
        delivered = app._paste_preserving_clipboard(text)
        monkeypatch.setattr(failure_reasons, 'post_clipping', lambda *a: None)
        failure_reasons.post_delivery_clipping(app, audio, delivered)
    else:
        StreamingSession._paste_with_retry(session, text)
    assert events == ['load', 'paste', 'post'] and len(posted) == 1
    posted.pop()()
    assert events[-1] == 'notice'
    paste_final(session, text)
    assert not posted  # a second delivery never repeats the notice


def test_recovery_after_gpu_fallback_keeps_retry_temperature(monkeypatch):
    app, session, worker, audio, events, _, params = harness(monkeypatch)
    guarded = []

    def guard(segments, text, decode_params, **kwargs):
        guarded.append(text)
        return 'bad_decode' if len(guarded) == 1 else None

    worker._decoder_guard = guard
    incoming = dict(beam_size=8, best_of=5, temperature=(0, .2, .4),
                    max_new_tokens=448, language='en')
    assert recovery.stream_final(worker, audio, incoming)[2]
    assert app.device_type == 'cpu' and events == ['load']
    assert len(params) == 2 and len(guarded) == 3
    assert params[0]['temperature'] == 0.0
    assert params[1]['temperature'] == .2
    assert all(p['beam_size'] == 3 and p['best_of'] == 1 for p in params)
    assert all(p['max_new_tokens'] <= 192 and p['language'] == 'en' for p in params)
    assert params[1]['initial_prompt'] is None and not params[1]['vad_filter']


def test_later_cpu_calls_preserve_explicit_temperature_and_still_apply_caps(monkeypatch):
    app, _, _, audio, _, _, params = harness(monkeypatch)
    app.model.transcribe(audio, temperature=.2, beam_size=8, best_of=5, max_new_tokens=448)
    app.model.transcribe(audio, temperature=.2, beam_size=8, best_of=5, max_new_tokens=448)
    app.model.transcribe(audio, beam_size=2, max_new_tokens=64)
    assert params == [
        dict(temperature=0.0, beam_size=3, best_of=1, max_new_tokens=192),
        dict(temperature=.2, beam_size=3, best_of=1, max_new_tokens=192),
        dict(temperature=0.0, beam_size=2, best_of=1, max_new_tokens=64),
    ]


def test_sparse_speech_region_final_shares_fallback_caps_and_load_exclusion(monkeypatch):
    app, session, worker, _, events, posted, params = harness(
        monkeypatch, gpu_delay=.04, load_delay=.2, timeout=.12)
    audio = np.ones(16000 * 20, np.float32) * .1
    monkeypatch.setattr(speech_regions.speech_presence._presence, 'measure', lambda *a: object())
    monkeypatch.setattr(speech_regions, 'locate', lambda *a, **k:
                        speech_regions.Regions(((0, 16000),), ((0, 16000),), len(audio)))
    monkeypatch.setattr(speech_regions, 'filter_words', lambda segs, evidence:
                        (segs, ''.join(s.text for s in segs).strip()))
    assert speech_regions.stream_final(worker, audio, {'beam_size': 9})[2]
    assert params[0]['beam_size'] == 3 and params[0]['word_timestamps']
    assert not posted


def test_cold_load_overlapping_model_lock_wait_is_excluded(monkeypatch):
    app, session, worker, audio, _, posted, params = harness(monkeypatch, timeout=.08)
    loading, release = threading.Event(), threading.Event()
    def transcribe(*a, **kwargs):
        params.append(kwargs)
        return iter([segment()]), NS(language='en')
    cpu = NS(transcribe=transcribe)
    def factory():
        loading.set()
        assert release.wait(2)
        return cpu
    app.model._cpu_factory = factory
    def partial():
        with app.model_lock:
            list(app.model.transcribe(audio)[0])
    thread = threading.Thread(target=partial)
    thread.start()
    timer = None
    try:
        assert loading.wait(1)
        timer = threading.Timer(.2, release.set)
        timer.start()
        assert recovery.stream_final(worker, audio, {'beam_size': 8, 'best_of': 5})[2]
        assert not posted
    finally:
        release.set()
        thread.join(2)
        if timer:
            timer.join(2)
    assert not thread.is_alive() and app._hold_decode_job.done.wait(1)
    # Params were selected while the partial still held a CUDA->CPU load.
    assert params[-1]['beam_size'] == 3 and params[-1]['best_of'] == 1
    assert params[-1]['temperature'] == 0 and params[-1]['max_new_tokens'] <= 192


def test_cpu_cancel_after_segment_one_never_pulls_segment_two(monkeypatch):
    pulled = []
    app, session, worker, audio, _, posted, _ = harness(monkeypatch)
    def segments():
        pulled.append(1)
        session.cancel_event.set()
        yield segment()
        pulled.append(2)
        yield segment(' Must not decode.')
    app.model._cpu_factory = lambda: NS(transcribe=lambda *a, **k: (segments(), NS(language='en')))
    assert not recovery.stream_final(worker, audio, {})[2]
    assert app._hold_decode_job.done.wait(1)
    assert pulled == [1] and not posted
    assert app.model_lock.acquire(blocking=False)
    app.model_lock.release()


@pytest.mark.parametrize('problem', ['rejected', 'cancelled', 'timeout'])
def test_unusable_recovered_final_never_posts_a_notice(monkeypatch, problem):
    app, session, worker, audio, _, posted, _ = harness(monkeypatch, timeout=.05)
    if problem == 'rejected':
        worker._decoder_guard = lambda *a, **k: 'bad_decode'
    elif problem == 'cancelled':
        session.cancel_event.set()
    else:
        app.model._cpu_factory = lambda: NS(transcribe=lambda *a, **k:
            (slow_segments(), NS(language='en')))
        def slow_segments():
            time.sleep(.15)
            yield segment()
    assert not recovery.stream_final(worker, audio, {})[2]
    job = getattr(app, '_hold_decode_job', None)
    if job:
        assert job.done.wait(1)
    assert not posted


@pytest.mark.parametrize('outcome', ['failure', 'cancel', 'exception'])
def test_failed_final_insertion_does_not_announce_recovery(monkeypatch, outcome):
    app, session, worker, audio, _, posted, _ = harness(monkeypatch)
    text = recovery.stream_final(worker, audio, {})[2]
    def insert(*args, **kwargs):
        if outcome == 'exception':
            raise RuntimeError('paste failed')
        if outcome == 'cancel':
            session.cancel_event.set()
        return outcome != 'failure'
    app._paste_preserving_clipboard = insert
    if outcome == 'exception':
        with pytest.raises(RuntimeError, match='paste failed'):
            paste_final(session, text)
    else:
        paste_final(session, text)
    assert not posted


def test_rejected_final_salvages_partial_without_announcing_recovery(monkeypatch):
    app, session, worker, audio, _, posted, _ = harness(monkeypatch)
    worker._decoder_guard = lambda *a, **k: 'bad_decode'
    session._last_partial = 'Previously accepted partial.'
    text = recovery.stream_final(worker, audio, {})[2]
    assert text == session._last_partial
    app._paste_preserving_clipboard = Mock(return_value=True)
    paste_final(session, text)
    app._paste_preserving_clipboard.assert_called_once()
    assert not posted


@pytest.mark.parametrize('method', ['_paste_retry_then_clipboard', '_direct_paste_final'])
def test_worker_and_delayed_final_paste_report_only_after_insertion(monkeypatch, method):
    from samsara import streaming
    app, session, worker, audio, events, posted, _ = harness(monkeypatch)
    text = recovery.stream_final(worker, audio, {})[2]
    session._paste_lock, session._last_pasted = threading.Lock(), False
    monkeypatch.setattr(streaming, '_release_held_modifiers', lambda: None)
    def insert(*a, **k):
        assert not posted
        events.append('paste')
        return True
    app._paste_preserving_clipboard = insert
    getattr(StreamingSession, method)(session, text)
    assert events == ['load', 'paste', 'post']


def test_cpu_session_stays_lazy_without_changing_ordinary_retry_parameters():
    pulled = []
    def segments():
        pulled.append(1)
        yield segment()
        pulled.append(2)
        yield segment()
    native = Mock(transcribe=Mock(side_effect=lambda *a, **k: (segments(), None)))
    model = ResilientSpeechModel(NS(), native, Mock(), device='cpu')
    iterator, _ = model.transcribe(object(), temperature=.2, beam_size=2)
    assert pulled == []
    next(iterator)
    assert pulled == [1]
    assert native.transcribe.call_args.kwargs == dict(temperature=.2, beam_size=2)


def test_fallback_dialog_is_non_activating(qapp):
    from PySide6.QtCore import Qt
    from samsara.ui.speech_hardware_qt import show_fallback_notice
    dialog = show_fallback_notice()
    try:
        assert dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert dialog.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert dialog.isVisible() and not dialog.isModal()
    finally:
        dialog.close()
        qapp.processEvents()


def test_startup_recovery_waits_for_ready_without_a_transcribe(monkeypatch):
    from samsara.ui import qt_runtime
    callbacks = []
    monkeypatch.setattr(qt_runtime, '_ready_callbacks', callbacks)
    monkeypatch.setattr(qt_runtime, '_state', qt_runtime._STARTING)
    monkeypatch.setattr(qt_runtime, '_app', None)
    monkeypatch.setattr(qt_runtime, '_watchdog', None)
    monkeypatch.setattr(qt_runtime, '_ready', threading.Event())
    posted, notice = [], Mock()
    monkeypatch.setattr(qt_runtime, 'post', posted.append)
    model = ResilientSpeechModel(NS(), Mock(), Mock(), device='cpu', recovered=True, notify=notice)
    assert callbacks == [model._report_recovery] and not posted
    notice.assert_not_called()
    # Exercise the real ready-queue drain without launching a UI or watchdog.
    qt_app = Mock()
    monkeypatch.setattr(qt_runtime, 'QApplication', Mock(return_value=qt_app))
    monkeypatch.setattr(qt_runtime, 'QTimer', Mock())
    monkeypatch.setattr(qt_runtime, '_StallWatchdog', Mock())
    monkeypatch.setattr(qt_runtime.thread_registry, 'spawn', Mock())
    qt_runtime._run_loop()
    assert callbacks == [] and len(posted) == 1
    qt_app.exec.assert_called_once()
    posted.pop()()
    model._report_recovery()
    notice.assert_called_once_with()


def test_gpu_and_cpu_decode_time_still_count_against_one_budget(monkeypatch):
    app, session, worker, audio, _, posted, _ = harness(
        monkeypatch, gpu_delay=.08, load_delay=.15, timeout=.12)
    def segments():
        time.sleep(.08)
        yield segment()
    app.model._cpu_factory = lambda: (time.sleep(.15) or
        NS(transcribe=lambda *a, **k: (segments(), NS(language='en'))))
    assert not recovery.stream_final(worker, audio, {})[2]
    assert app._hold_decode_job.done.wait(1) and not posted
