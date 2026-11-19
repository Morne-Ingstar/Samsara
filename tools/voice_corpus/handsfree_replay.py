"""Hands-free inventory and DERIVED preparation, before replay admission.

This module deliberately does not score or claim production replay fidelity.
Ten paired real-session captures are required before that work may proceed.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import time
import wave

import numpy as np

from . import RATE
from .storage import atomic_json, digest, external_path, read_wav

GAPS = (.4, .8, 1.5, 3.)


def safe_source(path):
    path = Path(path).resolve()
    if any(p.casefold() in (".samsara", ".samsara-daily") for p in path.parts):
        raise ValueError("Live profiles are forbidden")
    return path


def inventory(roots):
    """Metadata only: an unlabelled WAV never counts as a real session."""
    groups = defaultdict(lambda: dict(wavs=0, seconds=0., real_sessions=0,
                                     paired_real_sessions=0, unreadable=0))
    for source in roots:
        root = safe_source(source)
        for path in sorted(root.rglob("*.wav")):
            safe_source(path)
            key = str(root / path.relative_to(root).parent)
            group = groups[key]
            group["wavs"] += 1
            try:
                with wave.open(str(path), "rb") as wav:
                    duration = wav.getnframes() / wav.getframerate()
                    samples = wav.getnframes()
                    fmt = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
                group["seconds"] += duration
                side = path.with_suffix(".json")
                row = json.loads(side.read_text(encoding="utf-8-sig")) if side.is_file() else {}
                if row.get("kind") == "REAL_SESSION":
                    group["real_sessions"] += 1
                    journal = path.with_suffix(".jsonl")
                    if (row.get("complete") is True and fmt == (RATE, 1, 2)
                            and samples > 0 and row.get("samples") == samples and journal.is_file()):
                        events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
                        recorded = sum(e.get("samples", 0) for e in events if e.get("event") == "frame")
                        delivered = any(e.get("event") == "dispatch" and e.get("delivered") for e in events)
                        if recorded == samples and delivered:
                            group["paired_real_sessions"] += 1
            except (OSError, ValueError, wave.Error, EOFError):
                group["unreadable"] += 1
    return dict(schema=1, groups=dict(groups),
                paired_real_sessions=sum(g["paired_real_sessions"] for g in groups.values()),
                caveat="Paired capture is a prerequisite, not proof of production replay fidelity.")


def preflight(report):
    count = report["paired_real_sessions"]
    return dict(status="BLOCKED", eligible_sessions=count, required_sessions=10,
                matched=None, match_rate=None, fidelity_verified=False,
                reason=("Fewer than ten paired real sessions; STOP before scoring." if count < 10 else
                        "Paired captures exist; production replay fidelity is still unproved. STOP before scoring."))


def speech_spans(pcm):
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    return get_speech_timestamps(pcm.astype(np.float32) / 32768,
        VadOptions(min_silence_duration_ms=96, speech_pad_ms=0), sampling_rate=RATE)


def _single_sentence(row):
    text = row.get("expected_text", "").strip()
    return (row.get("synthetic") is False and row.get("kind") == "prose"
            and len(text.split()) >= 6 and not re.search(r"[.!?].*\w", text))


def _quiet_clip(pcm, spans):
    """A measured nonzero room recording, outside the VAD speech spans."""
    bounds = [(0, spans[0]["start"])] if spans else [(0, len(pcm))]
    bounds += [(a["end"], b["start"]) for a, b in zip(spans, spans[1:])]
    if spans:
        bounds.append((spans[-1]["end"], len(pcm)))
    candidates = []
    width = round(.1 * RATE)
    for start, end in bounds:
        for at in range(start, end - width + 1, width):
            rms = float(np.sqrt(np.mean((pcm[at:at + width].astype(np.float64) / 32768) ** 2)))
            if 1e-5 < rms < .01:
                candidates.append((rms, at, at + width))
    return min(candidates) if candidates else None


def derive(takes, *, spans_fn=speech_spans):
    """Build recipes from consecutive real takes, never saving their text.

    Replace an internal VAD gap in each single-sentence take with measured
    room tone; join each consecutive pair with a separately labelled take
    boundary. These are constructed challenges, not historical sessions.
    """
    root = safe_source(takes)
    sources = []
    room = None
    skipped = defaultdict(int)
    for path in sorted(root.glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8-sig"))
        wav = safe_source(path.with_suffix(".wav"))
        if not wav.is_file() or not _single_sentence(row):
            skipped["not_real_single_sentence_prose"] += 1
            continue
        pcm = read_wav(wav)
        sha = digest(wav)
        if row.get("audio_sha256") and row["audio_sha256"] != sha:
            raise ValueError("Source WAV no longer matches its recorder sidecar")
        spans = spans_fn(pcm)
        quiet = _quiet_clip(pcm, spans)
        if quiet is not None and (room is None or quiet[0] < room[0]):
            room = (quiet[0], dict(wav=str(wav), sha256=sha, start=quiet[1], end=quiet[2]))
        gaps = [(a["end"], b["start"]) for a, b in zip(spans, spans[1:])
                if b["start"] - a["end"] >= round(.06 * RATE)]
        gap = max(gaps, key=lambda pair: pair[1] - pair[0]) if gaps else None
        sources.append(dict(wav=str(wav), sha256=sha, samples=len(pcm), gap=gap,
                            timestamp=row.get("timestamp", "")))
    sources.sort(key=lambda row: (row["timestamp"], row["wav"]))
    rows = []
    if room is None:
        return dict(schema=1, kind="DERIVED", rows=[], skipped=dict(skipped),
                    reason="No measured room-tone segment; silence is not substituted.")
    for first, second in zip(sources, sources[1:]):
        if first["gap"] is None and second["gap"] is None:
            skipped["consecutive_pair_without_internal_gap"] += 1
            continue
        for gap_s in GAPS:
            gap_samples = round(gap_s * RATE)
            clips = [{k: source[k] for k in ("wav", "sha256", "samples", "gap")}
                     for source in (first, second)]
            pauses = sum(c["gap"] is not None for c in clips)
            samples = sum(c["samples"] - (c["gap"][1] - c["gap"][0]) + gap_samples
                          if c["gap"] is not None else c["samples"] for c in clips)
            samples += round(.8 * RATE)
            identity = json.dumps([clips, room[1], gap_s], sort_keys=True)
            rows.append(dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24], kind="DERIVED",
                clips=clips, room_tone=room[1], injected_gap_s=gap_s, internal_pauses=pauses,
                inter_take_gap_s=.8, samples=samples, duration_s=samples / RATE,
                tags=["short_pause" if gap_s <= .8 else "long_pause"],
                caveat="Script-indicated single sentences; internal gaps and room tone are VAD proxies, not human annotations."))
    return dict(schema=1, kind="DERIVED", rows=rows, skipped=dict(skipped),
                duration_s=sum(r["duration_s"] for r in rows),
                injected_pauses=sum(r["internal_pauses"] for r in rows),
                caveat="DERIVED ONLY; never combine with real-session fidelity or accuracy numbers.")


def render_derived(row):
    """Verify recipes and render DERIVED PCM in memory for a future replay."""
    if row.get("kind") != "DERIVED":
        raise ValueError("DERIVED recipe required")
    def audio(ref):
        path = safe_source(ref["wav"])
        if digest(path) != ref["sha256"]:
            raise ValueError("Derived source changed")
        return read_wav(path)
    tone = row["room_tone"]
    room = audio(tone)[tone["start"]:tone["end"]]
    if not len(room):
        raise ValueError("Room tone is empty")
    def gap(seconds):
        n = round(seconds * RATE)
        return np.tile(room, int(np.ceil(n / len(room))))[:n]
    clips = []
    for ref in row["clips"]:
        pcm = audio(ref)
        if ref["gap"] is None:
            clips.append(pcm)
            continue
        a, b = ref["gap"]
        if not 0 < a < b < len(pcm):
            raise ValueError("Gap is not internal to its source take")
        clips.append(np.concatenate([pcm[:a], gap(row["injected_gap_s"]), pcm[b:]]))
    parts = []
    for index, clip in enumerate(clips):
        if index:
            parts.append(gap(row["inter_take_gap_s"]))
        parts.append(clip)
    result = np.concatenate(parts)
    if len(result) != row["samples"]:
        raise ValueError("Derived sample count mismatch")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("inventory", "preflight"):
        child = sub.add_parser(command)
        child.add_argument("--root", type=Path, action="append", required=True)
        child.add_argument("--output", type=Path, required=True)
    child = sub.add_parser("derive")
    child.add_argument("--takes", type=Path, required=True)
    child.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    start = time.perf_counter()
    output = external_path(args.output)
    if args.command == "derive":
        result = derive(args.takes)
    else:
        result = inventory(args.root)
        if args.command == "preflight":
            result = preflight(result)
    result["elapsed_s"] = time.perf_counter() - start
    atomic_json(output, result)
    print(json.dumps({k: v for k, v in result.items() if k not in ("rows", "groups")}))
    return 2 if args.command == "preflight" else 0


if __name__ == "__main__":
    raise SystemExit(main())
