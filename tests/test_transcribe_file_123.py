"""Queue 123: transcribe an audio file that was never spoken live.

Nothing here imports dictation.py and nothing here loads a Whisper model.

  * The LOADER is real: `faster_whisper.audio.decode_audio` runs against
    WAVs written by these tests. That is the point of reusing it.
  * The SPLITTER is real too, and is the production one -- extracted from
    dictation.py's source by AST and executed in isolation, so the seam
    property is proved against the code that actually ships rather than a
    copy. (Importing dictation would start the app's module body.)
  * Only the DECODER is faked, because it is the one piece that needs a
    2 GB model. The fake records exactly what it was handed, which is how
    the "no microphone" and "never dispatched" claims are checked.
"""

from __future__ import annotations

import ast
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from samsara import transcribe_file as tf

RATE = tf.SAMPLE_RATE


# ---------------------------------------------------------------------------
# The real production splitter, without importing dictation
# ---------------------------------------------------------------------------

def _production_splitter():
    source = (REPO / "dictation.py").read_text(encoding="utf-8", errors="replace")
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == "_split_audio_at_silences")
    namespace = {"np": np}
    exec(compile(ast.Module([node], []), "<split>", "exec"), namespace)  # noqa: S102
    return namespace["_split_audio_at_silences"]


SPLIT = _production_splitter()


def _speech(seconds, seed=0, level=0.2):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(RATE * seconds)) * level).astype(np.float32)


def _silence(seconds):
    return np.zeros(int(RATE * seconds), dtype=np.float32)


def _write_wav(path, audio, rate=RATE):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return Path(path)


# ---------------------------------------------------------------------------
# A fake app. It cannot record, and it says so if asked.
# ---------------------------------------------------------------------------

class _Decode:
    def __init__(self, text):
        self.text = text
        self.low_confidence = False
        self.detected_lang = "en"
        self.seg_list = []
        self.diag_path = "short"
        self.language_rejected = False


class _App:
    """Only the four things transcribe_file touches, plus tripwires for the
    capture API it must never touch."""

    def __init__(self, texts=None, deliver_ok=True):
        self.texts = list(texts) if texts else None
        self.decoded: list = []
        self.params_built = 0
        self.typed: list = []
        self.chips: list = []
        self.history: list = []
        self._deliver_ok = deliver_ok
        # Hands-free / capture state. Nothing here may change.
        self.recording = False
        self.continuous_active = True
        self.wake_word_active = True
        self.command_mode_active = False
        self.snoozed = False
        self.microphone = 7
        self._split_audio_at_silences = staticmethod(SPLIT).__func__

    # -- the reused production seams -------------------------------------
    def _build_hotkey_transcribe_params(self):
        self.params_built += 1
        return {"beam_size": 5, "vad_filter": False}

    def _decode_hotkey_audio(self, chunk, params, duration, free_form=True):
        self.decoded.append((len(chunk), duration, free_form))
        if self.texts is not None:
            index = len(self.decoded) - 1
            return _Decode(self.texts[index] if index < len(self.texts) else "")
        return _Decode(f"c{len(self.decoded)}")

    # -- delivery ---------------------------------------------------------
    def _paste_preserving_clipboard(self, text, before_paste=None):
        self.typed.append(text)
        return self._deliver_ok

    def _show_outcome_chip(self, label, kind):
        self.chips.append((label, kind))

    class _Store:
        def __init__(self, sink):
            self._sink = sink

        def append(self, entry_type, text):
            self._sink.append((entry_type, text))

    @property
    def history_store(self):
        return _App._Store(self.history)

    # -- tripwires: the microphone --------------------------------------
    def start_recording(self, *a, **k):        # pragma: no cover - must not run
        raise AssertionError("transcribe_file opened the microphone")

    def start_continuous(self, *a, **k):       # pragma: no cover
        raise AssertionError("transcribe_file started capture")

    def stop_recording(self, *a, **k):         # pragma: no cover
        raise AssertionError("transcribe_file touched capture")


def _capture_state(app):
    return (app.recording, app.continuous_active, app.wake_word_active,
            app.command_mode_active, app.snoozed, app.microphone)


