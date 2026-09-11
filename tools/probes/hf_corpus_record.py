"""Guided hands-free reliability corpus recorder.

Uses only the default sounddevice input and writes 16 kHz mono PCM WAV files.
The recorder is deliberately independent of Samsara's configuration/audio
engine so it cannot change the running application's device state.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import threading
import time
import wave
from array import array
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
DEFAULT_OUT_DIR = Path(r"C:\Users\Morne\Documents\Claude\voice_samples")

READ_SENTENCES = [
    "The quick brown fox crossed the quiet bridge before sunrise.",
    "Please remember that clear speech includes short pauses between ideas.",
    "On Tuesday, July 14, 2026, the package weighed 2.75 kilograms.",
    "Samsara should open the notes window and type this sentence exactly.",
    "I can speak naturally while the television is playing in the background.",
    "When the recording is complete, say send to commit the dictated text.",
]

SCRIPT: list[dict] = []
for i, text in enumerate(READ_SENTENCES, 1):
    SCRIPT.append({"item": f"speech_owner_{i:02d}", "label": "speech_owner",
                   "prompt": f'Read: "{text}"', "expected_text": text,
                   "duration_s": 10.0, "until_enter": True})
for i, action in enumerate(("cough", "clear your throat", "sniff", "laugh", "sigh"), 1):
    SCRIPT.append({"item": f"nonspeech_body_{i:02d}", "label": "nonspeech_body",
                   "prompt": f"Now {action} once or twice, then wait.",
                   "expected_text": "", "duration_s": 5.0, "until_enter": False})
for i, action in enumerate(("type on the keyboard", "click the mouse", "move a cup", "close a door"), 1):
    SCRIPT.append({"item": f"nonspeech_room_{i:02d}", "label": "nonspeech_room",
                   "prompt": f"Now {action}, then wait.", "expected_text": "",
                   "duration_s": 5.0, "until_enter": False})
for i in range(1, 4):
    SCRIPT.append({"item": f"silence_{i:02d}", "label": "silence",
                   "prompt": "Say nothing; keep the room as it normally is.",
                   "expected_text": "", "duration_s": 20.0, "until_enter": False})
for i in range(1, 4):
    SCRIPT.append({"item": f"media_only_{i:02d}", "label": "media_only",
                   "prompt": "Play a show on the TV at your normal volume, say nothing.",
                   "expected_text": "", "duration_s": 30.0, "until_enter": False})
for i in range(1, 4):
    SCRIPT.append({"item": f"speech_owner_over_media_{i:02d}", "label": "speech_owner_over_media",
                   "prompt": 'With the TV still playing, read: "The television is background noise while I dictate this sentence."',
                   "expected_text": "The television is background noise while I dictate this sentence.",
                   "duration_s": 10.0, "until_enter": True})
for i, text in enumerate(READ_SENTENCES[:4], 1):
    SCRIPT.append({"item": f"speech_owner_over_media_leadin_{i:02d}",
                   "label": "speech_owner_over_media_leadin",
                   "prompt": 'TV playing at normal volume; wait for a line of dialogue, THEN press and read.\n'
                             f'Read: "{text}"',
                   "expected_text": text, "duration_s": 10.0, "until_enter": True})
for i in range(1, 3):
    SCRIPT.append({"item": f"wake_over_media_{i:02d}", "label": "wake_over_media",
                   "prompt": "With the TV playing, say the wake phrase then a command.",
                   "expected_text": "", "duration_s": 10.0, "until_enter": True})


def rms_dbfs(samples) -> float:
    """Return RMS in dBFS for numeric samples, or -inf for silence."""
    values = [float(x) for x in samples]
    if not values:
        return float("-inf")
    rms = math.sqrt(sum(x * x for x in values) / len(values))
    if rms <= 0:
        return float("-inf")
    peak = 32768.0 if max(abs(x) for x in values) > 1.5 else 1.0
    return 20.0 * math.log10(min(1.0, rms / peak))


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("manifest must contain a JSON list")
    return data


def _beep() -> None:
    try:
        import winsound
        winsound.Beep(880, 120)
    except Exception:
        print("[beep]")


class RollingRecorder:
    """One stream with a bounded history, including while prompts await input."""
    def __init__(self, lead_in_s: float = 0.0):
        if not math.isfinite(lead_in_s) or lead_in_s < 0:
            raise ValueError("lead-in must be a finite, non-negative number")
        self._ring = deque(maxlen=round(lead_in_s * SAMPLE_RATE))
        self._lock = threading.Lock()
        self._samples = None
        self._press_offset_s = 0.0

    def _callback(self, indata, frames, _time, status):
        if status:
            print(f"[audio] {status}")
        block = indata[:, 0].tolist()
        with self._lock:
            if self._samples is not None:
                self._samples.extend(block)
            self._ring.extend(block)

    def __enter__(self):
        import sounddevice as sd
        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                      dtype="float32", callback=self._callback)
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc)

    def start_take(self) -> None:
        with self._lock:
            self._samples = list(self._ring)
            # Use actual buffered frames, not the requested capacity: the
            # owner can press before the initial ring has filled completely.
            self._press_offset_s = len(self._samples) / SAMPLE_RATE

    def finish_take(self):
        import numpy as np
        with self._lock:
            samples, self._samples = self._samples, None
            press_offset_s = self._press_offset_s
        data = np.asarray(samples, dtype=np.float32)
        pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16)
        return pcm.tolist(), rms_dbfs(pcm), press_offset_s


def _record(item: dict, recorder: RollingRecorder | None = None):
    # Without lead-in, retain the original per-take stream lifetime.
    stream = contextlib.nullcontext(recorder) if recorder is not None else RollingRecorder()
    with stream as source:
        source.start_take()
        if recorder is not None and item["until_enter"]:
            # The persistent stream captures in its callback while input waits.
            # Do not leave a timed-out input thread competing for keep/redo.
            input("Press Enter to stop recording...\n")
            return source.finish_take()
        stop = threading.Event()
        if item["until_enter"]:
            def wait_for_enter():
                input("Press Enter to stop recording...\n")
                stop.set()
            threading.Thread(target=wait_for_enter, daemon=True).start()

        deadline = time.monotonic() + item["duration_s"]
        while time.monotonic() < deadline and not stop.is_set():
            time.sleep(0.05)
        return source.finish_take()


def _write_wav(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(array("h", samples).tobytes())


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--lead-in", type=float, default=0.0,
                        help="Seconds of rolling mic history to prepend to each take")
    parser.add_argument("--only", choices=sorted({item["label"] for item in SCRIPT}),
                        help="Record only this label; retain existing manifest rows")
    args = parser.parse_args(argv)
    if not math.isfinite(args.lead_in) or args.lead_in < 0:
        parser.error("--lead-in must be a finite, non-negative number")
    out = args.out_dir / "hf_corpus"
    manifest_path = out / "manifest.json"
    rows = load_manifest(manifest_path)
    done = {row.get("file") for row in rows}
    out.mkdir(parents=True, exist_ok=True)

    items = [item for item in SCRIPT if not args.only or item["label"] == args.only]
    pending = [item for item in items if f"{item['item']}.wav" not in done]
    # Keep this stream open across prompts, keep/redo decisions and takes.
    stream = (RollingRecorder(args.lead_in) if args.lead_in > 0 and pending
              else contextlib.nullcontext(None))
    with stream as recorder:
        _record_items(items, rows, done, out, manifest_path, args.lead_in, recorder)
    print(f"Total kept: {len(rows)}")
    print(f"Manifest: {manifest_path}")


def _record_items(items, rows, done, out, manifest_path, lead_in_s, recorder):
    def take(item):
        if recorder is not None:
            # No countdown/beep here: either would pollute the measured lead-in.
            input("Press Enter to begin the take...\n")
        else:
            print("Recording starts in 3...")
            for n in (3, 2, 1):
                print(n)
                time.sleep(1)
            _beep()
        return _record(item, recorder)

    for item in items:
        filename = f"{item['item']}.wav"
        if filename in done:
            print(f"[skip] {filename}")
            continue
        print(f"\n{item['prompt']}")
        samples, level, press_offset_s = take(item)
        wav_path = out / filename
        _write_wav(wav_path, samples)
        duration = len(samples) / SAMPLE_RATE
        while True:
            decision = input("keep / redo / skip: ").strip().lower()
            if decision in {"keep", "skip"}:
                if decision == "keep":
                    rows.append({"file": filename, "label": item["label"],
                                 "expected_text": item["expected_text"],
                                 "lead_in_s": lead_in_s,
                                 "press_offset_s": press_offset_s,
                                 "duration_s": round(duration, 3),
                                 "rms_dbfs": round(level, 3),
                                 "timestamp": datetime.now(timezone.utc).isoformat()})
                    write_manifest(manifest_path, rows)
                elif wav_path.exists():
                    wav_path.unlink()
                break
            if decision == "redo":
                print("Redoing this item.")
                samples, level, press_offset_s = take(item)
                _write_wav(wav_path, samples)
                duration = len(samples) / SAMPLE_RATE
                continue
            print("Please answer keep, redo, or skip.")


if __name__ == "__main__":
    main()
