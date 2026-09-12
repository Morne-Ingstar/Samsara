import json
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tools import hf_bench


class Segment:
    def __init__(self, text, no_speech_prob=0.01, avg_logprob=-0.1, compression_ratio=1.0):
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio


class FakePipeline:
    def __init__(self, outputs):
        self.outputs = outputs

    def run(self, audio, rate):
        item = self.outputs.pop(0)
        return item[0], item[1], 1.0

    def voiced_ms(self, audio, rate):
        return 32.0


def write_wav(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.asarray(data) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(pcm.tobytes())


def test_scoring_false_accept_reject_and_wer(tmp_path):
    corpus = tmp_path / "corpus"
    rows = [
        {"file": "silence.wav", "label": "silence", "expected_text": ""},
        {"file": "speech.wav", "label": "speech_owner", "expected_text": "hello world"},
        {"file": "media.wav", "label": "speech_owner_over_media", "expected_text": "hello world"},
    ]
    for row in rows:
        write_wav(corpus / row["file"], np.zeros(16000))
    (corpus / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
    pipe = FakePipeline([
        ("thanks for watching", [Segment("thanks for watching")]),
        ("hello", [Segment("hello")]),
        ("tv hello world bye", [Segment("tv hello world bye")]),
    ])
    result = hf_bench.run_bench(corpus, {}, pipe)
    assert result["summary"]["silence"]["false_accept_rate"] == 1.0
    assert result["summary"]["speech_owner"]["false_reject_rate"] == 0.0
    assert result["summary"]["speech_owner"]["wer"] == pytest.approx(0.5)
    over = result["rows"][2]
    assert over["leading_insertions"] == 1
    assert over["trailing_insertions"] == 1
    assert result["summary"]["speech_owner_over_media"]["pre_roll_contamination_words_per_utterance"] == 1.0


def test_manifest_label_set_and_wake_scoring(tmp_path):
    corpus = tmp_path / "corpus"
    rows = [{"file": "wake.wav", "label": "wake_over_media", "expected_text": "", "wake_phrase": "samsara"}]
    write_wav(corpus / "wake.wav", np.zeros(16000))
    (corpus / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
    result = hf_bench.run_bench(corpus, {}, FakePipeline([("samsara open notes", [Segment("samsara open notes")])]))
    assert set(result["summary"]) == {"wake_over_media"}
    assert result["rows"][0]["wake_detected"] is True
    assert result["rows"][0]["command_text"] == "open notes"


def test_print_delta(capsys):
    base = {
        "summary": {
            "silence": {"false_accept_rate": 0.5},
            "speech_owner": {"wer": 0.5},
        }
    }
    candidate = {
        "summary": {
            "silence": {"false_accept_rate": 0.75},
            "speech_owner": {"wer": 0.25},
        }
    }

    hf_bench.print_delta(base, candidate)

    lines = capsys.readouterr().out.splitlines()
    assert "silence | false_accept_rate | 0.5 | 0.75 | +0.2500" in lines
    assert "speech_owner | wer | 0.5 | 0.25 | -0.2500" in lines


def _pipeline_for_gate_test(apply_post_gates, production=None):
    pipeline = hf_bench.RealPipeline.__new__(hf_bench.RealPipeline)
    pipeline.production = production or hf_bench.load_production_adapter()
    pipeline.config = {"apply_post_gates": apply_post_gates}
    pipeline.model_rate = 16000
    pipeline.model = SimpleNamespace(
        transcribe=lambda audio, **params: ([Segment("raw transcript")], None)
    )
    pipeline.params = lambda: {}
    return pipeline


def test_apply_post_gates_routes_through_quality_gates(monkeypatch):
    production = hf_bench.load_production_adapter()
    calls = []

    def fake_quality_gates(segments, params, duration):
        calls.append((segments, params, duration))
        return "gated transcript", False

    monkeypatch.setattr(production, "_apply_segment_quality_gates", fake_quality_gates)
    gated = _pipeline_for_gate_test(True, production)
    ungated = _pipeline_for_gate_test(False, production)

    gated_text, _, _ = gated.run(np.zeros(16000), 16000)
    ungated_text, _, _ = ungated.run(np.zeros(16000), 16000)

    assert gated_text == "gated transcript"
    assert ungated_text == "raw transcript"
    assert len(calls) == 1


def test_gate_overrides_restores_dictation_attributes():
    production = hf_bench.load_production_adapter()

    names = {
        "_GATE_VAD_PROB": "vad_prob_threshold",
        "_GATE_MIN_CONTIG_MS": "vad_min_contig_ms",
        "_NO_SPEECH_THRESHOLD": "no_speech_threshold",
        "_LOGPROB_THRESHOLD": "log_prob_threshold",
        "_COMPRESSION_RATIO_THRESHOLD": "compression_ratio_threshold",
    }
    original = {name: getattr(production, name) for name in names}
    overrides = {
        "vad_prob_threshold": 0.91,
        "vad_min_contig_ms": 321,
        "no_speech_threshold": 0.12,
        "log_prob_threshold": -0.22,
        "compression_ratio_threshold": 7.7,
    }
    pipeline = hf_bench.RealPipeline.__new__(hf_bench.RealPipeline)
    pipeline.production = production
    pipeline.config = overrides

    with pipeline.gate_overrides():
        for name, key in names.items():
            assert getattr(production, name) == overrides[key]

    assert {name: getattr(production, name) for name in names} == original


@pytest.mark.parametrize("language,expected", [("auto", None), ("en", "en"), ("af", "af")])
def test_live_language_reaches_production_resolution(tmp_path, monkeypatch, language, expected):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"language": language, "model_size": "medium",
                                       "cloud_llm": {"api_key": "do-not-copy"}}))
    monkeypatch.setattr(hf_bench, "LIVE_CONFIG_PATH", config_path)
    pipe = _pipeline_for_gate_test(True)
    pipe.config = hf_bench.load_config(None)
    del pipe.params
    assert pipe.params()["language"] == expected
    assert "cloud_llm" not in pipe.config


