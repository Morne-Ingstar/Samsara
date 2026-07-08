"""Tests for samsara.smart_corrections: the optional LLM cleanup pass over
dictation output.

All backend calls are monkeypatched -- no network, no real Ollama/cloud_llm
traffic. Fakes use a minimal duck-typed `app` (config dict + a
voice_training_window stand-in), matching the pattern used elsewhere in this
suite (see test_transcription_params.py).
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samsara.smart_corrections as sc


def _make_app(smart_corrections=None, cloud_llm=None, vocab=None, corrections=None):
    app = types.SimpleNamespace()
    app.config = {
        'smart_corrections': smart_corrections or {},
        'cloud_llm': cloud_llm or {},
        'ollama': {},
    }
    app.voice_training_window = types.SimpleNamespace(
        custom_vocab=vocab or [],
        corrections_dict=corrections or {},
    )
    return app


# ============================================================================
# Gating -- disabled / under min_words / no backend resolved
# ============================================================================

class TestGating:
    def test_disabled_is_passthrough_backend_never_called(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: calls.append(app) or 'ollama')
        app = _make_app(smart_corrections={'enabled': False})

        result = sc.smart_correct("this text has enough words", app)

        assert result == "this text has enough words"
        assert calls == []

    def test_under_min_words_is_passthrough_backend_never_called(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: calls.append(app) or 'ollama')
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 3})

        result = sc.smart_correct("two words", app)

        assert result == "two words"
        assert calls == []

    def test_no_backend_resolved_is_passthrough(self, monkeypatch):
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: None)
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1})

        result = sc.smart_correct("plenty of words here to pass the gate", app)

        assert result == "plenty of words here to pass the gate"


# ============================================================================
# Happy path -- backend result flows through sanitize verbatim
# ============================================================================

class TestHappyPath:
    def test_backend_correction_returned_verbatim_after_sanitize(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'})
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: 'ollama')
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s: "Their going to the store.",
        )

        result = sc.smart_correct("there going too the store", app)

        assert result == "Their going to the store."

    def test_cloud_backend_is_used_when_resolved(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'cloud'})
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: 'cloud')
        monkeypatch.setattr(
            sc, '_call_cloud',
            lambda text, app, system_prompt, timeout_s: "Corrected sentence here.",
        )

        result = sc.smart_correct("original sentence here", app)

        assert result == "Corrected sentence here."


# ============================================================================
# Failure handling -- never raises, always falls back to original
# ============================================================================

class TestFailureHandling:
    def test_backend_raises_returns_original_no_exception(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'})
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: 'ollama')

        def _boom(text, app, system_prompt, timeout_s):
            raise TimeoutError("simulated timeout")

        monkeypatch.setattr(sc, '_call_ollama', _boom)

        result = sc.smart_correct("this should not raise at all", app)

        assert result == "this should not raise at all"

    def test_backend_returns_none_returns_original(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'})
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: 'ollama')
        monkeypatch.setattr(sc, '_call_ollama', lambda text, app, system_prompt, timeout_s: None)

        result = sc.smart_correct("connection failed but this must survive", app)

        assert result == "connection failed but this must survive"


# ============================================================================
# _sanitize_output -- pure-function guardrails
# ============================================================================

class TestSanitizeOutput:
    def test_strips_markdown_fences(self):
        raw = "```\nHello there, friend.\n```"
        result = sc._sanitize_output(raw, "hello there friend")
        assert result == "Hello there, friend."

    def test_strips_surrounding_quotes(self):
        raw = '"Hello there, friend."'
        result = sc._sanitize_output(raw, "hello there friend")
        assert result == "Hello there, friend."

    def test_strips_preamble_line(self):
        raw = "Corrected text: Hello there, friend."
        result = sc._sanitize_output(raw, "hello there friend")
        assert result == "Hello there, friend."

    def test_empty_output_returns_original(self):
        assert sc._sanitize_output("", "hello there friend") == "hello there friend"
        assert sc._sanitize_output("   ", "hello there friend") == "hello there friend"
        assert sc._sanitize_output(None, "hello there friend") == "hello there friend"

    def test_word_count_deviation_over_40_percent_returns_original(self):
        original = "one two three four five"  # 5 words
        raw = "one two three four five six seven eight"  # 8 words -> 60% deviation
        result = sc._sanitize_output(raw, original)
        assert result == original

    def test_word_count_deviation_within_40_percent_is_accepted(self):
        original = "one two three four five"  # 5 words
        raw = "one two three four five six seven"  # 7 words -> exactly 40%
        result = sc._sanitize_output(raw, original)
        assert result == raw

    def test_newline_collapse_when_original_has_none(self):
        original = "hello there friend"
        raw = "Hello,\nthere friend."
        result = sc._sanitize_output(raw, original)
        assert "\n" not in result
        assert result == "Hello, there friend."

    def test_newlines_preserved_when_original_has_newlines(self):
        original = "hello there\nfriend"
        raw = "Hello there,\nfriend."
        result = sc._sanitize_output(raw, original)
        assert result == "Hello there,\nfriend."
