"""949: synthetic audio only; never import dictation or open microphones."""
import ast
import importlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import numpy as np
import pytest

from samsara import mic_score as score
from samsara.history import HistoryManager


@pytest.fixture
def wav_audio(tmp_path):
    rate = 16000
    quiet = np.full(rate, .001, dtype=np.float32)
    speech = (.04 * np.sin(2 * np.pi * 220 * np.arange(rate * 2) / rate)).astype(np.float32)
    path = tmp_path / "synthetic.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes((np.r_[quiet, speech] * 32767).astype("<i2").tobytes())
    with wave.open(str(path), "rb") as stream:
        pcm = np.frombuffer(stream.readframes(stream.getnframes()), "<i2").astype(np.float32) / 32767
    return pcm[rate:], pcm[:rate]


@pytest.mark.parametrize("said,heard", [
    ("Jarvis, TEN minutes!", "jarvis 10 minutes"),
    ("There are twenty-three books.", "THERE are 23 books!"),
    ("Samsara: open Stremio.", "samsara open stremio"),
])
def test_normalization(said, heard):
    assert score.word_accuracy(said, heard)["accuracy"] == 1


def test_wer_counts_substitutions_deletions_and_insertions():
    result = score.word_accuracy("send Morne the notes", "send Mark notes today")
    assert result["errors"] == 3
    assert result["accuracy"] == .25
    assert len(result["differences"]) == 3
    assert score.word_accuracy("one word", "")['accuracy'] == 0


@pytest.mark.parametrize("accuracy,expected", [
    (1, 10), (.98, 10), (.979, 9), (.95, 9), (.949, 8),
    (.92, 8), (.89, 7), (.86, 6), (.83, 5), (.80, 4),
    (.77, 3), (.74, 2), (.739, 1), (0, 1),
])
def test_score_table_edges(accuracy, expected):
    assert score.score_accuracy(accuracy) == expected


def test_signal_explanations(wav_audio):
    audio, quiet = wav_audio
    assert score.signal_summary(audio, quiet)["level"] == "good"
    assert score.signal_summary(audio * .01, quiet)["level"] == "too quiet"
    assert score.signal_summary(np.ones(100, np.float32), quiet)["level"] == "clipping"
    assert score.signal_summary(audio, quiet)["noise_dbfs"] == pytest.approx(-60, abs=.3)
    with pytest.raises(ValueError, match="No usable"):
        score.signal_summary(np.empty(0), quiet)


def test_simultaneous_devices_and_separate_fallback_persisted(tmp_path, wav_audio):
    active, opens = set(), []

    class Source:
        def __init__(self, name):
            self.name = name
            active.add(name)

        def close(self):
            active.remove(self.name)

    def factory(device):
        name = device["name"]
        opens.append((name, set(active)))
        if name == "B" and active:
            raise OSError("cannot open concurrently")
        return Source(name)

    index = [0]
    store = score.ResultStore(tmp_path / "scores.json")
    run = score.ComparisonRun(None, [{"name": n} for n in "ABC"], source_factory=factory,
                              transcriber=lambda audio: (score.SENTENCES[index[0]], -.2), store=store)
    assert run.prepare()
    assert active == {"A", "C"}
    assert [d["name"] for d in run.fallback] == ["B"]
    for i in range(5):
        index[0] = i
        run.score_take(i, {name: wav_audio for name in run.sources})
    assert run.next_group()
    assert active == {"B"}
    assert opens[-1] == ("B", set())
    for i in range(5):
        index[0] = i
        run.score_take(i, {"B": wav_audio})
    assert not run.next_group()
    results = score.ResultStore(store.path).load()
    assert {r["device_name"]: r["sequential"] for r in results} == {"A": False, "C": False, "B": True}
    assert all(r["score"] == 10 and len(r["takes"]) == 5 for r in results)
    assert not active


def test_failed_device_is_reported_unscored(tmp_path):
    run = score.ComparisonRun(None, [{"name": "gone"}],
                              source_factory=Mock(side_effect=OSError("disconnected")),
                              store=score.ResultStore(tmp_path / "scores.json"))
    assert not run.prepare()
    assert "gone" in run.failures
    assert not run.completed
    assert not run.store.path.exists()


