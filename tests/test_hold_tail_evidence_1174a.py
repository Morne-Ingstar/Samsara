"""Copied synthetic PCM, clocks, fake decoder and temporary stores only."""
import json
import threading
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import hold_diagnostics as evidence, hold_tail_evidence as tail
from samsara import kept_dictations as kept, decode_recovery as decoder, speech_presence
from samsara.audio_engine import DictationSessionConsumer, FrameBus, FRAME_SIZE
from samsara.audio_engine.frame import PREBUFFER_FRAMES
from samsara.hold_final_policy import FinalAudit, current_audit


def consumer():
    bus = FrameBus()
    return DictationSessionConsumer(NS(register_consumer=bus.new_reader), NS()), bus


def release(monkeypatch, lane, body, ending, *, ambient=None, cancel=False):
    import samsara.audio_engine.dictation_consumer as module
    c, _ = consumer()
    pcm = lambda x: np.full(FRAME_SIZE, round(x * 32767), np.int16)
    c._frames = [(i, 0, pcm(body)) for i in range(10)]
    c._streaming_frames = [pcm(body).astype(np.float32) / 32767 for _ in range(10)]
    if ambient is not None:
        c._tail_ambient = tail.Ambient(100., PREBUFFER_FRAMES, True)
        for i in range(PREBUFFER_FRAMES):
            c._tail_ambient.observe(NS(seq=i, device_epoch=0, t_capture=98.6+i*.1, pcm=pcm(ambient)), .1)
    clock = NS(now=100., index=0)
    def sleep(seconds):
        clock.now += seconds
        if cancel and clock.now >= 100.05:
            c._drain_stop.set()
        while clock.index < len(ending) and clock.now + 1e-8 >= 100 + (clock.index + 1) * .1:
            data = pcm(ending[clock.index])
            c._frames.append((10 + clock.index, 0, data.copy()))
            c._streaming_frames.append(data.astype(np.float32) / 32767)
            clock.index += 1
    monkeypatch.setattr(module, 'time', NS(monotonic=lambda: clock.now, sleep=sleep))
    c.drain = lambda: np.concatenate([data for _, _, data in c._frames])
    if lane == 'streaming':
        result = c.wait_for_streaming_release_tail(should_abort=c._drain_stop.is_set)
    else:
        c.drain_after_release()
        result = c._streaming_tail_audit
    return result, clock.index, round((clock.now - 100) * 1000)


@pytest.mark.parametrize('lane', ['streaming', 'batch'])
@pytest.mark.parametrize('body', [.3, .012])
def test_loud_to_soft_and_soft_throughout_admit_the_same_tail(monkeypatch, lane, body):
    result, count, elapsed = release(monkeypatch, lane, body, [.012]*6 + [0]*4, ambient=0.)
    assert (result['reason'], count, elapsed, result['threshold']) == ('silence', 9, 900, .008)


@pytest.mark.parametrize('lane', ['streaming', 'batch'])
def test_trusted_room_noise_releases_promptly(monkeypatch, lane):
    result, count, elapsed = release(monkeypatch, lane, .3, [.017]*15, ambient=.017)
    assert (result['reason'], count, elapsed) == ('silence', 3, 300)
    assert .025 < result['threshold'] < .026
    if lane == 'streaming':
        assert result['threshold_source'] == 'prepress_p20'


@pytest.mark.parametrize('lane', ['streaming', 'batch'])
@pytest.mark.parametrize('ending,reason,count,elapsed', [
    ([.017]*15, 'max', 6, 600), ([], 'max', 0, 600),
    ([0]*4, 'silence', 3, 300), ([.004]*10, 'silence', 3, 300),
    ([.012,0,0,.012,.012,.012,0,0,0], 'max', 6, 600)])
def test_floor_fallback_stall_quiet_subfloor_and_unvoiced_gaps(monkeypatch, lane, ending, reason, count, elapsed):
    result, admitted, wait = release(monkeypatch, lane, .3, ending)
    assert (result['reason'], admitted, wait) == (reason, count, elapsed)


