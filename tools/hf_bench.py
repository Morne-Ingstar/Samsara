"""Offline hands-free corpus benchmark.

The production adapter binds Samsara's own VAD/decode parameter and
post-gate functions.  It does not call ``process_wake_word_buffer`` directly:
that method owns the live app/session/UI state and would inject benchmark text.
The seam is the smallest safe cut immediately around its audio buffer,
``model.transcribe`` call, and result routing; the bench never opens the live
audio device or writes config.json.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import json
import logging
import math
import re
import string
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CORPUS = Path(r"C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus")
LIVE_CONFIG_PATH = Path(r"C:\Users\Morne\.samsara\config.json")
DEFAULT_CONFIG = {
    "model_size": "base",
    "language": "en",
    "performance_mode": "balanced",
    "temperature": 0.0,
    "vad_prob_threshold": 0.5,
    "vad_min_contig_ms": 150,
    "apply_post_gates": True,
    "wake_word": "jarvis",
    "wake_targets": [],
}
MEDIA_SPEECH_LABELS = {"speech_owner_over_media", "speech_owner_over_media_leadin"}
SPEECH_LABELS = {"speech_owner", *MEDIA_SPEECH_LABELS}
FALSE_ACCEPT_LABELS = {"silence", "nonspeech_body", "nonspeech_room", "media_only"}
LANG_PROB_WINDOW_SIZES = (0.8, 1.2, 2.0, 3.0)


def gain_label(gain: float) -> str:
    """Stable, readable suffix for a linear gain, e.g. 1.0, 0.15, 0.08."""
    text = f"{float(gain):.4f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def is_false_accept_label(label: str) -> bool:
    return label in FALSE_ACCEPT_LABELS or label.startswith("media_only@")


def scale_int16(audio, gain: float):
    """Scale float samples by a linear gain through the int16 storage grid.

    The bench replays what the media stream would sound like after ducking,
    so the scaled signal is rounded and clipped exactly as a 16-bit WAV would
    hold it before it reaches the model.
    """
    import numpy as np
    scaled = np.asarray(audio, dtype=np.float32) * float(gain) * 32768.0
    clipped = np.clip(np.round(scaled), -32768.0, 32767.0).astype(np.int16)
    return clipped.astype(np.float32) / 32768.0


def floor_slice(audio, length: int):
    """Take a floor from sample zero, looping as needed to match the media."""
    import numpy as np
    samples = np.asarray(audio, dtype=np.float32)
    if not len(samples):
        raise ValueError("floor clip must not be empty")
    return np.resize(samples, length)


def mix_media(audio, gain: float, floor=None):
    """Duck only the media; keep the added room floor at its recorded level."""
    scaled = scale_int16(audio, gain)
    if floor is None:
        return scaled
    return scale_int16(scaled + floor_slice(floor, len(scaled)), 1.0)


def live_config() -> dict:
    try:
        data = json.loads(LIVE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    keys = ("model_size", "performance_mode", "compute_type", "device", "language",
            "initial_prompt",
            "wake_targets", "wake_word", "ducking")
    return {key: data[key] for key in keys if key in data}


def read_wav(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit mono WAV: {path}")
        rate = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0, rate


def words(text: str) -> list[str]:
    return re.findall(r"[\w']+", (text or "").lower())


def edit_distance(expected: list[str], actual: list[str]) -> tuple[int, int, int, int]:
    """Return (distance, substitutions, deletions, insertions)."""
    n, m = len(expected), len(actual)
    dp = [[(0, 0, 0, 0) for _ in range(m + 1)] for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = (i, 0, i, 0)
    for j in range(1, m + 1):
        dp[0][j] = (j, 0, 0, j)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if expected[i - 1] == actual[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                sub = dp[i - 1][j - 1]
                delete = dp[i - 1][j]
                insert = dp[i][j - 1]
                candidates = [
                    (sub[0] + 1, sub[1] + 1, sub[2], sub[3]),
                    (delete[0] + 1, delete[1], delete[2] + 1, delete[3]),
                    (insert[0] + 1, insert[1], insert[2], insert[3] + 1),
                ]
                dp[i][j] = min(candidates, key=lambda x: x[0])
    return dp[n][m]


def edge_insertions(expected: list[str], actual: list[str]) -> tuple[int, int]:
    """Count output words before/after the expected span."""
    if not actual:
        return 0, 0
    first = next((i for i, token in enumerate(actual) if token in expected), None)
    if first is None:
        return len(actual), 0
    last = max((i for i, token in enumerate(actual) if token in expected), default=first)
    return first, max(0, len(actual) - last - 1)


def rms_dbfs(audio) -> float:
    import numpy as np
    rms = float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float32) ** 2))) if len(audio) else 0.0
    return float("-inf") if rms <= 0 else 20.0 * math.log10(min(1.0, rms))


@dataclass
class BenchResult:
    row: dict
    text: str
    segments: list
    voiced_ms: float
    wall_ms: float
    detected_language: str | None = None
    language_probability: float | None = None


def _source_helpers(relative_path, names):
    """Read selected definitions without executing a package's __init__."""
    path = REPO_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if (isinstance(node, ast.FunctionDef) and node.name in names)
             or (isinstance(node, ast.Assign) and any(
                 isinstance(target, ast.Name) and target.id in names
                 for target in node.targets))]
    module = ModuleType(Path(relative_path).stem + "_offline")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), module.__dict__)
    if not names <= module.__dict__.keys():
        raise RuntimeError(f"missing helpers in {relative_path}: {names - module.__dict__.keys()}")
    return module


