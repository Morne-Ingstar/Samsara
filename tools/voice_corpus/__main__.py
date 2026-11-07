"""python -m tools.voice_corpus --help"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import uuid

from .storage import DEFAULT_ROOT, Session, atomic_json, digest, external_path, read_wav


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def replay_corpus(args):
    from .replay import isolate
    out = external_path(args.output)
    if out.exists():
        raise ValueError("Choose a new output filename; previous results are never overwritten")
    isolate(out.parent / ("profile_" + uuid.uuid4().hex))
    from .audio import local_model
    from .replay import run_take, build_fingerprint
    from .routing import check
    from .scoring import measure
    session = Session(args.corpus)
    rows = session.selected()
    if not rows:
        raise ValueError("No completed corpus takes are available")
    config = json.loads(external_path(args.settings).read_text(encoding="utf-8-sig")) if args.settings else {}
    # Whitelist settings needed by this lane, so unrelated plugin credentials,
    # personal commands and live state cannot enter reports or the replay app.
    allowed = {"model_size", "language", "performance_mode", "cleanup_mode", "add_trailing_space", "format_numbers",
               "auto_capitalize", "enable_case_formatters", "formatting_tokens", "dictation", "initial_prompt"}
    if set(config) - allowed:
        raise ValueError("Settings snapshot has unsupported keys; see Docs/VOICE_CORPUS.md")
    model_path = Path(args.model).resolve()
    model_files = [model_path / "model.bin", *model_path.glob("*.json"), *model_path.glob("vocabulary.*")]
    identity = dict(model_sha256=_hash({p.name: digest(p) for p in sorted(set(model_files))}), device=args.device,
                    compute_type=args.compute_type, config_sha256=_hash(config), partials=not args.no_partials)
    model = local_model(model_path, device=args.device, compute_type=args.compute_type)
    takes = []
    for row in rows:
        wav = session.takes / (row["id"] + ".wav")
        if digest(wav) != row["audio_sha256"]:
            raise ValueError("A corpus WAV changed after recording")
        runtime_config = dict(config, device=args.device, compute_type=args.compute_type)
        result = run_take(read_wav(wav), row["release_s"], model, runtime_config, partials=not args.no_partials)
        observed = result["raw"] if row["kind"] in ("command", "reply") else result["text"]
        metrics = measure(row["expected_text"], observed)
        route = check(row["intended_text"], observed) if row["kind"] in ("command", "reply") else {}
        takes.append(dict(id=row["id"], line_id=row["line_id"], tags=row["tags"], kind=row["kind"],
                          synthetic=row.get("synthetic", False), **metrics, **route,
                          **{key: result[key] for key in ("release_to_text_ms", "captured_samples", "pcm_preserved",
                             "partials", "tail", "final_state", "delivery_count", "decode_failed", "held_keep_routed")}))
    corpus_identity = [{k: row[k] for k in ("id", "audio_sha256", "expected_text", "release_s", "tags", "kind")} for row in rows]
    run = dict(schema=1, build_id=args.build_id, source_sha256=build_fingerprint(),
               created_at=datetime.now(timezone.utc).isoformat(), corpus_sha256=_hash(corpus_identity),
               settings_sha256=_hash(identity), model_settings=identity, takes=takes,
               scope="accelerated PCM hold replay; real tail clock, guards, postprocessing and fake paste; text routing probes separate")
    atomic_json(out, run)
    print(f"Replayed {len(takes)} takes to fake text. Numeric results saved; no transcript written.")
    return 0


def integration_enabled(root=DEFAULT_ROOT, environ=None):
    environ = os.environ if environ is None else environ
    return (not environ.get("CI") and environ.get("SAMSARA_VOICE_CORPUS") == "1"
            and Path(root).is_dir() and bool(environ.get("SAMSARA_VOICE_MODEL")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private local voice corpus; no cloud or application bootstrap")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="Open the standalone recorder")
    record.add_argument("--corpus", type=Path, default=DEFAULT_ROOT)
    record.add_argument("--settings", type=Path, help="Explicit offline microphone/model snapshot")
    replay = commands.add_parser("replay", help="Offline production hold replay")
    replay.add_argument("--corpus", type=Path, default=DEFAULT_ROOT)
    replay.add_argument("--model", type=Path, required=True, help="Local faster-whisper folder; no downloads")
    replay.add_argument("--build-id", required=True, help="Commit identity supplied by the caller")
    replay.add_argument("--settings", type=Path)
    replay.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    replay.add_argument("--compute-type", default="int8")
    replay.add_argument("--no-partials", action="store_true")
    replay.add_argument("--output", type=Path, required=True)
    score = commands.add_parser("score", help="Aggregate numeric replay results and save a dated baseline")
    score.add_argument("run", type=Path)
    score.add_argument("--output-dir", type=Path)
    comparison = commands.add_parser("compare", help="Compare identical takes/settings across builds")
    comparison.add_argument("before", type=Path)
    comparison.add_argument("after", type=Path)
    comparison.add_argument("--latency-ms", type=float, default=100.)
    comparison.add_argument("--output", type=Path)
    commands.add_parser("devices", help="Detect VB-CABLE without opening a stream")
    playback = commands.add_parser("virtual-play", help="Play only into an explicit VB-CABLE output; no app launch")
    playback.add_argument("wav", type=Path)
    playback.add_argument("--device", type=int, required=True)
    args = parser.parse_args(argv)
    # Production logs may contain recognition text. No transcript in tool logs.
    logging.disable(logging.CRITICAL)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    try:
        if args.command == "record":
            from .audio import settings_snapshot
            from .recorder import run
            snapshot = external_path(args.settings) if args.settings else external_path(args.corpus) / "recorder-settings.json"
            return run(args.corpus, settings_snapshot(snapshot if snapshot.exists() else None))
        if args.command == "replay":
            return replay_corpus(args)
        if args.command == "score":
            from .scoring import score
            run = json.loads(external_path(args.run).read_text(encoding="utf-8"))
            result = score(run)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", run["build_id"])[:64]
            folder = external_path(args.output_dir or args.run.parent)
            path = folder / f"baseline_{safe_id}_{stamp}.json"
            atomic_json(path, result)
            path.with_suffix(".md").write_text(result["summary"] + "\n\n" +
                "| Condition | Takes | WER | Stray | Missing ending |\n|---|---:|---:|---:|---:|\n" +
                "".join(f"| {tag} | {v['takes']} | {v['wer']:.1%} | {v['insertions']} | {v['missing_end_words']} |\n"
                        for tag, v in result["tags"].items()), encoding="utf-8")
            print(result["summary"])
            print("Baseline: " + str(path))
            return 0
        if args.command == "compare":
            from .scoring import compare
            result = compare(*(json.loads(external_path(p).read_text(encoding="utf-8"))
                               for p in (args.before, args.after)), latency_ms=args.latency_ms)
            if args.output:
                atomic_json(external_path(args.output), result)
            print(result["summary"])
            for row in result["regressions"]:
                print(row["line_id"] + ": " + ", ".join(row["regressed"]))
            return int(bool(result["regressions"]))
        from .virtual_mic import detect, play
        result = detect() if args.command == "devices" else play(external_path(args.wav), args.device)
        print(json.dumps(result, indent=2))
        return 0 if args.command != "devices" or result["available"] else 3
    except Exception as error:
        # Known ValueErrors carry only tool wording; never expose decoder errors,
        # configuration contents or an arbitrary exception's private text.
        message = str(error) if type(error) is ValueError else "Operation failed; previous takes are safe. Check the local model, device and folder access."
        if args.command == "record":
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(None, "Record voice corpus", message)
        else:
            print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
