"""Transcribe an audio file that was never spoken into Samsara (queue 123).

Every word Samsara has transcribed until now had to be said live into a
microphone. This takes a file instead: one file, one transcript.

**It never touches the microphone.** No capture starts, no device is opened,
hands-free is not disturbed. The only thing it shares with the live path is
the decoder, and it reuses that rather than copying it:

  loading   faster_whisper.audio.decode_audio -- the DECODER'S OWN loader.
            It is already installed (faster-whisper ships it and depends on
            PyAV), it resamples anything PyAV can demux to 16 kHz mono
            float32, and it means this module does not add a fourth WAV
            reader to the three the benches already carry.
  splitting DictationApp's own _split_audio_at_silences, at the same
            defaults the hotkey path uses.
  decoding  DictationApp._decode_hotkey_audio -- the production decode
            entry, with its segment quality gates, hallucination screening
            and language filter.

Chunk seams: the splitter cuts at pauses and "does NOT discard any samples
-- every sample appears in exactly one returned chunk" (its own docstring).
No overlap and no gap, so nothing can be lost or duplicated at a seam by
construction; chunk texts are joined with a single space. That is a
different problem from the ellipsis seams queue 81 and 100 dealt with, which
are about joining separately DICTATED fragments, not one recording split for
the decoder.

The transcript is TEXT. It is saved or typed; it is never offered to the
command matcher. A file that says "open chrome" produces those two words.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from samsara import audio_tools
from samsara.log import get_logger

logger = get_logger(__name__)

#: What the decoder wants. Matches DictationApp.model_rate.
SAMPLE_RATE = 16000

#: Where the text can go. SAVE is the default on purpose: a two-hour
#: recording typed into whatever happens to have focus is a disaster, and
#: "scratch that" cannot undo it.
DEST_SAVE = "save"
DEST_TYPE = "type"
DEST_BOTH = "both"
DEFAULT_DESTINATION = DEST_SAVE
DESTINATIONS = (DEST_SAVE, DEST_TYPE, DEST_BOTH)

#: The longest file this was actually tested on: 2 h loads in ~1.1 s, splits
#: in ~0.16 s into 360 chunks, costs about 460 MB of RSS, and is lossless at
#: every seam (see the queue 123 report). The binding constraint is MEMORY,
#: not time -- the whole file is held as float32 (16 kHz x 4 bytes = ~230 MB
#: per hour) plus the chunk copies. Past this the job is still attempted,
#: because refusing a recording the user actually has is worse than trying;
#: it is logged, and a MemoryError comes back as an ordinary TranscribeError
#: sentence rather than a crash.
MAX_TESTED_SECONDS = 120 * 60

#: The history entry type. Deliberately NOT "dictation": a transcript of a
#: file the user never spoke must be distinguishable from words they said,
#: in the History page's type filter and in any later analysis.
HISTORY_ENTRY_TYPE = "transcription"

#: Containers worth offering, if the installed PyAV can demux them. The real
#: list is probed (see supported_extensions) -- this is only the candidates.
_CANDIDATES = {
    ".wav": "wav", ".wave": "wav", ".mp3": "mp3", ".flac": "flac",
    ".ogg": "ogg", ".oga": "ogg", ".opus": "opus", ".m4a": "mp4",
    ".mp4": "mp4", ".aac": "aac", ".aiff": "aiff", ".aif": "aiff",
    ".caf": "caf", ".amr": "amr", ".webm": "webm", ".wma": "asf",
}
#: WAV is offered even if the probe fails: `wave` is in the standard library
#: and decode_audio has never not handled it.
_ALWAYS = (".wav", ".wave")

_probed: Optional[tuple] = None


class TranscribeError(Exception):
    """Something the user has to be told, in a sentence."""


class Cancelled(Exception):
    """The user cancelled. Nothing was written."""


@dataclass
class TranscriptResult:
    text: str
    duration_s: float
    chunks: int
    elapsed_s: float
    low_confidence: bool = False
    detected_language: Optional[str] = None
    chunk_texts: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# What we can open
# ---------------------------------------------------------------------------

def supported_extensions(refresh: bool = False) -> tuple:
    """The extensions this installation can actually open, lowercase, sorted.

    ESTABLISHED BY PROBING, not by a hard-coded list: we ask the installed
    PyAV which demuxers it has and keep the candidates it confirms. A build
    with a smaller PyAV therefore offers fewer formats and refuses the rest
    honestly, instead of claiming support and failing at decode time.
    """
    global _probed
    if _probed is not None and not refresh:
        return _probed
    available = set()
    try:
        import av  # noqa: PLC0415  -- a faster-whisper dependency, not a new one
        available = {str(name).lower() for name in av.formats_available}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[FILE-TX] could not probe PyAV formats: %s", exc)
    found = {ext for ext, demuxer in _CANDIDATES.items() if demuxer in available}
    found.update(_ALWAYS)
    _probed = tuple(sorted(found))
    return _probed


def describe_supported() -> str:
    """"WAV, MP3, FLAC, ..." for a message a user reads."""
    return ", ".join(ext.lstrip(".").upper() for ext in supported_extensions())


def file_filter() -> str:
    """A Qt file-dialog filter built from the probed list, so the picker can
    never offer something the decoder would then refuse."""
    patterns = " ".join(f"*{ext}" for ext in supported_extensions())
    return f"Audio files ({patterns});;All files (*)"


def check_supported(path) -> None:
    """Raise TranscribeError naming what IS supported, or return."""
    path = Path(path)
    # The extension is checked FIRST: "convert it" is the more specific and
    # more useful complaint than "no such file" when the path is both.
    extension = path.suffix.lower()
    if extension not in supported_extensions():
        got = extension.lstrip(".").upper() or "that"
        raise TranscribeError(
            f"{got} files are not supported. This build can open: {describe_supported()}. "
            f"Convert the file first -- Samsara will not run a converter for you."
        )
    if path.is_dir():
        raise TranscribeError(f"{path.name} is a folder. Pick one audio file.")
    if not path.exists():
        raise TranscribeError(f"There is no file at {path}.")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_audio(path, sample_rate: int = SAMPLE_RATE):
    """float32 mono at `sample_rate`, through the decoder's own loader.

    This is the fourth-WAV-loader question answered: faster_whisper.audio
    .decode_audio already does resampling and downmixing, it is installed,
    and using it means one loader rather than a private copy here.
    """
    check_supported(path)
    try:
        from faster_whisper.audio import decode_audio  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - faster-whisper always present
        raise TranscribeError(f"The audio decoder is unavailable: {exc}") from exc
    try:
        audio = decode_audio(str(path), sampling_rate=sample_rate)
    except MemoryError as exc:
        raise TranscribeError(
            f"{Path(path).name} is too large to hold in memory. Audio costs about "
            f"230 MB per hour once decoded; split the recording and transcribe the parts."
        ) from exc
    except Exception as exc:
        raise TranscribeError(
            f"{Path(path).name} could not be read as audio ({type(exc).__name__}). "
            f"It may be corrupt, or not really {Path(path).suffix.lstrip('.').upper()}."
        ) from exc
    if audio is None or len(audio) == 0:
        raise TranscribeError(f"{Path(path).name} contains no audio.")
    return audio


# ---------------------------------------------------------------------------
# Transcribing
# ---------------------------------------------------------------------------

def _split(app, audio):
    """The app's own splitter, at its own defaults."""
    splitter = getattr(app, "_split_audio_at_silences", None)
    if splitter is None:
        splitter = audio_tools._split_audio_at_silences
    return splitter(audio, SAMPLE_RATE)