def load_production_adapter():
    """Bind only offline decode helpers; never execute the app/log bootstrap."""
    import numpy as np
    from samsara import audio_tools, transcript_gates
    diagnostics = _source_helpers("samsara/diagnostics.py", {"segment_signals"})
    languages = _source_helpers("samsara/languages.py", {"resolve_transcribe_language"})
    constants_module = _source_helpers(
        "samsara/constants.py", {"MODEL_SAMPLE_RATE", "CONTIGUOUS_VAD_PROB_THRESHOLD"})

    path = REPO_ROOT / "dictation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Queue 128 moved these gates into an importable pure module.  Import it
    # directly: scraping an ImportFrom re-export in dictation.py silently
    # misses dependencies whenever that seam moves again.
    # Four thresholds remain local to the decode entry points.  The two
    # extraction-era quality-gate constants are owned by transcript_gates.
    dictation_constants = {
        "_NO_SPEECH_THRESHOLD", "_LOGPROB_THRESHOLD",
        "_GATE_VAD_PROB", "_GATE_MIN_CONTIG_MS",
    }
    gate_constants = {
        "_COMPRESSION_RATIO_THRESHOLD", "_HALLUCINATION_STRING_BLACKLIST",
    }
    constants = dictation_constants | gate_constants
    functions = {
        "_create_whisper_model", "_is_hallucinated_segments",
        "_is_quality_exhausted", "_keep_low_confidence_long_chunk",
        "_apply_segment_quality_gates",
    }
    method_names = {"get_transcription_params", "_vad_probabilities"}
    app = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "DictationApp")

    def _selected(body):
        return [node for node in body
                if (isinstance(node, ast.FunctionDef) and node.name in functions)
                or (isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in constants
                    for target in node.targets))]

    nodes = _selected(tree.body)
    methods = [node for node in app.body
               if isinstance(node, ast.FunctionDef) and node.name in method_names]
    module = ModuleType("hf_bench_production")
    module.__dict__.update(np=np, re=re, string=string, logging=logging,
                           diagnostics=diagnostics, _languages=languages,
                           MODEL_SAMPLE_RATE=constants_module.MODEL_SAMPLE_RATE,
                           CONTIGUOUS_VAD_PROB_THRESHOLD=constants_module.CONTIGUOUS_VAD_PROB_THRESHOLD,
                           transcript_gates=transcript_gates,
                           resample_audio=audio_tools.resample_audio)
    for name in gate_constants | {"_is_hallucinated_segments", "_is_quality_exhausted",
                                  "_keep_low_confidence_long_chunk", "_apply_segment_quality_gates"}:
        module.__dict__[name] = getattr(transcript_gates, name)
    exec(compile(ast.Module(body=nodes + methods, type_ignores=[]), str(path), "exec"),
         module.__dict__)
    required = constants | functions | method_names
    if not required <= module.__dict__.keys():
        raise RuntimeError(f"missing production helpers: {required - module.__dict__.keys()}")
    module.DictationApp = type("OfflineApp", (), {
        name: module.__dict__[name] for name in method_names})
    return module


