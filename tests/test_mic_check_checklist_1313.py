"""Check my mic result rows, using synthetic PCM and an isolated Qt window."""
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from scipy.signal import resample_poly

from samsara import mic_check as check


RATE = check.SAMPLE_RATE
MISSING_FIRST = check.SENTENCE.split(maxsplit=1)[1]


def take(seconds=check.PHRASE_S, *, rate=RATE):
    t = np.arange(round(seconds * rate)) / rate
    speech = .12 * np.sin(2 * np.pi * 900 * t) + .06 * np.sin(2 * np.pi * 2300 * t)
    if rate == RATE:
        speech += .04 * np.sin(2 * np.pi * 5800 * t)
    return speech.astype(np.float32)


def quiet(seconds=check.QUIET_S):
    return np.random.default_rng(1313).normal(0, .0004, round(seconds * RATE)).astype(np.float32)


def wav(tmp_path, name, audio):
    path = tmp_path / f"{name}.wav"
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    with wave.open(str(path), "rb") as recording:
        return np.frombuffer(recording.readframes(recording.getnframes()), dtype="<i2")


def fixture_recognizer(audio):
    # The first tagged word occupies 300 ms of the known synthetic sentence.
    # Recover its presence from the take so the test exercises the cut fixture.
    first = audio[:round(.3 * RATE)]
    return check.SENTENCE if np.any(first) else MISSING_FIRST