def test_decode_info_survives_empty_post_gate_result():
    pipe = _pipeline_for_gate_test(True)
    info = SimpleNamespace(language="nn", language_probability=0.45)
    pipe.model.transcribe = lambda audio, **params: ([Segment("thanks for watching")], info)
    text, segs, wall = pipe.run(np.zeros(16000), 16000)
    pipe.voiced_ms = lambda audio, rate: 0.0
    result = hf_bench.bench_result(pipe, {"file": "noise.wav", "label": "silence"},
                                   text, segs, np.zeros(16000), 16000, wall)
    score = hf_bench.score_row(result)
    assert score["text"] == ""
    assert score["detected_language"] == "nn"
    assert score["language_probability"] == 0.45


@pytest.mark.parametrize("noise_probability,separate", [(0.45, True), (0.94, False), (1.0, False)])
def test_language_separation_includes_empty_noise_and_media_owner(noise_probability, separate):
    rows = [
        {"label": "speech_owner", "text": "hello", "detected_language": "en", "language_probability": 0.99},
        {"label": "speech_owner_over_media", "text": "hello", "detected_language": "en", "language_probability": 0.94},
        {"label": "silence", "text": "", "detected_language": "en", "language_probability": noise_probability},
        {"label": "media_only", "text": "tv", "detected_language": "en", "language_probability": 1.0},
    ]
    result = hf_bench.summarize_language_confidence(rows)
    assert result["owner_minimum"] == 0.94
    assert result["noise_maximum"] == noise_probability
    assert result["separate"] is separate
    assert result["labels"]["silence"]["text_produced_count"] == 0


def test_force_language_cli_overrides_live_config(tmp_path, monkeypatch):
    row = {"file": "speech.wav", "label": "speech_owner", "expected_text": "hello"}
    write_wav(tmp_path / row["file"], np.zeros(16000))
    (tmp_path / "manifest.json").write_text(json.dumps([row]))
    monkeypatch.setattr(hf_bench, "live_config", lambda: {"language": "auto"})
    monkeypatch.setattr(hf_bench, "RealPipeline", lambda config: FakePipeline([("hello", [])]))
    out = tmp_path / "nested" / "result"
    assert hf_bench.main(["--corpus", str(tmp_path), "--out", str(out),
                          "--force-language", "af"]) == 0
    result = json.loads(out.with_suffix(".json").read_text())
    assert result["config"]["language"] == "af"
    assert result["language_confidence"]["separate"] is None


def test_offline_adapter_does_not_import_app_or_open_log(monkeypatch):
    import builtins
    import logging
    import sys
    before = dict(sys.modules)
    handlers = list(logging.getLogger().handlers)
    real_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        assert name != "dictation" and not name.startswith("samsara"), name
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    adapter = hf_bench.load_production_adapter()
    assert sys.modules.get("dictation") is before.get("dictation")
    assert logging.getLogger().handlers == handlers
    assert adapter._apply_segment_quality_gates([Segment("hello world")], {}, 1.0) == ("hello world", False)


