"""Private, offline real-hold scoreboard. Reports contain no recognition text.

References are model proxies, never human truth. The only transcript cache is
beside the source WAV; --memory-references disables all transcript persistence.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import contextlib
import gc
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

from . import RATE
from .scoring import _measure, measure, normalised_words
from .storage import atomic_json, digest, external_path, read_wav

CAVEAT = "vs large-v3 proxy, not human truth"
REFERENCE_PARAMS = dict(language="en", beam_size=5, hotwords=None, initial_prompt=None,
                        condition_on_previous_text=False, word_timestamps=True,
                        vad_filter=False, temperature=0)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def model_identity(path):
    path = Path(path).resolve()
    return {p.name: digest(p) for p in sorted(path.iterdir()) if p.is_file()}


def discover(folder, *, allow_unpaired=False):
    """Freeze a deterministic snapshot; incomplete ring-copy pairs stay visible."""
    root = external_path(folder)
    rows, skipped = [], []
    for wav in sorted(root.rglob("*.wav")):
        side = wav.with_suffix(".json")
        if not side.exists() and not allow_unpaired:
            skipped.append(dict(id=wav.stem, reason="missing sidecar"))
            continue
        rows.append(dict(id=wav.stem, wav=str(wav), sidecar=str(side) if side.exists() else None,
                         edition=next((p for p in wav.relative_to(root).parts[:-1]
                                       if p in ("private", "daily")), "unknown")))
    for side in sorted(root.rglob("*.json")):
        if side.parent.name == ".live_scoreboard_refs":
            continue
        if not side.with_suffix(".wav").exists():
            skipped.append(dict(id=side.stem, reason="missing WAV"))
    return rows, skipped


def release_point(side, duration, *, allow_unknown=False):
    """Never substitute stop latency for audio time or silently clamp a release."""
    source = "release_audio_ms"
    value = side.get(source)
    if value is None and side.get("admitted_tail_ms") is not None and side.get("captured_audio_ms") is not None:
        source = "captured_audio_ms-admitted_tail_ms"
        value = float(side["captured_audio_ms"]) - float(side["admitted_tail_ms"])
    if value is None and side.get("release_s") is not None:
        source, value = "release_s", float(side["release_s"]) * 1000
    if value is None:
        if not allow_unknown:
            raise ValueError("Unknown release point; opt in with --allow-unknown-release")
        return duration, "assumed-file-end"
    seconds = float(value) / 1000
    if not math.isfinite(seconds) or seconds < 0 or seconds > duration + .001:
        raise ValueError("Recorded release is outside retained audio")
    return min(seconds, duration), source


def prepare(rows, *, allow_unknown=False):
    prepared = []
    for row in rows:
        wav = external_path(row["wav"])
        side = load_json(external_path(row["sidecar"])) if row.get("sidecar") else {}
        pcm = read_wav(wav)
        release, source = release_point(side, len(pcm) / RATE, allow_unknown=allow_unknown)
        microphone = side.get("microphone_name") or side.get("device_name")
        capture = side.get("capture_status")
        if not microphone and isinstance(capture, dict):
            microphone = capture.get("device_name") or capture.get("microphone_name")
        # A path hash prevents colliding stems across editions/folders.
        take_id = row.get("id", wav.stem)
        prepared.append(dict(row, wav=str(wav), id=take_id,
                             audio_sha256=digest(wav), duration_s=len(pcm) / RATE,
                             release_s=release, release_source=source,
                             microphone=str(microphone or "unknown"), tags=side.get("tags", [])))
    ids = [r["id"] for r in prepared]
    for row in prepared:
        if ids.count(row["id"]) > 1:
            row["id"] += "_" + identity(row["wav"])[:10]
    return prepared


def deduplicate(rows):
    """Copied archives must not change the denominator; timing variants remain."""
    selected, duplicates, seen = [], [], {}
    for row in rows:
        key = (row["audio_sha256"], row["release_s"])
        if key in seen:
            duplicates.append(dict(id=row["id"], same_as=seen[key]))
        else:
            selected.append(row)
            seen[key] = row["id"]
    return selected, duplicates


def cache_path_allowed(path):
    return not any(p.name.casefold() == "reports" or (p / ".git").exists()
                   for p in (path.parent, *path.parents))


class ReferenceCache:
    def __init__(self, model_files, params=None, *, memory_only=False):
        self.policy = identity(dict(schema=1, model=model_files,
                                    parameters=REFERENCE_PARAMS if params is None else params))
        self.memory_only = memory_only
        self.memory = {}
        self.hits = self.decodes = 0

    def get(self, row, decode):
        key = identity([row["audio_sha256"], self.policy])
        path = Path(row["wav"]).parent / ".live_scoreboard_refs" / (key + ".json")
        if key in self.memory:
            self.hits += 1
            return self.memory[key]
        if not self.memory_only:
            # Source caches must never place dictated text in reports or a repo.
            if not cache_path_allowed(path):
                raise ValueError("Reference cache cannot live in reports/repository; use --memory-references")
            if path.exists():
                cached = load_json(path)
                if cached.get("key") == key:
                    self.hits += 1
                    self.memory[key] = cached["reference"]
                    return cached["reference"]
        reference = decode(row)
        self.decodes += 1
        self.memory[key] = reference
        if not self.memory_only:
            atomic_json(path, dict(key=key, reference=reference))
        return reference


def timed_tokens(reference, tags=()):
    """Map whole-utterance normalization to word times, including spoken punctuation.

    Prefix lengths locate tokens after multiword punctuation removal. Equality
    with the shared normalizer is mandatory; unalignable timestamps are unknown.
    """
    entries = reference["words"]
    full = normalised_words(reference["text"], tags)
    if normalised_words(" ".join(w["word"] for w in entries), tags) != full:
        return None
    mapped = []
    prefix = ""
    for w in entries:
        prefix += " " + w["word"]
        count = len(normalised_words(prefix, tags))
        mapped = mapped[:count]
        while len(mapped) < count:
            mapped.append((float(w["start"]), float(w["end"])))
    if len(mapped) != len(full):
        return None
    return mapped


def score_take(row, reference, hypothesis):
    tags = row.get("tags", [])
    result = measure(reference["text"], hypothesis, tags)
    aligned = _measure(normalised_words(reference["text"], tags),
                       normalised_words(hypothesis, tags), alignment=True)
    times = timed_tokens(reference, tags)
    deleted = aligned["deleted_reference_indices"]
    head = tail = False
    if times:
        start, end = min(t[0] for t in times), max(t[1] for t in times)
        head = any(times[i][0] < start + 1.5 for i in deleted)
        tail = any(times[i][1] > end - 1.5 for i in deleted)
    return dict(id=row["id"], edition=row.get("edition", "unknown"),
                microphone=row.get("microphone", "unknown"), duration_s=row["duration_s"],
                release_s=row["release_s"], release_source=row["release_source"],
                audio_sha256=row["audio_sha256"], **result, lost_head=head, lost_tail=tail,
                timing_known=times is not None, caveat=CAVEAT)


def totals(rows):
    # Deliberately retain all takes. off_script is diagnostic, never an exclusion
    # when the reference is another decoder rather than a fixed reading script.
    def counts(items):
        out = {k: sum(r[k] for r in items) for k in
               ("reference_words", "deletions", "insertions", "substitutions")}
        out["wer"] = sum(out[k] for k in ("deletions", "insertions", "substitutions")) / max(1, out["reference_words"])
        return out
    return dict(takes=len(rows), **counts(rows), strict=counts([r["strict"] for r in rows]),
                release_known=sum(r["release_source"] != "assumed-file-end" for r in rows),
                off_script_flags=sum(r["off_script"] for r in rows),
                decode_failures=sum(r.get("decode_failed", False) for r in rows),
                caveat=rows[0].get("caveat", CAVEAT) if rows else CAVEAT)


def summarize(rows):
    groups = {}
    for name in ("edition", "microphone", "length", "release_timing"):
        buckets = defaultdict(list)
        for r in rows:
            key = ("under-5s" if r["duration_s"] < 5 else "5-to-15s" if r["duration_s"] < 15 else
                   "15-to-30s" if r["duration_s"] < 30 else "30s-plus") if name == "length" else (
                       "assumed" if r["release_source"] == "assumed-file-end" else "recorded") if name == "release_timing" else r[name]
            buckets[key].append(r)
        groups[name] = {k: totals(v) for k, v in sorted(buckets.items())}
    return dict(totals=totals(rows), groups=groups,
                lost_last_1_5s=[r["id"] for r in rows if r["lost_tail"]],
                lost_first_1_5s=[r["id"] for r in rows if r["lost_head"]],
                inserted=[r["id"] for r in rows if r["insertions"]],
                unknown_word_timing=[r["id"] for r in rows if not r["timing_known"]],
                caveat=rows[0].get("caveat", CAVEAT) if rows else CAVEAT, takes=rows)


def gpu_yield(required_mb, *, timeout=600):
    """Cooperate between takes; never touch any app or its process."""
    deadline = time.monotonic() + timeout
    while True:
        probe = subprocess.run(["nvidia-smi", "--query-gpu=memory.free,utilization.gpu",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True)
        free, busy = (int(v.strip()) for v in probe.stdout.splitlines()[0].split(","))
        if free >= required_mb and busy < 70:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("GPU busy: yielded without loading another model")
        time.sleep(2)


def isolated(folder):
    from .replay import isolate
    isolate(folder)
    logging.disable(logging.CRITICAL)
    os.environ.update(HF_HUB_DISABLE_TELEMETRY="1", PYTHONDONTWRITEBYTECODE="1")


def load_model(path, device, *, large=False):
    if device == "cuda":
        gpu_yield(5200 if large else 1800)
        from samsara.cuda_detect import prepare_cuda_dll_search
        prepare_cuda_dll_search()
    from .audio import local_model
    return local_model(path, device=device, compute_type="float16" if device == "cuda" else "int8")


def reference_decode(model, row):
    if digest(row["wav"]) != row["audio_sha256"]:
        raise ValueError("Source WAV changed after selection")
    segments, _ = model.transcribe(read_wav(row["wav"]).astype(np.float32) / 32768,
                                   **REFERENCE_PARAMS)
    segments = list(segments)
    return dict(text=" ".join(s.text.strip() for s in segments),
                words=[dict(word=w.word, start=w.start, end=w.end)
                       for s in segments for w in (s.words or [])])


def replay_function():
    # Reuse the prior 1202/1205 accumulator barrier repair in memory. The
    # production reader cursor can advance before its frame append completes.
    import inspect
    from . import replay
    source = inspect.getsource(replay.run_take)
    old = "while consumer._reader._read_cursor < bus.write_cursor:"
    new = "while (consumer._reader._read_cursor < bus.write_cursor or len(consumer._streaming_frames) < bus.write_cursor):"
    if old in source:
        source = source.replace(old, new)
    namespace = dict(replay.__dict__)
    exec(compile(source, "<live-scoreboard-accumulator-barrier>", "exec"), namespace)
    return namespace["run_take"]


def worker():
    request = json.load(sys.stdin)
    checkout = Path(request["checkout"]).resolve()
    # This module is already loaded from the tool checkout. Production and the
    # replay harness below come from the explicitly selected candidate checkout.
    sys.path.insert(0, str(checkout))
    for name in list(sys.modules):
        if name == "samsara" or name.startswith("samsara.") or name in ("tools", "tools.voice_corpus", "tools.voice_corpus.replay"):
            del sys.modules[name]
    isolated(request["isolation"])
    from tools.voice_corpus import replay
    model = load_model(request["model"], request["device"])
    run_take = replay_function()
    outputs = []
    for row in request["rows"]:
        if digest(row["wav"]) != row["audio_sha256"]:
            raise ValueError("Source WAV changed after selection")
        if request["device"] == "cuda":
            gpu_yield(512)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = run_take(read_wav(row["wav"]), row["release_s"], model, request["settings"])
        outputs.append(dict(id=row["id"], text=result["text"],
                            decode_failed=result["decode_failed"], pcm_preserved=result["pcm_preserved"],
                            captured_samples=result["captured_samples"],
                            release_to_text_ms=result["release_to_text_ms"]))
        time.sleep(.2)
    json.dump(dict(source_sha256=replay.build_fingerprint(), outputs=outputs,
                   device=model.model.device, compute_type=model.model.compute_type), sys.stdout)


def cached_model(name, cache_root):
    repos = {"large-v3": "models--Systran--faster-whisper-large-v3",
             "large-v3-turbo": "models--deepdml--faster-whisper-large-v3-turbo-ct2",
             "distil-large-v3": "models--Systran--faster-distil-whisper-large-v3",
             "small.en": "models--Systran--faster-whisper-small.en"}
    snapshots = Path(cache_root) / repos[name] / "snapshots"
    found = sorted(p.parent for p in snapshots.glob("*/model.bin"))
    return found[-1] if found else None


def markdown(report):
    lines = ["# Live scoreboard", "", report["caveat"], "",
             "All takes included; D/I/S are aligned deletions/insertions/substitutions.",
             "Head/tail flags are proxy word-time deletion estimates, not proof of capture loss.", ""]
    for build, run in report["candidates"].items():
        lines += [f"## {build}", "", f"Every value below: {report['caveat']}", "",
                  "| Group | Takes | Words | D/I/S | WER | Strict WER | Recorded release |",
                  "|---|---:|---:|---|---:|---:|---:|"]
        entries = [("ALL", run["totals"])]
        entries += [(f"{group}: {key}", value) for group, values in run["groups"].items() for key, value in values.items()]
        for name, value in entries:
            lines.append(f"| {name} | {value['takes']} | {value['reference_words']} | {value['deletions']}/{value['insertions']}/{value['substitutions']} | {value['wer']:.2%} | {value['strict']['wer']:.2%} | {value['release_known']} |")
        for key in ("lost_last_1_5s", "lost_first_1_5s", "inserted", "unknown_word_timing"):
            lines += ["", key + ": " + (", ".join(run[key]) or "none")]
    if report.get("proxy_floor"):
        f = report["proxy_floor"]
        lines += ["", f"Proxy sanity check against script: {f['disagreeing_takes']}/{f['takes']} disagree; WER {f['wer']:.2%}, strict {f['strict']['wer']:.2%}; script itself is not verified verbatim speech."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("D:/SamsaraCorpus/live_holds"))
    parser.add_argument("--extra-folder", type=Path, action="append", default=[])
    parser.add_argument("--manifest", type=Path, help="Additional source WAV/sidecar/id/edition records; no transcripts")
    parser.add_argument("--script-corpus", type=Path)
    parser.add_argument("--candidate", type=Path, action="append", default=[])
    parser.add_argument("--candidate-model", type=Path)
    parser.add_argument("--reference-model", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--settings", type=Path, help="Explicit offline settings snapshot")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--isolation", type=Path, required=True)
    parser.add_argument("--memory-references", action="store_true")
    parser.add_argument("--allow-unknown-release", action="store_true")
    parser.add_argument("--allow-unpaired", action="store_true")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args(argv)
    cache_root = args.cache_root or Path.home() / ".cache/huggingface/hub"
    candidate_model = args.candidate_model or cached_model("small.en", cache_root)
    reference_model = args.reference_model or next((p for name in ("large-v3", "large-v3-turbo", "distil-large-v3")
                                                  if (p := cached_model(name, cache_root))), None)
    if not candidate_model or not reference_model:
        raise ValueError("Local candidate/reference models required; no download attempted")
    output, isolation = external_path(args.output), external_path(args.isolation)
    output.parent.mkdir(parents=True, exist_ok=True)
    isolated(isolation)
    settings = load_json(external_path(args.settings)) if args.settings else {}
    settings.update(device=args.device, compute_type="float16" if args.device == "cuda" else "int8")
    rows, skipped = discover(args.corpus, allow_unpaired=args.allow_unpaired)
    for folder in args.extra_folder:
        more, missing = discover(folder, allow_unpaired=args.allow_unpaired)
        rows += more
        skipped += missing
    if args.manifest:
        rows += load_json(external_path(args.manifest))
    rows = prepare(rows, allow_unknown=args.allow_unknown_release)
    rows, duplicates = deduplicate(rows)
    if not rows:
        raise ValueError("No takes available")
    scripts = prepare(discover(args.script_corpus)[0], allow_unknown=False) if args.script_corpus else []
    model_files = model_identity(reference_model)
    candidate_files = model_identity(candidate_model)
    reference_name = Path(reference_model).resolve().parent.parent.name.removeprefix("models--").replace("--", "/")
    caveat = CAVEAT if "large-v3" in reference_name and "turbo" not in reference_name and "distil" not in reference_name else f"vs {reference_name} proxy, not human truth"
    cache = ReferenceCache(model_files, memory_only=args.memory_references)
    model = None
    def decode(row):
        nonlocal model
        if model is None:
            model = load_model(reference_model, args.device, large=True)
        if args.device == "cuda":
            gpu_yield(512)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            value = reference_decode(model, row)
        time.sleep(.2)
        return value
    print(f"Selected {len(rows)} real takes and {len(scripts)} script takes; {caveat}", flush=True)
    references = {}
    for index, row in enumerate(rows, 1):
        references[row["id"]] = cache.get(row, decode)
        if index % 10 == 0 or index == len(rows):
            print(f"Reference progress {index}/{len(rows)}; {caveat}", flush=True)
    floor_rows = []
    for index, row in enumerate(scripts, 1):
        side = load_json(row["sidecar"])
        floor_rows.append(measure(side["expected_text"], cache.get(row, decode)["text"], row["tags"]))
        if index % 20 == 0:
            print(f"Proxy script check {index}/{len(scripts)}", flush=True)
    model = None
    gc.collect()
    report = dict(caveat=caveat, reference_model_files=model_files, reference_parameters=REFERENCE_PARAMS,
                  candidate_model_files=candidate_files, settings_sha256=identity(settings),
                  settings=settings, cache=dict(hits=cache.hits, decodes=cache.decodes, memory_only=args.memory_references),
                  skipped=skipped, duplicate_copies=duplicates, candidates={})
    if floor_rows:
        floor = totals([dict(r, release_source="script") for r in floor_rows])
        floor["disagreeing_takes"] = sum(r["wer"] != 0 for r in floor_rows)
        floor["caveat"] = "large-model proxy vs fixed script; neither is verified human truth"
        report["proxy_floor"] = floor
    for candidate in args.candidate or [Path.cwd()]:
        candidate = candidate.resolve()
        request = dict(checkout=str(candidate), model=str(candidate_model), device=args.device,
                       settings=settings, isolation=str(isolation / identity(str(candidate))[:12]), rows=rows)
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        result = subprocess.run([sys.executable, "-B", "-m", "tools.voice_corpus.live_scoreboard", "--worker"],
                                input=json.dumps(request), capture_output=True, text=True, env=env,
                                cwd=Path(__file__).resolve().parents[2])
        if result.returncode:
            # Child exception text can contain recognition data. Never echo it.
            raise RuntimeError(f"Candidate replay failed (exit {result.returncode}); no transcript/log persisted")
        replayed = json.loads(result.stdout)
        scored = []
        for row, observed in zip(rows, replayed["outputs"], strict=True):
            if row["id"] != observed["id"]:
                raise RuntimeError("Candidate take order changed")
            value = score_take(row, references[row["id"]], observed.pop("text"))
            value.update(observed, caveat=caveat)
            scored.append(value)
        run = summarize(scored)
        run.update(source_sha256=replayed["source_sha256"], device=replayed["device"],
                   compute_type=replayed["compute_type"], caveat=caveat)
        report["candidates"][str(candidate)] = run
        atomic_json(output, report)
        output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
        print(f"{len(scored)} takes; WER {run['totals']['wer']:.2%}; {caveat}", flush=True)
    return report


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    if sys.argv[1:] == ["--worker"]:
        worker()
    else:
        main()
