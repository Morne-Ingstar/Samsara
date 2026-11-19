"""Declarative Mic Lab certification, offline judges and adaptive take runner.

No device, profile, Qt or model is opened on import. Capture and reference
decode are injected so the same battery supports the CLI, a window and replay.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import re

import numpy as np

from samsara import mic_lab_dsp as dsp
from samsara.mic_lab_endpoint import Observation, volume_delta


@dataclass(frozen=True)
class Step:
    id: str
    instruction: str
    target: str
    distance_note: str
    takes: int
    judges: tuple[str, ...]
    seconds: float = 7.
    certification: bool = True


# PLACEHOLDER COPY (1267): instructions/labels chosen from the revised review.
BATTERY = (
    Step('native_probe', 'Remain silent during the native and ring probes.', '', 'Normal working distance', 1,
         ('noise', 'rumble_dc', 'tones', 'channels', 'native_clipping', 'gpu_whine'), 5.),
    Step('baseline', 'Speak in your normal conversational voice.',
         'The bright morning sun warmed the stone wall outside.', 'Normal working distance', 2,
         ('speech', 'snr', 'wer', 'dropouts', 'gain')),
    Step('cold_onset', 'Start speaking immediately at the Speak now cue.',
         'Stop right there and check the first word.', 'Normal working distance', 2,
         ('first_word', 'onset', 'wer', 'dropouts', 'gain')),
    Step('fricatives_hf', 'Speak clearly at a normal pace.',
         'Six thin fins shifted fast past the cheap seats.', 'Normal working distance', 2,
         ('bandwidth', 'wer', 'dropouts', 'gain')),
    Step('plosives_norm', 'Use your normal voice with crisp consonants.',
         'Pat bought five pale caps and a big copper pot.', 'Normal working distance', 2,
         ('clipping', 'wer', 'dropouts', 'gain')),
    Step('quiet_edges', 'Start softly and let the end trail off naturally.',
         'Well, maybe we left the soft note past eight.', 'Normal working distance', 1,
         ('edges', 'boundary', 'wer', 'dropouts', 'gain')),
    Step('dyn_contrast', 'Follow the normal, LOUD, three-second silence, then quiet cues.',
         'Normal level now. LOUD PEAK ONE TWO! quiet voice returns.', 'Normal working distance', 1,
         ('agc', 'clipping', 'drift', 'wer', 'dropouts', 'gain'), 15.),
    Step('proximity_pop', 'Move to 2–3 cm from the grille and use normal volume.',
         'Please put the paper packet back in place.', '2–3 cm from capsule/grille', 1,
         ('clipping', 'proximity', 'wer', 'dropouts', 'gain')),
    Step('off_axis_2x', 'Move to twice your normal distance and turn 45 degrees away.',
         'Turning slightly away drops the high frequencies first.', '2x normal distance, 45 degrees off-axis', 1,
         ('level_drop', 'snr', 'bandwidth', 'wer', 'dropouts', 'gain')),
    Step('desk_typing', 'Speak normally while typing steadily; this checks your mounting.',
         'Typing while speaking checks shock mount isolation.', 'Normal working distance', 1,
         ('rumble_dc', 'wer', 'dropouts', 'gain'), certification=False),
    Step('handsfree_e2e', 'Speak two sentences with a one-second pause, without a hotkey.',
         'First sentence hands free. Second sentence clean.', 'Normal working distance', 1,
         ('wer', 'dropouts', 'gain'), 10., certification=False),
)
STEPS = {step.id: step for step in BATTERY}
RANK = {'Good': 0, 'Watch': 1, 'Bad': 2}


def _words(text):
    return re.findall(r"\w+(?:['’]\w+)*", str(text).casefold())


def word_score(target, text):
    a, b = _words(target), _words(text)
    row = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        out = [i]
        for j, right in enumerate(b, 1):
            out.append(min(out[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
        row = out
    errors = row[-1]
    return dict(errors=errors, words=len(a), wer=errors / max(1, len(a)),
                first_word=bool(a and b and a[0] == b[0]),
                last_word=bool(a and b and a[-1] == b[-1]))


def _measurement(value, verdict, cause):
    return dsp.Measurement(value, verdict, cause)


def _unknown(cause):
    return _measurement(None, 'Watch', cause)


def _worst(measurements):
    return max((m.verdict for m in measurements), key=RANK.get, default='Watch')


def _native_clipping(probe):
    audio, rate = probe['audio'], probe['sample_rate']
    measured = dsp.clipping(audio, rate)
    if measured.value is None:
        return measured
    # 1267: native float silence can be smaller than the DSP judge's absolute
    # 1e-5 equality tolerance. Scale ONLY the plateau analysis to a .5 peak per
    # channel; use original PCM for full-scale counts and reported peak levels.
    # This still catches exact pre-scaled plateaus without calling random low
    # level noise a flat top. Stored/provided PCM is never modified.
    x = np.asarray(audio).reshape(len(audio), -1)
    peaks = np.max(np.abs(x), axis=0)
    normalized = x * (.5 / np.where(peaks > 0, peaks, 1))
    plateaus = dsp.clipping(normalized, rate).value
    original = measured.value if isinstance(measured.value, list) else [measured.value]
    plateau_rows = plateaus if isinstance(plateaus, list) else [plateaus]
    values = [dict(a, flat_top_runs=b['flat_top_runs'], runs=b['runs']) for a, b in zip(original, plateau_rows)]
    verdict = 'Bad' if any(v['flat_top_runs'] >= 4 or v['full_scale_samples'] > 20 for v in values) else 'Watch' if any(v['flat_top_runs'] or v['full_scale_samples'] >= 3 for v in values) else 'Good'
    return _measurement(values[0] if len(values) == 1 else values, verdict,
                        'Native silence clipping; quiet peak level is judged by the noise-floor check')


def offline_reference(take, decode):
    """Decode EVERY observed slice at zero temperature without another VAD pass.

    Repeated attempts at identical coordinates are decoded but only the last
    contributes to the reference. Overlapping *different* cuts have ambiguous
    joins; keep their individual texts and decline a clean pipeline attribution.
    """
    import hashlib
    chosen = {}
    rows = []
    for index, item in enumerate(take.get('slices', [])):
        params = dict(item.get('params', {}), temperature=0., vad_filter=False,
                      condition_on_previous_text=False)
        params.pop('vad_parameters', None)
        try:
            text = str(decode(item['audio'].copy(), params) or '')
            error = None
        except Exception as exc:
            text, error = '', type(exc).__name__
        a, b = item.get('capture_start'), item.get('capture_end')
        key = (a, b) if a is not None and b is not None else ('pcm', hashlib.sha256(item['audio'].tobytes()).hexdigest())
        rows.append(dict(slice=index, text=text, temperature=0., capture_start=a, capture_end=b, error_type=error))
        chosen[key] = index
    selected = sorted(chosen.values())
    mapped = [rows[i] for i in selected if rows[i]['capture_start'] is not None]
    ordered = sorted(mapped, key=lambda r: r['capture_start'])
    ambiguous = any(a['capture_end'] > b['capture_start'] for a, b in zip(ordered, ordered[1:]))
    return dict(text=' '.join(rows[i]['text'].strip() for i in selected).strip(),
                slices=rows, status='reference_failed' if any(r['error_type'] for r in rows) else
                    'ambiguous_overlap' if ambiguous else 'ok' if rows else 'no_decoder_input')


@dataclass
class JudgeContext:
    silence: object = None
    sample_rate: int = 16000
    baseline: object = None


def judge_take(step, take, context):
    raw, rate = take['raw'], take['sample_rate']
    metrics = {}
    silence = context.silence if context.sample_rate == rate else None
    noise = dsp.noise_floor(silence, rate) if silence is not None else _unknown('Matched ring silence is missing')
    floor = noise.value
    # Live and deterministic text are separate from the acoustic measurements.
    live = word_score(step.target, take.get('text', '')) if step.target else None
    reference = take.get('offline', {})
    offline = word_score(step.target, reference.get('text', '')) if step.target and reference.get('status') == 'ok' else None
    pipeline = bool(live and live['errors'] and offline and offline['errors'] == 0)
    if step.target:
        metrics['reference'] = _measurement(reference.get('status'),
            'Good' if reference.get('status') == 'ok' else 'Watch',
            'Temperature-zero reference on the observed decoder inputs; unavailable/ambiguous references cannot certify a clean take')
    for judge in step.judges:
        if judge == 'noise':
            metrics[judge] = dsp.noise_floor(raw, rate)
        elif judge == 'rumble_dc':
            metrics[judge] = dsp.rumble_dc(raw, rate)
        elif judge == 'tones':
            metrics[judge] = dsp.tonal_prominence(raw, rate)
        elif judge in ('channels', 'native_clipping'):
            probe = take.get('native_idle')
            metrics[judge] = ((dsp.channel_balance(probe['audio'], probe['sample_rate']) if judge == 'channels' else _native_clipping(probe))
                              if probe else _unknown('Native-rate probe is missing'))
        elif judge == 'gpu_whine':
            idle, load = take.get('native_idle'), take.get('native_load')
            if idle and load:
                values = [dsp.tonal_prominence(p['audio'], p['sample_rate']) for p in (idle, load)]
                metrics[judge] = _measurement([asdict(m) for m in values], _worst(values), 'Idle and inference-load tonal noise; a difference suggests USB/power coupling')
            else:
                metrics[judge] = _unknown('Inference-load native probe is missing')
        elif judge == 'speech':
            metrics[judge] = dsp.speech_level(raw, rate, noise_dbfs=floor)
        elif judge == 'snr':
            metrics[judge] = dsp.speech_snr(raw, silence, rate) if silence is not None else noise
        elif judge == 'bandwidth':
            metrics[judge] = dsp.upper_band_ratio(raw, rate)
        elif judge == 'clipping':
            metrics[judge] = dsp.clipping(raw, rate)
        elif judge == 'dropouts':
            metrics[judge] = dsp.dropouts(raw, rate, input_overflows=take.get('input_overflows', 0))
            receipt = take.get('capture_receipt') or {}
            if receipt.get('losses') or receipt.get('drain_incomplete'):
                metrics[judge] = _measurement(receipt.get('losses'), 'Bad', 'Capture has a ring/device discontinuity or incomplete drain')
        elif judge == 'gain':
            before, after = (take.get(k) for k in ('gain_before', 'gain_after'))
            delta = volume_delta(Observation(before), Observation(after)) if before is not None and after is not None else Observation(reason='Endpoint gain unavailable')
            metrics[judge] = (_measurement(delta.value['delta'] * 100, 'Bad' if delta.value['changed'] else 'Good', 'Windows input gain moved by at least two percentage points; check other apps')
                              if delta.known else _unknown(delta.reason))
        elif judge == 'wer':
            wer = live['wer']
            metrics[judge] = _measurement(live, 'Good' if not wer else 'Watch' if wer <= .1 else 'Bad',
                'Live text differs; clean temperature-zero reference identifies pipeline variance' if pipeline else 'Scripted word error rate; acoustic evidence is needed to blame the microphone')
        elif judge in ('first_word', 'edges'):
            survived = live['first_word'] and (judge == 'first_word' or live['last_word'])
            metrics[judge] = _measurement(survived, 'Good' if survived else 'Bad', 'Target first/last word survival')
        elif judge == 'onset':
            trigger = take.get('trigger_sample')
            metrics[judge] = (dsp.onset_delay(raw, rate, noise_dbfs=floor, trigger_sample=trigger)
                if floor is not None and trigger is not None and 0 <= trigger < len(raw)
                else _unknown('Press coordinate or matched noise floor is unavailable'))
        elif judge == 'boundary':
            cuts = [(s.get('capture_start'), s.get('capture_end')) for s in take.get('slices', [])]
            origin = (take.get('raw_coordinates') or {}).get('capture_start')
            if cuts and origin is not None and floor is not None and all(a is not None and b is not None for a, b in cuts):
                a, b = min(x[0] for x in cuts) - origin, max(x[1] for x in cuts) - origin
                margin = dsp.boundary_margin(raw, rate, cut_range=(a, b), noise_dbfs=floor)
                lost = not live['first_word'] or not live['last_word']
                metrics[judge] = _measurement(margin.value, margin.verdict if lost else 'Good',
                    'Missing edge word plus energy outside decoder cut suggests pipeline trimming' if lost and margin.verdict == 'Bad'
                    else 'Boundary energy alone does not prove a word was cut')
            else:
                metrics[judge] = _unknown('Exact mapped decoder boundaries or matched noise floor are unavailable')
        elif judge == 'agc':
            regions = take.get('dynamic_regions')
            try:
                metrics[judge] = dsp.agc_pumping(raw, rate, **regions) if regions else _unknown('Timed dynamic-contrast regions are unavailable')
            except ValueError:
                metrics[judge] = _unknown('Dynamic-contrast timing was incomplete; no AGC conclusion')
        elif judge == 'drift':
            timing = take.get('clock_timing')
            try:
                metrics[judge] = dsp.clock_drift(**timing) if timing else _unknown('Aligned capture frame/wall-clock timing is unavailable')
            except ValueError:
                metrics[judge] = _unknown('Clock timing was incomplete; no drift conclusion')
        elif judge in ('level_drop', 'proximity'):
            baseline = context.baseline
            if baseline is None:
                metrics[judge] = _unknown('Baseline take is unavailable')
            elif judge == 'level_drop':
                levels = [dsp.speech_level(p, rate).value for p in (baseline, raw)]
                delta = float(np.min(np.atleast_1d(levels[1]) - np.atleast_1d(levels[0]))) if all(v is not None for v in levels) else None
                metrics[judge] = _measurement(delta, 'Good' if delta is not None else 'Watch', 'Level change from baseline is descriptive; SNR determines remaining headroom')
            else:
                metrics[judge] = dsp.rumble_dc(raw, rate)
    if step.id == 'native_probe':
        probes = [take[k] for k in ('native_idle', 'native_load') if k in take]
        if any(p.get('input_overflow') for p in probes):
            metrics['native_overflow'] = _measurement(True, 'Bad', 'Native probe reported an input overflow')
    verdict = _worst(metrics.values())
    cause = 'pipeline' if pipeline else 'undetermined' if live and live['errors'] else 'mic_or_environment' if verdict != 'Good' else 'none'
    boundary = metrics.get('boundary')
    if boundary and boundary.verdict == 'Bad':
        cause = 'pipeline'
    return dict(verdict=verdict, cause=cause, live=live, offline=offline,
                metrics={key: asdict(value) for key, value in metrics.items()})


def step_verdict(takes):
    votes = [t['judgment']['verdict'] for t in takes]
    counts = Counter(votes)
    verdict, count = counts.most_common(1)[0]
    if len(votes) == 3 and count == 1:
        verdict = 'Watch'
    return dict(verdict=verdict, votes=votes, tie_breaker=len(takes) == 3,
                agreement=count >= 2 if len(votes) > 1 else True,
                causes=sorted({t['judgment']['cause'] for t in takes}))


class BatteryRunner:
    """State machine: capture -> reference -> judge -> save -> repeat/advance.

    capture(step, number) returns a complete observer payload. decode accepts
    (audio, params), save(step, number, take) persists it, progress(step, verdict)
    reports a finished step. Sandbox selection never changes the core grade.
    """
    def __init__(self, *, sandbox=()):
        unknown = set(sandbox) - {s.id for s in BATTERY if not s.certification}
        if unknown:
            raise ValueError('Unknown sandbox step')
        self.steps = tuple(s for s in BATTERY if s.certification or s.id in sandbox)
        self.state = 'ready'
        self.context = JudgeContext()
        self.results = []

    def run(self, capture, decode, *, save=None, progress=None):
        if self.state != 'ready':
            raise RuntimeError('BatteryRunner runs once; create a new runner for another run')
        try:
            for step in self.steps:
                takes, needed = [], step.takes
                while len(takes) < needed:
                    self.state = 'capture'
                    take = capture(step, len(takes) + 1)
                    take.update(step_id=step.id, number=len(takes) + 1)
                    self.state = 'reference'
                    take['offline'] = offline_reference(take, decode)
                    self.state = 'judge'
                    take['judgment'] = judge_take(step, take, self.context)
                    if save is not None:
                        self.state = 'save'
                        save(step, len(takes) + 1, take)
                    takes.append(take)
                    if step.takes == 2 and len(takes) == 2 and takes[0]['judgment']['verdict'] != takes[1]['judgment']['verdict']:
                        needed = 3
                result = dict(step_id=step.id, certification=step.certification,
                              **step_verdict(takes), takes=takes)
                self.results.append(result)
                if step.id == 'native_probe':
                    self.context.silence = takes[0]['raw']
                    self.context.sample_rate = takes[0]['sample_rate']
                if step.id == 'baseline':
                    best = min(takes, key=lambda t: RANK[t['judgment']['verdict']])
                    self.context.baseline = best['raw']
                if progress:
                    progress(step, result)
            self.state = 'done'
            return report_results(self.results)
        except BaseException:
            self.state = 'failed'
            raise


def report_results(results):
    core = [r['verdict'] for r in results if r['certification']]
    # Reports are JSON summaries; PCM and transcripts live only in take files.
    complete = all(s.id in {r['step_id'] for r in results} for s in BATTERY if s.certification)
    verdict = max(core, key=RANK.get, default='Watch')
    return dict(schema=1, verdict='Watch' if not complete and verdict == 'Good' else verdict,
        complete=complete,
        steps=[{k: v for k, v in r.items() if k != 'takes'} for r in results])


def rejudge_run(run):
    """Use saved references; no mic/model and no mutation of saved evidence."""
    context, results = JudgeContext(), []
    for step in BATTERY:
        takes = [dict(t) for t in run['takes'] if t['step_id'] == step.id]
        if not takes:
            continue
        takes.sort(key=lambda t: t['number'])
        if [t['number'] for t in takes] != list(range(1, len(takes) + 1)) or len(takes) > 3:
            raise ValueError('Saved run has duplicate or nonsequential take numbers')
        for take in takes:
            take['judgment'] = judge_take(step, take, context)
        result = dict(step_id=step.id, certification=step.certification, **step_verdict(takes), takes=takes)
        expected = step.takes + int(step.takes == 2 and len(takes) >= 2 and takes[0]['judgment']['verdict'] != takes[1]['judgment']['verdict'])
        if len(takes) != expected:
            result.update(verdict='Watch', agreement=False, incomplete=True)
        results.append(result)
        if step.id == 'native_probe':
            context.silence, context.sample_rate = takes[0]['raw'], takes[0]['sample_rate']
        if step.id == 'baseline':
            context.baseline = min(takes, key=lambda t: RANK[t['judgment']['verdict']])['raw']
    report = report_results(results)
    report['complete'] &= not any(r.get('incomplete') for r in results)
    return report
