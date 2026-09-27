"""Record owner wake samples and train an openWakeWord 0.6.0 ONNX model.

Training work stays under Documents/Claude/proofs/344. Only an accepted model
and its measured metrics are copied into samsara/wake_models.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_PROOF = Path.home() / "Documents" / "Claude" / "proofs" / "344"
DEFAULT_CORPUS = Path.home() / "Documents" / "Claude" / "voice_samples" / "hf_corpus"
TRAINING_PACKAGES = (
    "torchinfo>=1.8,<2", "torchmetrics>=0.11.4,<1", "audiomentations>=0.30,<1",
    "torch-audiomentations>=0.11,<1", "speechbrain>=0.5.14,<1",
    "acoustics>=0.2.6,<1", "scipy<1.16",
)
VOICES = ("en-US-AvaNeural", "en-US-GuyNeural")
GENERIC_SPEECH = (
    "Please place the book beside the window.",
    "We will meet after the morning train arrives.",
    "Turn the page and read the next paragraph.",
    "The music paused while the rain crossed the garden.",
    "Someone left a blue cup on the kitchen table.",
    "Open the calendar and check the next appointment.",
    "The monitor is on the left side of the desk.",
    "This recording contains ordinary conversation.",
)
ADVERSARIAL_SPEECH = (
    "hey cloud", "hey class", "hey claudia", "okay claude",
    "hey crowd", "play cloud", "hey close", "hey clover",
)

_EDGE_ADAPTER = '''"""Real two-voice Edge TTS generator for openWakeWord's generate_samples API."""
import asyncio
import random
import subprocess
import uuid
from pathlib import Path
import edge_tts

VOICES = ("en-US-AvaNeural", "en-US-GuyNeural")

def generate_samples(text, max_samples, batch_size=1, noise_scales=None,
                     noise_scale_ws=None, length_scales=None, output_dir=None,
                     auto_reduce_batch_size=True, file_names=None, **_kwargs):
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    phrases = [text] if isinstance(text, str) else list(text)
    if not phrases:
        raise ValueError("No text supplied for Edge TTS samples")
    existing = len(list(target.glob("*.wav")))
    for i in range(existing, int(max_samples)):
        spoken = phrases[i % len(phrases)]
        voice = VOICES[i % len(VOICES)]
        # Distinct rates/pitches make repeated text useful training examples.
        rng = random.Random(i * 7193 + len(spoken))
        rate = f"{rng.randint(-24, 24):+d}%"
        pitch = f"{rng.randint(-24, 24):+d}Hz"
        name = file_names[i] if file_names and i < len(file_names) else uuid.uuid4().hex + ".wav"
        output = target / name
        if output.exists():
            continue
        mp3 = output.with_suffix(".mp3")
        try:
            asyncio.run(edge_tts.Communicate(spoken, voice=voice, rate=rate,
                                             pitch=pitch).save(str(mp3)))
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                            "-i", str(mp3), "-ar", "16000", "-ac", "1",
                            "-c:a", "pcm_s16le", str(output)], check=True)
        finally:
            mp3.unlink(missing_ok=True)
    return sorted(target.glob("*.wav"))