def test_recording_uses_same_window_and_separate_quiet_measurement():
    now = [0.]
    done, cancel = threading.Event(), threading.Event()

    class Source:
        def pull(self):
            return np.full(320, .001 if now[0] < 1 else .04, np.float32)

    run = score.ComparisonRun(None, [])
    run.sources = {"A": Source(), "B": Source()}
    heard = []

    def sleep(seconds):
        now[0] += seconds
        if now[0] > 2:
            done.set()

    recordings = run.record(done, cancel, lambda: heard.append(now[0]), clock=lambda: now[0], sleep=sleep)
    assert len(heard) == 1 and heard[0] >= 1
    assert np.array_equal(recordings["A"][0], recordings["B"][0])
    assert np.all(recordings["A"][1] == .001)
    assert np.all(recordings["A"][0] == .04)
    cancel.set()
    with pytest.raises(InterruptedError):
        run.record(done, cancel, lambda: None, clock=lambda: now[0], sleep=sleep)


def test_results_sorted_and_corrupt_file_preserved(tmp_path):
    store = score.ResultStore(tmp_path / "scores.json")
    for value in (4, 10, 7):
        store.save(dict(score=value, accuracy=value / 10, date="2026-10-31"))
    assert [r["score"] for r in store.load()] == [10, 7, 4]
    store.path.write_text("broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        store.save(dict(score=10))
    assert store.path.read_text() == "broken"


def test_cancel_prevents_persistence(tmp_path, wav_audio):
    store = score.ResultStore(tmp_path / "scores.json")
    cancel = threading.Event()

    def transcribe(audio):
        cancel.set()
        return score.SENTENCES[0], -.2

    run = score.ComparisonRun(None, [], transcriber=transcribe, store=store)
    with pytest.raises(InterruptedError):
        run.score_take(0, {"A": wav_audio}, cancel)
    assert not store.path.exists()


@pytest.mark.parametrize("entry_type", ["dictation", "command"])
def test_production_history_writer_persists_numeric_confidence(tmp_path, monkeypatch, entry_type):
    # Execute the actual writer body without importing the live app module.
    tree = ast.parse(Path("dictation.py").read_text(encoding="utf-8-sig"))
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and any(
        isinstance(m, ast.FunctionDef) and m.name == "_log_history" for m in n.body))
    node = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "_log_history")
    namespace = {"mic_score": score}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "dictation.py", "exec"), namespace)
    from samsara import recent_privacy
    monkeypatch.setattr(recent_privacy, "_target_for_history", lambda app: recent_privacy.TargetPrivacy("", "fixture"))
    with_db = HistoryManager(tmp_path / "history.db")
    app = SimpleNamespace(config={"microphone": 1, "microphone_name": "A"}, history_db=with_db,
                          _get_foreground_app=lambda: "fixture")

    @score.capture_history(app)
    def write():
        segments = (SimpleNamespace(text=text, avg_logprob=prob)
                    for text, prob in [("synthetic ", -.2), ("fixture", -.4)])
        text = score.transcript_text(app, segments)
        app.config["microphone_name"] = "B"  # Capture ownership survives a later switch.
        namespace["_log_history"](app, text, entry_type=entry_type, matched_command="fixture")

    write()
    row = dict(with_db.recent_for_microphone("A")[0])
    assert row["log_prob"] == -.4 and row["microphone_name"] == "A"
    assert row["entry_type"] == entry_type
    assert row["raw_text"] == "synthetic fixture"
    assert score.history_fields(app)["log_prob"] is None
    with_db.close()


def test_streaming_handoff_is_capture_owned_and_scope_restored(monkeypatch):
    from samsara import speech_regions
    app = SimpleNamespace(config={"microphone": 2, "microphone_name": "A"})
    session = SimpleNamespace()
    score.start_streaming_history(session, app)
    assert session.app is app and session.start_time > 0
    app.config["microphone_name"] = "B"
    decoded = ([SimpleNamespace(avg_logprob=-.15)], SimpleNamespace(), "raw fixture")
    decode = Mock(return_value=decoded)
    monkeypatch.setattr(speech_regions, "stream_final", decode)
    worker, audio, params = SimpleNamespace(_session=session), np.zeros(10), {}
    assert score.stream_final(worker, audio, params) is decoded
    decode.assert_called_once_with(worker, audio, params)
    fields = score.deliver_with_history(session, lambda: score.history_fields(app))
    assert fields == {"microphone_name": "A", "log_prob": -.15}
    assert score.history_fields(app) == {"microphone_name": "B", "log_prob": None}


