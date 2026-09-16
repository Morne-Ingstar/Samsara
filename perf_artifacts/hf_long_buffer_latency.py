"""Measure production long-hold gate overhead; no app, Whisper model or devices."""
import ast
import json
import logging
import sys
import time
from pathlib import Path
from types import MethodType

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.hf_bench import RealPipeline, load_production_adapter


production = load_production_adapter()
production.logger = logging.getLogger("long_gate_latency")
source = ROOT / "dictation.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
names = {"_GATE_MAX_BUFFER_S", "_SANITY_RMS_FLOOR_DB"}
nodes = [n for n in tree.body if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
app_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
methods = {"_buffer_should_skip_decode", "_buffer_has_contiguous_speech", "_zcr_energy_contiguous_speech"}
nodes += [n for n in app_class.body if isinstance(n, ast.FunctionDef) and n.name in methods]
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), production.__dict__)
app = RealPipeline.__new__(RealPipeline)._load_vad_adapter()
if not app._vad_available:
    raise RuntimeError("bundled ONNX VAD is unavailable; do not report fallback latency as VAD")
for name in methods | {"_vad_probabilities"}:
    setattr(app, name, MethodType(getattr(production, name), app))

rate = 16000
silence = np.zeros(30 * rate, dtype=np.float32)
delayed = silence.copy()
t = np.arange(20 * rate) / rate
delayed[10 * rate:] = 0.04 * np.sin(2 * np.pi * 220 * t)
result = {"duration_s": 30, "sample_rate": rate, "prefix_s": production._GATE_MAX_BUFFER_S,
          "rms_constant": "_SANITY_RMS_FLOOR_DB", "rms_floor_dbfs": production._SANITY_RMS_FLOOR_DB,
          "vad": "faster-whisper bundled Silero ONNX", "iterations": 30,
          "baseline": "previous long-buffer bypass (no RMS/VAD); timings are added gate work"}
for label, audio in (("silence", silence), ("speech_after_10s", delayed)):
    started = time.perf_counter()
    skipped = app._buffer_should_skip_decode(audio, rate)
    first_ms = (time.perf_counter() - started) * 1000
    timings = []
    for _ in range(result["iterations"]):
        started = time.perf_counter()
        assert app._buffer_should_skip_decode(audio, rate) is skipped
        timings.append((time.perf_counter() - started) * 1000)
    result[label] = {"skipped": skipped, "first_call_ms": first_ms,
                     "mean_ms": float(np.mean(timings)), "p50_ms": float(np.median(timings)),
                     "p95_ms": float(np.percentile(timings, 95)), "samples_ms": timings}
assert result["silence"]["skipped"] and not result["speech_after_10s"]["skipped"]
out = ROOT / "perf_artifacts/hf_long_buffer_latency.json"
out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps({k: v if not isinstance(v, dict) else {a: b for a, b in v.items() if a != "samples_ms"}
                  for k, v in result.items()}, indent=2))
