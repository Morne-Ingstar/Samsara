"""Measure CPU Whisper startup and first/warm transcription timings."""
from __future__ import annotations

import argparse
import base64
import io
import os
import statistics
import sys
import time
import wave
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "893" / "five_seconds.wav.b64"
MODELS = ("small.en", "base.en")
COMPUTE_TYPES = ("int8", "float32")


def _duration_seconds(path: Path) -> float:
    path = Path(path)
    source = io.BytesIO(base64.b64decode(path.read_bytes())) if path.suffix == ".b64" else str(path)
    with wave.open(source, "rb") as audio:
        return audio.getnframes() / float(audio.getframerate())


def _materialize_fixture(path: Path, home: Path) -> Path:
    """Decode the text-encoded bundled WAV under the isolated benchmark home."""
    path = Path(path)
    if path.suffix != ".b64":
        return path
    target = Path(home) / "audio" / "five_seconds.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(path.read_bytes()))
    return target


def run_measurements(
    *,
    model_factory: Callable,
    fixture: Path,
    audio_decoder: Callable | None = None,
    models=MODELS,
    compute_types=COMPUTE_TYPES,
    runs: int = 3,
) -> list[dict]:
    """Return load, first-decode and warm-decode measurements per model/type."""
    if runs < 1:
        raise ValueError("runs must be at least 1")
    fixture = Path(fixture)
    duration = _duration_seconds(fixture)
    if audio_decoder is None:
        from faster_whisper.audio import decode_audio
        audio_decoder = lambda path: decode_audio(str(path), sampling_rate=16000)
    records = []
    for model_name in models:
        for compute_type in compute_types:
            loads, first_decodes, warm_decodes = [], [], []
            attempted = 0
            error = ""
            for _ in range(runs):
                attempted += 1
                model = None
                try:
                    started = time.perf_counter()
                    model = model_factory(model_name, compute_type)
                    loads.append(time.perf_counter() - started)
                    samples = audio_decoder(fixture)
                    started = time.perf_counter()
                    segments, _info = model.transcribe(samples, language="en", beam_size=1)
                    list(segments)
                    first_decodes.append(time.perf_counter() - started)
                    started = time.perf_counter()
                    segments, _info = model.transcribe(samples, language="en", beam_size=1)
                    list(segments)
                    warm_decodes.append(time.perf_counter() - started)
                except Exception as exc:  # retain a useful table for unavailable model assets
                    error = f"{type(exc).__name__}: {exc}"
                    break
                finally:
                    del model
            records.append({
                "model": model_name,
                "compute": compute_type,
                "load": statistics.median(loads) if loads else None,
                "first": statistics.median(first_decodes) if first_decodes else None,
                "warm": statistics.median(warm_decodes) if warm_decodes else None,
                "audio": duration,
                "runs": f"{len(loads)}/{attempted}",
                "error": error,
            })
    return records


def _fmt(value):
    return "—" if value is None else f"{value:.2f}s"


def render_markdown(records: list[dict], *, default_model: str, default_compute: str) -> str:
    lines = [
        "# CPU first-start timings",
        "",
        "Runs use CPU only (`CUDA_VISIBLE_DEVICES=''`) and report median seconds over the requested runs.",
        f"Default without CUDA: **{default_model} / {default_compute}**.",
        "",
        "| Model | Compute | Model load | First 5s transcription | Warm 5s transcription | Runs | Result |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in records:
        lines.append(
            f"| {row['model']} | {row['compute']} | {_fmt(row['load'])} | "
            f"{_fmt(row['first'])} | {_fmt(row['warm'])} | {row['runs']} | {row['error'] or 'ok'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=Path("timings.md"))
    parser.add_argument("--home", type=Path, required=True, help="throwaway SAMSARA_HOME_DIR / model cache root")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)

    # Isolate config and model downloads from the user's profile and force CPU.
    args.home.mkdir(parents=True, exist_ok=True)
    os.environ["SAMSARA_HOME_DIR"] = str(args.home)
    os.environ["HF_HOME"] = str(args.home / "huggingface")
    os.environ["CUDA_VISIBLE_DEVICES"] = ""

    import ctranslate2
    from faster_whisper import WhisperModel
    from samsara.config_defaults import DEFAULTS

    supported = set(ctranslate2.get_supported_compute_types("cpu"))
    compute_types = [kind for kind in COMPUTE_TYPES if kind in supported]
    default_model = DEFAULTS["model_size"]
    default_compute = "int8"
    if not compute_types:
        raise RuntimeError(f"CPU supports none of the requested compute types: {sorted(supported)}")
    records = run_measurements(
        model_factory=lambda name, compute: WhisperModel(
            name, device="cpu", compute_type=compute, cpu_threads=4, num_workers=2
        ),
        fixture=_materialize_fixture(args.fixture, args.home),
        compute_types=compute_types,
        runs=args.runs,
    )
    output = render_markdown(records, default_model=default_model, default_compute=default_compute)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