class RealPipeline:
    """Production adapter with only the live app/session seam isolated."""
    def __init__(self, config: dict):
        production = load_production_adapter()
        self.production = production
        self.config = config
        self.model = production._create_whisper_model(
            config.get("model_size", "base"), device=config.get("device", "auto"),
            # This benchmark decodes serially; a second worker only reserves GPU memory.
            compute_type=config.get("compute_type", "int8"), cpu_threads=4, num_workers=1,
            local_files_only=True,
        )
        self.model_rate = 16000
        self.vad_adapter = self._load_vad_adapter()

    def _load_vad_adapter(self):
        adapter = SimpleNamespace()
        adapter._vad_lock = __import__("threading").Lock()
        adapter._vad_model = None
        adapter._vad_available = False
        try:
            from faster_whisper.utils import get_assets_path
            from faster_whisper.vad import SileroVADModel
            asset = Path(get_assets_path()) / "silero_vad_v6.onnx"
            adapter._vad_model = SileroVADModel(str(asset))
            adapter._vad_available = True
        except Exception:
            pass
        return adapter

    def params(self) -> dict:
        app = SimpleNamespace()
        app.config = self.config
        app.voice_training_window = SimpleNamespace(
            get_initial_prompt=lambda include_vocabulary=True: self.config.get("initial_prompt") or None)
        params = self.production.DictationApp.get_transcription_params(app, include_vocabulary=False)
        params["temperature"] = 0.0
        params["condition_on_previous_text"] = False
        params["vad_filter"] = bool(self.config.get("vad_filter", True))
        params["no_speech_threshold"] = float(self.config.get("no_speech_threshold", 0.6))
        params["log_prob_threshold"] = float(self.config.get("log_prob_threshold", -1.0))
        return params

    def voiced_ms(self, audio, rate: int) -> float:
        if not self.vad_adapter._vad_available:
            frame = max(1, int(rate * 0.032))
            threshold = float(self.config.get("rms_threshold", 0.01))
            return sum(float((audio[i:i + frame] ** 2).mean() ** 0.5) > threshold
                       for i in range(0, len(audio), frame)) * 32.0
        voiced, _total = self.silero_frame_counts(audio, rate)
        return float(voiced * 32.0)

    def silero_frame_counts(self, audio, rate: int) -> tuple[int, int]:
        """Count actual Silero frames; the window experiment must not use RMS."""
        if not self.vad_adapter._vad_available:
            raise RuntimeError("--lang-prob-windows requires the bundled Silero VAD")
        # The installed Silero wrapper writes into a context view of its input.
        # Do not let VAD alter samples subsequently used for sliding windows.
        audio16 = self.production.resample_audio(audio, rate, 16000).copy()
        with self.vad_adapter._vad_lock:
            probs = self.production.DictationApp._vad_probabilities(self.vad_adapter, audio16)
        return int((probs > float(self.config.get("vad_prob_threshold", 0.5))).sum()), len(probs)

    @contextlib.contextmanager
    def gate_overrides(self):
        names = {"_GATE_VAD_PROB": "vad_prob_threshold",
                 "_GATE_MIN_CONTIG_MS": "vad_min_contig_ms",
                 "_NO_SPEECH_THRESHOLD": "no_speech_threshold",
                 "_LOGPROB_THRESHOLD": "log_prob_threshold",
                 "_COMPRESSION_RATIO_THRESHOLD": "compression_ratio_threshold"}
        old = []
        try:
            for name, key in names.items():
                if key in self.config:
                    # Gate functions retain transcript_gates' globals after
                    # direct import, so override there as well as on the
                    # adapter's public compatibility view.
                    for target in (self.production, self.production.transcript_gates):
                        if hasattr(target, name):
                            old.append((target, name, getattr(target, name)))
                            setattr(target, name, self.config[key])
            yield
        finally:
            for target, name, value in reversed(old):
                setattr(target, name, value)

    def run(self, audio, rate: int) -> tuple[str, list, float]:
        audio = self.production.resample_audio(audio, rate, self.model_rate)
        params = self.params()
        started = time.perf_counter()
        segments, info = self.model.transcribe(audio, **params)
        self.last_info = info
        segs = list(segments)
        if self.config.get("apply_post_gates", False):
            with self.gate_overrides():
                text, _low_conf = self.production._apply_segment_quality_gates(
                    segs, params, len(audio) / self.model_rate)
        else:
            text = "".join(getattr(s, "text", "") or "" for s in segs).strip()
        return text, segs, (time.perf_counter() - started) * 1000.0


def _resolved_wake_phrases(row: dict, config: dict) -> list[str]:
    row_phrase = row.get("wake_phrase")
    candidates = []
    if row_phrase:
        candidates.extend(row_phrase if isinstance(row_phrase, (list, tuple)) else [row_phrase])
    candidates.append(config.get("wake_word"))
    candidates.extend(target.get("phrase") for target in config.get("wake_targets", [])
                      if isinstance(target, dict) and target.get("enabled"))
    resolved = []
    for phrase in candidates:
        if not phrase:
            continue
        normalized = " ".join(words(str(phrase)))
        if normalized and normalized not in resolved:
            resolved.append(normalized)
    return resolved


def score_row(result: BenchResult, config: dict | None = None) -> dict:
    row, text = result.row, result.text.strip()
    config = {**DEFAULT_CONFIG, **(config or {})}
    label = row["label"]
    out = {"file": row["file"], "label": label, "text": text,
           "detected_language": result.detected_language,
           "language_probability": result.language_probability,
           "lead_in_s": row.get("lead_in_s", 0.0),
           "press_offset_s": row.get("press_offset_s", 0.0),
           "voiced_ms": round(result.voiced_ms, 2), "wall_ms": round(result.wall_ms, 2),
           "segments": [{k: getattr(s, k, None) for k in
                         ("no_speech_prob", "avg_logprob", "compression_ratio")} for s in result.segments]}
    if label == "wake_over_media":
        phrases = _resolved_wake_phrases(row, config)
        normalized = " ".join(words(text))
        phrase = next((candidate for candidate in phrases
                       if f" {candidate} " in f" {normalized} "), None)
        out["wake_phrases"] = phrases
        out["wake_detected"] = phrase is not None
        out["command_text"] = f" {normalized} ".split(f" {phrase} ", 1)[1].strip() if phrase else ""
    elif is_false_accept_label(label):
        out["false_accept"] = bool(text)
    elif label.startswith("speech_owner@w"):
        # There is no aligned reference transcript for a cropped window.
        out["false_reject"] = not bool(text)
    elif label in SPEECH_LABELS:
        expected = words(row.get("expected_text", ""))
        actual = words(text)
        distance, substitutions, deletions, insertions = edit_distance(expected, actual)
        leading, trailing = edge_insertions(expected, actual)
        out.update({"wer": distance / max(1, len(expected)), "distance": distance,
                    "substitutions": substitutions, "deletions": deletions,
                    "insertions": insertions, "leading_insertions": leading,
                    "trailing_insertions": trailing, "false_reject": not bool(text)})
        if label in MEDIA_SPEECH_LABELS:
            out["pre_roll_contamination_words"] = leading
    return out


