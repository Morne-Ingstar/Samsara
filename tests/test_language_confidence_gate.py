"""Real production methods bound to a fake app: no model, devices or app import."""
import ast
import collections
import copy
import importlib.util
import logging
import math
import threading
import time
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from tools import hf_bench


def load_gate_methods():
    root = Path(__file__).resolve().parents[1]
    from samsara import session_modes, transcript_gates
    spec = importlib.util.spec_from_file_location("offline_languages", root / "samsara/languages.py")
    languages = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(languages)
    module = hf_bench.load_production_adapter()
    module.__dict__.update(_languages=languages, collections=collections, math=math, time=time,
                           logger=logging.getLogger("language_gate_test"),
                           flight_recorder=SimpleNamespace(record=Mock()),
                           # Queue 128's pure gates are imported directly;
                           # dictation.py only re-exports them now.
                           _sanitise_context_tail=transcript_gates._sanitise_context_tail,
                           _CONTEXT_TAIL_CHARS=transcript_gates._CONTEXT_TAIL_CHARS,
                           _is_context_echo=transcript_gates._is_context_echo,
                           _CONTEXT_ECHO_CHIP=transcript_gates._CONTEXT_ECHO_CHIP,
                           _is_quality_exhausted=transcript_gates._is_quality_exhausted,
                           is_scratch_that=session_modes.is_scratch_that,
                           is_dictate_commit=session_modes.is_dictate_commit,
                           is_recover_draft=session_modes.is_recover_draft,
                           match_switch_word=session_modes.match_switch_word,
                           # Queue 69's optional policy integration is not
                           # the language gate under test.
                           _cancel_window_module=lambda: None)
    path = root / "dictation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = {"_HotkeyDecodeResult", "_LONG_DECODE_CEILING_S", "_GATE_MAX_BUFFER_S",
                 "_SANITY_RMS_FLOOR_DB", "_SANITY_RMS_WINDOW_S", "_SANITY_MIN_DURATION_S",
                 "_SANITY_MIN_CPS", "_SANITY_MIN_SPEECH_COVERAGE", "_SpeechRun", "_GateDecision"}
    functions = {"_apply_retry_on_suspected_loss", "_suspected_silent_data_loss", "_speech_rms_coverage"}
    methods = {"_filter_dictation_language", "_decode_hotkey_audio", "_buffer_should_skip_decode",
               "transcribe_continuous_buffer", "_decode_wake_word_buffer",
               "_handle_command_mode_utterance", "_dictate_commit_redecode", "_gate_scan",
               "_is_dictate_context_echo", "_log_cmd_utt_dropped"}
    nodes = [node for node in tree.body
             if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants
                                                     for t in node.targets))
             or (isinstance(node, ast.FunctionDef) and node.name in functions)
             or (isinstance(node, ast.ClassDef) and node.name in constants)]
    app = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DictationApp")
    nodes += [node for node in app.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), module.__dict__)
    module.method_names = methods
    return module


@pytest.fixture
def app():
    production = load_gate_methods()
    fake = SimpleNamespace(
        production=production, config={"language": "auto", "language_confidence_floor": 0.9},
        _language_confidence_gate=production._languages.LanguageConfidenceGate(),
        play_sound=Mock(), model_lock=threading.Lock(), model_rate=16000,
        _buffer_has_contiguous_speech=Mock(return_value=False),
        voice_training_window=SimpleNamespace(apply_corrections=lambda text: text),
        get_transcription_params=lambda **kwargs: {"language": None},
        _log_history=Mock(), _vad_reset=Mock(), _output_dictation=Mock(),
        app_state="asleep",
        _transcription_owners=SimpleNamespace(claim=Mock(), release=Mock()),
    )
    for name in production.method_names:
        method = getattr(production, name)
        setattr(fake, name, method if name == "_log_cmd_utt_dropped" else MethodType(method, fake))
    # _buffer_should_skip_decode now delegates to the richer run scanner.
    # Keep this test at that boundary: individual VAD/ZCR behavior has its
    # own tests, while these assertions cover which capture path is selected.
    fake._speech_run_scan = Mock(return_value=production._SpeechRun(
        False, 0, 0.0, 0.0, 0, "vad", False, 0.0))
    fake._is_dictate_context_echo = Mock(return_value=False)
    return fake