def test_leadin_scoring_and_contamination_averages():
    scored = []
    for text in ("breaking news hello world bye", "hello world", ""):
        row = {"file": "take.wav", "label": "speech_owner_over_media_leadin",
               "expected_text": "hello world"}
        result = hf_bench.BenchResult(row, text, [], 32.0, 1.0)
        score = hf_bench.score_row(result)
        legacy = hf_bench.score_row(hf_bench.BenchResult(
            {**row, "label": "speech_owner_over_media"}, text, [], 32.0, 1.0))
        for key in ("wer", "false_reject", "leading_insertions", "trailing_insertions"):
            assert score[key] == legacy[key]
        scored.append(score)
    assert scored[0]["leading_insertions"] == 2
    assert scored[0]["trailing_insertions"] == 1
    assert scored[0]["wer"] == 1.5
    summary = hf_bench.summarize(scored)["speech_owner_over_media_leadin"]
    assert summary["contamination_words_per_utterance"] == pytest.approx(2 / 3)
    assert summary["contaminated_rate"] == pytest.approx(1 / 3)
    assert summary["false_reject_rate"] == pytest.approx(1 / 3)
    assert summary["pre_buffer_comparison_count"] == 0
    assert summary["pre_buffer_leading_insertions_delta_per_utterance"] is None


