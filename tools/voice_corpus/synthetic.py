"""Generate fixed synthetic smoke takes with an installed offline eSpeak NG.

This is a manual validation utility, not an owner-audio importer. No playback.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile
import wave

import numpy as np
from scipy.signal import resample_poly

from . import RATE
from .storage import Session, external_path


def generate(root, executable, selected=(1, 21, 24, 27, 33, 40, 49, 51)):
    root = external_path(root)
    if (root / "session.json").exists():
        raise ValueError("Synthetic generation needs a new folder")
    session = Session(root)
    root.mkdir(parents=True, exist_ok=True)
    for index in range(1, 61):
        item = session.current
        if index not in selected:
            session.control("skip")
            continue
        text = item["intended_text"]
        if text:
            with tempfile.TemporaryDirectory(dir=root) as folder:
                raw = Path(folder) / "tts.wav"
                subprocess.run([str(executable), "-v", "en-us", "-s", "145", "-w", str(raw), text],
                               check=True, timeout=30, capture_output=True)
                with wave.open(str(raw), "rb") as source:
                    audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32)
                    audio = resample_poly(audio, RATE, source.getframerate())
            if index == 27:
                audio[-min(len(audio), RATE):] *= .25
            if index == 33:
                # Insert two genuine five-second gaps without using owner audio.
                audio = np.concatenate([audio[:len(audio)//3], np.zeros(5*RATE),
                    audio[len(audio)//3:2*len(audio)//3], np.zeros(5*RATE), audio[2*len(audio)//3:]])
                audio = np.pad(audio, (0, max(0, 46 * RATE - len(audio))))
            pcm = np.r_[np.zeros(RATE // 3), audio, np.zeros(round(1.2 * RATE))].clip(-32768, 32767).astype("<i2")
        else:
            pcm = np.zeros(round(6.2 * RATE), dtype="<i2")
        session.save(pcm, len(pcm) / RATE - 1.2, device_name="offline eSpeak NG synthetic", synthetic=True)
    return len(session.selected())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tts-exe", type=Path, required=True)
    args = parser.parse_args()
    print(f"Generated {generate(args.output, args.tts_exe)} synthetic takes; no playback.")


if __name__ == "__main__":
    main()