def info(language="nn", probability=0.45):
    return SimpleNamespace(language=language, language_probability=probability)


def set_decode(app, text="This is invented prose.", language="nn", probability=0.45):
    segment = SimpleNamespace(text=text, no_speech_prob=0.01, avg_logprob=-0.1, compression_ratio=1.0)
    app.model = SimpleNamespace(transcribe=Mock(return_value=([segment], info(language, probability))))


def test_outside_low_confidence_rejected_with_one_event_and_earcon(app, caplog):
    text = "This is a fabricated sentence longer than forty characters."
    caplog.set_level(logging.INFO, logger=app.production.logger.name)
    assert app._filter_dictation_language(text, info()) == ""
    app.play_sound.assert_called_once_with("scratch_refuse")
    app.production.flight_recorder.record.assert_called_once_with(
        "decode.language_rejected", language="nn", language_probability=0.45,
        reason="low_confidence", expected_languages=["en"])
    assert len(caplog.records) == 1
    assert repr(text[:40]) in caplog.records[0].message
    assert not app._language_confidence_gate.accepted


def test_same_probability_accepted_for_expected_language(app):
    text = "This is the owner's speech."
    assert app._filter_dictation_language(text, info("en")) == text
    app.play_sound.assert_not_called()


@pytest.mark.parametrize("text", ["ఇది ఒక వాక్యం", "これはひらがなです"])
def test_script_mismatch_rejected_even_with_expected_language_and_high_probability(app, text):
    assert app._filter_dictation_language(text, info("en", 0.99)) == ""
    assert app.production.flight_recorder.record.call_args.kwargs["reason"] == "script_mismatch"


@pytest.mark.parametrize("text", ["Enjoy the café.", "Great work 😊", "cafe\u0301", "😊 123!"])
def test_accents_and_emoji_are_accepted(app, text):
    assert app._filter_dictation_language(text, info("en", 0.45)) == text


def test_script_boundary_is_strictly_greater_than_30_percent(app):
    assert app._filter_dictation_language("abcdefgあいう", info("en")) == "abcdefgあいう"
    assert app._filter_dictation_language("abcdefあいうえ", info("en")) == ""


def test_rolling_window_evicts_previous_language_and_rejections_do_not_train(app):
    assert app._filter_dictation_language("Bonjour tout le monde", info("fr", 0.99))
    assert app._filter_dictation_language("Bonjour", info("fr", 0.45))
    for _ in range(20):
        assert app._filter_dictation_language("English speech", info("en", 0.99))
    assert list(app._language_confidence_gate.accepted) == ["en"] * 20
    assert app._filter_dictation_language("Invented prose", info("nn", 0.45)) == ""
    assert app._filter_dictation_language("Bonjour", info("fr", 0.45)) == ""
    assert list(app._language_confidence_gate.accepted) == ["en"] * 20


@pytest.mark.parametrize("language,text", [("ja", "これはひらがなです"), ("te", "ఇది ఒక వాక్యం"),
                                          ("ru", "Привет мир"), ("en", "Hello")])
def test_forced_language_uses_configured_script_and_expected_set(app, language, text):
    app.config["language"] = language
    before = copy.deepcopy(app.config)
    assert app._filter_dictation_language(text, info(language, 0.45)) == text
    assert app.config == before


def test_config_floor_override_and_default(app):
    app.config.pop("language_confidence_floor")
    assert app._filter_dictation_language("Invented prose", info("nn", 0.89)) == ""
    app.config["language_confidence_floor"] = 0.80
    assert app._filter_dictation_language("Actual prose", info("nn", 0.89))