@pytest.fixture
def wav(tmp_path):
    """Seven seconds: speech, a real pause, speech."""
    audio = np.concatenate([_speech(3, seed=1), _silence(1), _speech(3, seed=2)])
    return _write_wav(tmp_path / "meeting.wav", audio)


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------

def test_the_supported_list_is_probed_from_the_installed_decoder():
    """Not a hard-coded list: a build with a smaller PyAV must offer less and
    refuse the rest honestly, rather than claim support and fail at decode."""
    import av

    extensions = tf.supported_extensions(refresh=True)
    assert ".wav" in extensions
    demuxers = {str(n).lower() for n in av.formats_available}
    for extension in extensions:
        if extension in tf._ALWAYS:
            continue
        assert tf._CANDIDATES[extension] in demuxers, extension
    # Anything the probe rejected really is absent from this PyAV.
    for extension, demuxer in tf._CANDIDATES.items():
        if demuxer not in demuxers and extension not in tf._ALWAYS:
            assert extension not in extensions


@pytest.mark.parametrize("name", ["notes.docx", "clip.xyz", "song.mid", "noext"])
def test_an_unsupported_format_is_refused_naming_what_is_supported(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"not audio")
    with pytest.raises(tf.TranscribeError) as exc:
        tf.load_audio(path)
    message = str(exc.value)
    assert "not supported" in message
    assert "WAV" in message                       # it names what IS supported
    assert "converter" in message                 # and that we will not convert


def test_a_supported_extension_that_is_not_really_audio_fails_in_words(tmp_path):
    path = tmp_path / "liar.wav"
    path.write_bytes(b"this is not a wav at all")
    with pytest.raises(tf.TranscribeError) as exc:
        tf.load_audio(path)
    assert "could not be read as audio" in str(exc.value)


def test_a_missing_file_and_a_folder_are_told_apart(tmp_path):
    with pytest.raises(tf.TranscribeError, match="no file at"):
        tf.load_audio(tmp_path / "gone.wav")
    folder = tmp_path / "album.wav"
    folder.mkdir()
    with pytest.raises(tf.TranscribeError, match="folder"):
        tf.load_audio(folder)


def test_the_picker_filter_cannot_offer_what_the_loader_would_refuse():
    patterns = tf.file_filter().split(";;")[0]
    for extension in tf.supported_extensions():
        assert f"*{extension}" in patterns


# ---------------------------------------------------------------------------
# Loading -- the decoder's own loader, on real files
# ---------------------------------------------------------------------------

def test_a_real_wav_loads_as_float32_mono_at_the_model_rate(wav):
    audio = tf.load_audio(wav)
    assert audio.dtype == np.float32
    assert audio.ndim == 1
    assert abs(len(audio) / RATE - 7.0) < 0.05


def test_a_stereo_file_at_another_rate_is_downmixed_and_resampled(tmp_path):
    """The reason for reusing decode_audio rather than writing a fourth WAV
    reader: the three bench loaders all assume mono int16 at the model rate."""
    rng = np.random.default_rng(3)
    stereo = (rng.standard_normal((44100 * 2, 2)) * 0.2).astype(np.float32)
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes((stereo * 32767).astype(np.int16).tobytes())

    audio = tf.load_audio(path)
    assert audio.ndim == 1                        # downmixed
    assert abs(len(audio) / RATE - 2.0) < 0.05    # resampled to 16 kHz


def test_an_empty_file_is_refused_rather_than_transcribed_as_nothing(tmp_path):
    path = _write_wav(tmp_path / "silent.wav", np.zeros(0, dtype=np.float32))
    with pytest.raises(tf.TranscribeError):
        tf.load_audio(path)


# ---------------------------------------------------------------------------
# The seam -- against the production splitter
# ---------------------------------------------------------------------------

def test_the_splitter_loses_and_duplicates_nothing_at_any_seam():
    """The whole chunk-seam answer, asserted on the shipping algorithm: the
    concatenation of the chunks IS the input, sample for sample. No overlap
    to duplicate a word, no gap to drop one."""
    audio = np.concatenate([
        part for i in range(9) for part in (_speech(8, seed=i), _silence(2))
    ])
    chunks = SPLIT(audio, RATE)
    assert len(chunks) > 1, "the fixture must actually split"
    assert sum(len(c) for c in chunks) == len(audio)
    assert np.array_equal(np.concatenate(chunks), audio)


