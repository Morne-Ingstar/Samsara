"""Queue 118: WER of the SHIPPING DEFAULT, and WER that separates recognition
from formatting. Standalone -- loads faster-whisper directly, never imports
dictation.

    python tools/wer_bench_118.py --all

Why this exists
---------------
Every WER figure Samsara has quoted came from `small` or `medium`. The first
run of the app uses `base` (samsara/config_schema.py "model_size"), so the
number a new user experiences has never been measured. The one prior bench
(perf_artifacts/model_size_bench.py, queue 80) also scored the MEAN of
per-clip WER over 43 clips of 1-4 s, against a ground truth derived from the
app's own live decode -- see CORPORA below for why that cannot rank models.

What it measures
----------------
For each cell (model x device x decode params x language):

  wer_raw    exact tokens: casing and punctuation count as errors. This is
             what the user sees on screen.
  wer_norm   Whisper's own EnglishTextNormalizer (the normaliser the Whisper
             paper reports against: lower-cases, strips punctuation, folds
             "twenty one"/"21", "Mr."/"mister", colour/color). This is
             recognition accuracy with formatting taken out, and it is the
             number comparable to a published WER.
  wer_light  lower-case + punctuation stripped only (the plain reading of
             "normalised"). JSON only.

Every WER is aggregated the correct way for a corpus -- total edit distance
over total reference words -- and the mean of per-clip WER is reported
beside it, because that is what queue 80 published and the two differ a lot
on short clips.

CORPORA
-------
owner43      The 43 labelled wake-lane clips from queue 80 (the owner's own
             voice, mic and room). CAVEAT: its "truth" is the app's own live
             [HEAR] line put through a heuristic ("anything starting with
             jarvis" -> "jarvis dictate"). It is not independent of the model
             under test, so it can compare a decode against the live decode
             but CANNOT rank model sizes. Reported for continuity only.
libri-short  LibriSpeech test-clean (openslr.org/12), CC BY 4.0, read speech
             with verified transcripts. Independent ground truth, so model
             sizes CAN be ranked. CAVEAT: clean read audiobook speech, not
             this microphone in this room -- absolute WER is optimistic.
libri-long   Consecutive utterances of one chapter concatenated into >= 5
             minutes of continuous speech, reference concatenated the same
             way. CAVEAT: continuous read speech, not natural dictation with
             its restarts and fillers.

Decode parameters are dictation.py's own (_get_transcribe_params): balanced
is the shipped default (beam 3, vad_filter, condition_on_previous_text
False), accurate is beam 5 + condition_on_previous_text True. The user's
custom-vocabulary initial_prompt is NOT used: it is personal, so including it
would make these numbers unreproducible.

Models are loaded and released one at a time.
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import re
import statistics
import string
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_LIBRI = Path("D:/samsara-bench/LibriSpeech/test-clean")
OUT_JSON = REPO / "perf_artifacts" / "comparison_baseline_118_wer.json"

# dictation.py _get_transcribe_params, verbatim minus the personal
# initial_prompt (and minus vocabulary), plus the module constants it reads.
_NO_SPEECH_THRESHOLD = 0.6
_LOGPROB_THRESHOLD = -1.0
PARAMS = {
    "balanced": dict(beam_size=3, vad_filter=True,
                     vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
                     condition_on_previous_text=False, without_timestamps=True,
                     word_timestamps=False),
    "accurate": dict(beam_size=5, vad_filter=True,
                     vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=300),
                     condition_on_previous_text=True, without_timestamps=False,
                     word_timestamps=False),
    # What queue 80 used, so its 18.0% can be reproduced exactly.
    "queue80": dict(beam_size=5, vad_filter=False, condition_on_previous_text=True,
                    without_timestamps=False, no_speech_threshold=0.6,
                    log_prob_threshold=-1.0),
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_PUNCT = str.maketrans("", "", string.punctuation + "\u2014\u2013\u2018\u2019\u201c\u201d")


def light_norm(text: str) -> str:
    """The plain reading of "normalised": lower case, punctuation stripped."""
    return " ".join((text or "").lower().translate(_PUNCT).split())


def raw_tokens(text: str) -> list:
    return (text or "").split()


def _normalizer():
    from whisper.normalizers import EnglishTextNormalizer  # noqa: PLC0415

    return EnglishTextNormalizer()


def edits(ref: list, hyp: list) -> int:
    """Word-level Levenshtein distance (edit count, not a rate)."""
    if not ref:
        return len(hyp)
    prev = list(range(len(hyp) + 1))
    for i, rw in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, hw in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[len(hyp)]


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------

def load_owner43() -> list:
    """Queue 80's labelled clips, with their heuristic truth (see docstring)."""
    src = REPO / "perf_artifacts" / "model_size_bench.json"
    if not src.exists():
        return []
    data = json.loads(src.read_text(encoding="utf-8"))
    out = []
    for row in data.get("labelled", []):
        wav = row.get("wav")
        if wav and Path(wav).exists():
            out.append({"id": Path(wav).name, "audio": wav, "ref": row["truth"],
                        "seconds": row.get("seconds")})
    return out