@pytest.mark.parametrize(("fault", "expected"), [
    ("clipped", {"Level": "Too loud"}),
    ("too_quiet", {"Level": "Too quiet"}),
    ("noise", {"Background": "Too noisy"}),
    ("narrowband", {"Clarity": "Muffled"}),
    ("start_cut", {"First word": "Missing"}),
    ("dropout", {"Clarity": "Gaps in sound"}),
    ("gated", {"Background": "Cuts to silence"}),
    ("clean", {}),
])
def test_synthetic_wav_flags_exactly_its_own_row(tmp_path, fault, expected):
    audio, background = take(), quiet()
    if fault == "clipped":
        audio *= 10
    elif fault == "too_quiet":
        audio *= .03
    elif fault == "noise":
        background *= 100
    elif fault == "narrowband":
        audio = resample_poly(take(rate=8000), 2, 1)
    elif fault == "start_cut":
        audio[:round(.3 * RATE)] = 0
    elif fault == "dropout":
        audio[RATE:RATE + RATE // 20] = 0
    elif fault == "gated":
        audio[RATE:RATE + RATE // 4] = 0
        background.fill(0)
    audio = wav(tmp_path, "take", audio)
    result = check.evaluate(audio, wav(tmp_path, "quiet", background))
    findings, show_transcript = check.checklist(result, fixture_recognizer(audio))
    assert {name: finding.verdict for name, finding in zip(check.ROWS, findings)
            if finding.tone in {"error", "warning"}} == expected
    assert show_transcript == (fault == "start_cut")
    if fault == "start_cut":
        assert result["start_gap_ms"] == pytest.approx(300, abs=.1)
        assert "started with a gap" in findings[3].sentence
    if fault == "clean":
        assert [finding.verdict for finding in findings] == ["Good", "Quiet", "Sentence matched", "Heard"]


def test_transcript_comparison_uses_words_and_keeps_signal_findings_independent():
    result = check.evaluate(take(), quiet())
    findings, show = check.checklist(result, "THE quick brown fox jumps over the lazy dog!")
    assert not show and findings[2].verdict == "Sentence matched"
    findings, show = check.checklist(result, check.SENTENCE.replace("fox", "box"))
    assert show and findings[2].verdict == "One word misheard" and findings[3].verdict == "Heard"
    findings, show = check.checklist(result, check.SENTENCE.replace("The", "A", 1))
    assert show and findings[2].verdict == "One word misheard" and findings[3].verdict == "Different word"
    findings, show = check.checklist(result, MISSING_FIRST)
    assert show and findings[3].sentence == "Samsara missed the first word."
    assert check.checklist(result, None)[0][2].verdict == "Unavailable"
    assert check.checklist(result, "")[0][3].verdict == "Not checked"


def test_five_second_quiet_sample_keeps_later_noise():
    background = quiet()
    background[-RATE:] *= 100
    result = check.evaluate(take(), background)
    assert check.QUIET_S == 5
    assert check.checklist(result, check.SENTENCE)[0][1].verdict == "Too noisy"


def test_short_unconfirmed_gap_uses_second_take():
    audio = take()
    audio[RATE:RATE + round(.003 * RATE)] = 0
    result = check.evaluate(audio, quiet())
    assert result["possible_dropout"] and not result["dropout"]
    assert check.checklist(result, check.SENTENCE)[0][2].verdict == "Unclear"
    cleaned = check.evaluate(take(), quiet())
    assert check.checklist(result, check.SENTENCE, repeat_result=cleaned)[0][2].verdict == "Sentence matched"
    assert check.checklist(result, check.SENTENCE, repeat_result=result)[0][2].verdict == "Gaps in sound"


class Source:
    def __init__(self, audio, silence):
        self.blocks = iter([np.empty(0, np.float32), *np.array_split(np.r_[silence, audio], 113)])
        self.close = Mock()

    def pull(self):
        return next(self.blocks, np.empty(0, np.float32))


@pytest.fixture
def app():
    return SimpleNamespace(config={"microphone": 3, "microphone_name": "Fixture USB microphone"}, recording=False)


def test_window_rechecks_first_word_and_keeps_first_take_for_playback(qapp, qt_widget_scope, monkeypatch, app):
    from samsara.ui.mic_check_qt import MicCheckWindow

    audio = take()
    audio[:round(.3 * RATE)] = 0
    first = Source(audio, quiet())
    second = Source(take(check.REPEAT_S), quiet(check.REPEAT_PAUSE_S))
    recognize = Mock(side_effect=[(MISSING_FIRST, None), (check.REPEAT_SENTENCE.split(maxsplit=1)[1], None)])
    save = Mock()
    monkeypatch.setattr("samsara.runtime.thread_registry.spawn", lambda name, work, **kw: work())
    window = MicCheckWindow(app, source_factory=Mock(side_effect=[first, second]), transcriber=recognize,
                            inspect=lambda _: (.7, False), save=save)
    with qt_widget_scope(window):
        window.show()
        window.start_button.click()
        assert [label.text() for label, _, _ in window.rows] == list(check.ROWS)
        assert window.rows[3][1].text() == "Missing twice"
        assert window.heard.text() == f"“{MISSING_FIRST}”" and window.heard.isVisible()
        assert window.take.audio is recognize.call_args_list[0].args[0]
        np.testing.assert_array_equal(window.take.audio, audio)
        assert recognize.call_args_list[1].args[0].size == check.REPEAT_S * RATE
        assert window.details.isHidden()
        window.details_button.click()
        assert "Windows input volume: 70%" in window.details.text()
        first.close.assert_called_once()
        second.close.assert_called_once()
        save.assert_called_once()


@pytest.mark.parametrize("finish", ["close", "retry"])
def test_playback_uses_only_in_memory_audio_and_clears_on_exit(qapp, qt_widget_scope, monkeypatch, app, finish):
    from PySide6 import QtMultimedia
    from samsara.ui.mic_check_qt import CheckTake, MicCheckWindow

    audio = take()
    transcribe = Mock()
    monkeypatch.setattr("samsara.runtime.thread_registry.spawn", Mock())
    output = Mock()
    output.isNull.return_value = False
    sink = Mock()
    monkeypatch.setattr(QtMultimedia, "QMediaDevices", SimpleNamespace(defaultAudioOutput=lambda: output))
    monkeypatch.setattr(QtMultimedia, "QAudioSink", Mock(return_value=sink))
    window = MicCheckWindow(app, transcriber=transcribe)
    with qt_widget_scope(window):
        window.show()
        window.show_result(CheckTake(check.evaluate(audio, quiet()), audio, check.SENTENCE))
        window.play_button.click()
        buffer = sink.start.call_args.args[0]
        assert bytes(buffer.data()) == np.rint(audio * 32767).astype("<i2").tobytes()
        assert window.play_button.text() == "Stop playback"
        state_changed = sink.stateChanged.connect.call_args.args[0]
        state_changed(QtMultimedia.QAudio.State.IdleState)
        sink.stop.assert_not_called()  # The Windows sink can briefly start idle.
        if finish == "close":
            window.close()
        else:
            window.start_button.click()
        sink.stop.assert_called_once()
        assert not bytes(buffer.data()) and window.take is None and not window.heard.text()
        transcribe.assert_not_called()


def test_timeout_shows_completed_measurements_and_ignores_late_decode(qapp, qt_widget_scope, monkeypatch, app):
    from samsara.ui.mic_check_qt import CheckTake, MicCheckWindow

    monkeypatch.setattr("samsara.runtime.thread_registry.spawn", Mock())
    audio = take()
    window = MicCheckWindow(app)
    with qt_widget_scope(window):
        window.start_check()
        result = check.evaluate(audio, quiet())
        window.on_progress("decoding", CheckTake(result, audio))
        window.on_timeout()
        assert window.rows[0][1].text() == "Good" and window.rows[2][1].text() == "Unavailable"
        assert window.cancel.is_set() and window.busy and not window.start_button.isEnabled()
        window.on_progress("result", CheckTake(result, audio, check.SENTENCE))
        assert window.rows[2][1].text() == "Unavailable"
        assert not window.busy and window.start_button.isEnabled()
