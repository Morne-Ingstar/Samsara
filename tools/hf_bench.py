"""Offline hands-free corpus benchmark.

The production adapter imports Samsara's own VAD/decode parameter and
post-gate functions.  It does not call ``process_wake_word_buffer`` directly:
that method owns the live app/session/UI state and would inject benchmark text.
The seam is the smallest safe cut immediately around its audio buffer,
``model.transcribe`` call, and result routing; the bench never opens the live
audio device or writes config.json.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

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
    keys = ("model_size", "performance_mode", "compute_type", "device",
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


class RealPipeline:
    """Production adapter with only the live app/session seam isolated."""
    def __init__(self, config: dict):
        import dictation as production
        self.production = production
        self.config = config
        self.model = production._create_whisper_model(
            config.get("model_size", "base"), device=config.get("device", "auto"),
            compute_type=config.get("compute_type", "int8"), cpu_threads=4, num_workers=2,
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
        app.config = {"performance_mode": self.config.get("performance_mode", "balanced"),
                      "initial_prompt": self.config.get("initial_prompt", "")}
        app.voice_training_window = SimpleNamespace(
            get_initial_prompt=lambda include_vocabulary=True: self.config.get("initial_prompt") or None)
        params = self.production.DictationApp.get_transcription_params(app, include_vocabulary=False)
        params["language"] = self.config.get("language", "en")
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
        audio16 = self.production.resample_audio(audio, rate, 16000)
        with self.vad_adapter._vad_lock:
            probs = self.production.DictationApp._vad_probabilities(self.vad_adapter, audio16)
        return float((probs > float(self.config.get("vad_prob_threshold", 0.5))).sum() * 32.0)

    @contextlib.contextmanager
    def gate_overrides(self):
        names = {"_GATE_VAD_PROB": "vad_prob_threshold",
                 "_GATE_MIN_CONTIG_MS": "vad_min_contig_ms",
                 "_NO_SPEECH_THRESHOLD": "no_speech_threshold",
                 "_LOGPROB_THRESHOLD": "log_prob_threshold",
                 "_COMPRESSION_RATIO_THRESHOLD": "compression_ratio_threshold"}
        old = {}
        try:
            for name, key in names.items():
                if key in self.config:
                    old[name] = getattr(self.production, name)
                    setattr(self.production, name, self.config[key])
            yield
        finally:
            for name, value in old.items():
                setattr(self.production, name, value)

    def run(self, audio, rate: int) -> tuple[str, list, float]:
        audio = self.production.resample_audio(audio, rate, self.model_rate)
        params = self.params()
        started = time.perf_counter()
        segments, _info = self.model.transcribe(audio, **params)
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
              floor_label: str = "silence", no_floor: bool = False) -> dict:
    print("RESOLVED: model={} perf={} compute={} post_gates={}".format(
        config.get("model_size", DEFAULT_CONFIG["model_size"]),
        config.get("performance_mode", DEFAULT_CONFIG["performance_mode"]),
        config.get("compute_type", DEFAULT_CONFIG.get("compute_type", "int8")),
        bool(config.get("apply_post_gates", DEFAULT_CONFIG["apply_post_gates"])),
    ))
    manifest = corpus / "manifest.json"
    rows = json.loads(manifest.read_text(encoding="utf-8"))
    floor_rows = [row for row in rows if row["label"] == floor_label]
    if media_gains and not no_floor and any(row["label"] == "media_only" for row in rows):
        if not floor_rows:
            raise ValueError(f"no floor clips with label {floor_label!r} in {manifest}")
    pipe = pipeline or RealPipeline(config)
    scored = []
    media_index = 0
    for row in rows:
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
                    BenchResult(gain_row, text, segs, pipe.voiced_ms(ducked, rate), wall), config)
                score["media_gain"] = float(gain)
                score["rms_dbfs"] = round(rms_dbfs(ducked), 2)
                score["floor_label"] = None if no_floor else floor_label
                score["floor_file"] = floor_file
                score["floor_rms_dbfs"] = None if floor is None else round(rms_dbfs(floor), 2)
                scored.append(score)
            continue
        started = time.perf_counter()
        text, segs, wall = pipe.run(audio, rate)
        if wall == 0:
            wall = (time.perf_counter() - started) * 1000.0
        score = score_row(BenchResult(row, text, segs, pipe.voiced_ms(audio, rate), wall), config)
        if press_offset_s > 0:
            post_press = audio[round(press_offset_s * rate):]
            post_text, _post_segs, post_wall = pipe.run(post_press, rate)
            score["post_press_text"] = post_text.strip()
            score["post_press_wall_ms"] = round(post_wall, 2)
            if row["label"] in SPEECH_LABELS:
                post_leading, _post_trailing = edge_insertions(
                    words(row.get("expected_text", "")), words(post_text))
                score["post_press_leading_insertions"] = post_leading
                # Full WAV minus identical decode settings with the buffer removed.
                # Keep signed deltas: a negative value is also useful evidence.
                score["pre_buffer_leading_insertions_delta"] = (
                    score["leading_insertions"] - post_leading)
        scored.append(score)
    return {"config": config, "rows": scored, "summary": summarize(scored)}


def summarize(rows: list[dict]) -> dict:
    summary = {}
    for label in sorted({r["label"] for r in rows}):
        group = [r for r in rows if r["label"] == label]
        if is_false_accept_label(label):
            fails = [r for r in group if r.get("false_accept")]
            summary[label] = {"count": len(group), "false_accept_rate": len(fails) / len(group),
                              "phrases": sorted({r["text"] for r in fails})}
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


def write_outputs(result: dict, out: Path) -> tuple[Path, Path]:
    json_path = out.with_suffix(".json")
    md_path = out.with_suffix(".md")
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = ["# Hands-free bench", ""]
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
        if args.no_post_gates:
            base_config["apply_post_gates"] = False
            candidate_config["apply_post_gates"] = False
        base = run_bench(args.corpus, base_config, media_gains=args.media_gain,
                         floor_label=args.floor_label, no_floor=args.no_floor)
        cand = run_bench(args.corpus, candidate_config, media_gains=args.media_gain,
                         floor_label=args.floor_label, no_floor=args.no_floor)
        print_delta(base, cand)
        return 0
    if not (args.corpus / "manifest.json").exists():
        print(f"real corpus not present: {args.corpus}")
        return 0
    config = load_config(args.config, use_live=use_live)
    if args.no_post_gates:
        config["apply_post_gates"] = False
    result = run_bench(args.corpus, config, media_gains=args.media_gain,
                       floor_label=args.floor_label, no_floor=args.no_floor)
    md, js = write_outputs(result, args.out)
    print(md)
    print(js)
    for label, data in result["summary"].items():
        print(label, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