def _libri_rows(root: Path) -> list:
    rows = []
    for trans in sorted(root.rglob("*.trans.txt")):
        for line in trans.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            uid, _, text = line.partition(" ")
            flac = trans.parent / f"{uid}.flac"
            if flac.exists():
                rows.append({"id": uid, "audio": str(flac), "ref": text.strip(),
                             "chapter": trans.parent.name, "speaker": trans.parent.parent.name})
    return rows


def load_libri_short(root: Path, n: int, seed: int = 118) -> list:
    rows = _libri_rows(root)
    rng = random.Random(seed)
    sample = rng.sample(rows, min(n, len(rows)))
    sample.sort(key=lambda r: r["id"])
    import soundfile as sf  # noqa: PLC0415

    for row in sample:
        info = sf.info(row["audio"])
        row["seconds"] = round(info.frames / info.samplerate, 2)
    return sample


def load_libri_long(root: Path, minutes: float, chapters: int, cache: Path) -> list:
    """Consecutive utterances of one chapter concatenated into one long clip."""
    import soundfile as sf  # noqa: PLC0415

    rows = _libri_rows(root)
    by_chapter: dict = {}
    for row in rows:
        by_chapter.setdefault((row["speaker"], row["chapter"]), []).append(row)
    cache.mkdir(parents=True, exist_ok=True)
    out = []
    for key in sorted(by_chapter)[:50]:
        group = sorted(by_chapter[key], key=lambda r: r["id"])
        audio, ref, total = [], [], 0.0
        for row in group:
            data, sr = sf.read(row["audio"], dtype="float32")
            if sr != 16000:
                continue
            audio.append(data)
            ref.append(row["ref"])
            total += len(data) / sr
            if total >= minutes * 60:
                break
        if total < minutes * 60:
            continue
        joined = np.concatenate(audio)
        path = cache / f"longform_{key[0]}_{key[1]}.wav"
        if not path.exists():
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((np.clip(joined, -1, 1) * 32767).astype(np.int16).tobytes())
        out.append({"id": path.stem, "audio": str(path), "ref": " ".join(ref),
                    "seconds": round(total, 1), "utterances": len(ref)})
        if len(out) >= chapters:
            break
    return out


# ---------------------------------------------------------------------------
# Running one cell
# ---------------------------------------------------------------------------

def load_audio(path: str) -> np.ndarray:
    """16 kHz mono float32, the shape the app feeds Whisper."""
    import soundfile as sf  # noqa: PLC0415

    data, sr = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != 16000:
        idx = np.linspace(0, len(data) - 1, int(len(data) * 16000 / sr))
        data = np.interp(idx, np.arange(len(data)), data).astype(np.float32)
    return data


