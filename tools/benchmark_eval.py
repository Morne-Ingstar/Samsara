"""Offline personal WER evaluation harness for Samsara.

Run manually, NOT from inside the app -- this loads multiple Whisper
models sequentially and needs the GPU (or CPU) to itself:

    python tools/benchmark_eval.py [--yes]

Sweeps a config matrix (MODELS x HOTWORDS_OPTIONS, defined below) over
every gold-confirmed sample in ~/.samsara/benchmark/samples.jsonl
(collected via Settings -> Advanced -> "Collect benchmark samples" and
reviewed in Tools -> Benchmark Review). For each cell, transcribes every
sample and reports WER (word-level Levenshtein against the gold
transcript, same normalization as the stress-test wizard), a
substitution/insertion/deletion breakdown, WER after applying the user's
corrections dictionary, and a count of custom-vocabulary words missed.

Models are loaded and unloaded one at a time (explicit del + cleanup
between cells) -- written for a single ~10GB card, not a multi-model
residency scheme. Refuses to run with fewer than 10 gold-confirmed
samples, and requires --yes for a sweep of more than
_MAX_CELLS_WITHOUT_CONFIRM cells.
"""

import argparse
import gc
import inspect
import json
import sys
import tempfile
import time
import types
import unicodedata
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import benchmark_store
from samsara.paths import samsara_home_dir

# ---------------------------------------------------------------------------
# Config matrix -- edit here to change what gets swept.
# ---------------------------------------------------------------------------

MODELS = ["small", "distil-large-v3", "large-v3"]
HOTWORDS_OPTIONS = [False, True]   # hotwords off / on (on = user's custom vocabulary)

_MAX_CELLS_WITHOUT_CONFIRM = 6
_MIN_GOLD_SAMPLES = 10


# ---------------------------------------------------------------------------
# WER -- pure function, word-level Levenshtein. No new dependencies.
# ---------------------------------------------------------------------------

def _levenshtein_ops(ref_words: list, hyp_words: list):
    """Standard edit-distance DP + backtrace.

    Returns (substitutions, deletions, insertions) -- deletions are ref
    words missing from hyp, insertions are extra hyp words not in ref.
    """
    n, m = len(ref_words), len(hyp_words)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])

    i, j = n, m
    sub = deletions = insertions = 0
    while i > 0 or j > 0:
        if (i > 0 and j > 0 and ref_words[i - 1] == hyp_words[j - 1]
                and dp[i][j] == dp[i - 1][j - 1]):
            i -= 1
            j -= 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            sub += 1
            i -= 1
            j -= 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            deletions += 1
            i -= 1
        elif j > 0 and dp[i][j] == dp[i][j - 1] + 1:
            insertions += 1
            j -= 1
        else:
            break
    return sub, deletions, insertions


def word_error_rate(reference: str, hypothesis: str) -> dict:
    """Word-level WER of hypothesis against reference.

    Both strings are split on whitespace as-is -- callers normalize
    (lowercase/strip punctuation) beforehand if that's the comparison they
    want; see samsara.ui.voice_training_qt._normalize_phrase, the same
    normalization the stress-test wizard uses.

    Returns {'wer', 'substitutions', 'deletions', 'insertions', 'ref_words'}.
    wer is 0.0 when both are empty, 1.0 when the reference is empty but the
    hypothesis is not (the ratio is otherwise undefined for an empty
    reference).

    Both strings are NFC-normalized before splitting so visually-identical
    multilingual text encoded in different Unicode forms (e.g. a precomposed
    "é" vs "e" + combining acute accent) compares equal instead of counting
    as a spurious substitution.
    """
    ref_words = unicodedata.normalize('NFC', reference).split()
    hyp_words = unicodedata.normalize('NFC', hypothesis).split()
    sub, deletions, insertions = _levenshtein_ops(ref_words, hyp_words)
    n = len(ref_words)
    if n == 0:
        wer = 0.0 if not hyp_words else 1.0
    else:
        wer = (sub + deletions + insertions) / n
    return {
        'wer': wer,
        'substitutions': sub,
        'deletions': deletions,
        'insertions': insertions,
        'ref_words': n,
    }


# ---------------------------------------------------------------------------
# App state (config, vocabulary, corrections) -- read directly rather than
# importing dictation.py/instantiating DictationApp, so this script never
# triggers the app's hotkey/audio/tray hooks.
# ---------------------------------------------------------------------------

def _config_path() -> Path:
    return samsara_home_dir() / "config.json"