def test_a_long_file_crossing_a_chunk_boundary_keeps_every_chunk_text(tmp_path):
    """End to end over a real 90 s file: every chunk the splitter produced is
    decoded exactly once and every chunk's text appears exactly once, in
    order, joined by one space."""
    audio = np.concatenate([
        part for i in range(9) for part in (_speech(8, seed=i), _silence(2))
    ])
    path = _write_wav(tmp_path / "long.wav", audio)
    words = [f"part{i}" for i in range(40)]
    app = _App(texts=words)

    result = tf.transcribe_path(app, path)

    assert result.chunks > 1
    assert len(app.decoded) == result.chunks           # each chunk decoded ONCE
    expected = words[:result.chunks]
    assert result.chunk_texts == expected
    assert result.text == " ".join(expected)
    words_out = result.text.split()
    for word in expected:
        assert words_out.count(word) == 1, word      # once: not dropped, not doubled
    assert words_out == expected                     # and in order
    # And the chunks that were decoded account for the whole file.
    assert abs(sum(d for _n, d, _f in app.decoded) - len(audio) / RATE) < 0.05


def test_a_short_file_is_one_chunk_and_still_decodes(wav):
    app = _App(texts=["the whole thing"])
    result = tf.transcribe_path(app, wav)
    assert result.chunks == 1 and len(app.decoded) == 1
    assert result.text == "the whole thing"


def test_the_production_decode_entry_is_the_one_called(wav):
    """Reuse, asserted: the params come from _build_hotkey_transcribe_params
    and every chunk goes through _decode_hotkey_audio, free-form."""
    app = _App()
    tf.transcribe_path(app, wav)
    assert app.params_built == 1
    assert app.decoded and all(free_form is True for _n, _d, free_form in app.decoded)

    source = (REPO / "samsara" / "transcribe_file.py").read_text(encoding="utf-8")
    assert "_decode_hotkey_audio" in source
    assert "_split_audio_at_silences" in source
    assert "decode_audio" in source
    # No fourth WAV loader: this module never opens a wave file itself.
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "wave" not in imported
    assert "soundfile" not in imported and "librosa" not in imported


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

def test_cancel_mid_transcription_stops_and_leaves_no_partial_output(tmp_path):
    audio = np.concatenate([
        part for i in range(9) for part in (_speech(8, seed=i), _silence(2))
    ])
    path = _write_wav(tmp_path / "long.wav", audio)
    app = _App()
    seen = []

    def cancel():
        return len(app.decoded) >= 2               # stop once two chunks are in

    with pytest.raises(tf.Cancelled):
        tf.transcribe_path(app, path, progress=lambda d, t: seen.append(d), cancel=cancel)

    assert len(app.decoded) == 2                   # it really stopped early
    assert not tf.transcript_path(path).exists()   # NOTHING was written
    assert app.typed == []
    assert app.history == []


def test_cancel_before_the_first_chunk_decodes_nothing(wav):
    app = _App()
    with pytest.raises(tf.Cancelled):
        tf.transcribe_path(app, wav, cancel=lambda: True)
    assert app.decoded == []
    assert not tf.transcript_path(wav).exists()


# ---------------------------------------------------------------------------
# Where the text goes
# ---------------------------------------------------------------------------

def test_the_default_destination_is_the_file_not_the_focused_window(wav):
    """A two-hour transcript typed into whatever has focus cannot be undone
    by "scratch that". Saving is the default, and this pins it."""
    assert tf.DEFAULT_DESTINATION == tf.DEST_SAVE
    app = _App(texts=["hello there"])
    result = tf.transcribe_path(app, wav)

    outcome = tf.deliver(app, wav, result.text)    # no destination argument

    assert app.typed == []                         # nothing went to the window
    assert outcome["saved"] == tf.transcript_path(wav)
    assert tf.transcript_path(wav).read_text(encoding="utf-8") == "hello there"
    assert tf.transcript_path(wav).name == "meeting.wav.txt"


def test_typing_is_opt_in_and_goes_through_the_delivery_chokepoint(wav):
    app = _App()
    tf.deliver(app, wav, "typed text", tf.DEST_TYPE)
    assert app.typed == ["typed text"]
    assert not tf.transcript_path(wav).exists()

    app2 = _App()
    tf.deliver(app2, wav, "both", tf.DEST_BOTH)
    assert app2.typed == ["both"]
    assert tf.transcript_path(wav).read_text(encoding="utf-8") == "both"