def run_cell(clips, model_size, device, params_name, language, normalizer, warmup=True):
    from faster_whisper import WhisperModel  # noqa: PLC0415

    compute = "float16" if device == "cuda" else "int8"   # dictation.py:5601
    t0 = time.perf_counter()
    model = WhisperModel(model_size, device=device, compute_type=compute)
    load_ms = (time.perf_counter() - t0) * 1000.0

    opts = dict(PARAMS[params_name])
    opts.setdefault("no_speech_threshold", _NO_SPEECH_THRESHOLD)
    opts.setdefault("log_prob_threshold", _LOGPROB_THRESHOLD)
    opts["language"] = None if language == "auto" else language

    if warmup and clips:
        # One throwaway decode so the first real clip is not paying for lazy
        # kernel/JIT init -- the app is already warm when the user dictates.
        model.transcribe(load_audio(clips[0]["audio"])[:16000], **opts)

    records = []
    for clip in clips:
        audio = load_audio(clip["audio"])
        t1 = time.perf_counter()
        segments, info = model.transcribe(audio, **opts)
        text = "".join(s.text for s in segments).strip()
        ms = (time.perf_counter() - t1) * 1000.0
        ref, hyp = clip["ref"], text
        rec = {
            "id": clip["id"], "seconds": clip.get("seconds") or round(len(audio) / 16000, 2),
            "ref": ref, "hyp": hyp, "ms": round(ms, 1),
            "detected_language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None),
        }
        for tag, fn in (("raw", raw_tokens), ("light", lambda t: light_norm(t).split()),
                        ("norm", lambda t: normalizer(t).split())):
            r, h = fn(ref), fn(hyp)
            rec[f"edits_{tag}"] = edits(r, h)
            rec[f"refwords_{tag}"] = len(r)
            rec[f"wer_{tag}"] = (rec[f"edits_{tag}"] / len(r)) if r else (1.0 if h else 0.0)
        records.append(rec)

    del model
    gc.collect()
    try:
        import torch  # noqa: PLC0415

        if device == "cuda":
            torch.cuda.empty_cache()
    except Exception:
        pass

    cell = {
        "model": model_size, "device": device, "compute_type": compute,
        "params": params_name, "language": language, "load_ms": round(load_ms, 1),
        "clips": len(records),
        "audio_seconds": round(sum(r["seconds"] for r in records), 1),
        "decode_ms_median": round(statistics.median([r["ms"] for r in records]), 1) if records else None,
        "decode_ms_p95": round(pct([r["ms"] for r in records], 0.95), 1) if records else None,
        "rtf_median": None,
        "records": records,
    }
    rtfs = [(r["ms"] / 1000.0) / r["seconds"] for r in records if r["seconds"]]
    if rtfs:
        cell["rtf_median"] = round(statistics.median(rtfs), 4)
    for tag in ("raw", "light", "norm"):
        total_e = sum(r[f"edits_{tag}"] for r in records)
        total_w = sum(r[f"refwords_{tag}"] for r in records)
        cell[f"wer_{tag}"] = round(total_e / total_w, 4) if total_w else None
        cell[f"wer_{tag}_mean_per_clip"] = (
            round(statistics.mean([r[f"wer_{tag}"] for r in records]), 4) if records else None)
    return cell


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", choices=["owner43", "libri-short", "libri-long"], default="libri-short")
    ap.add_argument("--models", nargs="+", default=["base", "small", "medium"])
    ap.add_argument("--devices", nargs="+", default=["cuda"], choices=["cuda", "cpu"])
    ap.add_argument("--params", nargs="+", default=["balanced"], choices=list(PARAMS))
    ap.add_argument("--languages", nargs="+", default=["en"])
    ap.add_argument("--libri-root", default=str(DEFAULT_LIBRI))
    ap.add_argument("--n", type=int, default=120, help="libri-short utterances")
    ap.add_argument("--long-minutes", type=float, default=5.0)
    ap.add_argument("--long-chapters", type=int, default=2)
    ap.add_argument("--all", action="store_true", help="the full matrix behind the report")
    ap.add_argument("--out", default=str(OUT_JSON))
    args = ap.parse_args(argv)

    normalizer = _normalizer()
    libri_root = Path(args.libri_root)
    cache = libri_root.parent / "longform"

    def corpus(name):
        if name == "owner43":
            return load_owner43()
        if name == "libri-short":
            return load_libri_short(libri_root, args.n)
        return load_libri_long(libri_root, args.long_minutes, args.long_chapters, cache)

    if args.all:
        # Exactly the cells perf_artifacts/comparison_baseline_118.md shows, so
        # this one command reproduces the whole page.
        plan = [
            # 2. the shipping default and its neighbours
            ("libri-short", "base", "cuda", "balanced", "en"),
            ("libri-short", "small", "cuda", "balanced", "en"),
            ("libri-short", "medium", "cuda", "balanced", "en"),
            ("libri-short", "base", "cpu", "balanced", "en"),
            ("libri-short", "medium", "cuda", "accurate", "en"),
            # 2b. queue 80's 43 clips, with queue 80's own parameters
            ("owner43", "base", "cuda", "queue80", "en"),
            ("owner43", "small", "cuda", "queue80", "en"),
            ("owner43", "medium", "cuda", "queue80", "en"),
            # 3. long form, both parameter sets -- the decode-parameter finding
            ("libri-long", "base", "cuda", "balanced", "en"),
            ("libri-long", "small", "cuda", "balanced", "en"),
            ("libri-long", "medium", "cuda", "balanced", "en"),
            ("libri-long", "base", "cuda", "accurate", "en"),
            ("libri-long", "small", "cuda", "accurate", "en"),
            ("libri-long", "medium", "cuda", "accurate", "en"),
            # 4. what auto-detect costs
            ("libri-short", "base", "cuda", "balanced", "auto"),
            ("libri-short", "medium", "cuda", "balanced", "auto"),
            ("owner43", "base", "cuda", "queue80", "auto"),
            ("owner43", "medium", "cuda", "queue80", "auto"),
        ]
    else:
        plan = [(args.corpus, m, d, p, lang)
                for m in args.models for d in args.devices
                for p in args.params for lang in args.languages]

    out_path = Path(args.out)
    existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    results = existing.get("cells", [])
    corpora_meta = existing.get("corpora", {})
    clip_cache: dict = {}

    for corpus_name, model, device, params_name, language in plan:
        if corpus_name not in clip_cache:
            clip_cache[corpus_name] = corpus(corpus_name)
            clips = clip_cache[corpus_name]
            corpora_meta[corpus_name] = {
                "clips": len(clips),
                "audio_seconds": round(sum(c.get("seconds") or 0 for c in clips), 1),
                "seconds_min": min((c.get("seconds") or 0) for c in clips) if clips else None,
                "seconds_median": round(statistics.median([c.get("seconds") or 0 for c in clips]), 2) if clips else None,
                "seconds_max": max((c.get("seconds") or 0) for c in clips) if clips else None,
                "ref_words": sum(len((c["ref"] or "").split()) for c in clips),
            }
        clips = clip_cache[corpus_name]
        if not clips:
            print(f"[SKIP] {corpus_name}: no clips")
            continue
        label = f"{corpus_name} {model}/{device}/{params_name}/lang={language}"
        print(f"[RUN ] {label} ({len(clips)} clips)", flush=True)
        t0 = time.perf_counter()
        cell = run_cell(clips, model, device, params_name, language, normalizer)
        cell["corpus"] = corpus_name
        cell["wall_s"] = round(time.perf_counter() - t0, 1)
        results = [c for c in results if not (
            c["corpus"] == corpus_name and c["model"] == model and c["device"] == device
            and c["params"] == params_name and c["language"] == language)]
        results.append(cell)
        print(f"[DONE] {label}: WER raw {cell['wer_raw']} norm {cell['wer_norm']} "
              f"median {cell['decode_ms_median']} ms in {cell['wall_s']}s", flush=True)
        out_path.write_text(json.dumps(
            {"corpora": corpora_meta, "cells": results,
             "params_used": {k: {kk: vv for kk, vv in v.items() if kk != 'vad_parameters'}
                             for k, v in PARAMS.items()}},
            indent=1), encoding="utf-8")

    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