def load_config(path: Path | None, use_live: bool = True) -> dict:
    config = dict(DEFAULT_CONFIG)
    if use_live:
        config.update(live_config())
    if path:
        try:
            overrides = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(overrides, dict):
                raise ValueError("config must be an object")
            if isinstance(overrides.get("config"), dict):
                overrides = overrides["config"]
        except (OSError, TypeError, ValueError):
            return dict(DEFAULT_CONFIG)
        config.update(overrides)
    return config


def run_bench(corpus: Path, config: dict, pipeline=None, media_gains=None,
              floor_label: str = "silence", no_floor: bool = False,
              lang_prob_windows: bool = False) -> dict:
    print("RESOLVED: model={} perf={} compute={} post_gates={} language={}".format(
        config.get("model_size", DEFAULT_CONFIG["model_size"]),
        config.get("performance_mode", DEFAULT_CONFIG["performance_mode"]),
        config.get("compute_type", DEFAULT_CONFIG.get("compute_type", "int8")),
        bool(config.get("apply_post_gates", DEFAULT_CONFIG["apply_post_gates"])),
        config.get("language", DEFAULT_CONFIG["language"]),
    ))
    manifest = corpus / "manifest.json"
    rows = json.loads(manifest.read_text(encoding="utf-8"))
    floor_rows = [row for row in rows if row["label"] == floor_label]
    if media_gains and not no_floor and any(row["label"] == "media_only" for row in rows):
        if not floor_rows:
            raise ValueError(f"no floor clips with label {floor_label!r} in {manifest}")
    pipe = pipeline or RealPipeline(config)
    if lang_prob_windows:
        if not callable(getattr(pipe, "silero_frame_counts", None)):
            raise RuntimeError("--lang-prob-windows requires Silero frame counts")
        if isinstance(pipe, RealPipeline) and pipe.params().get("language") is not None:
            raise ValueError("--lang-prob-windows requires automatic language detection")
    scored = []
    window_counts = []
    media_index = 0
    for row_index, row in enumerate(rows, 1):
        audio, rate = read_wav(corpus / row["file"])
        press_offset_s = float(row.get("press_offset_s", 0.0))
        if not math.isfinite(press_offset_s) or not 0 <= press_offset_s <= len(audio) / rate:
            raise ValueError(f"invalid press_offset_s for {row['file']}: {press_offset_s}")
        if media_gains and row["label"] == "media_only":
            # One decode of the same media per requested duck level; the row is
            # reported once per gain and the owner rows are left untouched.
            floor = None
            floor_file = None
            if not no_floor:
                # Pair by media row, keeping the identical floor at every gain.
                floor_file = floor_rows[media_index % len(floor_rows)]["file"]
                floor_audio, floor_rate = read_wav(corpus / floor_file)
                if floor_rate != rate:
                    raise ValueError(f"floor/media sample rates differ: {floor_file}, {row['file']}")
                floor = floor_slice(floor_audio, len(audio))
            media_index += 1
            for gain in media_gains:
                ducked = mix_media(audio, gain, floor)
                started = time.perf_counter()
                text, segs, wall = pipe.run(ducked, rate)
                if wall == 0:
                    wall = (time.perf_counter() - started) * 1000.0
                gain_row = {**row, "label": f"media_only@{gain_label(gain)}"}
                score = score_row(
                    bench_result(pipe, gain_row, text, segs, ducked, rate, wall), config)
                score["media_gain"] = float(gain)
                score["rms_dbfs"] = round(rms_dbfs(ducked), 2)
                score["floor_label"] = None if no_floor else floor_label
                score["floor_file"] = floor_file
                score["floor_rms_dbfs"] = None if floor is None else round(rms_dbfs(floor), 2)
                scored.append(score)
                print(f"{row['file']} @gain={gain_label(gain)}: "
                      f"language={score['detected_language']} p={score['language_probability']}",
                      flush=True)
            if not lang_prob_windows:
                continue
        started = time.perf_counter()
        text, segs, wall = pipe.run(audio, rate)
        if wall == 0:
            wall = (time.perf_counter() - started) * 1000.0
        score = score_row(bench_result(pipe, row, text, segs, audio, rate, wall), config)
        if press_offset_s > 0:
            post_press = audio[round(press_offset_s * rate):]
            post_text, _post_segs, post_wall = pipe.run(post_press, rate)
            score["post_press_text"] = post_text.strip()
            score["post_press_wall_ms"] = round(post_wall, 2)
            post_info = getattr(pipe, "last_info", None)
            score["post_press_detected_language"] = getattr(post_info, "language", None)
            score["post_press_language_probability"] = getattr(post_info, "language_probability", None)
            if row["label"] in SPEECH_LABELS:
                post_leading, _post_trailing = edge_insertions(
                    words(row.get("expected_text", "")), words(post_text))
                score["post_press_leading_insertions"] = post_leading
                # Full WAV minus identical decode settings with the buffer removed.
                # Keep signed deltas: a negative value is also useful evidence.
                score["pre_buffer_leading_insertions_delta"] = (
                    score["leading_insertions"] - post_leading)
        scored.append(score)
        print(f"{row_index}/{len(rows)} {row['file']}: "
              f"language={score['detected_language']} p={score['language_probability']} "
              f"text={'yes' if text.strip() else 'no'}", flush=True)
        if lang_prob_windows and row["label"] in {"speech_owner", "media_only"}:
            windows, counts = decode_language_windows(pipe, row, audio, rate, config)
            scored.extend(windows)
            window_counts.extend(counts)
    result = {"config": config, "rows": scored, "summary": summarize(scored),
              "language_confidence": summarize_language_confidence(scored)}
    if lang_prob_windows:
        result["language_probability_windows"] = summarize_language_windows(scored, window_counts)
    return result