def test_an_unknown_destination_is_refused(wav):
    with pytest.raises(tf.TranscribeError):
        tf.deliver(_App(), wav, "x", "email-it-to-my-mother")


def test_the_history_row_is_distinguishable_from_live_dictation(wav):
    """A transcript of a file the user never spoke must not be filtered or
    counted as words they said."""
    app = _App()
    tf.deliver(app, wav, "from a file")
    assert app.history == [("transcription", "from a file")]
    assert tf.HISTORY_ENTRY_TYPE == "transcription" != "dictation"


def test_an_empty_transcript_writes_nothing(wav):
    app = _App()
    outcome = tf.deliver(app, wav, "")
    assert outcome["saved"] is None and outcome["typed"] is False
    assert not tf.transcript_path(wav).exists()
    assert app.history == []


# ---------------------------------------------------------------------------
# The microphone, and the command matcher
# ---------------------------------------------------------------------------

def test_no_microphone_is_opened_and_hands_free_is_unchanged(wav):
    """The tripwires on _App raise if capture is touched at all; the state
    tuple catches anything that changed a flag without calling a method."""
    app = _App(texts=["said into a file"])
    before = _capture_state(app)

    result = tf.transcribe_path(app, wav)
    tf.deliver(app, wav, result.text)

    assert _capture_state(app) == before

    source = (REPO / "samsara" / "transcribe_file.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "sounddevice" not in imported
    for capture in ("start_recording", "stop_recording", "start_continuous",
                    "InputStream", "resume_listening", "snooze_listening"):
        assert capture not in source, capture


COMMAND_TEXTS = [
    "open chrome",
    "scratch that",
    "go to sleep",
    "halt",
    "close window and delete everything",
    "insert tab",
]


@pytest.mark.parametrize("payload", COMMAND_TEXTS)
def test_a_transcript_containing_a_command_phrase_is_text_only(tmp_path, wav, payload):
    """It is a transcript. A recording of someone saying "open chrome"
    produces those two words and opens nothing."""
    app = _App(texts=[payload])
    result = tf.transcribe_path(app, wav)
    tf.deliver(app, wav, result.text)

    assert result.text == payload
    assert tf.transcript_path(wav).read_text(encoding="utf-8") == payload
    assert app.history == [("transcription", payload)]

    # There is no route from this module to the matcher at all.
    source = (REPO / "samsara" / "transcribe_file.py").read_text(encoding="utf-8")
    for dispatcher in ("process_text", "command_executor", "dispatch_utterance",
                       "_process_wake_command", "CommandMatcher"):
        assert dispatcher not in source, dispatcher


# ---------------------------------------------------------------------------
# Long files
# ---------------------------------------------------------------------------

def test_past_the_tested_ceiling_it_warns_and_still_tries(tmp_path, monkeypatch, caplog):
    """Refusing a recording the user actually has would be worse than trying.
    It says so in the log and carries on."""
    monkeypatch.setattr(tf, "MAX_TESTED_SECONDS", 3)
    audio = np.concatenate([_speech(3, seed=1), _silence(1), _speech(3, seed=2)])
    path = _write_wav(tmp_path / "over.wav", audio)
    app = _App(texts=["still transcribed"])

    with caplog.at_level("WARNING"):
        result = tf.transcribe_path(app, path)

    assert result.text == "still transcribed"
    assert any("tested to" in r.message or "tested to" in r.getMessage()
               for r in caplog.records)


def test_progress_reaches_the_end_and_never_exceeds_the_total(tmp_path):
    audio = np.concatenate([
        part for i in range(9) for part in (_speech(8, seed=i), _silence(2))
    ])
    path = _write_wav(tmp_path / "long.wav", audio)
    app = _App()
    seen: list = []

    result = tf.transcribe_path(app, path, progress=lambda d, t: seen.append((d, t)))

    assert len(seen) == result.chunks
    assert all(0 < done <= total + 0.01 for done, total in seen)
    assert abs(seen[-1][0] - seen[-1][1]) < 0.05        # it finishes at 100%
    assert seen == sorted(seen)                        # and only moves forward


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------

@pytest.fixture
def dialog(qapp, wav):
    from samsara.ui.transcribe_file_qt import TranscribeFilePage

    app = _App(texts=["dialog text"])
    spawned: list = []
    page = TranscribeFilePage(app, spawn=lambda name, fn: spawned.append(name) or fn())
    page.show()
    for _ in range(10):
        qapp.processEvents()
    yield page, app, wav, spawned
    page.hide()
    page.deleteLater()


def test_the_dialog_defaults_to_saving(dialog):
    page, _app, _wav, _spawned = dialog
    assert page.destination() == tf.DEST_SAVE
    assert page._dest_save.isChecked()
    assert not page._dest_type.isChecked() and not page._dest_both.isChecked()


def test_the_chosen_destination_is_visible(dialog):
    """The first render had an invisible checked indicator -- the window-wide
    QWidget background rule painted over Qt's default one, so the page could
    not show which destination was selected. Which destination is chosen is
    the one thing about this dialog that has to be unambiguous."""
    page, _app, _wav, _spawned = dialog
    sheet = page.styleSheet()
    assert "QRadioButton::indicator" in sheet
    assert "QRadioButton::indicator:checked" in sheet
    from samsara.ui import theme
    assert theme.ACCENT in sheet.split("QRadioButton::indicator:checked", 1)[1][:120]


def test_the_dialog_runs_off_the_ui_thread_through_the_registry(dialog, qapp):
    page, app, wav_path, spawned = dialog
    assert page.set_file(wav_path) is True
    assert page.start() is True
    for _ in range(20):
        qapp.processEvents()

    assert spawned == ["transcribe-file"]          # the registry, by name
    assert tf.transcript_path(wav_path).read_text(encoding="utf-8") == "dialog text"
    assert app.typed == []
    assert any(label.startswith("transcribed") and kind == "success"
               for label, kind in app.chips)       # the end-of-job announcement


def test_the_dialog_refuses_an_unsupported_file_in_words(dialog, tmp_path):
    page, _app, _wav, _spawned = dialog
    bad = tmp_path / "notes.docx"
    bad.write_bytes(b"x")
    assert page.set_file(bad) is False
    assert "not supported" in page._status.text()
    assert not page._start_btn.isEnabled()


def test_the_dialogs_controls_are_focusable_and_44px(dialog):
    page, _app, _wav, _spawned = dialog
    for widget in page._tab_chain:
        assert widget.accessibleName(), widget
        assert widget.focusPolicy() != Qt_NoFocus(), widget.accessibleName()
        assert widget.minimumHeight() >= 44, widget.accessibleName()
    assert page._tab_chain[0] is page._choose_btn


def Qt_NoFocus():
    from PySide6.QtCore import Qt
    return Qt.FocusPolicy.NoFocus


def test_every_size_in_the_dialog_is_a_theme_token():
    import re

    source = (REPO / "samsara" / "ui" / "transcribe_file_qt.py").read_text(encoding="utf-8")
    assert re.findall(r"font-size:\s*(\d+)px", source) == []
    assert "theme.TYPE_BODY" in source
    # No modal message boxes: they block the keyboard and the automation path.
    assert "QMessageBox" not in source


def test_closing_the_dialog_stops_the_work(dialog, qapp):
    page, _app, wav_path, _spawned = dialog
    page.set_file(wav_path)
    page.close()
    assert page._cancelled is True


# ---------------------------------------------------------------------------
# The voice entry point
# ---------------------------------------------------------------------------

def test_the_command_points_at_an_app_method_that_exists():
    """commands.json's MethodHandler silently no-ops with a warning when the
    method is missing, so a command that names a method the app does not have
    is a silent failure. This pins both halves."""
    import json

    commands = json.loads((REPO / "commands.json").read_text(encoding="utf-8"))["commands"]
    entry = commands["transcribe file"]
    assert entry["type"] == "method"
    method = entry["method"]
    assert method == "open_transcribe_file"
    assert entry.get("description")

    source = (REPO / "dictation.py").read_text(encoding="utf-8", errors="replace")
    assert f"def {method}(self)" in source
    assert "from samsara.ui.transcribe_file_qt import open_transcribe_file" in source
