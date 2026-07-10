"""A/B decode-parameter experiment for the "you know" hotkey word-loss defect.

Follows commit 9442536 (which fixed gate integrity but explicitly did not
address this -- the WAV dumps prove the assembled buffer reaches Whisper
untouched, so any remaining loss must live in DECODE PARAMETERS, not
capture). Standalone tool -- no samsara app import beyond what's needed to
reconstruct the real, live initial_prompt (samsara.commands.CommandExecutor
+ samsara.ui.voice_training_qt.VoiceTrainingQt), so the experiment uses the
EXACT prompt content the app actually sends, not an approximation.

Usage:
    python tools/transcribe_ab.py <wav_path> [<wav_path> ...]

Loads each WAV (assumed 16kHz mono, matching DictationApp.model_rate and
tools/stremio-unrelated debug dumps under ~/.samsara/debug/hotkey_*.wav),
transcribes it 5 ways, and prints each variant's text.

Variants:
  1. exact hotkey params        (_build_hotkey_transcribe_params today)
  2. hotkey params, initial_prompt REMOVED
  3. hotkey params, initial_prompt -> conversational register with fillers
  4. exact wake-path params     (get_transcription_params today, balanced)
  5. hotkey params, condition_on_previous_text flipped
"""
import json
import sys
import wave
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# faster-whisper native hallucination-suppression thresholds, mirrored from
# dictation.py's module-level constants (see _NO_SPEECH_THRESHOLD /
# _LOGPROB_THRESHOLD there) -- kept as plain literals here so this tool has
# zero dependency on dictation.py itself (that module has heavy import-time
# side effects unsuitable for a standalone CLI probe).
_NO_SPEECH_THRESHOLD = 0.6
_LOGPROB_THRESHOLD = -1.0

_CONVERSATIONAL_PROMPT = "Yeah, you know, I mean, it's like we said earlier."


def _load_wav_16k_mono_float32(path: Path) -> np.ndarray:
    with wave.open(str(path), 'rb') as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sampwidth != 2:
        raise ValueError(f"{path}: expected 16-bit PCM, got sampwidth={sampwidth}")
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    if n_channels == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    if sr != 16000:
        print(f"  [WARN] {path.name}: sample rate is {sr}Hz, not the expected 16000Hz "
              f"-- resampling with simple linear interpolation")
        duration = len(audio) / sr
        new_len = int(duration * 16000)
        idx = np.linspace(0, len(audio) - 1, new_len)
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def _get_live_initial_prompt() -> str:
    """Reconstruct the EXACT live initial_prompt both the hotkey and wake
    paths currently send -- same call (VoiceTrainingQt.get_initial_prompt())
    against the real config.json / training_data.json in this repo, via a
    minimal fake app (matches the pattern in tests/test_voice_training.py's
    create_test_voice_training helper)."""
    from unittest.mock import Mock
    from samsara.commands import CommandExecutor
    from samsara.ui.voice_training_qt import VoiceTrainingQt

    config_path = _REPO_ROOT / "config.json"
    with open(config_path, encoding='utf-8') as f:
        cfg = json.load(f)

    app = Mock()
    app.config = cfg
    app.config_path = config_path
    app.command_executor = CommandExecutor(app=app)

    vt = VoiceTrainingQt(app)
    return vt.get_initial_prompt() or ""


def _base_hotkey_params(initial_prompt: str) -> dict:
    """Mirrors dictation.py's _build_hotkey_transcribe_params() exactly,
    for performance_mode='balanced' (this repo's live config.json setting)
    and command_mode_recording=False (plain hotkey dictation, not a
    command-mode press)."""
    return {
        'language': 'en',
        'initial_prompt': initial_prompt,
        'no_speech_threshold': _NO_SPEECH_THRESHOLD,
        'log_prob_threshold': _LOGPROB_THRESHOLD,
        'beam_size': 3,
        'vad_filter': False,
        'vad_parameters': dict(min_silence_duration_ms=500, speech_pad_ms=200),
        'condition_on_previous_text': False,
        'without_timestamps': True,
        'word_timestamps': False,
    }


def _base_wake_params(initial_prompt: str) -> dict:
    """Mirrors dictation.py's get_transcription_params() for
    performance_mode='balanced', as called unmodified by
    process_wake_word_buffer() when Silero VAD is available (the normal
    case on this machine)."""
    return {
        'language': 'en',
        'initial_prompt': initial_prompt,
        'no_speech_threshold': _NO_SPEECH_THRESHOLD,
        'log_prob_threshold': _LOGPROB_THRESHOLD,
        'beam_size': 3,
        'vad_filter': True,
        'vad_parameters': dict(min_silence_duration_ms=500, speech_pad_ms=200),
        'condition_on_previous_text': False,
        'without_timestamps': True,
        'word_timestamps': False,
    }


def build_variants(initial_prompt: str) -> "list[tuple[str, dict]]":
    hotkey = _base_hotkey_params(initial_prompt)

    v2 = dict(hotkey)
    v2['initial_prompt'] = ""

    v3 = dict(hotkey)
    v3['initial_prompt'] = _CONVERSATIONAL_PROMPT

    v4 = _base_wake_params(initial_prompt)

    v5 = dict(hotkey)
    v5['condition_on_previous_text'] = not hotkey['condition_on_previous_text']

    return [
        ("1. exact hotkey params", hotkey),
        ("2. hotkey params, initial_prompt REMOVED", v2),
        ("3. hotkey params, initial_prompt -> conversational register", v3),
        ("4. exact wake-path params", v4),
        ("5. hotkey params, condition_on_previous_text flipped", v5),
    ]


def _load_model():
    from faster_whisper import WhisperModel
    try:
        import ctranslate2
        device = "cuda" if 'cuda' in ctranslate2.get_supported_compute_types('cuda') else "cpu"
    except Exception:
        device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    print(f"[transcribe_ab] Loading Whisper 'base' on {device} ({compute_type})...")
    return WhisperModel("base", device=device, compute_type=compute_type,
                         cpu_threads=4, num_workers=2)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <wav_path> [<wav_path> ...]")
        sys.exit(1)

    wav_paths = [Path(p) for p in sys.argv[1:]]
    for p in wav_paths:
        if not p.exists():
            print(f"[transcribe_ab] ERROR: {p} does not exist")
            sys.exit(1)

    initial_prompt = _get_live_initial_prompt()
    print(f"\n[transcribe_ab] Live initial_prompt ({len(initial_prompt)} chars):")
    print(f"  {initial_prompt!r}\n")

    model = _load_model()

    for wav_path in wav_paths:
        print(f"\n{'=' * 70}\n{wav_path}\n{'=' * 70}")
        audio = _load_wav_16k_mono_float32(wav_path)
        print(f"  duration: {len(audio) / 16000:.2f}s")

        for label, params in build_variants(initial_prompt):
            segments, info = model.transcribe(audio, **params)
            text = "".join(seg.text for seg in segments).strip()
            print(f"\n  {label}")
            print(f"    -> {text!r}")


if __name__ == "__main__":
    main()