def test_unavailable_confidence_never_breaks_dictation():
    assert score.segment_log_prob([SimpleNamespace(), SimpleNamespace(avg_logprob=None),
                                   SimpleNamespace(avg_logprob=float("nan")),
                                   SimpleNamespace(avg_logprob="invalid")]) is None


def test_everyday_estimate_is_per_mic_and_handles_empty_and_misses():
    def row(**kw):
        return dict(microphone_name="A", log_prob=-.1, raw_text="fixture", status="success",
                    entry_type="dictation", privacy_reason="", **kw)
    healthy = [row() for _ in range(5)]
    healthy += [dict(row(), microphone_name="B", log_prob=-2) for _ in range(100)]
    estimate = score.everyday_estimate(healthy, "A")
    assert estimate["recognition"]["score"] == 9 and estimate["recognition"]["samples"] == 5
    assert estimate["command_matching"] is None
    poor = [dict(row(), raw_text="", status="empty") for _ in range(2)]
    poor += [dict(row(), entry_type="command", matched_command=None) for _ in range(3)]
    weaker = score.everyday_estimate(poor, "A")
    assert weaker["recognition"]["score"] < estimate["recognition"]["score"]
    assert weaker["recognition"]["empty_rate"] == .4 and weaker["command_matching"]["miss_rate"] == 1
    assert score.everyday_estimate(healthy, "missing")["recognition"] is None
    assert score.everyday_estimate([dict(row(), log_prob=None) for _ in range(5)], "A")["recognition"] is None
    assert score.everyday_estimate([dict(row(), privacy_reason="password_field") for _ in range(5)], "A")["recognition"] is None


def test_real_hold_decoder_uses_loaded_model_and_raw_output(monkeypatch, wav_audio):
    from samsara import speech_presence
    monkeypatch.setattr(speech_presence, "skip_decode", lambda *a, **kw: False)
    calls = []
    sentence = score.SENTENCES[0]

    def transcribe(audio, **params):
        calls.append(params)
        return iter([SimpleNamespace(text=sentence, avg_logprob=-.2, compression_ratio=1,
                                     no_speech_prob=.01, temperature=0.)]), SimpleNamespace(language="en")

    params = dict(language="en", beam_size=3, temperature=0., vad_filter=True,
                  initial_prompt=None, no_speech_threshold=.6, log_prob_threshold=-1.)
    app = SimpleNamespace(model=SimpleNamespace(transcribe=transcribe), model_lock=threading.Lock(),
                          model_rate=16000, config={}, device_type="cuda",
                          get_transcription_params=lambda **kw: dict(params),
                          _buffer_should_skip_decode=lambda *a, **kw: None,
                          _filter_dictation_language=lambda text, info: text,
                          process_transcription=Mock(side_effect=AssertionError("formatting must stay off")))
    text, confidence = score.DictationTranscriber(app)(wav_audio[0])
    assert text == sentence and confidence == -.2
    assert calls and calls[0]["beam_size"] == 3
    assert calls[0]["language"] == "en" and calls[0]["vad_filter"] is False
    assert calls[0]["condition_on_previous_text"] is False
    app.process_transcription.assert_not_called()


def test_use_mic_resolves_current_id_and_rejects_disconnected():
    app = SimpleNamespace(get_available_microphones=lambda: [{"id": 29, "name": "A"}],
                          switch_microphone=Mock(), recording=False)
    score.use_microphone(app, "A")
    app.switch_microphone.assert_called_once_with(29)
    with pytest.raises(RuntimeError, match="disconnected"):
        score.use_microphone(app, "gone")


