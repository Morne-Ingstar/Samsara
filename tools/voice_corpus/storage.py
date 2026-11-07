"""Recoverable opaque WAV/JSON pairs, compatible with KeptStore's PCM layout.

KeptStore itself is intentionally not used: it resolves the application profile
and saves recognized text, neither of which belongs in recorder truth.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import uuid
import wave

import numpy as np

from . import RATE
from .script import SCRIPT

DEFAULT_ROOT = Path("D:/SamsaraCorpus")
REPO = Path(__file__).resolve().parents[2]


@contextmanager
def recording_lock(root):
    """OS releases the lock after a crash; two windows cannot interleave state."""
    root = external_path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".recorder.lock").open("a+b") as handle:
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("A corpus recorder is already open") from None
        try:
            yield
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def external_path(path):
    path = Path(path).resolve()
    if path == REPO or REPO in path.parents or any(
            part.casefold().startswith((".samsara", ".samsara-daily")) for part in path.parts):
        raise ValueError("Choose a private folder outside the repository and live profiles")
    return path


def recording_root(path):
    """Real microphone data cannot enter any repository, including another checkout.

    Numeric reports and explicitly synthetic evidence may live in the reports
    tree (whose parent is itself a different repository). The microphone entry
    point applies this stricter rule before opening a stream or writing audio.
    """
    path = external_path(path)
    if any((parent / ".git").exists() for parent in (path, *path.parents)):
        raise ValueError("Record microphone audio outside every repository")
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_wav(path):
    with wave.open(str(path), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (RATE, 1, 2):
            raise ValueError("Corpus WAV must be 16 kHz mono PCM16")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").copy()


def write_wav(path, pcm):
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        target.writeframes(np.asarray(pcm, dtype="<i2").tobytes())


def validate_sidecar(row):
    if row.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{32}", row.get("id", "")):
        raise ValueError("Invalid corpus sidecar")
    for key in ("intended_text", "expected_text", "line_id", "timestamp", "device_name", "script_sha256"):
        if not isinstance(row.get(key), str):
            raise ValueError("Invalid corpus sidecar field: " + key)
    if (row.get("sample_rate") != RATE or row.get("channels") != 1 or
            not isinstance(row.get("tags"), list) or not all(isinstance(t, str) for t in row["tags"]) or
            row.get("kind") not in ("prose", "command", "reply")):
        raise ValueError("Invalid corpus audio/reference schema")
    for key in ("duration_s", "release_s", "peak", "rms"):
        if not isinstance(row.get(key), (int, float)) or not math.isfinite(row[key]) or row[key] < 0:
            raise ValueError("Invalid corpus measurement: " + key)
    if row["release_s"] > row["duration_s"] or row["peak"] > 1 or row["rms"] > 1:
        raise ValueError("Invalid corpus measurement bounds")
    return row


def script_hash(script=SCRIPT):
    return hashlib.sha256(json.dumps(script, sort_keys=True).encode()).hexdigest()


class Session:
    def __init__(self, root=DEFAULT_ROOT, script=SCRIPT):
        self.root = external_path(root)
        self.takes = self.root / "takes"
        self.script = script
        self.script_sha = script_hash(script)
        self.path = self.root / "session.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "schema": 1, "script_sha256": self.script_sha, "selected": {}, "skipped": [], "paused": False}
        if self.state.get("script_sha256") != self.script_sha:
            raise ValueError("Script changed; use a new corpus folder to preserve existing references")
        # Completed sidecar is the commit record. Recover a crash before state save.
        rows = []
        if self.takes.exists():
            for path in self.takes.glob("*.json"):
                row = validate_sidecar(json.loads(path.read_text(encoding="utf-8")))
                if row["script_sha256"] != self.script_sha:
                    raise ValueError("Mixed script versions in corpus")
                if path.stem == row["id"] and path.with_suffix(".wav").is_file():
                    rows.append(row)
        for row in sorted(rows, key=lambda r: (r["timestamp"], r["id"])):
            self.state["selected"][row["line_id"]] = row["id"]
        for line_id, take_id in self.state["selected"].items():
            if not any(r["id"] == take_id and r["line_id"] == line_id for r in rows):
                raise ValueError("A saved take is incomplete; preserve the folder and repair it")
        self.redo_id = None

    @property
    def current(self):
        if self.redo_id:
            return next(x for x in self.script if x["id"] == self.redo_id)
        return next((x for x in self.script if x["id"] not in self.state["selected"]
                     and x["id"] not in self.state["skipped"]), None)

    @property
    def completed(self):
        return len(set(self.state["selected"]) | set(self.state["skipped"]))

    def persist(self):
        atomic_json(self.path, self.state)

    def control(self, phrase):
        phrase = str(phrase).strip().casefold().rstrip(".!?")
        if phrase == "again":
            rows = self.selected()
            if rows:
                self.redo_id = max(rows, key=lambda r: r["timestamp"])["line_id"]
        elif phrase == "skip":
            if self.redo_id:
                self.redo_id = None  # Keep the earlier take if a redo is skipped.
            elif self.current:
                self.state["skipped"].append(self.current["id"])
        elif phrase == "pause session":
            self.state["paused"] = True
        else:
            return False
        self.persist()
        return True

    def selected(self):
        rows = []
        for item in self.script:
            take_id = self.state["selected"].get(item["id"])
            if take_id:
                if not re.fullmatch(r"[0-9a-f]{32}", take_id):
                    raise ValueError("Invalid selected take")
                rows.append(validate_sidecar(json.loads((self.takes / f"{take_id}.json").read_text(encoding="utf-8"))))
        return rows

    def save(self, pcm, release_s, *, device_name, device_id=None, synthetic=False, capture_status=()):
        item = self.current
        if item is None:
            raise ValueError("Session complete")
        pcm = np.asarray(pcm, dtype="<i2").reshape(-1)
        if not pcm.size:
            raise ValueError("No audio frames received; retry this line")
        values = pcm.astype(np.float64) / 32768
        row = dict(item, schema=1, id=uuid.uuid4().hex, line_id=item["id"],
                   script_sha256=self.script_sha, timestamp=datetime.now(timezone.utc).isoformat(),
                   sample_rate=RATE, channels=1, duration_s=len(pcm) / RATE,
                   release_s=min(len(pcm) / RATE, max(0, float(release_s))),
                   peak=float(np.max(np.abs(values))), rms=float(np.sqrt(np.mean(values ** 2))),
                   device_name=str(device_name), device_id=device_id, gain="unchanged system input gain",
                   synthetic=bool(synthetic), capture_status=list(capture_status))
        validate_sidecar(row)
        self.takes.mkdir(parents=True, exist_ok=True)
        path = self.takes / (row["id"] + ".wav")
        write_wav(path.with_suffix(".tmp"), pcm)
        path.with_suffix(".tmp").replace(path)
        row["audio_sha256"] = digest(path)
        atomic_json(path.with_suffix(".json"), row)
        self.state["selected"][item["id"]] = row["id"]
        if item["id"] in self.state["skipped"]:
            self.state["skipped"].remove(item["id"])
        self.redo_id = None
        self.persist()
        return row