def decode_language_windows(pipe, row, audio, rate, config):
    """Decode full nonoverlapping windows, dropping those below 50% voiced frames."""
    scored, counts = [], []
    for seconds in LANG_PROB_WINDOW_SIZES:
        size = round(seconds * rate)
        label = f"{row['label']}@w{gain_label(seconds)}"
        inventory = {"file": row["file"], "label": label, "window_s": seconds,
                     "candidate_count": len(audio) // size, "kept_count": 0,
                     "dropped_count": 0, "tail_samples_excluded": len(audio) % size,
                     "sample_rate": rate, "dropped_windows": []}
        for start in range(0, len(audio) - size + 1, size):
            window = audio[start:start + size].copy()
            voiced, frames = pipe.silero_frame_counts(window, rate)
            metadata = {"window_s": seconds, "window_start_s": start / rate,
                        "window_end_s": (start + size) / rate,
                        "start_sample": start, "end_sample": start + size,
                        "sample_rate": rate, "voiced_frames": voiced, "vad_frames": frames,
                        "voiced_fraction": voiced / frames if frames else 0.0}
            if not frames or voiced * 2 < frames:
                inventory["dropped_count"] += 1
                inventory["dropped_windows"].append(metadata)
                continue
            text, segs, wall = pipe.run(window, rate)
            info = getattr(pipe, "last_info", None)
            score = score_row(BenchResult(
                {**row, "label": label}, text, segs, voiced * 32.0, wall,
                getattr(info, "language", None), getattr(info, "language_probability", None)), config)
            score.update(metadata)
            scored.append(score)
            inventory["kept_count"] += 1
        counts.append(inventory)
        print(f"{row['file']} @w{gain_label(seconds)}: "
              f"kept={inventory['kept_count']} dropped={inventory['dropped_count']}", flush=True)
    return scored, counts


def summarize_language_windows(rows, counts):
    windows = [row for row in rows if "window_s" in row]
    owner = [row for row in windows if row["label"].startswith("speech_owner@w")]
    media = [row for row in windows if row["label"].startswith("media_only@w")]

    def valid(row):
        p = row.get("language_probability")
        return isinstance(p, (int, float)) and math.isfinite(p) and 0 <= p <= 1

    complete = bool(owner and media) and all(valid(row) for row in owner + media)
    media_max = max(row["language_probability"] for row in media) if complete else None
    # p >= floor accepts; the next float above the maximum rejects every media
    # window and gives the smallest possible owner FRR for a single floor.
    floor = math.nextafter(media_max, math.inf) if complete and media_max < 1.0 else None
    sizes = {}
    for seconds in LANG_PROB_WINDOW_SIZES:
        own = [row for row in owner if row["window_s"] == seconds]
        tv = [row for row in media if row["window_s"] == seconds]
        available = bool(own and tv) and all(valid(row) for row in own + tv)
        owner_min = min(row["language_probability"] for row in own) if available else None
        maximum = max(row["language_probability"] for row in tv) if available else None
        rejected = sum(row["language_probability"] < floor for row in own) if floor is not None else None
        size_counts = [item for item in counts if item["window_s"] == seconds]
        sizes[gain_label(seconds)] = {
            "owner_count": len(own), "media_count": len(tv), "owner_min": owner_min,
            "media_max": maximum, "gap": owner_min - maximum if available else None,
            "owner_false_reject_count": rejected,
            "owner_frr": rejected / len(own) if rejected is not None and own else None,
            "media_accepted_count": sum(row["language_probability"] >= floor for row in tv)
            if floor is not None else None,
            "owner_dropped_count": sum(item["dropped_count"] for item in size_counts
                                       if item["label"].startswith("speech_owner@")),
            "media_dropped_count": sum(item["dropped_count"] for item in size_counts
                                       if item["label"].startswith("media_only@")),
        }
    usable = floor is not None and all(
        sizes[key]["owner_frr"] is not None and sizes[key]["media_count"] > 0
        and sizes[key]["owner_frr"] <= limit for key, limit in (("1.2", 0.05), ("2.0", 0.02)))
    return {"window_sizes_s": list(LANG_PROB_WINDOW_SIZES), "hop": "window",
            "vad": "bundled Silero; independent scan per window; complete 512-sample frames",
            "minimum_voiced_fraction": 0.5, "incomplete_tail": "excluded, not decoded or padded",
            "floor_scope": "all retained raw media_only windows across all four sizes",
            "accept_rule": "language_probability >= floor, independent of post-gate text",
            "frr_denominator": "retained speech_owner windows; VAD drops excluded and reported separately",
            "metadata_complete": complete, "global_media_max": media_max, "floor": floor,
            "verdict": "usable" if usable else "not usable", "sizes": sizes, "counts": counts}