'''


def phrase_slug(phrase: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")
    if phrase.lower().strip() != "hey claude" or slug != "hey_claude":
        raise ValueError("This trainer accepts only the documented phrase 'hey claude'.")
    return slug


def samples_dir(slug: str) -> Path:
    home = Path(os.environ.get("SAMSARA_HOME_DIR") or Path.home() / ".samsara")
    return home / "wake_samples" / slug


def inventory(phrase: str, proof_dir: Path = DEFAULT_PROOF,
              corpus_dir: Path = DEFAULT_CORPUS) -> dict:
    slug = phrase_slug(phrase)
    sample_files = sorted(samples_dir(slug).glob("*.wav"))
    manifest = corpus_dir / "manifest.json"
    rows = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else []
    negatives = [row for row in rows if row.get("file") and
                 phrase.lower() not in str(row.get("expected_text", "")).lower()]
    return {
        "phrase": phrase.lower().strip(), "model": slug + ".onnx",
        "owner_samples": len(sample_files), "owner_samples_required": 20,
        "negative_clips": len(negatives),
        "negative_hours": round(sum(float(row.get("duration_s", 0)) for row in negatives) / 3600, 6),
        "proof_dir": str(proof_dir), "corpus_dir": str(corpus_dir),
        "command": "python -m openwakeword.train --training_config <generated training_config.yml> "
                   "--augment_clips --train_model",
    }


def record_samples(phrase: str, count: int = 20, seconds: float = 2.5,
                   device: int | None = None) -> int:
    """Beep-guided recording; launched once from a terminal the owner controls by voice."""
    import numpy as np
    import sounddevice as sd
    import winsound
    from math import gcd
    from scipy.signal import resample_poly

    slug = phrase_slug(phrase)
    target = samples_dir(slug)
    target.mkdir(parents=True, exist_ok=True)
    existing = len(list(target.glob("*.wav")))
    if existing >= count:
        print(f"Already have {existing} samples in {target}")
        return existing
    if device is None:
        config = target.parents[1] / "config.json"
        try:
            device = json.loads(config.read_text(encoding="utf-8")).get("microphone")
        except (OSError, ValueError, TypeError):
            device = None
    input_info = sd.query_devices(device, kind="input")
    native_rate = round(float(input_info["default_samplerate"]))
    if native_rate <= 0:
        raise ValueError(f"Invalid default input sample rate: {native_rate}")
    print(f"Input device: {input_info['name']} at {native_rate} Hz; "
          "saving 16000 Hz WAV files.", flush=True)
    print(f"Recording {count - existing} samples. Speak '{phrase}' after each high tone; "
          "the low tone ends a sample. Ctrl+C stops safely.", flush=True)
    next_index = max([int(p.stem) for p in target.glob("*.wav") if p.stem.isdigit()] or [0]) + 1
    while existing < count:
        print(f"Sample {existing + 1} of {count}", flush=True)
        winsound.Beep(880, 150)
        recording = sd.rec(round(seconds * native_rate), samplerate=native_rate, channels=1,
                           dtype="int16", device=device, blocking=True)
        winsound.Beep(440, 100)
        divisor = gcd(native_rate, 16000)
        resampled = resample_poly(np.asarray(recording[:, 0], dtype=np.float32),
                                  16000 // divisor, native_rate // divisor)
        pcm = np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)
        rms = float(np.sqrt(np.mean((pcm.astype(np.float32) / 32768.0) ** 2)))
        if rms < 0.003:
            print("Too quiet; repeating this sample.", flush=True)
            continue
        path = target / f"{next_index:03d}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm.tobytes())
        next_index += 1
        existing += 1
    winsound.Beep(660, 120)
    winsound.Beep(880, 180)
    print(f"Recorded {existing} wake samples in {target}", flush=True)
    return existing


def _venv_python(proof_dir: Path) -> Path:
    return proof_dir / "venv" / "Scripts" / "python.exe"


def prepare_training_env(proof_dir: Path) -> Path:
    """Install missing training imports only in a throwaway child venv."""
    import venv
    python = _venv_python(proof_dir)
    if not python.is_file():
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(proof_dir / "venv")
    check = subprocess.run([str(python), "-c", "import openwakeword.train"],
                           capture_output=True, text=True)
    if check.returncode:
        subprocess.run([str(python), "-m", "pip", "install", *TRAINING_PACKAGES], check=True)
    # 0.6.0 always converts its final ONNX to TFLite. The latter needs an old
    # TensorFlow unavailable on Python 3.11, while Samsara ships only ONNX.
    import openwakeword
    package = Path(openwakeword.__file__).parent
    target = proof_dir / "venv" / "Lib" / "site-packages" / "openwakeword"
    if not target.exists():
        shutil.copytree(package, target)
    train_py = target / "train.py"
    source = train_py.read_text(encoding="utf-8")
    old = ('        convert_onnx_to_tflite(os.path.join(config["output_dir"], '
           'config["model_name"] + ".onnx"),\n'
           '                               os.path.join(config["output_dir"], '
           'config["model_name"] + ".tflite"))')
    if old in source:
        train_py.write_text(source.replace(old,
            '        logging.info("Skipping optional TFLite export; Samsara ships ONNX only")'),
            encoding="utf-8")
    elif 'Skipping optional TFLite export' not in source:
        raise RuntimeError("Installed openWakeWord training export changed; inspect train.py")
    source = train_py.read_text(encoding="utf-8")
    loader_old = "batch_size=None, num_workers=n_cpus, prefetch_factor=16)"
    loader_new = "batch_size=None, num_workers=0)  # Windows: generator has local lambdas"
    if loader_old in source:
        train_py.write_text(source.replace(loader_old, loader_new), encoding="utf-8")
    elif loader_new not in source:
        raise RuntimeError("Installed openWakeWord data loader changed; inspect train.py")
    source = train_py.read_text(encoding="utf-8")
    shape_old = 'F.get_embedding_shape(config["total_length"]//16000)'
    shape_new = 'F.get_embedding_shape(config["total_length"]/16000)'
    if shape_old in source:
        source = source.replace(shape_old, shape_new)
    elif shape_new not in source:
        raise RuntimeError("Installed openWakeWord model shape changed; inspect train.py")
    if 'def f(x, n=16):' in source:
        source = source.replace('def f(x, n=16):', 'def f(x, n=input_shape[0]):')
    elif 'def f(x, n=input_shape[0]):' not in source:
        raise RuntimeError("Installed openWakeWord feature transform changed; inspect train.py")
    sampling_old = ('    n = 50  # sample size\n'
                    '    positive_clips = [str(i) for i in Path(positive_test_output_dir).glob("*.wav")]\n'
                    '    duration_in_samples = []\n'
                    '    for i in range(n):\n'
                    '        sr, dat = scipy.io.wavfile.read(positive_clips[np.random.randint(0, len(positive_clips))])\n'
                    '        duration_in_samples.append(len(dat))')
    sampling_new = ('    positive_clips = [str(i) for i in Path(positive_test_output_dir).glob("*.wav")]\n'
                    '    duration_in_samples = [len(scipy.io.wavfile.read(path)[1])\n'
                    '                           for path in positive_clips]')
    if sampling_old in source:
        source = source.replace(sampling_old, sampling_new)
    elif sampling_new not in source:
        raise RuntimeError("Installed openWakeWord clip length sampling changed; inspect train.py")
    train_py.write_text(source, encoding="utf-8")
    # openWakeWord 0.6.0 leaves memmaps open while replacing feature files.
    # Unix allows unlinking them; Windows raises WinError 32. Patch only the
    # throwaway package copy, not the owner's installed environment.
    utils_py = target / "utils.py"
    utils_source = utils_py.read_text(encoding="utf-8")
    utils_old = "    # Trip empty rows from the mmapped array\n    trim_mmap(output_file)"
    utils_new = ("    # Close the writer before trimming on Windows.\n"
                 "    fp._mmap.close()\n    del fp\n\n" + utils_old)
    if utils_old in utils_source and utils_new not in utils_source:
        utils_py.write_text(utils_source.replace(utils_old, utils_new), encoding="utf-8")
    elif utils_new not in utils_source:
        raise RuntimeError("Installed openWakeWord feature writer changed; inspect utils.py")
    data_py = target / "data.py"
    data_source = data_py.read_text(encoding="utf-8")
    data_old = "    # Remove old mmaped file\n    os.remove(mmap_path)"
    data_new = ("    # Close both mappings before replacing files on Windows.\n"
                "    mmap_file1._mmap.close()\n    mmap_file2._mmap.close()\n"
                "    del mmap_file1, mmap_file2\n\n" + data_old)
    if data_old in data_source and data_new not in data_source:
        data_py.write_text(data_source.replace(data_old, data_new), encoding="utf-8")
    elif data_new not in data_source:
        raise RuntimeError("Installed openWakeWord feature trim changed; inspect data.py")
    subprocess.run([str(python), "-c", "import openwakeword.train"], check=True,
                   stdout=subprocess.DEVNULL)
    return python


def _edge_generator(proof_dir: Path):
    adapter = proof_dir / "edge_adapter"
    adapter.mkdir(parents=True, exist_ok=True)
    module_file = adapter / "generate_samples.py"
    module_file.write_text(_EDGE_ADAPTER, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("wake_edge_samples_344", module_file)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return adapter, module.generate_samples


def _read_pcm(path: Path):
    import numpy as np
    from scipy.io import wavfile
    from scipy.signal import resample_poly
    from math import gcd
    rate, audio = wavfile.read(path)
    if audio.ndim > 1:
        audio = audio.astype(np.float32).mean(axis=1)
    if audio.dtype.kind in "iu":
        audio = audio.astype(np.float32) / max(1, np.iinfo(audio.dtype).max)
    else:
        audio = audio.astype(np.float32)
    if rate != 16000:
        divisor = gcd(rate, 16000)
        audio = resample_poly(audio, 16000 // divisor, rate // divisor)
    return np.clip(audio * 32767, -32768, 32767).astype(np.int16)


def _corpus_split(corpus_dir: Path) -> tuple[list[Path], list[Path]]:
    """Keep every other benchmark clip unseen until model acceptance."""
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    paths = [corpus_dir / row["file"] for row in manifest
             if row.get("file") and (corpus_dir / row["file"]).is_file()
             and "hey claude" not in str(row.get("expected_text", "")).lower()]
    return paths[::2], paths[1::2]


def _feature_data(proof_dir: Path, corpus_dir: Path, generic_dir: Path,
                  clip_samples: int = 32000,
                  negative_paths: list[Path] | None = None) -> tuple[Path, Path]:
    """Generic speech for training; HF benchmark audio reserved for validation."""
    import numpy as np
    from openwakeword.utils import AudioFeatures
    features = AudioFeatures(inference_framework="onnx", device="cpu")

    def windows(paths):
        chunks = []
        for path in paths:
            pcm = _read_pcm(path)
            for start in range(0, len(pcm), clip_samples):
                chunk = pcm[start:start + clip_samples]
                if len(chunk) < clip_samples:
                    chunk = np.pad(chunk, (0, clip_samples - len(chunk)))
                chunks.append(chunk)
        return np.stack(chunks)

    generic_wavs = sorted(generic_dir.glob("*.wav"))
    if negative_paths is None:
        training_paths, held_out_paths = _corpus_split(corpus_dir)
        corpus_wavs = training_paths + held_out_paths
    else:
        corpus_wavs = negative_paths
    if not generic_wavs or not corpus_wavs:
        raise ValueError("Generic speech or HF benchmark negative corpus is empty")
    generic = features.embed_clips(windows(generic_wavs), batch_size=16, ncpu=1)
    validation = features.embed_clips(windows(corpus_wavs), batch_size=16, ncpu=1)
    generic_path = proof_dir / "generic_features.npy"
    validation_path = proof_dir / "hf_validation_features.npy"
    np.save(generic_path, generic.astype(np.float32))
    np.save(validation_path, validation.reshape(-1, validation.shape[-1]).astype(np.float32))
    return generic_path, validation_path


def _write_training_config(proof_dir: Path, adapter: Path, generic: Path,
                           validation: Path, *, synthetic: int, steps: int,
                           negative_weight: int = 200) -> Path:
    import yaml
    config = {
        "model_name": "hey_claude", "target_phrase": ["hey claude"],
        "custom_negative_phrases": list(ADVERSARIAL_SPEECH),
        "n_samples": synthetic, "n_samples_val": max(40, synthetic // 6),
        "tts_batch_size": 1, "augmentation_batch_size": 16,
        "piper_sample_generator_path": str(adapter),
        "output_dir": str(proof_dir / "training"),
        "rir_paths": [], "background_paths": [str(proof_dir / "generic_speech")],
        "background_paths_duplication_rate": [1],
        "false_positive_validation_data_path": str(validation),
        "augmentation_rounds": 1,
        "feature_data_files": {"generic_speech": str(generic)},
        "batch_n_per_class": {"generic_speech": 64,
                              "adversarial_negative": 32, "positive": 32},
        "model_type": "dnn", "layer_size": 32, "steps": steps,
        "max_negative_weight": negative_weight, "target_false_positives_per_hour": 0.2,
    }
    path = proof_dir / "training_config.yml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _copy_owner_samples(phrase: str, train_dir: Path, test_dir: Path):
    samples = sorted(samples_dir(phrase_slug(phrase)).glob("*.wav"))
    if len(samples) < 20:
        raise ValueError("20 owner recordings are required")
    for index, path in enumerate(samples[:20]):
        destination = test_dir if index % 5 == 0 else train_dir
        shutil.copy2(path, destination / f"owner_{index + 1:03d}.wav")
        if destination == train_dir:
            for repeat in range(2, 5):
                shutil.copy2(path, destination / f"owner_{index + 1:03d}_rep{repeat}.wav")


def _copy_hf_training_negatives(paths: list[Path], target: Path):
    from scipy.io import wavfile

    for index, path in enumerate(paths, 1):
        pcm = _read_pcm(path)
        for repeat in range(1, 6):
            wavfile.write(target / f"hf_train_{index:03d}_rep{repeat}.wav", 16000, pcm)


def _score_wav(detector, path: Path) -> tuple[bool, int]:
    import numpy as np
    from samsara.audio_engine.frame import FRAME_SIZE
    from samsara.audio_engine.wake_prefilter import pcm_to_float, oww_prefilter
    pcm = _read_pcm(path)
    detector.reset()
    hits = 0
    cooldown = 0
    for start in range(0, len(pcm), FRAME_SIZE):
        chunk = pcm[start:start + FRAME_SIZE]
        if len(chunk) < FRAME_SIZE:
            chunk = np.pad(chunk, (0, FRAME_SIZE - len(chunk)))
        if detector.process_audio(oww_prefilter(pcm_to_float(chunk))) >= detector._threshold:
            if cooldown <= 0:
                hits += 1
            cooldown = 20  # two seconds at 100 ms per production ring frame
            detector.reset()
        cooldown -= 1
    return hits > 0, hits


def _measure(model: Path, phrase: str, corpus_dir: Path, holdout_dir: Path,
             negative_paths: list[Path] | None = None) -> dict:
    from samsara.wake_detector import WakeWordDetector
    detector = WakeWordDetector(phrase, threshold=0.2, model_path=model)
    if not detector.is_available:
        raise RuntimeError("Trained ONNX did not load in Samsara's detector")
    held_out = sorted(holdout_dir.glob("*.wav"))
    if not held_out:
        raise RuntimeError("No held-out positive recordings")
    accepted = sum(_score_wav(detector, path)[0] for path in held_out)
    owner_holdout = [path for path in held_out if path.name.startswith("owner_")]
    owner_accepted = sum(_score_wav(detector, path)[0] for path in owner_holdout)
    if negative_paths is None:
        negative_paths = _corpus_split(corpus_dir)[1]
    false_accepts = sum(_score_wav(detector, path)[1] for path in negative_paths)
    hours = sum(len(_read_pcm(path)) / 16000 for path in negative_paths) / 3600
    if hours <= 0:
        raise RuntimeError("Negative corpus has no measured duration")
    return {"phrase": phrase, "model": model.name,
            "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
            "threshold": 0.2, "false_accepts": false_accepts,
            "held_out_negative_clips": len(negative_paths),
            "negative_hours": hours, "false_accepts_per_hour": false_accepts / hours,
            "held_out_positives": len(held_out), "true_accepts": accepted,
            "true_accept_rate": accepted / len(held_out),
            "owner_held_out_positives": len(owner_holdout),
            "owner_true_accept_rate": owner_accepted / len(owner_holdout) if owner_holdout else 0.0,
            "negative_corpus": str(corpus_dir)}


def gate_benchmark(corpus_dir: Path, transcript_json: Path) -> dict:
    """Replay the existing negative corpus through the generic OWW gate."""
    from samsara.wake_detector import WakeWordDetector
    from samsara.wake_word_matcher import match_wake_phrase
    detector = WakeWordDetector("jarvis", threshold=0.2)
    if not detector.is_available:
        raise RuntimeError("Generic Jarvis OWW model unavailable")
    rows = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads(transcript_json.read_text(encoding="utf-8"))
    transcripts = {row.get("file"): str(row.get("text") or "")
                   for row in results.get("rows", [])}
    negatives = [row for row in rows if row.get("file")
                 and "hey claude" not in str(row.get("expected_text", "")).lower()]
    admitted = set()
    for row in negatives:
        if _score_wav(detector, corpus_dir / row["file"])[0]:
            admitted.add(row["file"])
    before_false = sum(bool(match_wake_phrase(transcripts.get(row["file"], ""),
                                             "okay claude")[0]) for row in negatives)
    after_false = sum(bool(match_wake_phrase(transcripts.get(row["file"], ""),
                                            "okay claude")[0]) for row in negatives
                      if row["file"] in admitted)
    hours = sum(float(row.get("duration_s", 0)) for row in negatives) / 3600
    return {"negative_hours": hours, "negative_clips": len(negatives),
            "before_whisper_admissions": len(negatives),
            "after_whisper_admissions": len(admitted),
            "before_false_wakes": before_false,
            "after_false_wakes": after_false,
            "before_false_wakes_per_hour": before_false / hours,
            "after_false_wakes_per_hour": after_false / hours,
            "transcripts_available": sum(bool(transcripts.get(row["file"])) for row in negatives)}


def train(phrase: str, proof_dir: Path, corpus_dir: Path, *, synthetic=240,
          steps=2000, negative_weight=200) -> dict:
    """Use openWakeWord's YAML CLI, then deploy only if measured acceptance passes."""
    phrase_slug(phrase)
    if len(list(samples_dir("hey_claude").glob("*.wav"))) < 20:
        raise ValueError("Record 20 owner samples before training")
    proof_dir.mkdir(parents=True, exist_ok=True)
    python = prepare_training_env(proof_dir)
    adapter, generate_samples = _edge_generator(proof_dir)
    training = proof_dir / "training"
    base = training / "hey_claude"
    positive_train, positive_test = base / "positive_train", base / "positive_test"
    negative_train, negative_test = base / "negative_train", base / "negative_test"
    generic_dir = proof_dir / "generic_speech"
    for path in (positive_train, positive_test, negative_train, negative_test, generic_dir):
        path.mkdir(parents=True, exist_ok=True)
    generate_samples(phrase, synthetic, output_dir=positive_train)
    generate_samples(phrase, max(40, synthetic // 6), output_dir=positive_test)
    generate_samples(list(ADVERSARIAL_SPEECH), synthetic, output_dir=negative_train)
    generate_samples(list(ADVERSARIAL_SPEECH), max(40, synthetic // 6), output_dir=negative_test)
    generate_samples(list(GENERIC_SPEECH), 32, output_dir=generic_dir)
    _copy_owner_samples(phrase, positive_train, positive_test)
    hf_train, hf_holdout = _corpus_split(corpus_dir)
    _copy_hf_training_negatives(hf_train, negative_train)
    # Match the exact augmentation clip length chosen by the training CLI.
    import numpy as np
    durations = [len(_read_pcm(path)) for path in positive_test.glob("*.wav")]
    total_length = int(round(float(np.median(durations)) / 1000) * 1000) + 12000
    if total_length < 32000 or abs(total_length - 32000) <= 4000:
        total_length = 32000
    generic, validation = _feature_data(proof_dir, corpus_dir, generic_dir,
                                        clip_samples=total_length,
                                        negative_paths=hf_holdout)
    config = _write_training_config(proof_dir, adapter, generic, validation,
                                    synthetic=synthetic, steps=steps,
                                    negative_weight=negative_weight)
    command = [str(python), "-m", "openwakeword.train", "--training_config", str(config),
               "--augment_clips", "--train_model"]
    feature_files = [base / f"{label}_features_{split}.npy"
                     for label in ("positive", "negative")
                     for split in ("train", "test")]
    expected_rows = {"positive_train": len(list(positive_train.glob("*.wav"))),
                     "positive_test": len(list(positive_test.glob("*.wav"))),
                     "negative_train": len(list(negative_train.glob("*.wav"))),
                     "negative_test": len(list(negative_test.glob("*.wav")))}
    if any(not path.is_file() for path in feature_files) or any(
            np.load(path, mmap_mode="r").shape[0]
            != expected_rows[path.stem.replace("_features", "")]
            for path in feature_files if path.is_file()):
        # openWakeWord skips all augmentation when only the first output exists.
        # Its documented --overwrite flag rebuilds a partial feature set.
        command.append("--overwrite")
    with (proof_dir / "train.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, check=True)
    model = training / "hey_claude.onnx"
    if not model.is_file():
        raise RuntimeError("openWakeWord did not produce hey_claude.onnx")
    metrics = _measure(model, phrase, corpus_dir, positive_test, hf_holdout)
    (proof_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    if (metrics["false_accepts_per_hour"] >= 1
            or metrics["true_accept_rate"] < 0.95
            or metrics["owner_true_accept_rate"] < 0.95):
        raise RuntimeError("Model failed acceptance; kept only in the proof folder")
    destination = ROOT / "samsara" / "wake_models"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(model, destination / model.name)
    (destination / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phrase")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--record-samples", action="store_true")
    parser.add_argument("--gate-benchmark", action="store_true")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--device", type=int)
    parser.add_argument("--proof-dir", type=Path, default=DEFAULT_PROOF)
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--synthetic", type=int, default=240)
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--negative-weight", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        phrase_slug(args.phrase)
        if args.record_samples:
            record_samples(args.phrase, args.count, device=args.device)
            return 0
        if args.gate_benchmark:
            result = gate_benchmark(args.corpus_dir,
                                    ROOT / "perf_artifacts" / "hf_bench_post_fix.json")
            print(json.dumps(result, indent=2))
            return 0
        plan = inventory(args.phrase, args.proof_dir, args.corpus_dir)
        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return 0
        if plan["owner_samples"] < 20:
            parser.error("Record 20 owner samples first with --record-samples")
        metrics = train(args.phrase, args.proof_dir, args.corpus_dir,
                        synthetic=args.synthetic, steps=args.steps,
                        negative_weight=args.negative_weight)
        print(json.dumps(metrics, indent=2))
        return 0
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