def test_voice_plugin_aliases_and_estimate_reply(monkeypatch):
    from samsara import plugin_commands
    import plugins.commands.mic_score_voice as voice
    voice = importlib.reload(voice)
    for phrase in ("test my mic", "mic score", "which mic is best", "how's my mic doing"):
        assert plugin_commands.find_command(phrase) is not None
    monkeypatch.setattr(score, "current_estimate", lambda app: {
        "recognition": {"score": 8}, "command_matching": {"match_rate": .6}})
    monkeypatch.setattr(score, "current_read_aloud", lambda app: {"accuracy": .98})
    app = SimpleNamespace(audio_coordinator=SimpleNamespace(speak=Mock()), _show_outcome_chip=Mock())
    assert voice.how_is_my_mic(app)
    assert "8 out of 10" in app.audio_coordinator.speak.call_args.args[0]
    spoken = app.audio_coordinator.speak.call_args.args[0]
    assert "Read-aloud accuracy measured 98%" in spoken
    assert "Command matching 60%; router, not the mic" in spoken


def test_window_runs_five_sentences_without_hardware(qapp, tmp_path, monkeypatch, wav_audio):
    from samsara.ui.mic_score_qt import MicScoreWindow
    from samsara.runtime import thread_registry
    store = score.ResultStore(tmp_path / "scores.json")
    monkeypatch.setattr(score, "ResultStore", lambda: store)
    monkeypatch.setattr(score, "open_source", lambda app, device: SimpleNamespace(close=lambda: None))
    def record(self, done, cancel, listening):
        listening()
        return {name: wav_audio for name in self.sources}

    monkeypatch.setattr(score.ComparisonRun, "record", record)
    monkeypatch.setattr(thread_registry, "spawn", lambda name, function, **kw: function())
    index = [0]
    monkeypatch.setattr(score, "DictationTranscriber", lambda app: lambda pcm: (score.SENTENCES[index[0]], -.2))
    app = SimpleNamespace(config={"microphone": 1, "microphone_name": "A"},
                          get_available_microphones=lambda: [{"id": 1, "name": "A"}],
                          switch_microphone=Mock(), recording=False)
    window = MicScoreWindow(app)
    assert callable(window.done)  # QDialog.done is a virtual method used by native close.
    window.show()
    qapp.processEvents()
    for i in range(5):
        index[0] = i
        window.advance()
    assert window.run is None and not window.busy
    assert app._mic_score_active is False
    assert "Test complete" in window.status.text()
    assert store.load()[0]["score"] == 10
    window.close()


def test_test_readings_cannot_reach_wake_commands():
    from samsara.audio_engine.wake_consumer import WakeConsumer
    consumer = object.__new__(WakeConsumer)
    consumer._app = SimpleNamespace(_mic_score_active=True)
    consumer.abort_utterance = Mock()
    # No frame fields or recognizer exist: the score window must intercept it.
    consumer._process_frame(object())
    consumer.abort_utterance.assert_called_once()
    assert consumer._hold_not_before > 0


def test_settings_button_opens_score_window(qapp, monkeypatch):
    from PySide6.QtWidgets import QComboBox, QWidget, QVBoxLayout, QPushButton
    from samsara.ui.settings import audio_page, calm_layout
    from samsara.ui import mic_score_qt
    container = QWidget()
    page = SimpleNamespace(container=container, base=QVBoxLayout(container))
    app = SimpleNamespace()
    # 997b: Audio shares the microphone model already built by General.
    owner = SimpleNamespace(app=app, _widgets={'mic_combo': QComboBox(container)},
                            make_page=lambda *a: page)
    monkeypatch.setattr(calm_layout, "group_page", lambda *a: None)
    opened = Mock()
    monkeypatch.setattr(mic_score_qt, "open_mic_score", opened)
    assert audio_page.build(owner) is page
    button = next(b for b in container.findChildren(QPushButton) if b.text() == "Mic score...")
    button.click()
    opened.assert_called_once_with(app)
    container.close()