@pytest.mark.parametrize('lane', ['streaming', 'batch'])
def test_cancel_responsive_without_frames(monkeypatch, lane):
    result, count, elapsed = release(monkeypatch, lane, .3, [], cancel=True)
    assert result['reason'] == 'abort' and count == 0 and elapsed == 50


@pytest.mark.parametrize('bad', ['disabled','postpress','incomplete','epoch','sequence'])
def test_ambient_requires_complete_prepress_provenance(bad):
    ambient = tail.Ambient(100., PREBUFFER_FRAMES, bad != 'disabled')
    for i in range(PREBUFFER_FRAMES - (bad == 'incomplete')):
        ambient.observe(NS(seq=i*2 if bad=='sequence' else i,
            device_epoch=i%2 if bad=='epoch' else 0,
            t_capture=100.1+i*.1 if bad=='postpress' else 98.6+i*.1,
            pcm=np.full(FRAME_SIZE,10000,np.int16)), .1)
    assert ambient.threshold(.008,1.5) == (.008,'configured_floor',0.)


@pytest.mark.parametrize('lane', ['streaming', 'batch'])
@pytest.mark.parametrize('cap', [300, 600, 900, 1200])
def test_untrusted_latency_budget_and_soft_ending_survival(monkeypatch, lane, cap):
    monkeypatch.setattr(tail, 'UNTRUSTED_TAIL_CAP_MS', cap)
    result, admitted, wait = release(monkeypatch, lane, .3, [.012]*6 + [0]*6)
    assert admitted == min(cap // 100, 9) and wait == min(cap, 900)
    # At exactly 900 ms the deadline wins over processing the last quiet
    # frame; that copied frame is still admitted to the final PCM.
    assert result['reason'] == ('silence' if cap > 900 else 'max')
    # A quiet room with real pre-press provenance keeps the full quiet run.
    result, admitted, wait = release(monkeypatch, lane, .3, [.012]*6 + [0]*6, ambient=0.)
    assert (result['reason'], admitted, wait) == ('silence', 9, 900)


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('invalid', ['none', 'short', 'overrun', 'epoch', 'tts', 'duck', 'playback'])
def test_ring_provenance_excludes_postpress_or_suppressed_ambient(monkeypatch, streaming, invalid):
    import samsara.audio_engine.dictation_consumer as module
    c, bus = consumer()
    count = 0 if invalid == 'none' else PREBUFFER_FRAMES - (invalid == 'short')
    for i in range(count):
        bus.write(np.full(FRAME_SIZE, 1000, np.int16), float(i), device_epoch=i % 2 if invalid == 'epoch' else 0)
    if invalid == 'tts':
        c._app.audio_coordinator = NS(is_speaking=True)
    elif invalid == 'duck':
        c._app._hold_capture_duck_confirmed_at = 100.
    monkeypatch.setattr(module.thread_registry, 'spawn', lambda *a, **kw: None)
    assert (c.activate_streaming if streaming else c.activate)()
    if invalid == 'overrun':
        for i in range(601):
            bus.write(np.full(FRAME_SIZE, 2000, np.int16), 100.+i*.1, device_epoch=0)
    if invalid == 'playback':
        monkeypatch.setattr(module.preduck, 'excludes', lambda *a: True)
    from samsara.audio_engine.ring import EMPTY
    while (frame := c._reader.read_next()) is not EMPTY:
        c._frame_inside_capture_duck(frame)
    # Plenty of live frames must never complete an incomplete pre-press window.
    for i in range(PREBUFFER_FRAMES):
        bus.write(np.full(FRAME_SIZE, 2000, np.int16), 200.+i*.1, device_epoch=0)
        c._frame_inside_capture_duck(c._reader.read_next())
    assert c._release_threshold(.008, batch=not streaming) == (.008, 'configured_floor', 0.)


@pytest.mark.parametrize('streaming', [False, True])
def test_ring_sequence_proves_prepress_ambient_in_other_clock_domain(monkeypatch, streaming):
    import samsara.audio_engine.dictation_consumer as module
    c, bus = consumer()
    for i in range(PREBUFFER_FRAMES):
        bus.write(np.full(FRAME_SIZE, 900, np.int16), float(i), device_epoch=0)
    monkeypatch.setattr(module.thread_registry, 'spawn', lambda *a, **kw: None)
    assert (c.activate_streaming if streaming else c.activate)()
    for i in range(PREBUFFER_FRAMES):
        c._frame_inside_capture_duck(c._reader.read_next())
    threshold, source, noise = c._release_threshold(.008, batch=not streaming)
    assert source == 'prepress_p20' and noise == pytest.approx(900 / 32767)
    assert threshold == pytest.approx(1.5 * noise)


def test_capture_cutoff_keeps_each_admitted_block_once_and_copies_ring(monkeypatch):
    import samsara.audio_engine.dictation_consumer as module
    c, bus = consumer()
    monkeypatch.setattr(module.thread_registry, 'spawn', lambda *a, **kw: None)
    assert c.activate_streaming()
    c._capture_not_before, c._capture_not_after = 100.1, 100.3
    for i in range(4):
        bus.write(np.full(FRAME_SIZE, 1000+i, np.int16), 100.1+i*.1, device_epoch=0)
    audio = c.stop_streaming()
    assert len(audio) == FRAME_SIZE*2
    np.testing.assert_array_equal(audio, np.repeat(np.array([1001,1002],np.float32)/32767,FRAME_SIZE))
    for _ in range(601):
        bus.write(np.zeros(FRAME_SIZE,np.int16),101.,device_epoch=0)
    assert np.all(audio > 0) and c.stop_streaming() is None


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), -.01, 1.01])
def test_unknown_or_invalid_endpoint_never_becomes_zero(value):
    assert tail.endpoint(value, 1., 20.) is None
    item = NS(start=0.,end=value,words=None)
    assert tail.endpoints([item],1.,20.) == (None,None)


