"""Observer isolation and exact provenance, without hardware or app bootstrap."""
from types import SimpleNamespace as NS
import threading

import numpy as np
import pytest

from samsara import diagnostics as diag
from samsara.audio_engine.capture_snapshot import CaptureSnapshot, CommittedFrame
from samsara.hold_final_policy import FinalAudit, current_audit
from samsara.hold_recording_recovery import incident_model


@pytest.fixture(autouse=True)
def detach():
    diag.set_mic_lab_observer(None)
    yield
    diag.flush_mic_lab_observer()
    diag.set_mic_lab_observer(None)


def capture():
    frames = tuple(CommittedFrame(seq, 2, i / 10, (np.arange(1600, dtype=np.int16) + i).tobytes(), i == 2)
                   for i, seq in enumerate((40, 41, 44, 45)))
    return CaptureSnapshot(5, frames, (), False, .1).audio()


def app(model):
    return NS(model=model, model_rate=16000, config={})


class Model:
    def __init__(self):
        self.inputs, self.pulled = [], 0

    def transcribe(self, audio, **params):
        self.inputs.append(audio)
        def segments():
            self.pulled += 1
            yield NS(text=' testing', start=0., end=.1, temperature=.2, avg_logprob=-.4, compression_ratio=1.2)
        return segments(), NS(language='en')


def test_off_has_no_queue_no_callback_no_audit_allocation(monkeypatch):
    model, audit = Model(), FinalAudit()
    monkeypatch.setattr(diag, '_MicLabSink', lambda *a: pytest.fail('sink allocated while off'))
    monkeypatch.setattr(diag, 'mic_lab_model', lambda *a: pytest.fail('observer called while off'))
    token = current_audit.set(audit)
    try:
        segments, _ = incident_model(app(model)).transcribe(capture())
        assert len(list(segments)) == 1
    finally:
        current_audit.reset(token)
    assert diag._mic_lab_observer is None
    assert not hasattr(audit, '_mic_lab_1267')


def test_on_copies_exact_decoder_pcm_maps_frames_and_keeps_lazy_iteration():
    events, threads = [], []
    diag.set_mic_lab_observer(lambda event: (events.append(event), threads.append(threading.current_thread().name)))
    raw, model, audit = capture(), Model(), FinalAudit()
    owner = app(model)
    diag.begin_mic_lab_hold(audit, raw, app=owner, route='streaming')
    decoder = raw[800:4000].copy()
    expected = np.array(decoder, copy=True)
    token = current_audit.set(audit)
    try:
        segments, _ = incident_model(owner).transcribe(decoder, temperature=(0., .2), vad_filter=False)
        assert model.pulled == 0
        assert list(segments)[0].text == ' testing'
    finally:
        current_audit.reset(token)
    decoder[:] = -.9
    text = 'long ' * 90
    diag.complete_mic_lab_hold(audit, text)
    assert diag.flush_mic_lab_observer()
    assert len(events) == 1 and threads == ['mic-lab-observer']
    event, item = events[0], events[0]['slices'][0]
    assert event['text'] == text
    assert np.array_equal(item['audio'], expected)
    assert not np.shares_memory(item['audio'], decoder)
    assert not np.shares_memory(event['raw'], raw)
    assert (item['start_frame'], item['end_frame']) == (40, 45)
    assert [(r['start_frame'], r['start_offset'], r['end_offset']) for r in item['ranges']] == [(40, 800, 1600), (41, 0, 1600), (44, 0, 800)]
    assert (item['capture_start'], item['capture_end']) == (800, 4000)
    assert (item['temperature'], item['avg_logprob'], item['compression_ratio'], item['retry']) == (.2, -.4, 1.2, True)
    item['audio'][:] = 10
    assert not np.any(raw == 10)


def test_multiple_inputs_and_unknown_provenance_not_guessed():
    events = []
    diag.set_mic_lab_observer(events.append)
    audit, model = FinalAudit(), Model()
    owner = app(model)
    raw = capture()
    diag.begin_mic_lab_hold(audit, raw, app=owner, route='retry')
    observed = diag.mic_lab_model(model, audit, owner)
    for pcm in (raw[:1600], raw[1600:3200], np.array(raw[3200:], copy=True)):
        list(observed.transcribe(pcm)[0])
    diag.complete_mic_lab_hold(audit, 'testing')
    diag.flush_mic_lab_observer()
    slices = events[0]['slices']
    assert len(slices) == 3
    assert slices[-1]['start_frame'] is None and slices[-1]['ranges'] == []
    assert all(s['retry'] for s in slices)


