"""CPU-only, torch-free speaker-verification experiment; no Samsara integration.

Run with F:\\envs\\sami\\python.exe tools/probes/sv_feasibility.py --download-model.
Only model/results files are written; the corpus is read-only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.abc import MetaPathFinder
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import sys
import time
import urllib.error
import urllib.request
import wave


class NoTorchFinder(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'torch' or fullname.startswith('torch.'):
            raise ImportError('This feasibility probe forbids torch; ONNX CPU only')
        return None


def assert_torch_free():
    if any(name == 'torch' or name.startswith('torch.') for name in sys.modules):
        raise RuntimeError('Torch was loaded: experiment must remain torch-free')


def block_torch():
    assert_torch_free()
    if not any(isinstance(finder, NoTorchFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, NoTorchFinder())


if __name__ == '__main__':
    block_torch()  # Before any third-party import, without sys.modules placeholders.

import numpy as np

SAMPLE_RATE = 16000
MODEL_NAME = 'wespeaker_en_voxceleb_CAM++.onnx'
# The upstream release tag really is spelled "recongition".
MODEL_URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/'
             'speaker-recongition-models/wespeaker_en_voxceleb_CAM%2B%2B.onnx')
MODEL_BYTES = 29292684
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = Path(r'C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus')
EXPECTED_LABELS = {'speech_owner': 6, 'speech_owner_over_media': 3,
                   'wake_over_media': 2, 'media_only': 3, 'nonspeech_body': 5,
                   'nonspeech_room': 4, 'silence': 3}


def rms(samples):
    values = np.asarray(samples, dtype=np.float64)
    return float(np.sqrt(np.mean(values * values)))


def windows(samples, duration_s, sample_rate=SAMPLE_RATE):
    """Yield (sample offset, view) for every full, non-overlapping window."""
    size = int(round(duration_s * sample_rate))
    if size <= 0:
        raise ValueError('Window duration must be positive')
    for start in range(0, len(samples) - size + 1, size):
        yield start, samples[start:start + size]


def duck_envelope(size, sample_rate=SAMPLE_RATE):
    envelope = np.full(size, .15, dtype=np.float64)
    envelope[:int(round(.5 * sample_rate))] = 1.0
    return envelope


def mix_at_sir(owner, media, sir_db, *, duck=False, sample_rate=SAMPLE_RATE):
    """Set pre-duck SIR using component RMS; common gain avoids clipping."""
    owner = np.asarray(owner, dtype=np.float64)
    media = np.asarray(media, dtype=np.float64)
    if owner.shape != media.shape or owner.ndim != 1:
        raise ValueError('Owner and media must be equal-length mono arrays')
    owner_rms, media_rms = rms(owner), rms(media)
    if owner_rms == 0 or media_rms == 0:
        raise ValueError('Cannot define SIR for a zero-energy component')
    scale = owner_rms / (media_rms * 10 ** (sir_db / 20))
    interference = media * scale
    if duck:
        interference *= duck_envelope(len(owner), sample_rate)
    mixture = owner + interference
    peak = float(np.max(np.abs(mixture)))
    common_gain = min(1.0, .99 / peak) if peak else 1.0
    return (mixture * common_gain).astype(np.float32), {
        'target_sir_db': float(sir_db), 'media_scale': scale,
        'shared_peak_gain': common_gain,
        'actual_whole_sir_db': 20 * math.log10(owner_rms / rms(interference)),
        'duck_first_s': .5 if duck else None,
        'duck_after_gain': .15 if duck else None,
    }


def unit(vector):
    vector = np.asarray(vector, dtype=np.float64).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm == 0:
        raise ValueError('Embedding is zero or nonfinite')
    return vector / norm


def cosine(left, right):
    return float(np.clip(np.dot(unit(left), unit(right)), -1.0, 1.0))


def enroll(owner_audio, embedding_fn):
    """Mean clip embeddings, then normalize; each LOO fold excludes itself."""
    embeddings = [np.asarray(embedding_fn(samples), dtype=np.float64) for samples in owner_audio]
    if len(embeddings) != 6:
        raise ValueError('Enrollment requires exactly six owner clips')
    full = unit(np.mean(embeddings, axis=0))
    folds = [cosine(embedding, np.mean(embeddings[:i] + embeddings[i + 1:], axis=0))
             for i, embedding in enumerate(embeddings)]
    return full, folds


def zero_false_accept_threshold(media_scores):
    scores = np.asarray(media_scores, dtype=np.float64)
    if not len(scores) or not np.all(np.isfinite(scores)):
        raise ValueError('Need finite, VAD-retained media scores to set the threshold')
    # Acceptance is score >= threshold, so equality with the maximum must reject.
    return float(np.nextafter(np.max(scores), math.inf))


def rejection_rate(scores, threshold):
    return float(np.mean(np.asarray(scores) < threshold)) if len(scores) else None


def equal_error_rate(genuine_scores, impostor_scores):
    """Pooled empirical EER with linear interpolation at the ROC crossing."""
    genuine = np.asarray(genuine_scores, dtype=np.float64)
    impostor = np.asarray(impostor_scores, dtype=np.float64)
    if not len(genuine) or not len(impostor):
        return None
    values = np.unique(np.concatenate([genuine, impostor]))
    thresholds = np.concatenate([[values[0]], np.nextafter(values, math.inf)])
    far = np.array([np.mean(impostor >= threshold) for threshold in thresholds])
    frr = np.array([np.mean(genuine < threshold) for threshold in thresholds])
    difference = far - frr
    upper = int(np.flatnonzero(difference <= 0)[0])
    if difference[upper] == 0:
        rate, threshold = far[upper], thresholds[upper]
    else:
        lower = upper - 1
        weight = difference[lower] / (difference[lower] - difference[upper])
        rate = far[lower] + weight * (far[upper] - far[lower])
        threshold = thresholds[lower] + weight * (thresholds[upper] - thresholds[lower])
    return {'eer': float(rate), 'threshold_interpolated': float(threshold),
            'genuine_n': len(genuine), 'impostor_n': len(impostor),
            'method': 'linear interpolation of pooled empirical FAR/FRR crossing'}


def read_wav(path):
    with wave.open(str(path), 'rb') as stream:
        if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth(),
                stream.getcomptype()) != (SAMPLE_RATE, 1, 2, 'NONE'):
            raise ValueError(f'{path}: expected 16 kHz mono PCM16 WAV')
        return np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768.0


def read_corpus(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if Counter(row['label'] for row in manifest) != Counter(EXPECTED_LABELS):
        raise ValueError('Manifest label counts do not match the requested corpus')
    clips = []
    for row in manifest:
        path = (root / row['file']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Manifest entry escapes the corpus directory')
        audio = read_wav(path)
        clips.append({'file': row['file'], 'label': row['label'], 'audio': audio,
                      'duration_s': len(audio) / SAMPLE_RATE,
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return clips


def ensure_model(path, download):
    if not path.is_file():
        if not download:
            raise FileNotFoundError(f'Missing model. Download {MODEL_URL} to {path}')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.onnx.part')
        request = urllib.request.Request(MODEL_URL, headers={'User-Agent': 'Samsara-SV-feasibility'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f'Download stopped: {exc}. Owner download URL: {MODEL_URL}') from exc
        if temporary.stat().st_size != MODEL_BYTES:
            raise ValueError(f'Model download size mismatch: {temporary.stat().st_size}')
        temporary.replace(path)
    if path.stat().st_size != MODEL_BYTES:
        raise ValueError('Expected the single upstream English CAM++ model asset')
    return {'url': MODEL_URL, 'file': str(path.resolve()), 'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


class CpuEmbedding:
    def __init__(self, path):
        import sherpa_onnx
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(path), num_threads=1, debug=False, provider='cpu')
        if not config.validate():
            raise ValueError(f'Invalid speaker embedding configuration: {config}')
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.config = str(config)
        assert_torch_free()

    def __call__(self, samples):
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE,
                               waveform=np.ascontiguousarray(samples, dtype=np.float32))
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            raise ValueError(f'Embedding not ready for {len(samples) / SAMPLE_RATE:.3f}s')
        return np.asarray(self.extractor.compute(stream), dtype=np.float64)


class BundledSilero:
    def __init__(self):
        # Matches DictationApp._load_vad_model; optional conversion imports are
        # blocked by NoTorchFinder, with no None entry for scipy to dereference.
        from faster_whisper.utils import get_assets_path
        from faster_whisper.vad import SileroVADModel
        self.path = Path(get_assets_path()) / 'silero_vad_v6.onnx'
        self.model = SileroVADModel(str(self.path))
        self.providers = self.model.session.get_providers()
        if self.providers != ['CPUExecutionProvider']:
            raise RuntimeError(f'VAD must use CPU only: {self.providers}')
        assert_torch_free()

    def __call__(self, samples):
        usable = len(samples) // 512 * 512
        # Silero's wrapper writes its last context slice; never give it a view
        # of the trial waveform. Ignore incomplete 32 ms frames, as the app does.
        audio = np.array(samples[:usable], dtype=np.float32, copy=True)
        probabilities = np.asarray(self.model(audio)).reshape(-1) if usable else np.array([])
        voiced = int(np.count_nonzero(probabilities > .5))
        return {'voiced_frames': voiced, 'vad_frames': len(probabilities),
                'voiced_fraction': voiced / len(probabilities) if len(probabilities) else 0.0,
                'vad_ignored_tail_samples': len(samples) - usable}


def score_trial(samples, enrollment, embedding_fn, vad_fn, **metadata):
    trial = dict(metadata, duration_s=len(samples) / SAMPLE_RATE,
                 score=None, embedding_ms=None, included=False, drop_reason=None)
    trial.update(vad_fn(samples))
    if trial['voiced_fraction'] < .5:
        trial['drop_reason'] = 'vad_below_50_percent'
        return trial
    start = time.perf_counter_ns()
    embedding = embedding_fn(samples)
    trial['embedding_ms'] = (time.perf_counter_ns() - start) / 1e6
    trial['score'] = cosine(embedding, enrollment)
    trial['included'] = True
    return trial


def summarize(trials, threshold):
    rows = []
    for condition in dict.fromkeys(trial['condition'] for trial in trials):
        group = [trial for trial in trials if trial['condition'] == condition]
        scores = [trial['score'] for trial in group if trial['included']]
        genuine = group[0]['genuine']
        rejected = sum(score < threshold for score in scores)
        rows.append({
            'condition': condition, 'genuine': genuine, 'total': len(group),
            'kept': len(scores), 'vad_dropped': len(group) - len(scores),
            'mean': float(np.mean(scores)) if scores else None,
            'min': min(scores) if scores else None, 'max': max(scores) if scores else None,
            'frr': rejected / len(scores) if scores and genuine else None,
            'far': (len(scores) - rejected) / len(scores) if scores and not genuine else None,
            'combined_reject_rate': (len(group) - len(scores) + rejected) / len(group) if genuine else None,
        })
    return rows


def format_table(rows):
    lines = ['| Condition | Kept/total | VAD dropped | Mean | Min | Max | SV FRR / FAR |',
             '|---|---:|---:|---:|---:|---:|---:|']
    def score(value):
        return 'n/a' if value is None else f'{value:.4f}'
    for row in rows:
        rate = row['frr'] if row['genuine'] else row['far']
        error = 'n/a' if rate is None else f"{100 * rate:.1f}% {'FRR' if row['genuine'] else 'FAR'}"
        lines.append(f"| {row['condition']} | {row['kept']}/{row['total']} | {row['vad_dropped']} | "
                     f"{score(row['mean'])} | {score(row['min'])} | {score(row['max'])} | {error} |")
    return '\n'.join(lines)


def verdict_line(rows, threshold):
    by_condition = {row['condition']: row for row in rows}
    def value(condition):
        row = by_condition[condition]
        if row['frr'] is None:
            return 'n/a (no VAD-retained trials)'
        return f"{100 * row['frr']:.1f}% ({round(row['frr'] * row['kept'])}/{row['kept']})"
    return (f'VERDICT: At zero observed media false accepts (threshold={threshold:.17g}), '
            f'owner SV false-reject rates after VAD are: 1.2 s clean={value("clean_1.2s")}; '
            f'2.0 s at 0 dB SIR={value("overlap_+0dB")}; '
            f'duck-simulated post-500 ms={value("duck_post_500ms")}.')


def run_experiment(clips, embedding_fn, vad_fn, seed=20260910, latency_repeats=5):
    rng = np.random.default_rng(seed)
    owner = [clip for clip in clips if clip['label'] == 'speech_owner']
    media = [clip for clip in clips if clip['label'] == 'media_only']
    enrollment, fold_scores = enroll([clip['audio'] for clip in owner], embedding_fn)
    trials, folds, latency_windows = [], [], {'0.8': [], '3.0': []}
    for i, (clip, score) in enumerate(zip(owner, fold_scores)):
        vad = vad_fn(clip['audio'])
        included = vad['voiced_fraction'] >= .5
        fold = dict(condition='clean_LOO_whole', genuine=True, file=clip['file'],
                    start_sample=0, duration_s=clip['duration_s'], score=score,
                    fold=i + 1, enrolled_files=[other['file'] for j, other in enumerate(owner) if i != j],
                    included=included, drop_reason=None if included else 'vad_below_50_percent', **vad)
        folds.append(fold)
        trials.append(fold)

    def add(audio, condition, genuine=True, **details):
        trial = score_trial(audio, enrollment, embedding_fn, vad_fn,
                            condition=condition, genuine=genuine, **details)
        trials.append(trial)
        return trial

    for clip in clips:
        if clip['label'] in ('speech_owner_over_media', 'wake_over_media'):
            add(clip['audio'], clip['label'] + '_whole', file=clip['file'], start_sample=0)
    for duration in (.8, 1.2, 2.0, 3.0):
        for clip in owner:
            for start, audio in windows(clip['audio'], duration):
                trial = add(audio, f'clean_{duration:.1f}s', file=clip['file'], start_sample=start)
                key = f'{duration:.1f}'
                if key in latency_windows and trial['included']:
                    latency_windows[key].append((clip['file'], start, audio))

    for clip in owner:
        for start, audio in windows(clip['audio'], 2.0):
            # One seeded slice per owner window, reused across SIR/duck pairs.
            other = media[int(rng.integers(len(media)))]
            media_start = int(rng.integers(len(other['audio']) - len(audio) + 1))
            noise = other['audio'][media_start:media_start + len(audio)]
            details = {'file': clip['file'], 'start_sample': start,
                       'media_file': other['file'], 'media_start_sample': media_start,
                       'pair_id': f"{clip['file']}:{start}"}
            for sir in (6, 0, -6, -12):
                mixed, mixing = mix_at_sir(audio, noise, sir)
                add(mixed, f'overlap_{sir:+d}dB', **details, **mixing)
            ducked, mixing = mix_at_sir(audio, noise, 0, duck=True)
            add(ducked, 'duck_whole_2.0s', **details, **mixing)
            add(ducked[SAMPLE_RATE // 2:], 'duck_post_500ms',
                analysis_offset_samples=SAMPLE_RATE // 2, **details, **mixing)

    for clip in clips:
        if clip['label'] in ('media_only', 'nonspeech_body', 'nonspeech_room', 'silence'):
            for start, audio in windows(clip['audio'], 2.0):
                add(audio, clip['label'] + '_2.0s', genuine=False,
                    file=clip['file'], start_sample=start)

    media_scores = [trial['score'] for trial in trials
                    if trial['condition'] == 'media_only_2.0s' and trial['included']]
    threshold = zero_false_accept_threshold(media_scores)
    rows = summarize(trials, threshold)
    eer = equal_error_rate(
        [trial['score'] for trial in trials if trial['genuine'] and trial['included']],
        [trial['score'] for trial in trials if not trial['genuine'] and trial['included']])

    latency = {}
    for duration, candidates in latency_windows.items():
        samples = []
        if candidates:
            for _ in range(3):
                embedding_fn(candidates[0][2])
        for repeat in range(latency_repeats):
            for file, start, audio in candidates:
                begin = time.perf_counter_ns()
                embedding_fn(audio)
                elapsed = (time.perf_counter_ns() - begin) / 1e6
                samples.append({'file': file, 'start_sample': start,
                                'repeat': repeat + 1, 'ms': elapsed})
        values = [sample['ms'] for sample in samples]
        latency[duration] = {'p50_ms': float(np.percentile(values, 50)) if values else None,
                             'p95_ms': float(np.percentile(values, 95)) if values else None,
                             'n': len(values), 'distinct_windows': len(candidates),
                             'samples': samples}
    assert_torch_free()
    return {'enrolled_files': [clip['file'] for clip in owner], 'folds': folds,
            'trials': trials, 'zero_media_fa_threshold': threshold, 'metrics': rows,
            'eer_overall': eer, 'latency': latency, 'seed': seed,
            'verdict': verdict_line(rows, threshold), 'torch_absent': True,
            'vad_dropped_genuine': sum(trial['genuine'] and not trial['included'] for trial in trials),
            'vad_dropped_impostor': sum(not trial['genuine'] and not trial['included'] for trial in trials)}


def main(argv=None):
    block_torch()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=DEFAULT_CORPUS)
    parser.add_argument('--model', type=Path, default=REPO_ROOT / 'perf_artifacts' / 'sv_models' / MODEL_NAME)
    parser.add_argument('--output-dir', type=Path, default=REPO_ROOT / 'perf_artifacts')
    parser.add_argument('--download-model', action='store_true')
    parser.add_argument('--seed', type=int, default=20260910)
    parser.add_argument('--latency-repeats', type=int, default=5)
    args = parser.parse_args(argv)
    if args.latency_repeats < 1:
        parser.error('--latency-repeats must be positive')
    model_info = ensure_model(args.model, args.download_model)
    print(f'Model: {model_info["file"]} ({model_info["bytes"]} bytes)', flush=True)
    clips = read_corpus(args.corpus)
    embed = CpuEmbedding(args.model)
    vad = BundledSilero()
    sanity_audio = next(clip['audio'] for clip in clips if clip['label'] == 'speech_owner').copy()
    original = sanity_audio.copy()
    sanity_embedding = embed(sanity_audio)
    repeated_embedding = embed(sanity_audio)
    embed_unchanged = bool(np.array_equal(sanity_audio, original))
    vad(sanity_audio)
    vad_unchanged = bool(np.array_equal(sanity_audio, original))
    repeat_cosine = cosine(sanity_embedding, repeated_embedding)
    if not embed_unchanged or not vad_unchanged or repeat_cosine < .999999:
        raise RuntimeError('Waveform immutability or embedding repeatability sanity check failed')
    print('Running CPU trials, Silero filtering, and latency measurements; torch blocked.', flush=True)
    result = run_experiment(clips, embed, vad, args.seed, args.latency_repeats)
    result.update({
        'created_utc': datetime.now(timezone.utc).isoformat(), 'model': model_info,
        'sanity_checks': {'embedding_preserves_waveform': embed_unchanged,
                          'vad_preserves_waveform': vad_unchanged,
                          'repeated_full_clip_cosine': repeat_cosine},
        'corpus': str(args.corpus.resolve()),
        'clips': [{key: value for key, value in clip.items() if key != 'audio'} for clip in clips],
        'runtime': {'python': sys.version, 'platform': platform.platform(),
                    'sherpa_onnx': importlib.metadata.version('sherpa-onnx'),
                    'onnxruntime': importlib.metadata.version('onnxruntime'),
                    'faster_whisper': importlib.metadata.version('faster-whisper'),
                    'numpy': np.__version__, 'embedding_provider': 'cpu',
                    'embedding_num_threads': 1, 'extractor_config': embed.config,
                    'vad_providers': vad.providers, 'vad_model': str(vad.path)},
        'method': {
            'acceptance': 'cosine >= threshold; threshold is next float above largest retained media score',
            'enrollment': 'mean of raw clip embeddings, then L2 normalized; six LOO folds plus full-six enrollment',
            'vad': 'Silero v6; complete 512-sample frames; probability > 0.5; retain >= 50% voiced frames; no audio trimming',
            'metrics': 'SV FRR/FAR and EER exclude VAD drops; combined genuine rejection including VAD is in each metrics row',
            'eer': 'all retained genuine conditions including LOO pooled against all retained impostors; correlated descriptive estimate',
            'latency': 'create stream + features + embedding compute, CPU one thread; excludes VAD/I/O/loading; three warmups then repeated retained clean windows',
            'limitations': 'Full enrollment reuses the clean clips sliced/mixed for evaluation, so these scores are optimistic. Zero FA is observed on this small corpus only; not a deployment guarantee. Overlap/duck pairs share a seeded media slice. No source separation or augmentation beyond the requested mixing.'}})
    table = format_table(result['metrics'])
    fold_lines = [f"- Fold {fold['fold']} ({fold['file']}): {fold['score']:.6f}; "
                  f"VAD retained={fold['included']}" for fold in result['folds']]
    latency_lines = [f"- {duration} s: p50={stats['p50_ms']:.2f} ms, p95={stats['p95_ms']:.2f} ms; "
                     f"n={stats['n']} across {stats['distinct_windows']} windows"
                     if stats['n'] else f'- {duration} s: n/a, no VAD-retained windows'
                     for duration, stats in result['latency'].items()]
    eer = result['eer_overall']
    eer_line = (f"Pooled EER: {100 * eer['eer']:.2f}% ({eer['genuine_n']} genuine, "
                f"{eer['impostor_n']} impostor scores)." if eer else 'Pooled EER: unavailable.')
    report = '\n'.join([
        '# Speaker verification feasibility', '',
        f"Run: {result['created_utc']}; seed={args.seed}; CPU provider, one inference thread; torch absent.", '',
        f"Model: [{MODEL_NAME}]({MODEL_URL}), {model_info['bytes']} bytes.",
        f"File: `{model_info['file']}`; SHA256: `{model_info['sha256']}`.", '',
        '## Leave-one-out enrollment', '', *fold_lines, '',
        '## Metrics', '',
        f"Zero-media-FA threshold: `{result['zero_media_fa_threshold']:.17g}` (accept >= threshold).", '',
        table, '', eer_line,
        f"VAD dropped: {result['vad_dropped_genuine']} genuine and {result['vad_dropped_impostor']} impostor trials.", '',
        result['verdict'], '',
        '## CPU embedding latency', '', *latency_lines, '',
        '## Interpretation limits', '',
        result['method']['limitations'], '',
        result['method']['vad'] + '.', '',
        result['method']['metrics'] + '.', '',
        result['method']['latency'] + '.', '',
        'The three verdict rates measure false rejection of retained owner trials; any VAD rejection is reported separately. '
        'A high rate in any of them argues against using this model as a hard pre-decode gate. '
        'The duck comparison changes both interference and analyzed duration; it cannot by itself prove a discontinuity-specific failure.', '',
    ])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / 'sv_feasibility.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    (args.output_dir / 'sv_feasibility.md').write_text(report, encoding='utf-8')
    print('\n'.join(fold_lines))
    print(table)
    print(eer_line)
    print(result['verdict'])
    print('\n'.join(latency_lines))
    print(f"VAD dropped: {result['vad_dropped_genuine']} genuine, {result['vad_dropped_impostor']} impostor; torch absent: True")
    print(f'Results: {args.output_dir / "sv_feasibility.md"} and sv_feasibility.json')


if __name__ == '__main__':
    main()