def test_native_stream_uses_full_channels_and_shared_conversion(monkeypatch):
    import sounddevice as sd
    from samsara import audio_devices
    from samsara.audio_engine.wake_prefilter import NativeRateFrames
    opened = []
    rate = 48000
    raw = np.zeros((4800, 2), np.float32)
    raw[:, 0] = .03 * np.sin(2 * np.pi * 220 * np.arange(4800) / rate)
    raw[:, 1] = 1  # Channel zero, not an average with the second channel.

    class Stream:
        def __init__(self, **kw):
            self.kw = kw
            opened.append(self)

        def start(self):
            self.kw['callback'](raw, 4800, None, False)

        stop = Mock()
        close = Mock()

    monkeypatch.setattr(audio_devices, "detect_capture_rate", lambda device: rate)
    monkeypatch.setattr(audio_devices, "get_device_info", lambda *args: {"max_input_channels": 2})
    monkeypatch.setattr(sd, "InputStream", Stream)
    source = score.StreamSource({"id": 4, "name": "fixture"})
    assert opened[0].kw['channels'] == 2
    assert np.array_equal(source.pull(), NativeRateFrames(rate).convert(raw[:, 0]))
    source.close()
    opened[0].stop.assert_called_once()
    opened[0].close.assert_called_once()


def test_current_engine_is_shared_and_device_change_rejected(monkeypatch):
    from samsara.audio_engine import guide_capture
    reader = SimpleNamespace(overrun_count=0)
    engine = SimpleNamespace(_running=True, capture_name="A", register_consumer=Mock(return_value=reader),
                             unregister_consumer=Mock())
    app = SimpleNamespace(config={}, _ace_engine=engine)
    monkeypatch.setattr(guide_capture, "drain_float", lambda reader: np.zeros(1600, np.float32))
    source = score.open_source(app, {"id": 1, "name": "A"})
    assert isinstance(source, score.RingSource)
    assert len(source.pull()) == 1600
    engine.capture_name = "B"
    with pytest.raises(RuntimeError, match="changed"):
        source.pull()
    source.close()
    engine.unregister_consumer.assert_called_once_with(reader)

def context_app():
    return SimpleNamespace(
        config={"model_size": "small", "dictation": {"speech_presence_gate": True}},
        device_type="cuda", compute_type="float16", model_rate=16000,
        get_transcription_params=lambda **kw: dict(
            language="en", beam_size=5, vad_filter=True,
            vad_parameters={"threshold": .5}, no_speech_threshold=.6,
            log_prob_threshold=-1., initial_prompt="synthetic custom prompt"))


def test_routing_outcomes_cannot_change_recognition_or_mic_ranking(tmp_path, wav_audio):
    # Same actual WAV samples and stubbed decoder confidence in both runs.
    histories, saved = [], []
    for routed in (True, False):
        store = score.ResultStore(tmp_path / f"scores-{routed}.json")
        index = [0]
        def transcribe(audio):
            assert np.array_equal(audio, wav_audio[0])
            return score.SENTENCES[index[0]], -.1
        run = score.ComparisonRun(context_app(), [], transcriber=transcribe, store=store)
        for i in range(5):
            index[0] = i
            run.score_take(i, {"A": wav_audio, "B": wav_audio})
        saved.append(store.load())
        histories.append([dict(microphone_name=name, raw_text="synthetic fixture", log_prob=-.1,
                               entry_type="command", status="success" if routed else "failed",
                               matched_command="fixture" if routed else None)
                          for name in ("A", "B") for _ in range(5)])
    estimates = [{name: score.everyday_estimate(rows, name) for name in ("A", "B")}
                 for rows in histories]
    assert estimates[0]["A"]["recognition"] == estimates[1]["A"]["recognition"]
    assert estimates[0]["A"]["command_matching"]["match_rate"] == 1
    assert estimates[1]["A"]["command_matching"]["match_rate"] == 0
    ranking = lambda rows: [r["device_name"] for r in rows]
    assert ranking(score.rank_results(saved[0], estimates[0])) == ranking(score.rank_results(saved[1], estimates[1]))
    assert [r["accuracy"] for r in saved[0]] == [r["accuracy"] for r in saved[1]]
    # Measured WER and router outcomes never break a recognition tie.
    altered = [dict(r, accuracy=0., score=1) if r["device_name"] == "A" else r for r in saved[0]]
    assert ranking(score.rank_results(altered, estimates[0])) == ranking(score.rank_results(saved[0], estimates[0]))


def test_router_metric_survives_insufficient_confidence_and_excludes_private_rows():
    rows = [dict(microphone_name="A", entry_type="command", raw_text="fixture",
                 matched_command="fixture", status="failed", log_prob=None)]
    estimates = score.everyday_estimate(rows, "A")
    assert estimates["recognition"] is None
    # Execution failure after a match is not a router miss.
    assert estimates["command_matching"]["match_rate"] == 1
    assert score.everyday_estimate([dict(rows[0], privacy_reason="redacted")], "A")["command_matching"] is None