def test_local_endpoints_and_real_word_availability():
    item=NS(start=.1,end=.9,words=[NS(start=.2,end=.8)])
    assert tail.endpoints([item],1.,20.) == (20900,20800)
    item.words=None
    assert tail.endpoints([item],1.,20.) == (20900,None)
    item.words=[NS(start=.2,end=float('nan'))]
    assert tail.endpoints([item],1.,20.) == (20900,None)


def suspect(**changes):
    values=dict(captured_audio_ms=2000,last_speech_end_ms=1800,last_speech_source='vad_p01',
        tail_vad_run_ms=96,decoder_raw_end_ms=1200,decoder_accepted_end_ms=1200,
        decoder_timeline_source='capture_samples')
    values.update(changes)
    return values


@pytest.mark.parametrize('raw,accepted,source,run,expected', [
    (1200,1200,'vad_p01',64,1), (1201,1201,'vad_p01',96,0),
    (1199,1199,'vad_p01',63,0), (1200,1200,'energy_tail_floor',96,0),
    (None,1200,'vad_p01',96,0), (1800,1000,'vad_p01',96,0),
    (float('nan'),1000,'vad_p01',96,0), (2100,1000,'vad_p01',96,0)])
def test_conservative_trigger_and_intentionally_screened_tail(raw,accepted,source,run,expected):
    result=tail.tail_snapshot(suspect(decoder_raw_end_ms=raw,decoder_accepted_end_ms=accepted,
        last_speech_source=source,tail_vad_run_ms=run))
    assert result['possible_missing_tail']==expected
    assert result['decoder_gap_ms'] is None or result['decoder_gap_ms']>=0
    if raw==1800:
        assert result['tail_observation_source']=='accepted_reduction'
        assert result['decoder_accepted_reduction_ms']==800