def _load_app_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _voice_training(config: dict):
    """A VoiceTrainingQt instance (custom_vocab / corrections_dict /
    apply_corrections() / get_initial_prompt()) with no Qt window --
    its constructor only loads training_data.json from disk, it never
    builds a widget (that's deferred to .show(), never called here)."""
    from samsara.ui.voice_training_qt import VoiceTrainingQt
    app_stub = types.SimpleNamespace(
        config_path=str(_config_path()), config=config, command_executor=None,
    )
    return VoiceTrainingQt(app_stub)


def _build_base_params(config: dict) -> dict:
    """Mirrors dictation.py's get_transcription_params() mode branches plus
    the hotkey-path overrides from _build_hotkey_transcribe_params() --
    benchmark samples are collected from the hotkey dictation call site, so
    eval must transcribe them the way the app actually did. `language` and
    `initial_prompt` are set by the caller (they depend on per-run state:
    hotwords sweep, resolved language)."""
    mode = config.get('performance_mode', 'balanced')
    params = {
        'no_speech_threshold': 0.6,   # dictation.py _NO_SPEECH_THRESHOLD
        'log_prob_threshold': -1.0,   # dictation.py _LOGPROB_THRESHOLD
    }
    if mode == 'fast':
        params['beam_size'] = 1
        params['temperature'] = 0.0
    elif mode == 'accurate':
        params['beam_size'] = 5
    else:
        params['beam_size'] = 3
    # Hotkey-path overrides: VAD off (user explicitly triggered capture),
    # clean per-utterance conversational slate (no carried-over context).
    params['vad_filter'] = False
    params['condition_on_previous_text'] = False
    params['without_timestamps'] = True
    params['word_timestamps'] = False
    return params


def _lock_file_present() -> bool:
    return (Path(tempfile.gettempdir()) / "samsara.lock").exists()


# ---------------------------------------------------------------------------
# Audio I/O
# ---------------------------------------------------------------------------