def test_context_persisted_per_result_and_per_take(tmp_path, wav_audio):
    app = context_app()
    store = score.ResultStore(tmp_path / "scores.json")
    index = [0]
    run = score.ComparisonRun(app, [], transcriber=lambda audio: (score.SENTENCES[index[0]], -.2), store=store)
    run.sources["A"] = SimpleNamespace(sample_rate=48000)
    for i in range(5):
        index[0] = i
        run.score_take(i, {"A": wav_audio})
    result = store.load()[0]
    context = result["context"]
    assert context["model_name"] == "small" and context["compute_device"] == "cuda"
    assert context["compute_type"] == "float16"
    assert context["decode_settings"]["beam_size"] == 5
    assert context["decode_settings"]["language"] == "en"
    assert context["decode_settings"]["vad_filter"] is False  # Actual ordinary hold settings.
    assert context["decode_settings"]["no_speech_threshold"] == .6
    assert context["decode_settings"]["vad_parameters"] == {"threshold": .5}
    assert "synthetic custom prompt" not in store.path.read_text()
    assert len(context["decode_settings"]["initial_prompt_sha256"]) == 64
    assert context["input_device_name"] == "A" and context["input_sample_rate"] == 48000
    assert context["model_sample_rate"] == 16000 and context["samsara_version"]
    assert all(t["context"] == context for t in result["takes"])
    assert result["measurement"] == "read_aloud_word_accuracy"
    assert result["decoder_confidence"]["mean_log_prob"] == pytest.approx(-.2)
    assert not result["context_changed"]
    assert not score.context_difference(result, dict(context, input_device_name="B"))
    assert "input sample rate" in score.context_difference(result, dict(context, input_sample_rate=44100))
    assert "compute device" in score.context_difference(result, dict(context, compute_device="cpu"))
    assert "unavailable" in score.context_difference({}, context)


@pytest.mark.parametrize("field,value", [
    ("model_name", "large-v3"), ("compute_type", "int8"),
    ("decode_settings", {"beam_size": 1}), ("samsara_version", "other"),
    ("admission", {"speech_presence": False}),
])
def test_changed_context_is_flagged(field, value):
    context = score.comparison_context(context_app(), "A", 48000)
    assert score.context_difference({"context": context}, dict(context, **{field: value}))


def test_settings_changed_mid_test_are_not_presented_as_comparable(tmp_path, wav_audio):
    app = context_app()
    store = score.ResultStore(tmp_path / "scores.json")
    run = score.ComparisonRun(app, [], transcriber=lambda audio: ("", -.2), store=store)
    run.sources["A"] = SimpleNamespace(sample_rate=48000)
    for i in range(5):
        if i == 2:
            app.config["model_size"] = "base"
        run.score_take(i, {"A": wav_audio})
    row = store.load()[0]
    assert row["context_changed"]
    assert "changed during" in score.context_difference(row, row["context"])


def test_ui_separates_measures_and_flags_legacy_context(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from samsara.ui.mic_score_qt import MicScoreWindow
    store = score.ResultStore(tmp_path / "scores.json")
    store.save(dict(device_name="A", date="2026-10-31", score=10, accuracy=1., takes=[], sequential=False))
    monkeypatch.setattr(score, "ResultStore", lambda: store)
    rows = [dict(microphone_name="A", raw_text="fixture", log_prob=-.1,
                 entry_type="command", matched_command=None) for _ in range(5)]
    app = context_app()
    app.get_available_microphones = lambda: [{"id": 1, "name": "A"}]
    app.history_db = SimpleNamespace(recent_for_microphone=lambda name: rows)
    window = MicScoreWindow(app)
    text = "\n".join(label.text() for label in window.findChildren(QLabel))
    assert "Read-aloud accuracy (measured): 10 / 10" in text
    assert "Recognition (decoder confidence estimate): 9 / 10" in text
    assert "Command matching (router, not about the mic): 0%" in text
    assert "Comparison context unavailable" in text
    window.close()