def bench_result(pipe, row, text, segs, audio, rate, wall):
    info = getattr(pipe, "last_info", None)
    return BenchResult(row, text, segs, pipe.voiced_ms(audio, rate), wall,
                       getattr(info, "language", None),
                       getattr(info, "language_probability", None))


def summarize_language_confidence(rows: list[dict]) -> dict:
    groups = {}
    for label in sorted({row["label"] for row in rows}):
        group = [row for row in rows if row["label"] == label]
        probabilities = [row["language_probability"] for row in group
                         if row.get("language_probability") is not None]
        groups[label] = {
            "count": len(group), "measured_count": len(probabilities),
            "detected_languages": sorted({row["detected_language"] for row in group
                                          if row.get("detected_language")}),
            "probability_min": min(probabilities) if probabilities else None,
            "probability_mean": sum(probabilities) / len(probabilities) if probabilities else None,
            "probability_max": max(probabilities) if probabilities else None,
            "text_produced_count": sum(bool(row["text"].strip()) for row in group),
        }
    owner = [row for row in rows if row["label"] in {"speech_owner", "speech_owner_over_media"}]
    noise = [row for row in rows if row["label"] in {"silence", "nonspeech_room", "nonspeech_body"}]
    complete = bool(owner and noise) and all(
        row.get("language_probability") is not None for row in owner + noise)
    owner_min = min(row["language_probability"] for row in owner) if complete else None
    noise_max = max(row["language_probability"] for row in noise) if complete else None
    return {"labels": groups, "owner_count": len(owner), "noise_count": len(noise),
            "owner_minimum": owner_min, "noise_maximum": noise_max,
            "separate": owner_min > noise_max if complete else None}


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for label in sorted({r["label"] for r in rows}):
        group = [r for r in rows if r["label"] == label]
        if is_false_accept_label(label):
            fails = [r for r in group if r.get("false_accept")]
            summary[label] = {"count": len(group), "false_accept_rate": len(fails) / len(group),
                              "phrases": sorted({r["text"] for r in fails})}
        elif label.startswith("speech_owner@w"):
            summary[label] = {"count": len(group),
                              "false_reject_rate": sum(r["false_reject"] for r in group) / len(group),
                              "notes": "post-gate empty text; probability-floor FRR is reported separately"}
        elif label in SPEECH_LABELS:
            summary[label] = {"count": len(group),
                              "wer": sum(r["wer"] for r in group) / len(group),
                              "false_reject_rate": sum(r["false_reject"] for r in group) / len(group),
                              "leading_insertions": sum(r["leading_insertions"] for r in group),
                              "trailing_insertions": sum(r["trailing_insertions"] for r in group)}
            paired = [r for r in group if "pre_buffer_leading_insertions_delta" in r]
            summary[label]["pre_buffer_comparison_count"] = len(paired)
            summary[label]["pre_buffer_leading_insertions_delta_per_utterance"] = (
                sum(r["pre_buffer_leading_insertions_delta"] for r in paired) / len(paired)
                if paired else None)
            if label in MEDIA_SPEECH_LABELS:
                summary[label]["contamination_words_per_utterance"] = (
                    sum(r["leading_insertions"] for r in group) / len(group))
                summary[label]["contaminated_rate"] = (
                    sum(r["leading_insertions"] >= 1 for r in group) / len(group))
                # Retain the legacy output name for existing report consumers.
                summary[label]["pre_roll_contamination_words_per_utterance"] = (
                    sum(r["pre_roll_contamination_words"] for r in group) / len(group))
        else:
            summary[label] = {"count": len(group), "wake_detected": sum(r.get("wake_detected", False) for r in group),
                              "command_text": [r.get("command_text", "") for r in group],
                              "wake_phrases": sorted({phrase for r in group for phrase in r.get("wake_phrases", [])}),
                              "notes": "This matches the wake phrase in the TRANSCRIPT and is not an OpenWakeWord detection."}
    return summary