def _read_wav_float32(path: Path):
    with wave.open(str(path), 'rb') as wf:
        n_frames = wf.getnframes()
        sample_rate = wf.getframerate()
        raw = wf.readframes(n_frames)
    pcm = np.frombuffer(raw, dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0, sample_rate


# ---------------------------------------------------------------------------
# faster-whisper hotwords support detection
# ---------------------------------------------------------------------------

def faster_whisper_version() -> str:
    import faster_whisper
    return getattr(faster_whisper, '__version__', 'unknown')


def hotwords_supported() -> bool:
    from faster_whisper import WhisperModel
    sig = inspect.signature(WhisperModel.transcribe)
    return 'hotwords' in sig.parameters


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _transcribe_all(model, samples, params) -> list:
    hyps = []
    for row in samples:
        audio, _sr = _read_wav_float32(benchmark_store.audio_path(row))
        segments, _info = model.transcribe(audio, **params)
        hyps.append("".join(s.text for s in segments).strip())
    return hyps


def _vocab_miss_count(gold_norm_words, hyp_norm_words, vocab_norm_set) -> int:
    hyp_set = set(hyp_norm_words)
    return sum(1 for w in gold_norm_words if w in vocab_norm_set and w not in hyp_set)


def evaluate_cell(model, samples, hotwords_text, base_params, vt, normalize) -> dict:
    params = dict(base_params)
    params['initial_prompt'] = vt.get_initial_prompt() or ""
    if hotwords_text:
        params['hotwords'] = hotwords_text

    hyps = _transcribe_all(model, samples, params)
    vocab_norm_set = {normalize(w) for w in vt.custom_vocab}

    total = {'sub': 0, 'del': 0, 'ins': 0, 'ref': 0}
    total_c = {'sub': 0, 'del': 0, 'ins': 0, 'ref': 0}
    vocab_misses = 0

    for row, hyp in zip(samples, hyps):
        gold_norm = normalize(row.get('gold') or "")
        hyp_norm = normalize(hyp)

        r = word_error_rate(gold_norm, hyp_norm)
        total['sub'] += r['substitutions']
        total['del'] += r['deletions']
        total['ins'] += r['insertions']
        total['ref'] += r['ref_words']

        hyp_corrected_norm = normalize(vt.apply_corrections(hyp))
        r_c = word_error_rate(gold_norm, hyp_corrected_norm)
        total_c['sub'] += r_c['substitutions']
        total_c['del'] += r_c['deletions']
        total_c['ins'] += r_c['insertions']
        total_c['ref'] += r_c['ref_words']

        vocab_misses += _vocab_miss_count(gold_norm.split(), hyp_norm.split(), vocab_norm_set)

    def _rate(t):
        return (t['sub'] + t['del'] + t['ins']) / t['ref'] if t['ref'] else 0.0

    return {
        'n_samples': len(samples),
        'wer': _rate(total),
        'substitutions': total['sub'],
        'deletions': total['del'],
        'insertions': total['ins'],
        'ref_words': total['ref'],
        'wer_corrected': _rate(total_c),
        'substitutions_corrected': total_c['sub'],
        'deletions_corrected': total_c['del'],
        'insertions_corrected': total_c['ins'],
        'vocab_misses': vocab_misses,
    }


def _unload_model(model) -> None:
    del model
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def _print_table(results: list) -> None:
    if not results:
        print("\nNo cells evaluated.")
        return
    print("\n" + "=" * 100)
    print(f"{'model':<18}{'hotwords':<10}{'WER':>8}{'WER(corr)':>11}"
          f"{'S':>6}{'D':>6}{'I':>6}{'vocab miss':>12}")
    print("-" * 100)
    for cell in results:
        print(
            f"{cell['model']:<18}"
            f"{('on' if cell['hotwords'] else 'off'):<10}"
            f"{cell['wer']:>8.3f}"
            f"{cell['wer_corrected']:>11.3f}"
            f"{cell['substitutions']:>6}"
            f"{cell['deletions']:>6}"
            f"{cell['insertions']:>6}"
            f"{cell['vocab_misses']:>12}"
        )
    print("=" * 100)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--yes', action='store_true',
                         help=f"skip the confirmation gate for >{_MAX_CELLS_WITHOUT_CONFIRM} sweep cells")
    args = parser.parse_args(argv)

    if _lock_file_present():
        print("WARNING: Samsara's lock file was found -- the app may still be "
              "running. Close it first: a single GPU can't hold two models "
              "resident at once.")

    samples = [r for r in benchmark_store.list_samples() if r.get('gold')]
    print(f"{len(samples)} gold-confirmed sample(s) found.")
    if len(samples) < _MIN_GOLD_SAMPLES:
        print(f"Need at least {_MIN_GOLD_SAMPLES} gold-confirmed samples to run "
              "an evaluation. Review more samples in Tools -> Benchmark Review.")
        return 1

    fw_version = faster_whisper_version()
    hw_supported = hotwords_supported()
    print(f"faster-whisper version: {fw_version}")
    print(f"hotwords parameter supported: {hw_supported}")

    hotwords_options = HOTWORDS_OPTIONS if hw_supported else [False]
    if not hw_supported and True in HOTWORDS_OPTIONS:
        print(f"Skipping hotwords=on cells -- installed faster-whisper "
              f"{fw_version} does not support the hotwords parameter.")

    n_cells = len(MODELS) * len(hotwords_options)
    print(f"Estimated cells: {n_cells} (models={MODELS}, hotwords={hotwords_options})")
    if n_cells > _MAX_CELLS_WITHOUT_CONFIRM and not args.yes:
        print(f"More than {_MAX_CELLS_WITHOUT_CONFIRM} cells -- re-run with --yes to proceed.")
        return 1

    from samsara import languages as _languages
    from samsara.ui.voice_training_qt import _normalize_phrase
    from faster_whisper import WhisperModel

    config = _load_app_config()
    vt = _voice_training(config)
    hotwords_text = ", ".join(vt.custom_vocab) if vt.custom_vocab else ""
    device = config.get('device', 'cpu')
    compute_type = config.get('compute_type', 'float16' if device == 'cuda' else 'int8')

    base_params = _build_base_params(config)
    app_stub_for_lang = types.SimpleNamespace(config=config)
    base_params['language'] = _languages.resolve_transcribe_language(app_stub_for_lang)

    results = []
    for model_name in MODELS:
        print(f"\n=== Loading model: {model_name} "
              f"(device={device}, compute_type={compute_type}) ===")
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception as exc:
            print(f"  FAILED to load {model_name}: {exc}")
            continue
        try:
            for hw_on in hotwords_options:
                label = f"{model_name} / hotwords={'on' if hw_on else 'off'}"
                print(f"--- {label} ---")
                t0 = time.time()
                cell = evaluate_cell(
                    model, samples,
                    hotwords_text if hw_on else "",
                    base_params, vt, _normalize_phrase,
                )
                cell['model'] = model_name
                cell['hotwords'] = hw_on
                cell['elapsed_s'] = time.time() - t0
                results.append(cell)
                print(
                    f"  WER={cell['wer']:.3f}  "
                    f"(S={cell['substitutions']} D={cell['deletions']} I={cell['insertions']})  "
                    f"WER(corrected)={cell['wer_corrected']:.3f}  "
                    f"vocab_misses={cell['vocab_misses']}  "
                    f"[{cell['elapsed_s']:.1f}s]"
                )
        finally:
            _unload_model(model)

    _print_table(results)

    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    out_path = Path(__file__).resolve().parent / f"benchmark_results_{stamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({'n_samples': len(samples), 'cells': results}, f, indent=2)
    print(f"\nResults written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
