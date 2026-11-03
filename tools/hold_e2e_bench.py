"""Offline human-audio hold benchmark; never launches the application.

Run with an explicit local faster-whisper model directory and output directory.
See bench_fixtures/MANIFEST.md for provenance, calibration and replay limits.
Exit 1 means an acoustic acceptance failure; exit 2 means a harness error.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name('bench_fixtures')
RATE = 16000


def words(text):
    """Case/punctuation tolerant; normalize common corpus spelling conventions."""
    import re
    text = text.casefold().replace('’', "'").replace('mr.', 'mister')
    text = text.replace('mr ', 'mister ')
    return re.findall(r'[a-z0-9]+', text)


def align(reference, hypothesis):
    """Levenshtein alignment, preserving ref indices for time-range recall.

    Recall counts exact normalized matches (substitutions are missing words).
    Insertions count toward WER, preventing loops from looking successful.
    """
    a, b = words(reference), words(hypothesis)
    d = np.zeros((len(a) + 1, len(b) + 1), dtype=np.int32)
    d[:, 0], d[0, :] = np.arange(len(a) + 1), np.arange(len(b) + 1)
    for i, x in enumerate(a, 1):
        for j, y in enumerate(b, 1):
            d[i, j] = min(d[i-1, j-1] + (x != y), d[i-1, j] + 1, d[i, j-1] + 1)
    i, j = len(a), len(b)
    matched, substitutions, deletions, insertions = [], 0, 0, 0
    while i or j:
        if i and j and d[i, j] == d[i-1, j-1] + (a[i-1] != b[j-1]):
            if a[i-1] == b[j-1]:
                matched.append(i-1)
            else:
                substitutions += 1
            i, j = i-1, j-1
        elif i and d[i, j] == d[i-1, j] + 1:
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1
    return dict(reference_words=len(a), hypothesis_words=len(b),
                wer=(substitutions + deletions + insertions)/max(1, len(a)),
                recall=len(matched)/max(1, len(a)), matched_indices=sorted(matched),
                substitutions=substitutions, deletions=deletions, insertions=insertions)


def repeated_run(text):
    tokens = words(text)
    for width in range(1, 9):
        for start in range(len(tokens) - 4*width + 1):
            phrase = tokens[start:start+width]
            if tokens[start:start+4*width] == phrase*4:
                return ' '.join(phrase)
    return None


def read_audio(path):
    audio, rate = sf.read(path, dtype='float32', always_2d=True)
    audio = audio.mean(axis=1)
    if rate != RATE:
        from math import gcd
        common = gcd(rate, RATE)
        audio = resample_poly(audio, RATE//common, rate//common).astype(np.float32)
    return audio


def source(name):
    data = base64.b64decode((FIXTURES / (name + '.b64')).read_bytes())
    return read_audio(io.BytesIO(data))


def resize(audio, samples):
    # Small, documented rate changes retain human speech, never tile it.
    from scipy.signal import resample
    return resample(audio, samples).astype(np.float32)


def rms(audio):
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def peak(audio, level):
    return audio * (level / max(float(np.max(np.abs(audio))), 1e-12))


def mix_room(audio, noise, snr=26):
    # Only stationary room tone repeats, never speech.
    bed = np.resize(noise, len(audio))
    bed = bed * rms(audio) / max(rms(bed), 1e-12) / 10**(snr/20)
    return audio + bed


def build_fixtures(output):
    """Materialize PCM WAVs from checksummed lossless human source assets."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    meta = json.loads((FIXTURES / 'sources.json').read_text(encoding='utf-8'))
    for name, info in meta['assets'].items():
        raw = base64.b64decode((FIXTURES / (name + '.b64')).read_bytes())
        if hashlib.sha256(raw).hexdigest() != info['sha256']:
            raise ValueError('source checksum mismatch: ' + name)
    clips = {i: source('speech_' + str(i)) for i in [0, 1, 2, 3, 4, 5, 6, 7, 14]}
    noise = source('room')
    cases = []

    def add(case, audio, ref, **details):
        for variant, gain in [('peak0995', 1.), ('peak_minus12', 10**(-12/20)/.995)]:
            take = np.clip(audio * gain, -1, 1).astype(np.float32)
            name = case + '_' + variant
            wav = output / (name + '.wav')
            sf.write(wav, take, RATE, subtype='PCM_16')
            calibrated = dict(details)
            if 'quiet_phrase_rms_dbfs' in calibrated:
                calibrated['quiet_phrase_rms_dbfs'] += 20*np.log10(gain)
            # The ring represents complete 100 ms blocks; pad below at replay.
            cases.append(dict(id=name, case=case, wav=str(wav.resolve()), reference=ref,
                              duration_s=len(take)/RATE, peak=float(np.max(np.abs(take))),
                              rms_dbfs=20*np.log10(max(rms(take), 1e-12)),
                              sha256=hashlib.sha256(wav.read_bytes()).hexdigest(), **calibrated))

    intro = resize(np.concatenate([clips[0], clips[1]]), 12*RATE)
    intro = peak(mix_room(intro, noise), .995)
    quiet = clips[14] * (10**(-26/20)/rms(clips[14]))
    # Keep room tone across a thinking pause and the quiet trailing phrase.
    bed = np.resize(noise, round((.8 + len(quiet)/RATE + .3)*RATE))
    bed = bed * 10**(-48/20)/rms(bed)
    tail = np.concatenate([np.zeros(round(.8*RATE)), quiet, np.zeros(round(.3*RATE))])
    a = np.concatenate([intro, tail + bed])
    a = peak(a, .995)
    add('1026', a, ' '.join(meta['text'][str(i)] for i in [0, 1, 14]),
        trailing_reference=meta['text']['14'], trailing_required=['harry', 'quilter'],
        quiet_phrase_start_s=12.8, quiet_phrase_rms_dbfs=20*np.log10(rms(a[round(12.8*RATE):round((12.8+len(quiet)/RATE)*RATE)])))

    # Five consecutive, unique utterances: no sentence tiling. Pause is inside
    # utterance 3, whose clause continues across the 30 s decoder boundary.
    original = np.concatenate([clips[i] for i in range(5)])
    first = resize(original, round(54.2*RATE))
    first[round(27.5*RATE):round(33*RATE)] *= .16
    first = np.concatenate([first[:round(29.2*RATE)], np.zeros(round(.8*RATE)), first[round(29.2*RATE):]])
    last = resize(np.concatenate([clips[i] for i in [5, 6, 7]]), 20*RATE)
    b = peak(mix_room(np.concatenate([first, last]), noise), .995)
    # Source timing audit: the first word (Linnell's, two normalized tokens)
    # is 0..1.14 s in source 4, projecting across 30 s in this recipe. All
    # remaining words, pictures..man, fall wholly inside 30..55 s. Exclude
    # that boundary word; never derive reference text from a hypothesis.
    before = sum(len(words(meta['text'][str(i)])) for i in range(4))
    clause_end = before+len(words(meta['text']['4']))
    tail_start = clause_end
    add('1043', b, ' '.join(meta['text'][str(i)] for i in range(8)),
        score_indices=list(range(before+2, clause_end)),
        clear_tail_indices=list(range(tail_start, sum(len(words(meta['text'][str(i)])) for i in range(8)))),
        score_span_s=[30., 55.], score_method='complete reference words inside 30..55; excludes boundary word Linnell\'s',
        hesitation_s=[29.2, 30.0], quiet_clause_s=[27.5, 33.8])

    c = peak(np.resize(noise, round(1.2*RATE)), .07)
    add('1042', c, '', simulated_hook_stall_ms=20.)
    cue = source('cue_start')
    d = peak(mix_room(clips[2], noise), .995)
    at = round(1.6*RATE)
    d[at:at+len(cue)] += cue[:min(len(cue), len(d)-at)]
    d = np.clip(d, -.995, .995)
    d[at] = .995
    add('1051', d, meta['text']['2'], cue_at_s=1.6, cue_source_sha256=meta['assets']['cue_start']['sha256'])
    (output / 'fixtures.json').write_text(json.dumps(cases, indent=2)+'\n', encoding='utf-8')
    return cases