def transcribe_path(app, path, *, progress: Optional[Callable[[float, float], None]] = None,
                    cancel: Optional[Callable[[], bool]] = None) -> TranscriptResult:
    """Decode one file to text. Runs on the caller's thread -- the UI hands
    it to the thread registry.

    `progress(done_seconds, total_seconds)` is called after each chunk.
    `cancel()` is polled before each chunk; a True answer raises Cancelled
    and NOTHING is written, because writing happens in deliver(), after this
    returns.
    """
    started = time.perf_counter()
    audio = load_audio(path)
    total_s = len(audio) / SAMPLE_RATE

    if total_s > MAX_TESTED_SECONDS:
        logger.warning(
            "[FILE-TX] %s is %.0f min, past the %.0f min this was tested to. "
            "Attempting anyway; memory is the limit (~230 MB per hour of audio).",
            Path(path).name, total_s / 60, MAX_TESTED_SECONDS / 60)

    params = app._build_hotkey_transcribe_params()
    chunks = _split(app, audio)
    logger.info("[FILE-TX] %s: %.1fs in %d chunk(s)", Path(path).name, total_s, len(chunks))

    texts: list = []
    low_confidence = False
    detected = None
    done_s = 0.0
    for index, chunk in enumerate(chunks):
        if cancel is not None and cancel():
            logger.info("[FILE-TX] cancelled after %d/%d chunk(s); nothing written",
                        index, len(chunks))
            raise Cancelled()
        chunk_s = len(chunk) / SAMPLE_RATE
        if chunk_s < 0.2:                       # the production path skips these too
            done_s += chunk_s
            continue
        # Each chunk is already under the single-decode ceiling, so this takes
        # _decode_hotkey_audio's "short" branch -- the identical call the live
        # path would make for audio of this length.
        result = app._decode_hotkey_audio(chunk, params, chunk_s, free_form=True)
        if result.text:
            texts.append(result.text.strip())
        low_confidence = low_confidence or bool(result.low_confidence)
        detected = getattr(result, "detected_lang", None) or detected
        done_s += chunk_s
        if progress is not None:
            progress(done_s, total_s)

    if cancel is not None and cancel():
        raise Cancelled()
    # One space between chunk texts. The splitter guarantees every sample is
    # in exactly one chunk, so there is nothing to de-duplicate and nothing
    # missing -- the join is the whole seam handling.
    text = " ".join(t for t in texts if t)
    return TranscriptResult(
        text=text, duration_s=total_s, chunks=len(chunks),
        elapsed_s=time.perf_counter() - started, low_confidence=low_confidence,
        detected_language=detected, chunk_texts=list(texts),
    )