@pytest.mark.parametrize("floor", [None, "bad", float("nan"), 1.1, -0.1])
def test_invalid_floor_uses_measured_default(app, floor):
    app.config["language_confidence_floor"] = floor
    assert app._filter_dictation_language("Invented prose", info()) == ""


def test_preview_and_empty_results_do_not_train_or_play_sound(app):
    assert app._filter_dictation_language("Bonjour", info("fr", 0.99), remember=False, feedback=False)
    assert app._filter_dictation_language("", info("fr", 0.99)) == ""
    assert app._filter_dictation_language("Invented prose", info(), remember=False, feedback=False) == ""
    assert not app._language_confidence_gate.accepted
    app.play_sound.assert_not_called()


def test_30_second_silence_scans_in_bounded_eight_second_chunks(app):
    audio = np.zeros(30 * 16000, dtype=np.float32)
    assert app._buffer_should_skip_decode(audio, 16000, head_grace_ms=100)
    args, kwargs = app._speech_run_scan.call_args
    assert args[0] is audio
    # Long quiet buffers now scan every bounded eight-second chunk, rather
    # than silently deciding from only the head.
    assert kwargs["chunk_s"] == 8
    assert kwargs["head_grace_ms"] == 100


def test_speech_only_after_ten_seconds_above_existing_floor_skips_vad_not_decode(app):
    floor = 10 ** (app.production._SANITY_RMS_FLOOR_DB / 20.0)
    audio = np.zeros(30 * 16000, dtype=np.float32)
    t = np.arange(20 * 16000) / 16000
    audio[10 * 16000:] = floor * 4 * np.sin(2 * np.pi * 220 * t)
    assert np.sqrt(np.mean(audio ** 2)) > floor
    assert not app._buffer_should_skip_decode(audio, 16000)
    app._speech_run_scan.assert_not_called()


def test_quiet_long_buffer_with_contiguous_prefix_speech_is_kept(app):
    app._speech_run_scan.return_value = app.production._SpeechRun(
        True, 160, 0.0, 8.0, 1, "vad", False, 30.0)
    assert not app._buffer_should_skip_decode(np.zeros(30 * 16000), 16000)


def test_short_buffer_retains_existing_presence_gate(app):
    audio = np.ones(8 * 16000, dtype=np.float32)
    assert app._buffer_should_skip_decode(audio, 16000)
    assert app._speech_run_scan.call_args.args[0] is audio
    assert app._speech_run_scan.call_args.kwargs["chunk_s"] is None


def test_hotkey_language_rejection_cannot_be_retried_into_output(app):
    set_decode(app)
    audio = np.ones(30 * 16000, dtype=np.float32)
    result = app._decode_hotkey_audio(audio, {"language": None}, 30)
    assert result.text == "" and result.language_rejected
    retry = Mock()
    assert app.production._apply_retry_on_suspected_loss(result, retry, audio, 16000, 30, False) == (
        result, False, False)
    retry.assert_not_called()


def test_rejected_retry_preserves_accepted_original(app):
    original = app.production._HotkeyDecodeResult("Short accepted text", False, [], "en", "short")
    rejected = original._replace(text="", language_rejected=True)
    result, suspected, retried = app.production._apply_retry_on_suspected_loss(
        original, lambda: rejected, np.ones(30 * 16000), 16000, 30, False)
    assert result is original and suspected and retried


def test_command_hotkey_bypasses_language_gate(app):
    set_decode(app)
    result = app._decode_hotkey_audio(np.zeros(16000), {}, 1, free_form=False)
    assert result.text and not result.language_rejected
    app.play_sound.assert_not_called()


def test_hotkey_accepts_owner_at_low_confidence(app):
    set_decode(app, language="en")
    result = app._decode_hotkey_audio(np.zeros(16000), {}, 1)
    assert result.text == "This is invented prose." and not result.language_rejected
    assert list(app._language_confidence_gate.accepted) == ["en"]