def build_preduck_fixtures(output):
    """1070: identical mic PCM with independent render references, two levels.

    The last stream word is the unmodified 4.80..5.30 s 'gospel' from
    public source 0. A simulated press occurs at .4 s, duck confirmation at
    .5 s, and source 3 starts at .6 s. The controls deliberately relabel
    that same opening word as the user's onset. Only the stream case has a
    matching render waveform. The overlap control plays different speech;
    a volume lease is not evidence of who spoke into the microphone.
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    meta = json.loads((FIXTURES / 'sources.json').read_text(encoding='utf-8'))
    audio = np.concatenate([source('speech_0')[round(4.8*RATE):round(5.3*RATE)],
                            np.zeros(round(.1*RATE)), source('speech_3')])
    audio = peak(audio, .995)
    cases = []
    for case, media, user_onset in [('1070', True, False),
                                   ('1070_firstword', False, True),
                                   ('1070_overlap', True, True)]:
        for variant, gain in [('peak0995', 1.), ('peak_minus12', 10**(-12/20)/.995)]:
            take = (audio * gain).astype(np.float32)
            wav = output / (case + '_' + variant + '.wav')
            sf.write(wav, take, RATE, subtype='PCM_16')
            render = np.zeros_like(take)
            if case == '1070':
                render[:round(.5*RATE)] = take[:round(.5*RATE)] * .8
            elif case == '1070_overlap':
                render[:round(.5*RATE)] = peak(source('speech_1')[:round(.5*RATE)], .6)*gain
            render_wav = output / (case + '_' + variant + '_render.wav')
            sf.write(render_wav, render, RATE, subtype='FLOAT')
            cases.append(dict(id=wav.stem, case=case, wav=str(wav.resolve()),
                              render_wav=str(render_wav.resolve()),
                              playback_only_blocks=5 if case == '1070' else 0,
                              suppressed_prefix_blocks=4 if case == '1070' else 0,
                              reference=('GOSPEL ' if user_onset else '') + meta['text']['3'],
                              press_s=.4, duck_effective_s=.5, media_ducked=media,
                              user_onset=user_onset, onset_word='gospel',
                              duration_s=len(take)/RATE,
                              sha256=hashlib.sha256(wav.read_bytes()).hexdigest()))
    return cases


class TraceModel:
    """Observe real lazy faster-whisper results without replacing any metric."""
    def __init__(self, model):
        self.native = model
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.native, name)

    def transcribe(self, audio, **params):
        started = time.perf_counter()
        call = dict(audio_s=len(audio)/RATE, params=params, segments=[],
                    audio_sha256=hashlib.sha256(np.asarray(audio).tobytes()).hexdigest())
        self.calls.append(call)
        segments, info = self.native.transcribe(audio, **params)

        def traced():
            try:
                for s in segments:
                    call['segments'].append({k: getattr(s, k, None) for k in (
                        'start', 'end', 'text', 'avg_logprob', 'no_speech_prob',
                        'compression_ratio', 'temperature', 'seek', 'tokens')})
                    yield s
            except Exception as exc:
                call['error'] = type(exc).__name__ + ': ' + str(exc)
                raise
            finally:
                call['elapsed_ms'] = round((time.perf_counter()-started)*1000, 2)
        return traced(), info


class OverlaySink:
    """Inert UI boundary. Never creates a window, pastes, or plays sound."""
    def show(self):
        pass

    def close(self):
        pass

    def update_text(self, *args, **kwargs):
        pass

    def flash_done_and_fade(self, callback):
        callback()


class EmptyDictionary:
    custom_vocab = ()

    def get_initial_prompt(self, **kwargs):
        return None

    def apply_corrections(self, text):
        return text


def wake_cue_probe(app):
    """Execute only the actual wake-start method with inert service facades.

    No dictation import/bootstrap, device creation, sound playback or real
    process interaction. Unsupported new dependencies are harness errors,
    never a silent PASS. This probe may start a *fixture* wake state only.
    """
    from samsara import config_defaults
    from samsara.vocabulary_decode import log_vocabulary
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8-sig'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'start_wake_word_mode')
    sounds = []
    app.model_loaded, app.loading_model = True, False
    app.wake_word_active = False
    app._request_wake_models = lambda **kw: True
    app._start_hands_free_idle_duck = lambda: None
    app._warn_wake_fallback_once = lambda: None
    app._ensure_wake_consumer = lambda *a: None
    app._request_icon_chase = lambda *a: None
    app.set_app_state = lambda **kw: [setattr(app, k, v) for k, v in kw.items()]
    app.play_sound = lambda name, **kw: sounds.append(name)
    namespace = dict(logger=logging.getLogger('hold_bench'), time=time,
                     config_defaults=config_defaults, log_vocabulary=log_vocabulary)
    # Include module imports only if the selected method uses an imported helper.
    # No implicit import/exec of the rest of dictation.py is permitted.
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    exec(compile(module, '<isolated wake-start source>', 'exec'), namespace)
    namespace['start_wake_word_mode'](app)
    return dict(sounds_requested=sounds, cue_while_recording=bool(sounds),
                wake_armed=app.wake_word_active,
                method_sha256=hashlib.sha256(ast.dump(method).encode()).hexdigest())


def run_case(case, model, output, *, device='cpu', partials=False, drop_playback_preroll=True):
    """Real FrameBus -> real consumer -> StreamingWorker -> session/history.

    Session construction substitutes only the UI/desktop boundary. All decode,
    speech gates, window recovery, cleanup, accepted-preview tail policy and
    final delivery decisions are production methods. Replay advances audio
    time in batches; it does not claim wall-clock device/OS-hook integration.
    """
    from samsara.streaming import StreamingSession, StreamingWorker
    from samsara.audio_engine.ring import FrameBus
    from samsara.audio_engine.frame import FRAME_SIZE
    from samsara.audio_engine.dictation_consumer import DictationSessionConsumer
    from samsara.history import HistoryManager
    from samsara.transcription_params import get_transcription_params
    from samsara.formatting_tokens import apply_formatting_tokens
    from samsara import config_defaults

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    audio = read_audio(case['wav'])
    # Explicitly isolated SQLite, never the owner's database or configuration.
    history = HistoryManager(':memory:')
    calls_at_start = len(model.calls)
    notices, partial_results = [], []
    config = dict(model_size=Path(str(getattr(model.native, 'model_path', 'local'))).name,
                  performance_mode=config_defaults.DEFAULTS['performance_mode'],
                  language='en', mode='hold', hotkey='mouse5', auto_paste=False,
                  cleanup_mode='clean', diagnostics={'write_jsonl': False},
                  smart_corrections={'modes': {'streaming': False}})
    app = NS(config=config, model=model, model_rate=RATE, device_type=device,
             model_lock=threading.Lock(), dictionary_store=EmptyDictionary(),
             recording=True, _hotkey_recording=True, history_db=history,
             _hold_down_mouse=True, _main_hotkey_source='mouse', _stop_in_flight=False,
             _show_outcome_chip=lambda *a, **kw: notices.append([a, kw]),
             add_to_history=lambda *a, **kw: None, _notify_main_window=lambda *a: None,
             _get_foreground_app=lambda: 'Public corpus fixture',
             _schedule_ui=lambda fn, *a: fn(*a), process_transcription=lambda t: t,
             _apply_formatting_tokens=apply_formatting_tokens,
             _on_streaming_session_finished=lambda *a: None)
    # A known public-fixture destination bypasses OS focus privacy inspection;
    # actual HistoryManager insertion/schema and session failure rows are real.
    app._log_history = lambda **kw: history.add(app_context='Public corpus fixture', **kw)
    app.get_transcription_params = lambda **kw: get_transcription_params(
        config, language='en', initial_prompt=None, no_speech_threshold=.6, log_prob_threshold=-1.)
    bus = FrameBus()
    engine = NS(register_consumer=bus.new_reader, _overflow_count=0)
    consumer = DictationSessionConsumer(engine, app)
    timed_onset = 'duck_effective_s' in case
    epoch = time.perf_counter() - case.get('duck_effective_s', 0.)
    if timed_onset:
        # 1070 is experimental/off in production; bench opts in explicitly.
        config['dictation'] = {'drop_playback_preroll': drop_playback_preroll}
        from samsara.hold_capture_onset import HoldOnset
        from samsara.speech_presence import HoldEvidence
        app._speech_presence_capture = HoldEvidence(epoch + case['press_s'])
        app._hold_capture_duck_token = -1
        app._hold_capture_duck_confirmed_at = epoch + case['duck_effective_s']
        app._hold_capture_onset = HoldOnset(
            -1, epoch + case['press_s'], app._hold_capture_duck_confirmed_at,
            not case['media_ducked'], lambda *a, **kw: None)
        render = read_audio(case['render_wav'])[:round(case['duck_effective_s']*RATE)]
        # Substitute only the capture device: matching and policy are real.
        engine._hold_render_reference = NS(snapshot=lambda: (
            epoch + case['duck_effective_s'] + 1e-6, render.copy()))
    app._dictation_consumer = consumer
    session = StreamingSession.__new__(StreamingSession)
    session.app, session.start_time = app, time.time()
    session.stop_event, session.cancel_event = threading.Event(), threading.Event()
    session._state, session._state_lock = session.STATE_STREAMING, threading.Lock()
    session._capture_cleanup_lock = threading.Lock()
    session._capture_cleaned = session._finished_notified = False
    session._direct_paste = session._last_pasted = session._parking_enabled = False
    session._tutorial_owner, session._last_partial = None, ''
    session._overlay = OverlaySink()
    app._streaming_session = session
    worker = StreamingWorker(session)
    session._worker = worker
    cue_result = wake_cue_probe(app) if case['case'] == '1051' else None
    started = time.perf_counter()
    tail_thread, tail_errors = None, []
    try:
        def write_block(start):
            block = np.zeros(FRAME_SIZE, dtype=np.int16)
            chunk = audio[start:start+FRAME_SIZE]
            block[:len(chunk)] = np.round(np.clip(chunk, -1, 1)*32767).astype(np.int16)
            stamp = epoch + (start+FRAME_SIZE)/RATE + 1e-6 if timed_onset else time.perf_counter()
            bus.write(block, stamp, 0)

        live_start = round(case['duck_effective_s']*RATE) if timed_onset else 0
        for start in range(0, live_start, FRAME_SIZE):
            write_block(start)
        if not consumer.activate_streaming():
            raise RuntimeError('fixture capture did not activate')
        checkpoints = sorted({x for x in [13., 30., 55., len(audio)/RATE-.5]
                              if .5 <= x < len(audio)/RATE}) if partials else []
        next_checkpoint = 0
        release_index = max(0, ((len(audio)-round(.3*RATE))//FRAME_SIZE)*FRAME_SIZE)

        def release_tail():
            try:
                from samsara.constants import HOLD_RELEASE_TAIL_SPEECH_THRESHOLD
                tail = consumer.wait_for_streaming_release_tail(
                    silence_ms=300, max_tail_ms=1200,
                    speech_threshold=HOLD_RELEASE_TAIL_SPEECH_THRESHOLD,
                    should_abort=session.cancel_event.is_set)
                session._release_tail_signals = dict(tail, speech_floor=HOLD_RELEASE_TAIL_SPEECH_THRESHOLD)
            except Exception as exc:
                tail_errors.append(exc)

        for start in range(live_start, len(audio), FRAME_SIZE):
            if start == release_index and case['case'] != '1042':
                deadline = time.monotonic()+2
                while consumer._reader._read_cursor < bus.write_cursor and time.monotonic() < deadline:
                    time.sleep(.002)
                tail_thread = threading.Thread(target=release_tail, name='fixture-release-tail')
                tail_thread.start()
                time.sleep(.02)
            write_block(start)
            # Bound replay bursts well below the ring capacity. A real drain
            # thread copies PCM; yielding lets it run without 75 seconds' sleep.
            if tail_thread is not None:
                time.sleep(.1)
            elif start % (FRAME_SIZE*20) == 0:
                time.sleep(.006)
            elapsed_audio = (start+FRAME_SIZE)/RATE
            if next_checkpoint < len(checkpoints) and elapsed_audio >= checkpoints[next_checkpoint]:
                deadline = time.monotonic()+2
                while consumer._reader._read_cursor < bus.write_cursor and time.monotonic() < deadline:
                    time.sleep(.002)
                t0 = time.perf_counter()
                text = worker._transcribe_partial()
                if text:
                    session.on_partial(text)
                partial_results.append(dict(audio_s=elapsed_audio, text=text,
                                            elapsed_ms=(time.perf_counter()-t0)*1000))
                next_checkpoint += 1
        if tail_thread is not None:
            tail_thread.join(timeout=3)
            if tail_thread.is_alive() or tail_errors:
                raise RuntimeError('release-tail policy did not complete: '+str(tail_errors))
        if timed_onset:
            # Accelerated audio time extends past wall time. Bound the capture
            # at the replayed release, before finalize snapshots its PCM.
            consumer._capture_not_after = epoch + np.ceil(len(audio)/FRAME_SIZE)*FRAME_SIZE/RATE + 2e-6
        if case['case'] == '1042':
            from samsara.mouse_hook import MouseHook
            from samsara.hotkeys import HotkeyCluster
            # Queue the release before the dispatcher is deliberately stalled.
            hook = MouseHook(lambda name, pressed: HotkeyCluster._on_main_hotkey_mouse(app, pressed))
            app.stop_recording = session.finalize
            hook._events.put(('mouse5', False, time.perf_counter(), False))
            stall_start = time.perf_counter()
            time.sleep(case['simulated_hook_stall_ms']/1000)
            hook.dispatch_pending()
            if not session.stop_event.wait(3):
                raise RuntimeError('queued release never finalized the fixture hold')
            hook_ms = (time.perf_counter()-stall_start)*1000
        else:
            session.finalize()
            hook_ms = None
        worker._final_pass()
        job = getattr(app, '_hold_decode_job', None)
        if job is not None and not job.done.wait(60):
            raise RuntimeError('abandoned native decode still running; do not reuse model')
        rows = [dict(r) for r in history.recent()]
        delivered = '\n'.join(r['display_text'] for r in reversed(rows) if r['status'] == 'success')
        reference = case.get('reference')
        metric = align(reference, delivered) if reference is not None else None
        verdicts = {}
        if case['case'] == '1026':
            # The rare name must occur in the trailing clause, not an earlier
            # occurrence of Quilter in the introductory reference.
            tail = words(delivered)[-12:]
            required = case['trailing_required']
            verdicts['trailing_phrase_retained'] = any(tail[i:i+len(required)] == required for i in range(len(tail)))
        elif case['case'] == '1043':
            selected = set(case['score_indices'])
            recall = len(selected & set(metric['matched_indices']))/len(selected)
            metric['recall_30_55'] = recall
            metric['range_reference_words'] = len(selected)
            verdicts['recall_30_55_at_least_90pct'] = recall >= .9
            tail_selected = set(case['clear_tail_indices'])
            tail_recall = len(tail_selected & set(metric['matched_indices']))/len(tail_selected)
            metric['clear_tail_recall_55_75'] = tail_recall
            # A narrowly passing middle-range score must not hide complete
            # loss of the following clear control speech.
            verdicts['clear_tail_retained_at_least_90pct'] = tail_recall >= .9
        elif case['case'] == '1042':
            retry = getattr(app, '_hold_retry_audio', None)
            verdicts['zero_blank_history_rows'] = bool(rows) and all(r['display_text'].strip() for r in rows)
            stopped = getattr(session, '_stopped_audio_1042', None)
            verdicts['audio_slot_retained'] = retry is not None and stopped is not None and np.array_equal(retry.audio, stopped)
            verdicts['release_after_stall'] = hook_ms > 5 and session.stop_event.is_set()
        elif case['case'] == '1051':
            verdicts['no_cue_while_recording'] = not cue_result['cue_while_recording'] and cue_result['wake_armed']
            verdicts['no_repeated_token_run'] = repeated_run(delivered) is None
            verdicts['speech_delivered'] = bool(delivered.strip())
        elif timed_onset:
            tokens = words(delivered)
            if case['user_onset']:
                verdicts['user_first_word_retained'] = bool(tokens) and tokens[0] == case['onset_word']
            else:
                verdicts['no_stream_word'] = case['onset_word'] not in tokens
            verdicts['following_speech_retained'] = align(case['reference'], delivered)['recall'] >= .9
        else:
            before = case.get('before')
            verdicts['delivered_not_empty'] = bool(delivered.strip())
            if before is not None:
                verdicts['before_words_retained'] = align(before, delivered)['recall'] == 1.
        verdicts['no_capture_loss'] = not consumer.capture_loss
        captured = getattr(session, '_stopped_audio_1042', None)
        expected = np.pad(np.round(np.clip(audio, -1, 1)*32767).astype(np.int16),
                          (0, (-len(audio)) % FRAME_SIZE)).astype(np.float32)/32767.
        if timed_onset:
            block_starts = epoch + np.arange(len(expected)//FRAME_SIZE)*FRAME_SIZE/RATE + 1e-6
            allowed = block_starts >= consumer._capture_not_before
            if drop_playback_preroll:
                expected[:case['suppressed_prefix_blocks']*FRAME_SIZE] = 0
            expected = expected.reshape(-1, FRAME_SIZE)[allowed].ravel()
        pcm_check = 'selected_input_pcm_preserved' if timed_onset else 'complete_input_pcm_preserved'
        verdicts[pcm_check] = captured is not None and np.array_equal(captured, expected)
        verdicts['delivery_completed'] = session._state == session.STATE_DONE
        result = dict(case=case, delivered_text=delivered, metrics=metric,
                      checks=verdicts, passed=all(verdicts.values()),
                      elapsed_ms=(time.perf_counter()-started)*1000, hook_stall_ms=hook_ms,
                      history=rows, audio_retained=hasattr(app, '_hold_retry_audio'),
                      decode_failure=getattr(session, '_decode_failure', None),
                      partials=partial_results, native_calls=model.calls[calls_at_start:],
                      cue_probe=cue_result, notices=notices,
                      final_state=session._state)
        result['release_tail'] = getattr(session, '_release_tail_signals', None)
        if timed_onset:
            result['capture_boundary_s'] = consumer._capture_not_before - epoch
            prefix = consumer._playback_prefix
            result['playback_prefix'] = dict(removed=prefix.removed if prefix else 0,
                                            measurements=prefix.measurements if prefix else [],
                                            enabled=drop_playback_preroll)
        (output / 'result.json').write_text(json.dumps(result, indent=2, default=str)+'\n', encoding='utf-8')
        return result
    finally:
        if tail_thread is not None:
            tail_thread.join(timeout=3)
        consumer.stop_streaming()
        history.close()


def isolate(output):
    """Must run before importing samsara, including in the slow test."""
    home = Path(output).resolve() / 'isolated_profile'
    home.mkdir(parents=True, exist_ok=True)
    os.environ['SAMSARA_HOME_DIR'] = str(home)
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    return home


def load_model(path, device='cpu', compute_type='int8'):
    if not (Path(path) / 'model.bin').is_file():
        raise ValueError('--model must be a local faster-whisper directory containing model.bin')
    from faster_whisper import WhisperModel
    return TraceModel(WhisperModel(str(path), device=device, compute_type=compute_type,
                                  cpu_threads=4, local_files_only=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, help='explicit local model directory; no downloads')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--compute-type', default='int8')
    parser.add_argument('--partials', action='store_true', help='exercise real partials at fixed audio checkpoints, including on CPU')
    parser.add_argument('--case', choices=['1026', '1043', '1042', '1051', '1070', '1070_firstword', '1070_overlap'], action='append')
    parser.add_argument('--variant', choices=['peak0995', 'peak_minus12'])
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--revision', default='unspecified', help='source revision label supplied by the caller')
    parser.add_argument('--debug-holds', type=Path, help='explicit sanitized export only; live ~/.samsara is forbidden')
    args = parser.parse_args(argv)
    isolate(args.output)
    sys.path.insert(0, str(ROOT))
    logging.basicConfig(filename=args.output / 'bench.log', level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    cases = build_fixtures(args.output / 'fixtures') + build_preduck_fixtures(args.output / 'fixtures')
    if args.prepare_only:
        print(f'Prepared {len(cases)} real-human audio WAVs in {args.output / "fixtures"}')
        return 0
    if args.model is None:
        parser.error('--model is required unless --prepare-only')
    if args.case:
        cases = [c for c in cases if c['case'] in args.case]
    if args.variant:
        cases = [c for c in cases if c['id'].endswith(args.variant)]
    if args.debug_holds:
        export = args.debug_holds.resolve()
        if '.samsara' in {p.casefold() for p in export.parts}:
            parser.error('--debug-holds must be an exported copy outside .samsara')
        for p in sorted(export.glob('*.wav')):
            before = p.with_suffix('.before.txt')
            cases.append(dict(id='export_'+p.stem, case='regression', wav=str(p), reference=None,
                              before=before.read_text(encoding='utf-8') if before.exists() else None))
    model = load_model(args.model, args.device, args.compute_type)
    results, table = [], ['case | result | recall | WER | elapsed ms | failed checks',
                          '--- | --- | --- | --- | --- | ---']
    print('case | result | recall | WER | elapsed ms | failed checks', flush=True)
    for case in cases:
        try:
            result = run_case(case, model, args.output / case['id'], device=args.device, partials=args.partials)
        except Exception as exc:
            logging.exception('Harness error in %s', case['id'])
            result = dict(case=case, passed=False, harness_error=type(exc).__name__+': '+str(exc))
        results.append(result)
        metric = result.get('metrics') or {}
        line = (f"{case['id']} | {'ERROR' if result.get('harness_error') else 'PASS' if result['passed'] else 'FAIL'} | "
              f"{metric.get('recall', float('nan')):.3f} | {metric.get('wer', float('nan')):.3f} | "
              f"{result.get('elapsed_ms', 0):.0f} | "
              f"{result.get('harness_error') or ', '.join(k for k,v in result['checks'].items() if not v)}")
        print(line, flush=True)
        table.append(line)
        (args.output / 'table.md').write_text('\n'.join(table)+'\n', encoding='utf-8')
        report = dict(model=str(args.model.resolve()), device=args.device, compute_type=args.compute_type,
                      revision=args.revision,
                      partial_replay=args.partials, results=results,
                      versions={p: __import__('importlib.metadata', fromlist=['version']).version(p)
                                for p in ['faster-whisper', 'ctranslate2', 'numpy', 'scipy', 'soundfile']})
        (args.output / 'results.json').write_text(json.dumps(report, indent=2, default=str)+'\n', encoding='utf-8')
    return 2 if any(r.get('harness_error') for r in results) else int(any(not r['passed'] for r in results))


if __name__ == '__main__':
    raise SystemExit(main())