# ---------------------------------------------------------------------------
# Where the text goes
# ---------------------------------------------------------------------------

def transcript_path(audio_path) -> Path:
    """<audio>.txt, beside the audio. Beside it, not in a Samsara folder: the
    user knows where they put the recording."""
    return Path(audio_path).with_suffix(Path(audio_path).suffix + ".txt")


def save_transcript(audio_path, text: str) -> Path:
    out = transcript_path(audio_path)
    try:
        out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise TranscribeError(f"Could not write {out.name}: {exc}") from exc
    logger.info("[FILE-TX] wrote %s (%d chars)", out, len(text))
    return out


def deliver(app, audio_path, text: str, destination: str = DEFAULT_DESTINATION) -> dict:
    """Save and/or type the transcript. Returns what happened.

    Typing goes through DictationApp._paste_preserving_clipboard, the app's
    one delivery chokepoint (clipboard preserved, elevated windows refused,
    undo captured). The text is NEVER handed to the command matcher: this
    function does not know the matcher exists.
    """
    if destination not in DESTINATIONS:
        raise TranscribeError(f"Unknown destination {destination!r}.")
    outcome = {"saved": None, "typed": False, "chars": len(text)}
    if not text:
        return outcome
    if destination in (DEST_SAVE, DEST_BOTH):
        outcome["saved"] = save_transcript(audio_path, text)
    if destination in (DEST_TYPE, DEST_BOTH):
        deliverer = getattr(app, "_paste_preserving_clipboard", None)
        outcome["typed"] = bool(deliverer(text)) if callable(deliverer) else False
    _record_history(app, text)
    return outcome


def _record_history(app, text: str) -> None:
    """One history row, typed "transcription" rather than "dictation", so a
    file transcript is never counted or filtered as something the user
    said. Best effort: history must not break a finished transcript."""
    store = getattr(app, "history_store", None)
    append = getattr(store, "append", None)
    if not callable(append):
        return
    try:
        append(HISTORY_ENTRY_TYPE, text)
    except Exception as exc:
        logger.debug("[FILE-TX] history append failed: %s", exc)