def model_app(monkeypatch, duration, *, sparse=False):
    def measure(audio,rate):
        if not sparse:
            return speech_presence.Presence(False)
        scores=tuple(.9 if 40 <= i*.032 < 41 else 0. for i in range((len(audio)+511)//512))
        return speech_presence.Presence(True, probabilities=scores,samples=len(audio))
    monkeypatch.setattr(speech_presence,'measure',measure)
    app=NS(config={'dictation':{},'cleanup_mode':'verbatim'},model_rate=16000,device_type='cpu',
        model_lock=threading.Lock(),dictionary_store=NS(custom_vocab=[],_builtin_preferences=NS(disabled=set())))
    joined=evidence.begin(app)
    joined.values['captured_audio_ms']=round(duration*1000)
    audit=FinalAudit();audit._hold_evidence_1121=joined
    return app,joined,audit


@pytest.mark.parametrize('sparse', [False,True])
def test_decoder_original_offsets_after_chunks_and_sparse_trim_without_extra_decode(monkeypatch,sparse):
    duration=60 if sparse else 30
    app,joined,audit=model_app(monkeypatch,duration,sparse=sparse)
    audio=np.zeros(duration*16000,np.float32)
    seen=[]
    def native(data,**params):
        origin=(data.ctypes.data-audio.ctypes.data)/audio.itemsize/16000
        seen.append(origin)
        item=NS(start=.1,end=.8,text=' Synthetic.',words=[NS(start=.2,end=.8,word=' Synthetic.')],
                avg_logprob=0.,compression_ratio=1.,no_speech_prob=0.,temperature=0.)
        return iter([item]),NS(language='en')
    app.model=NS(transcribe=native)
    token=current_audit.set(audit)
    try:
        result=decoder._transcribe(app,audio,{},decoder._deadline(app,duration),threading.Event())
    finally:
        current_audit.reset(token)
    assert result.segments and len(seen)==(1 if sparse else 2)
    assert seen[-1] > (39 if sparse else 24)
    row=joined.snapshot()
    assert row['decoder_raw_end_ms']==row['decoder_accepted_end_ms']==round((seen[-1]+.8)*1000)
    assert row['decoder_word_end_ms']==row['decoder_accepted_end_ms']
    assert row['decoder_timeline_source']=='capture_samples'


def test_retry_slice_and_nested_recovery_endpoints_use_capture_coordinates(monkeypatch):
    app,joined,audit=model_app(monkeypatch,30)
    original=np.zeros(30*16000,np.float32)
    @tail.observed_decode
    def nested(app,audio):
        tail.observe_raw([NS(start=.1,end=1.,words=None)],audio,16000,0,len(audio))
        return decoder.DecodedHold(segments=[NS(start=.1,end=1.,words=None)])
    @tail.observed_decode
    def outer(app,audio):
        nested(app,audio[16000:2*16000])
        item=NS(start=1.1,end=2.,words=None)
        return decoder.DecodedHold(segments=[item])
    token=current_audit.set(audit)
    try:
        outer(app,original[20*16000:25*16000])
    finally:
        current_audit.reset(token)
    row=joined.snapshot()
    assert row['decoder_raw_end_ms']==row['decoder_accepted_end_ms']==22000
    assert row['decoder_word_end_ms'] is None
    assert row['decoder_timeline_source']=='capture_range_samples'
    assert row['decoder_gap_ms'] is None and row['possible_missing_tail']==0


@pytest.mark.parametrize('case', ['native_retry','screened_tail','observation_failure'])
def test_real_decoder_retry_screening_and_observation_failure_preserve_delivery(monkeypatch,case):
    app,joined,audit=model_app(monkeypatch,2)
    scores=tuple(.9 if case=='native_retry' or i<30 else 0. for i in range(63))
    monkeypatch.setattr(speech_presence,'measure',lambda *a: speech_presence.Presence(
        True,probabilities=scores,samples=32000))
    calls=[]
    def segment(text,start,end,**kw):
        values=dict(text=text,start=start,end=end,words=None,avg_logprob=0.,
                    compression_ratio=1.,temperature=0.,no_speech_prob=0.)
        values.update(kw)
        return NS(**values)
    def native(data,**params):
        calls.append(params)
        pieces=[segment(' Synthetic.',.1,.8)]
        if case=='native_retry' and len(calls)==1: pieces=[]
        if case=='screened_tail': pieces.append(segment(' Imagined.',1.8,2.,avg_logprob=-2.,temperature=.4,no_speech_prob=.8))
        return iter(pieces),NS(language='en')
    app.model=NS(transcribe=native)
    if case=='observation_failure':
        def broken(*a,**kw): raise ValueError('synthetic observation failure')
        monkeypatch.setattr(tail,'endpoints',broken)
    token=current_audit.set(audit)
    try:
        result=decoder._transcribe(app,np.zeros(32000,np.float32),{'no_speech_threshold':.6},
            decoder._deadline(app,2),threading.Event())
    finally: current_audit.reset(token)
    assert ''.join(s.text for s in result.segments).strip()=='Synthetic.'
    assert len(calls)==(2 if case=='native_retry' else 1)
    row=joined.snapshot()
    if case=='screened_tail':
        assert row['decoder_raw_end_ms']==2000 and row['decoder_accepted_end_ms']==800
        assert row['decoder_accepted_reduction_ms']==1200 and row['possible_missing_tail']==0
        assert 'unsupported_speech' in audit.dropped
    elif case=='native_retry':
        assert row['decoder_raw_end_ms']==row['decoder_accepted_end_ms']==800
        assert row['decoder_word_end_ms'] is None and calls[-1]['no_speech_threshold'] is None
    else: assert row['decoder_raw_end_ms'] is None


@pytest.fixture
def owner(tmp_path,monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR',str(tmp_path/'profile'))
    app=NS(config={'kept_dictations':{'enabled':False,'maximum':1},'diagnostics':{'keep_incident_holds':False}},
           model_rate=16000,recording=True,_incident_first_1052a=True)
    yield app
    store=getattr(app,'_kept_dictations',None)
    if store and store.worker: store.worker.shutdown(wait=True)
    store=getattr(app,'_incident_store_1052a',None)
    if store:
        if getattr(store,'expiry',None): store.expiry.cancel()
        if store.worker: store.worker.shutdown(wait=True)
    evidence._active=None


def held(app):
    from samsara.hold_recording_recovery import begin_incident
    evidence.begin(app)
    session=NS(app=app,_decode_audio=np.ones(32000,np.float32)*.02,cancel_event=threading.Event(),
               _hold_final_audit=FinalAudit())
    begin_incident(session)
    evidence.get(session).values.update(suspect())
    return session


def test_consent_off_snapshot_in_ram_without_arming_or_saving_audio(owner):
    from samsara.hold_recording_recovery import complete_incident
    session=held(owner)
    kept.capture_held(session,'','')
    complete_incident(session,'')
    store=kept.store_for(owner)
    assert store.last['metadata']['tail_evidence']['possible_missing_tail']==1
    assert not store.armed and not store.folder.exists()
    assert not hasattr(owner,'_incident_store_1052a')


def test_batch_capture_attaches_evidence_to_its_existing_keep_take(owner):
    joined=evidence.begin(owner);joined.values.update(suspect())
    take_id=kept.capture(owner,np.zeros(32000,np.float32),'','', 'hold')
    store=kept.store_for(owner)
    assert store.last['metadata']['id']==take_id
    assert store.last['metadata']['tail_evidence']['hold_seq_id']==joined.hold_seq_id
    assert not store.folder.exists() and not store.armed
    store.keep();store.flush()
    assert store.list()[0]['id']==take_id
    assert store.list()[0]['tail_evidence']['possible_missing_tail']==1


def test_explicit_keep_and_incident_share_take_id_generation_and_numeric_snapshot(owner):
    from samsara.hold_recording_recovery import complete_incident
    owner.config['diagnostics']['keep_incident_holds']=True
    session=held(owner)
    kept.capture_held(session,'','')
    store=kept.store_for(owner)
    take_id=session._kept_take_id
    store.keep()  # The simulated owner's explicit request, never observation.
    complete_incident(session,'')
    store.flush()
    incident=owner._incident_store_1052a
    incident.worker.submit(lambda:None).result(timeout=5)
    row,=store.list()
    incident_row=json.loads(next(incident.folder.glob('*.json')).read_text())
    assert row['id']==incident_row['kept_take_id']==take_id
    assert incident_row['kept_generation']==session._kept_generation==store.generation
    assert row['tail_evidence']['hold_seq_id']==incident_row['hold_seq_id']
    assert 'possible_missing_tail' in incident_row['reasons']
    assert not store.armed and len(list(store.folder.glob('*.wav')))==1


@pytest.mark.parametrize('change', [dict(decoder_raw_end_ms=None),dict(last_speech_source='energy_tail_floor'),
    dict(decoder_raw_end_ms=1800,decoder_accepted_end_ms=1000)])
def test_unknown_energy_or_intentionally_screened_tail_cannot_create_incident(owner,change):
    from samsara.hold_recording_recovery import complete_incident
    owner.config['diagnostics']['keep_incident_holds']=True
    session=held(owner)
    evidence.get(session).values.update(change)
    complete_incident(session,'Synthetic.')
    store=owner._incident_store_1052a
    store.worker.submit(lambda:None).result(timeout=5)
    assert not store.folder.exists()


@pytest.mark.parametrize('barrier', ['clear','delete','rotation','newer','expiry'])
def test_late_observation_never_recreates_or_attaches_to_newer_take(owner,barrier):
    session=held(owner)
    kept.capture_held(session,'','')
    store=kept.store_for(owner)
    store.keep();store.flush()
    take_id=session._kept_take_id
    gate=threading.Event()
    store.worker.submit(lambda:gate.wait(5))
    kept.attach_tail_evidence(session)
    if barrier=='clear': store.clear()
    elif barrier=='delete': store.delete(take_id)
    elif barrier in ('rotation','newer'):
        owner.config['kept_dictations']['enabled']=barrier=='rotation'
        store.remember(np.zeros(1600,np.float32),16000,'','')
    else: store.last['expires']=-1
    gate.set();store.flush()
    before=[p.name for p in store.folder.glob('*')]
    evidence.decoded(session,'Synthetic.')
    store.flush()
    assert before==[p.name for p in store.folder.glob('*')]
    if barrier in ('clear','delete','rotation'): assert not store.audio_path(take_id).exists()
    if barrier in ('rotation','newer'): assert 'tail_evidence' not in store.last['metadata']
    assert not store.armed


def test_late_same_take_metadata_refresh_keeps_pcm_and_ttl(owner):
    session=held(owner)
    kept.capture_held(session,'','')
    store=kept.store_for(owner);store.keep();store.flush()
    wav=store.audio_path(session._kept_take_id)
    before=wav.read_bytes();expiry=store.last['expires']
    evidence.get(session).values['decoder_word_end_ms']=1190
    evidence.decoded(session,'Synthetic.')
    store.flush()
    assert store.list()[0]['tail_evidence']['decoder_word_end_ms']==1190
    assert wav.read_bytes()==before and store.last['expires']==expiry and not store.armed


def test_queued_automatic_evidence_respects_consent_changed_before_worker(owner):
    owner.config['kept_dictations']['enabled']=True
    session=held(owner);kept.capture_held(session,'','')
    store=kept.store_for(owner);store.flush()
    sidecar=store.audio_path(session._kept_take_id).with_suffix('.json')
    before=sidecar.read_bytes()
    gate=threading.Event();store.worker.submit(lambda:gate.wait(5))
    evidence.get(session).values['decoder_word_end_ms']=1190
    kept.attach_tail_evidence(session)
    owner.config['kept_dictations']['enabled']=False
    gate.set();store.flush()
    assert sidecar.read_bytes()==before and not store.armed