def language_windows_markdown(result):
    experiment = result["language_probability_windows"]
    floor = experiment["floor"]
    lines = ["## Language probability windows", "",
             "Full windows start at sample zero with hop equal to window size; incomplete tails are excluded. "
             "Silero is scanned independently for each window using complete 512-sample/32 ms frames. "
             "Frames above the configured VAD probability threshold are voiced; windows below 50% are dropped. "
             "Per-file drops, frame counts, timestamps and tail sample counts are in the JSON.", "",
             f"Single probability floor: {floor!r}; accept iff language_probability >= floor. "
             "The floor is the next representable float above the maximum over ALL retained raw media windows. "
             "Probability FRR includes every retained owner window, regardless of post-gate text; "
             "VAD drops are excluded from its denominator. Gain/floor mixes are a separate full-clip comparison.", "",
             "| window s | kept owner/media | dropped owner/media | owner min | media max | gap | owner FRR at floor | media accepted |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for size, data in experiment["sizes"].items():
        stats = " | ".join(f"{data[key]:.9f}" if data[key] is not None else "n/a"
                           for key in ("owner_min", "media_max", "gap"))
        frr = (f"{data['owner_false_reject_count']}/{data['owner_count']} ({data['owner_frr']:.2%})"
               if data["owner_frr"] is not None else "n/a")
        lines.append(f"| {size} | {data['owner_count']}/{data['media_count']} | "
                     f"{data['owner_dropped_count']}/{data['media_dropped_count']} | {stats} | "
                     f"{frr} | {data['media_accepted_count']} |")
    frr_values = [experiment["sizes"][key]["owner_frr"] for key in ("1.2", "2.0")]
    frr_text = [f"{value:.2%}" if value is not None else "n/a" for value in frr_values]
    lines.extend(["", f"VERDICT LINE: {experiment['verdict']}; floor={floor!r}; "
                  f"owner FRR at 1.2 s={frr_text[0]} (limit 5%), "
                  f"at 2.0 s={frr_text[1]} (limit 2%).", "",
                  "## Full-clip media gain language probability", "",
                  "| file | gain | silence floor | detected language | language probability |",
                  "|---|---:|---|---|---:|"])
    for row in result["rows"]:
        if row["label"] == "media_only" or "media_gain" in row:
            lines.append(f"| {row['file']} | {row.get('media_gain', 'raw')} | "
                         f"{row.get('floor_file') or 'none'} | {row['detected_language']} | "
                         f"{row['language_probability']} |")
    lines.extend(["", "## Retained window metadata", "",
                  "| file | label | start-end s | voiced/total frames | language | probability | text after gates |",
                  "|---|---|---|---:|---|---:|---|"])
    for row in result["rows"]:
        if "window_s" in row:
            lines.append(f"| {row['file']} | {row['label']} | "
                         f"{row['window_start_s']:.3f}-{row['window_end_s']:.3f} | "
                         f"{row['voiced_frames']}/{row['vad_frames']} | {row['detected_language']} | "
                         f"{row['language_probability']} | {'yes' if row['text'].strip() else 'no'} |")
    return lines + [""]


def write_outputs(result: dict, out: Path) -> tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    json_path = out.with_suffix(".json")
    md_path = out.with_suffix(".md")
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = ["# Hands-free bench", ""]
    config = result["config"]
    lines.extend([
        f"Resolved config: model={config.get('model_size')}, "
        f"perf={config.get('performance_mode')}, compute={config.get('compute_type')}, "
        f"device={config.get('device')}, language={config.get('language')}, "
        f"post_gates={config.get('apply_post_gates', True)}.",
        "Language metadata is retained before post-gates; text below is after gates.",
        "This is the offline hf_bench decode/segment-gate seam, not live capture or injection.",
        "", "| label | detected language(s) | probability min/mean/max | text produced yes/no |",
        "|---|---|---|---|",
    ])
    confidence = result.get("language_confidence") or summarize_language_confidence(result["rows"])
    for label, data in confidence["labels"].items():
        stats = " / ".join(f"{data[key]:.6f}" if data[key] is not None else "n/a"
                           for key in ("probability_min", "probability_mean", "probability_max"))
        count = data["text_produced_count"]
        lines.append(f"| {label} | {', '.join(data['detected_languages']) or 'n/a'} | "
                     f"{stats} | {'yes' if count else 'no'} ({count}/{data['count']}) |")
    lines.extend(["", f"Owner minimum ({confidence['owner_count']} rows): {confidence['owner_minimum']}",
                  f"Noise maximum ({confidence['noise_count']} rows): {confidence['noise_maximum']}",
                  f"Separate: {confidence['separate']}", ""])
    if "language_probability_windows" in result:
        lines.extend(language_windows_markdown(result))
    media_rows = [row for row in result["rows"] if "media_gain" in row]
    if media_rows:
        config = result["config"]
        capture_duck = float(config.get("ducking", {}).get("hands_free_level", 0.15))
        floor_label = media_rows[0].get("floor_label")
        lines.extend([
            f"Live/resolved config: model={config.get('model_size')}, "
            f"perf={config.get('performance_mode')}, compute={config.get('compute_type')}, "
            f"device={config.get('device')}, post_gates={config.get('apply_post_gates', True)}.",
            f"Floor: {floor_label or 'none (--no-floor, plain scaling)'}. "
            "Floor clips cycle in manifest order, start at sample zero and loop to media length; "
            "each media clip uses the same floor at every gain. Mixes are rounded/clipped to int16.",
            "", "| gain | false-accept |", "|---:|---:|",
        ])
        clean = []
        for gain in sorted({row["media_gain"] for row in media_rows}, reverse=True):
            group = [row for row in media_rows if row["media_gain"] == gain]
            accepts = sum(row["false_accept"] for row in group)
            lines.append(f"| {gain_label(gain)} | {accepts}/{len(group)} |")
            if accepts == 0:
                clean.append((gain, len(group)))
        if clean:
            gain, count = max(clean)
            comparison = "above" if gain > capture_duck else "below" if gain < capture_duck else "equal to"
            verdict = (f"Verdict: largest tested gain with media_only false-accept 0/{count} "
                       f"is {gain_label(gain)} with {floor_label or 'no'} floor; "
                       f"{comparison} the current capture duck of {gain_label(capture_duck)}.")
        else:
            verdict = (f"Verdict: no tested gain has zero media_only false accepts with "
                       f"{floor_label or 'no'} floor; no threshold to compare with "
                       f"the current capture duck of {gain_label(capture_duck)}.")
        lines.extend(["", verdict, "", "## Media rows", "",
                      "| file | gain | floor file | floor RMS dBFS | mix RMS dBFS | false-accept |",
                      "|---|---:|---|---:|---:|---|"])
        for row in media_rows:
            lines.append(f"| {row['file']} | {gain_label(row['media_gain'])} | "
                         f"{row.get('floor_file') or 'none'} | {row.get('floor_rms_dbfs')} | "
                         f"{row['rms_dbfs']} | {row['false_accept']} |")
        lines.extend(["", "## Exact accepted media texts", ""])
        for row in media_rows:
            if row["text"]:
                lines.extend([f"### {row['file']} @ {gain_label(row['media_gain'])}", "",
                              row["text"], ""])
    lines.extend(["## All labels", "", "| label | count | metrics |", "|---|---:|---|"])
    for label, data in result["summary"].items():
        metrics = ", ".join(f"{k}={v}" for k, v in data.items() if k != "count")
        lines.append(f"| {label} | {data['count']} | {metrics} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


def print_delta(base: dict, candidate: dict) -> None:
    print("label | metric | baseline | candidate | delta")
    for label in sorted(set(base["summary"]) | set(candidate["summary"])):
        b, c = base["summary"].get(label, {}), candidate["summary"].get(label, {})
        for metric in sorted(set(b) & set(c)):
            if isinstance(b[metric], (int, float)) and isinstance(c[metric], (int, float)):
                print(f"{label} | {metric} | {b[metric]} | {c[metric]} | {c[metric]-b[metric]:+.4f}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, default=Path("hf_bench"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-post-gates", action="store_true")
    parser.add_argument("--stock-config", action="store_true")
    parser.add_argument("--force-language", metavar="LANG",
                        help="override configured language (ISO code, or auto for detection)")
    parser.add_argument("--lang-prob-windows", action="store_true",
                        help="add 0.8/1.2/2.0/3.0 s owner/media windows with at least 50%% "
                             "Silero-voiced frames; requires automatic language detection")
    parser.add_argument("--media-gain", type=float, action="append", metavar="G",
                        help="repeatable linear gain applied to media_only rows plus an unscaled floor; "
                             "each is decoded and reported as media_only@G")
    parser.add_argument("--floor-label", default="silence",
                        help="manifest label for room-floor clips (default: silence; also nonspeech_room)")
    parser.add_argument("--no-floor", action="store_true",
                        help="plain media scaling without an added floor, reproducing the old bench")
    parser.add_argument("--ab", nargs=2, type=Path, metavar=("BASELINE.json", "CANDIDATE.json"))
    args = parser.parse_args(argv)
    use_live = not args.stock_config
    if args.ab:
        base_config = load_config(args.ab[0], use_live=use_live)
        candidate_config = load_config(args.ab[1], use_live=use_live)
        if args.force_language is not None:
            base_config["language"] = candidate_config["language"] = args.force_language
        if args.no_post_gates:
            base_config["apply_post_gates"] = False
            candidate_config["apply_post_gates"] = False
        base = run_bench(args.corpus, base_config, media_gains=args.media_gain,
                         floor_label=args.floor_label, no_floor=args.no_floor,
                         lang_prob_windows=args.lang_prob_windows)
        cand = run_bench(args.corpus, candidate_config, media_gains=args.media_gain,
                         floor_label=args.floor_label, no_floor=args.no_floor,
                         lang_prob_windows=args.lang_prob_windows)
        print_delta(base, cand)
        return 0
    if not (args.corpus / "manifest.json").exists():
        print(f"real corpus not present: {args.corpus}")
        return 0
    config = load_config(args.config, use_live=use_live)
    if args.force_language is not None:
        config["language"] = args.force_language
    if args.no_post_gates:
        config["apply_post_gates"] = False
    result = run_bench(args.corpus, config, media_gains=args.media_gain,
                       floor_label=args.floor_label, no_floor=args.no_floor,
                       lang_prob_windows=args.lang_prob_windows)
    md, js = write_outputs(result, args.out)
    print(md)
    print(js)
    for label, data in result["summary"].items():
        print(label, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