@pytest.mark.parametrize("full_text,post_text,delta", [
    ("tv dialogue hello world", "hello world", 2),
    ("hello world", "tv hello world", -1),
])
def test_press_offset_slices_second_decode_and_reports_signed_delta(
    tmp_path, full_text, post_text, delta,
):
    rows = [
        {"file": "take.wav", "label": "speech_owner_over_media_leadin",
         "expected_text": "hello world", "lead_in_s": 3.0, "press_offset_s": 4001 / 16000},
        {"file": "old.wav", "label": "speech_owner_over_media_leadin", "expected_text": "hello world"},
    ]
    for row in rows:
        write_wav(tmp_path / row["file"], np.linspace(-0.5, 0.5, 16000))
    (tmp_path / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")

    class CapturingPipeline(FakePipeline):
        def __init__(self):
            super().__init__([(full_text, []), (post_text, []), ("hello world", [])])
            self.inputs = []

        def run(self, audio, rate):
            self.inputs.append((audio.copy(), rate))
            return super().run(audio, rate)

    pipe = CapturingPipeline()
    result = hf_bench.run_bench(tmp_path, {}, pipe)
    assert len(pipe.inputs) == 3  # Two for the lead-in row, only one for the old row.
    np.testing.assert_array_equal(pipe.inputs[1][0], pipe.inputs[0][0][4001:])
    assert pipe.inputs[1][1] == 16000
    score = result["rows"][0]
    assert score["pre_buffer_leading_insertions_delta"] == delta
    assert score["post_press_text"] == post_text
    assert score["press_offset_s"] == rows[0]["press_offset_s"]
    assert "post_press_text" not in result["rows"][1]
    summary = result["summary"]["speech_owner_over_media_leadin"]
    assert summary["pre_buffer_comparison_count"] == 1
    assert summary["pre_buffer_leading_insertions_delta_per_utterance"] == delta


@pytest.mark.parametrize("offset", [-0.01, 1.01, float("nan"), float("inf")])
def test_invalid_press_offset_fails_before_decode(tmp_path, offset):
    row = {"file": "take.wav", "label": "speech_owner_over_media_leadin",
           "expected_text": "hello", "press_offset_s": offset}
    write_wav(tmp_path / "take.wav", np.zeros(16000))
    (tmp_path / "manifest.json").write_text(json.dumps([row]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid press_offset_s"):
        hf_bench.run_bench(tmp_path, {}, FakePipeline([]))


@pytest.mark.parametrize("text,detected", [
    ("jarvis foo", True), ("Hey Claude foo", True), ("activate hermes foo", True),
    ("custom wake foo", True), ("disabled phrase foo", False), ("notjarvis foo", False),
    ("notjarvis jarvis foo", True),
])
def test_wake_word_and_enabled_targets_form_union(text, detected):
    config = {"wake_word": "jarvis", "wake_targets": [
        {"phrase": "hey claude", "enabled": True},
        {"phrase": "activate hermes", "enabled": True},
        {"phrase": "disabled phrase", "enabled": False},
        {"phrase": " JARVIS ", "enabled": True},
        {"enabled": True},
    ]}
    row = {"file": "wake.wav", "label": "wake_over_media", "wake_phrase": "custom wake"}
    scored = hf_bench.score_row(hf_bench.BenchResult(row, text, [], 32.0, 1.0), config)
    assert scored["wake_phrases"] == ["custom wake", "jarvis", "hey claude", "activate hermes"]
    assert scored["wake_detected"] is detected
    assert scored["command_text"] == ("foo" if detected else "")


@pytest.mark.parametrize("gain,sample,expected_int16", [
    (0.15, 1.0, 4915),        # scaled below full scale, rounded onto the grid
    (0.0, -1.0, 0),           # fully muted media
    (2.0, 0.9, 32767),        # positive clip
    (2.0, -0.9, -32768),      # negative clip at the int16 floor
    (1.0, -1.0, -32768),      # unity keeps the existing floor sample
])
def test_scale_int16_scales_and_clips(gain, sample, expected_int16):
    scaled = hf_bench.scale_int16(np.array([sample], dtype=np.float32), gain)
    assert scaled.dtype == np.float32
    assert round(float(scaled[0]) * 32768.0) == expected_int16


def test_gain_label_formats_trailing_zeros():
    assert [hf_bench.gain_label(g) for g in (1.0, 0.3, 0.15, 0.08, 0.04, 0.0)] == [
        "1.0", "0.3", "0.15", "0.08", "0.04", "0.0"]


def _media_gain_corpus(tmp_path):
    rows = [
        {"file": "media.wav", "label": "media_only", "expected_text": ""},
        {"file": "speech.wav", "label": "speech_owner", "expected_text": "hello world"},
    ]
    for row in rows:
        write_wav(tmp_path / row["file"], np.linspace(-1.0, 1.0, 16000))
    (tmp_path / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
    return rows


class CapturingFakePipeline(FakePipeline):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.inputs = []

    def run(self, audio, rate):
        self.inputs.append(np.asarray(audio).copy())
        return super().run(audio, rate)


def test_no_floor_emits_one_labelled_row_per_gain_and_plain_scales(tmp_path):
    _media_gain_corpus(tmp_path)
    pipe = CapturingFakePipeline([
        ("thanks for watching", [Segment("thanks for watching")]),
        ("", []),
        ("hello world", [Segment("hello world")]),
    ])

    result = hf_bench.run_bench(tmp_path, {}, pipe, media_gains=[1.0, 0.15], no_floor=True)

    assert [r["label"] for r in result["rows"]] == [
        "media_only@1.0", "media_only@0.15", "speech_owner"]
    assert [r["media_gain"] for r in result["rows"][:2]] == [1.0, 0.15]
    assert result["rows"][0]["false_accept"] is True
    assert result["rows"][1]["false_accept"] is False
    assert result["summary"]["media_only@1.0"] == {
        "count": 1, "false_accept_rate": 1.0, "phrases": ["thanks for watching"]}
    assert result["summary"]["media_only@0.15"] == {
        "count": 1, "false_accept_rate": 0.0, "phrases": []}
    assert "media_only" not in result["summary"]
    # The owner row is decoded from the untouched audio.
    np.testing.assert_allclose(pipe.inputs[1], pipe.inputs[0] * 0.15, atol=1 / 32768.0)
    original = hf_bench.read_wav(tmp_path / "media.wav")[0]
    for gain, actual, row in zip((1.0, 0.15), pipe.inputs, result["rows"]):
        np.testing.assert_array_equal(actual, hf_bench.scale_int16(original, gain))
        assert row["floor_rms_dbfs"] is None
        assert row["floor_file"] is None
    np.testing.assert_array_equal(pipe.inputs[2], hf_bench.read_wav(tmp_path / "speech.wav")[0])


def test_absent_media_gain_leaves_rows_unchanged(tmp_path):
    _media_gain_corpus(tmp_path)
    outputs = [("thanks for watching", [Segment("thanks for watching")]),
               ("hello world", [Segment("hello world")])]
    pipe = CapturingFakePipeline(list(outputs))
    baseline = CapturingFakePipeline(list(outputs))

    result = hf_bench.run_bench(tmp_path, {}, pipe, media_gains=None)
    empty = hf_bench.run_bench(tmp_path, {}, baseline, media_gains=[])

    assert [r["label"] for r in result["rows"]] == ["media_only", "speech_owner"]
    assert [r["label"] for r in empty["rows"]] == ["media_only", "speech_owner"]
    assert "media_gain" not in result["rows"][0]
    assert len(pipe.inputs) == 2
    np.testing.assert_array_equal(pipe.inputs[0], hf_bench.read_wav(tmp_path / "media.wav")[0])
    assert result["summary"]["media_only"]["false_accept_rate"] == 1.0


@pytest.mark.parametrize("length,expected", [
    (2, [0.01, 0.02]),
    (3, [0.01, 0.02, 0.03]),
    (8, [0.01, 0.02, 0.03, 0.01, 0.02, 0.03, 0.01, 0.02]),
])
def test_floor_slice_matches_media_length_and_loops(length, expected):
    floor = hf_bench.floor_slice([0.01, 0.02, 0.03], length)
    assert len(floor) == length
    np.testing.assert_array_equal(floor, np.asarray(expected, dtype=np.float32))


def test_floor_mix_rms_matches_combined_power():
    # Orthogonal tones have zero cross-power; expected powers add exactly.
    time = np.arange(16000) / 16000
    media = 0.1 * np.sin(2 * np.pi * 440 * time)
    floor = 0.01 * np.sin(2 * np.pi * 97 * time)
    gain = 0.15
    mixed = hf_bench.mix_media(media, gain, floor)
    expected_dbfs = 10 * np.log10((gain * 0.1) ** 2 / 2 + 0.01 ** 2 / 2)
    assert hf_bench.rms_dbfs(mixed) == pytest.approx(expected_dbfs, abs=0.5)


@pytest.mark.parametrize("floor_label", ["silence", "nonspeech_room"])
def test_media_floor_cycles_by_media_row_and_stays_fixed_across_gains(tmp_path, floor_label):
    rows = [{"file": f"media_{i}.wav", "label": "media_only"} for i in range(4)]
    for row in rows:
        write_wav(tmp_path / row["file"], np.linspace(-0.1, 0.1, 8))
    for i in range(3):
        row = {"file": f"floor_{i}.wav", "label": floor_label}
        write_wav(tmp_path / row["file"], np.array([0.001, -0.002, 0.003]) * (i + 1))
        rows.append(row)
    (tmp_path / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
    gains = [1.0, 0.15, 0.0]
    pipe = CapturingFakePipeline([("", [])] * (4 * len(gains) + 3))
    kwargs = {} if floor_label == "silence" else {"floor_label": floor_label}
    result = hf_bench.run_bench(tmp_path, {}, pipe, media_gains=gains, **kwargs)

    for i in range(4):
        media = hf_bench.read_wav(tmp_path / rows[i]["file"])[0]
        floor_file = f"floor_{i % 3}.wav"
        source = hf_bench.read_wav(tmp_path / floor_file)[0]
        expected_floor = np.tile(source, 3)[:len(media)]
        for j, gain in enumerate(gains):
            index = i * len(gains) + j
            np.testing.assert_array_equal(
                pipe.inputs[index], hf_bench.scale_int16(media, gain) + expected_floor)
            score = result["rows"][index]
            assert score["floor_file"] == floor_file
            assert score["floor_label"] == floor_label
            assert score["floor_rms_dbfs"] == round(hf_bench.rms_dbfs(expected_floor), 2)
        np.testing.assert_array_equal(pipe.inputs[i * len(gains) + 2], expected_floor)
    # Floor source rows themselves are still decoded untouched.
    for i in range(3):
        np.testing.assert_array_equal(pipe.inputs[12 + i],
                                      hf_bench.read_wav(tmp_path / f"floor_{i}.wav")[0])
    md, js = hf_bench.write_outputs(result, tmp_path / "result")
    assert (f"Verdict: largest tested gain with media_only false-accept 0/4 is 1.0 "
            f"with {floor_label} floor; above the current capture duck of 0.15.") in md.read_text()
    assert json.loads(js.read_text())["rows"] == result["rows"]


def test_missing_or_empty_floor_fails_explicitly(tmp_path):
    _media_gain_corpus(tmp_path)
    with pytest.raises(ValueError, match="no floor clips"):
        hf_bench.run_bench(tmp_path, {}, FakePipeline([]), media_gains=[0.0])
    with pytest.raises(ValueError, match="floor clip must not be empty"):
        hf_bench.floor_slice([], 10)


def test_no_floor_cli_uses_plain_scale_without_floor_clips(tmp_path, monkeypatch):
    _media_gain_corpus(tmp_path)
    pipe = CapturingFakePipeline([("", [])] * 3)
    monkeypatch.setattr(hf_bench, "RealPipeline", lambda config: pipe)
    assert hf_bench.main(["--corpus", str(tmp_path), "--out", str(tmp_path / "result"),
                          "--stock-config", "--media-gain", "0.15", "--media-gain", "0.0",
                          "--floor-label", "absent", "--no-floor"]) == 0
    original = hf_bench.read_wav(tmp_path / "media.wav")[0]
    np.testing.assert_array_equal(pipe.inputs[0], hf_bench.scale_int16(original, 0.15))
    np.testing.assert_array_equal(pipe.inputs[1], np.zeros_like(original))