def test_empty_temperature_zero_repeat_is_marked_as_retry():
    events = []
    diag.set_mic_lab_observer(events.append)
    model = NS(transcribe=lambda *a, **k: (iter(()), None))
    audit, pcm = FinalAudit(), capture()
    owner = app(model)
    diag.begin_mic_lab_hold(audit, pcm, app=owner, route='batch')
    observed = diag.mic_lab_model(model, audit, owner)
    list(observed.transcribe(pcm, temperature=0.)[0])
    list(observed.transcribe(pcm, temperature=0., no_speech_threshold=None)[0])
    diag.complete_mic_lab_hold(audit, '')
    assert diag.flush_mic_lab_observer()
    assert [s['retry'] for s in events[0]['slices']] == [False, True]


def test_callback_failure_warns_once_without_leaking_text(caplog):
    def broken(event):
        raise ValueError('private callback data must not be logged')
    diag.set_mic_lab_observer(broken)
    sink = diag._mic_lab_observer
    for _ in range(2):
        sink.queue.put(dict(text='private sample'))
    assert diag.flush_mic_lab_observer()
    assert caplog.text.count('observation_failed=1') == 1
    assert 'private' not in caplog.text


def test_copy_failure_does_not_stop_decoder(monkeypatch):
    diag.set_mic_lab_observer(lambda event: None)
    model, audit = Model(), FinalAudit()
    owner = app(model)
    diag.begin_mic_lab_hold(audit, capture(), app=owner, route='batch')
    import numpy
    monkeypatch.setattr(numpy, 'array', lambda *a, **k: (_ for _ in ()).throw(MemoryError()))
    segments, _ = diag.mic_lab_model(model, audit, owner).transcribe(np.zeros(10, np.float32))
    assert len(list(segments)) == 1


@pytest.mark.parametrize('lazy', [False, True])
def test_native_errors_remain_native_errors(lazy):
    diag.set_mic_lab_observer(lambda event: None)
    class Broken:
        def transcribe(self, audio, **params):
            if not lazy:
                raise LookupError('native call failed')
            def pull():
                raise LookupError('native pull failed')
                yield
            return pull(), None
    model, audit = Broken(), FinalAudit()
    with pytest.raises(LookupError, match='native'):
        list(diag.mic_lab_model(model, audit, app(model)).transcribe(capture())[0])


def test_delivery_full_text_precedes_diagnostics_truncation():
    events = []
    diag.set_mic_lab_observer(events.append)
    owner, audit = app(Model()), FinalAudit()
    diag.begin_mic_lab_hold(audit, capture(), app=owner, route='batch')
    diag.pending_mic_lab_batch(audit, owner, 'raw')
    final = 'final ' * 100
    record = diag.DiagRecord('hotkey', 2., 'fake', 'cpu', 'int8', text=final)
    diag.record(record, owner)
    diag.flush_mic_lab_observer()
    assert events[0]['text'] == final and len(record.text) == 200


def test_registration_change_does_not_cross_wire_old_hold():
    first, second = [], []
    diag.set_mic_lab_observer(first.append)
    audit = FinalAudit()
    diag.begin_mic_lab_hold(audit, capture(), app=app(Model()), route='batch')
    diag.set_mic_lab_observer(second.append)
    diag.complete_mic_lab_hold(audit, 'old')
    assert diag.flush_mic_lab_observer()
    assert not first and not second


def test_batch_pending_is_thread_local_for_overlapping_holds():
    events = []
    diag.set_mic_lab_observer(events.append)
    owner, barrier = app(Model()), threading.Barrier(2)
    def complete(number):
        audit = FinalAudit()
        diag.begin_mic_lab_hold(audit, capture(), app=owner, route='batch')
        diag.pending_mic_lab_batch(audit, owner, 'raw')
        barrier.wait(5)
        diag.record(diag.DiagRecord('hotkey', 1., 'fake', 'cpu', 'int8', text=str(number)), owner)
    workers = [threading.Thread(target=complete, args=(n,)) for n in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    diag.flush_mic_lab_observer()
    assert sorted(event['text'] for event in events) == ['0', '1']
    assert len({e['id'] for e in events}) == 2