def test_long_split_checks_each_decode_info(app):
    segment = SimpleNamespace(text="A complete phrase. ", no_speech_prob=0.01,
                              avg_logprob=-0.1, compression_ratio=1.0)
    app.model = SimpleNamespace(transcribe=Mock(side_effect=[
        ([segment], info("en", 0.99)), ([segment], info("nn", 0.45))]))
    app.production._split_audio_at_silences = lambda audio, rate: [audio[:16000], audio[16000:32000]]
    result = app._decode_hotkey_audio(np.zeros(181 * 16000), {}, 181)
    assert result.language_rejected and result.text == ""
    app.play_sound.assert_called_once_with("scratch_refuse")


def test_continuous_rejection_never_reaches_command_or_paste(app, caplog):
    set_decode(app)
    app.command_executor = SimpleNamespace(process_text=Mock())
    app._paste_preserving_clipboard = Mock()
    app.transcribe_continuous_buffer([np.zeros(16000)], src_rate=16000)
    app.play_sound.assert_called_once_with("scratch_refuse")
    app.command_executor.process_text.assert_not_called()
    app._paste_preserving_clipboard.assert_not_called()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_wake_rejection_uses_existing_empty_history_branch(app, caplog):
    set_decode(app)
    app._wake_audio_is_below_gate = lambda *args, **kwargs: False
    app._vad_available = True
    app._decode_wake_word_buffer([np.zeros(16000)], 16000)
    app.play_sound.assert_called_once_with("scratch_refuse")
    assert app._log_history.call_args.kwargs["status"] == "empty"
    app._output_dictation.assert_not_called()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("lane", ["DICTATE", "AVA", "COMMAND"])
def test_toggle_gates_only_free_form_lanes(app, lane, caplog):
    modes = SimpleNamespace(COMMAND=object(), DICTATE=object(), AVA=object())
    app.production.SessionMode = modes
    manager = SimpleNamespace(mode=getattr(modes, lane), dictate_context_tail=lambda: "",
                              dispatch_utterance=Mock(return_value=SimpleNamespace(kind="empty", detail="")))
    # The production log reads mode.value, so use small enum-like objects.
    for name in ("COMMAND", "DICTATE", "AVA"):
        setattr(modes, name, SimpleNamespace(value=name))
    manager.mode = getattr(modes, lane)
    app._ensure_session_mode_manager = lambda: manager
    app._transcription_owners = SimpleNamespace(claim=Mock(), release=Mock())
    app._command_mode_ghost_tap = False
    app._dictate_preview = None
    app._compute_switch_gate_signals = Mock()
    app._handle_session_dispatch_outcome = Mock()
    app.production._drop_trailing_garbage_segments = lambda segs: segs
    app.production._trim_trailing_garbage_run = lambda text: text
    set_decode(app, text="これはひらがなです", language="en", probability=1.0)
    app._handle_command_mode_utterance([np.zeros(16000)], 16000)
    if lane == "COMMAND":
        manager.dispatch_utterance.assert_called_once()
        app.play_sound.assert_not_called()
    else:
        manager.dispatch_utterance.assert_not_called()
        app.play_sound.assert_called_once_with("scratch_refuse")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_commit_redecode_is_gated(app):
    set_decode(app)
    app.production._drop_trailing_garbage_segments = lambda segs: segs
    app.production._trim_trailing_garbage_run = lambda text: text
    assert app._dictate_commit_redecode("old text", [np.zeros(16000)]) is None
    app.play_sound.assert_called_once_with("scratch_refuse")


def test_preview_filters_before_display_without_learning_or_feedback(app):
    set_decode(app, text="これはひらがなです", language="en", probability=1.0)
    path = hf_bench.REPO_ROOT / "samsara/streaming.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictatePreviewSession")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_transcribe_partial")
    namespace = {"logger": app.production.logger}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
    preview = SimpleNamespace(app=app, _snapshot_audio=lambda: np.zeros(16000), _partial_params=lambda: {})
    assert namespace["_transcribe_partial"](preview) == ""
    app.play_sound.assert_not_called()
    assert not app._language_confidence_gate.accepted
    assert app.model_lock.acquire(blocking=False)
    app.model_lock.release()
