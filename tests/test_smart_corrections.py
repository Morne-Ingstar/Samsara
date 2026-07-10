"""Tests for samsara.smart_corrections: the optional LLM cleanup pass over
dictation output.

All backend calls are monkeypatched -- no network, no real Ollama/cloud_llm
traffic. Fakes use a minimal duck-typed `app` (config dict + a
voice_training_window stand-in), matching the pattern used elsewhere in this
suite (see test_transcription_params.py).

smart_correct() resolves its backend via _resolve_backend_detailed() (not
the public resolve_backend() wrapper) so it can also see used_cloud_fallback
and skip_reason for logging/notification purposes -- tests that drive
smart_correct()'s gating/happy-path/failure behavior monkeypatch
_resolve_backend_detailed directly. Tests of the routing matrix itself
(auto/ollama/cloud x reachability x allow_cloud_fallback) exercise the real
resolve_backend()/_resolve_backend_detailed() logic against a monkeypatched
_ollama_reachable + cloud_llm.is_enabled.
"""

import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samsara.smart_corrections as sc


def _make_app(smart_corrections=None, cloud_llm=None, vocab=None, corrections=None, language=None):
    app = types.SimpleNamespace()
    app.config = {
        'smart_corrections': smart_corrections or {},
        'cloud_llm': cloud_llm or {},
        'ollama': {},
    }
    if language is not None:
        app.config['language'] = language
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
        monkeypatch.setattr(
            sc, '_resolve_backend_detailed',
            lambda app: (calls.append(app) or 'ollama', False, None),
        )
        app = _make_app(smart_corrections={'enabled': False})

        result = sc.smart_correct("this text has enough words", app)

        assert result == "this text has enough words"
        assert calls == []

    def test_under_min_words_is_passthrough_backend_never_called(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            sc, '_resolve_backend_detailed',
            lambda app: (calls.append(app) or 'ollama', False, None),
        )
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 3})

        result = sc.smart_correct("two words", app)

        assert result == "two words"
        assert calls == []

    def test_no_backend_resolved_is_passthrough(self, monkeypatch):
        monkeypatch.setattr(
            sc, '_resolve_backend_detailed',
            lambda app: (None, False, 'ollama_down_fallback_disabled'),
        )
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1})

        result = sc.smart_correct("plenty of words here to pass the gate", app)

        assert result == "plenty of words here to pass the gate"


# ============================================================================
# Happy path -- backend result flows through sanitize verbatim
# ============================================================================

class TestHappyPath:
    def test_backend_correction_returned_verbatim_after_sanitize(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'})
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s, model: ("Their going to the store.", False),
        )

        result = sc.smart_correct("there going too the store", app)

        assert result == "Their going to the store."

    def test_cloud_backend_is_used_when_resolved(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'cloud'})
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('cloud', False, None))
        monkeypatch.setattr(
            sc, '_call_cloud',
            lambda text, app, system_prompt, timeout_s: ("Corrected sentence here.", False),
        )

        result = sc.smart_correct("original sentence here", app)

        assert result == "Corrected sentence here."


# ============================================================================
# Failure handling -- never raises, always falls back to original
# ============================================================================

class TestFailureHandling:
    def test_backend_raises_returns_original_no_exception(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'})
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))

        def _boom(text, app, system_prompt, timeout_s, model):
            raise TimeoutError("simulated timeout")

        monkeypatch.setattr(sc, '_call_ollama', _boom)

        result = sc.smart_correct("this should not raise at all", app)

        assert result == "this should not raise at all"

    def test_backend_returns_none_returns_original(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'})
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s, model: (None, False),
        )

        result = sc.smart_correct("connection failed but this must survive", app)

        assert result == "connection failed but this must survive"


# ============================================================================
# Routing matrix -- backend='auto' x ollama reachability x allow_cloud_fallback
# (Task 2: auto-fallback must be opt-in, never a silent cloud-routing surprise)
# ============================================================================

class TestRoutingMatrix:
    def test_auto_ollama_reachable_uses_ollama_regardless_of_fallback_setting(self, monkeypatch):
        monkeypatch.setattr(sc, '_ollama_reachable', lambda app: True)
        app = _make_app(smart_corrections={'backend': 'auto', 'allow_cloud_fallback': False})

        assert sc.resolve_backend(app) == 'ollama'

    def test_auto_ollama_down_fallback_disabled_skips_with_info_log(self, monkeypatch, caplog):
        monkeypatch.setattr(sc, '_ollama_reachable', lambda app: False)
        app = _make_app(smart_corrections={'backend': 'auto', 'allow_cloud_fallback': False})

        with caplog.at_level('INFO'):
            backend = sc.resolve_backend(app)

        assert backend is None
        assert any(
            "local backend down, cloud fallback disabled -- skipping" in r.message
            for r in caplog.records
        )

    def test_auto_ollama_down_fallback_enabled_cloud_configured_uses_cloud(self, monkeypatch):
        monkeypatch.setattr(sc, '_ollama_reachable', lambda app: False)
        monkeypatch.setattr(sc.cloud_llm, 'is_enabled', lambda app: True)
        app = _make_app(
            smart_corrections={'backend': 'auto', 'allow_cloud_fallback': True},
            cloud_llm={'enabled': True},
        )

        backend, used_fallback, skip_reason = sc._resolve_backend_detailed(app)

        assert backend == 'cloud'
        assert used_fallback is True
        assert skip_reason is None

    def test_auto_ollama_down_fallback_enabled_no_cloud_configured_skips(self, monkeypatch, caplog):
        monkeypatch.setattr(sc, '_ollama_reachable', lambda app: False)
        monkeypatch.setattr(sc.cloud_llm, 'is_enabled', lambda app: False)
        app = _make_app(smart_corrections={'backend': 'auto', 'allow_cloud_fallback': True})

        with caplog.at_level('INFO'):
            backend = sc.resolve_backend(app)

        assert backend is None
        assert any(
            "no cloud provider configured -- skipping" in r.message
            for r in caplog.records
        )

    def test_explicit_backend_ollama_down_resolves_none(self, monkeypatch):
        monkeypatch.setattr(sc, '_ollama_reachable', lambda app: False)
        app = _make_app(smart_corrections={'backend': 'ollama'})

        assert sc.resolve_backend(app) is None

    def test_explicit_backend_cloud_always_uses_cloud_when_enabled(self, monkeypatch):
        monkeypatch.setattr(sc.cloud_llm, 'is_enabled', lambda app: True)
        app = _make_app(smart_corrections={'backend': 'cloud', 'allow_cloud_fallback': False})

        assert sc.resolve_backend(app) == 'cloud'


# ============================================================================
# Backend attribution in logs (Task 1) -- every completed call names its
# backend (+ model + elapsed ms where applicable)
# ============================================================================

class TestBackendAttributionLogging:
    def test_ollama_success_with_change_logs_backend_model_ms(self, monkeypatch, caplog):
        app = _make_app(smart_corrections={
            'enabled': True, 'min_words': 1, 'backend': 'ollama', 'ollama_model': 'qwen3:8b',
        })
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s, model: ("Their going to the store.", False),
        )

        with caplog.at_level('INFO'):
            result = sc.smart_correct("there going too the store", app)

        assert result == "Their going to the store."
        msgs = [r.message for r in caplog.records]
        assert any(
            m.startswith('[SMART][ollama qwen3:8b')
            and 'ms]' in m
            and m.endswith('"there going too the store" -> "Their going to the store."')
            for m in msgs
        )

    def test_cloud_no_change_logs_backend_and_ms_no_model(self, monkeypatch, caplog):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'cloud'})
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('cloud', False, None))
        monkeypatch.setattr(
            sc, '_call_cloud',
            lambda text, app, system_prompt, timeout_s: ("same text here", False),
        )

        with caplog.at_level('INFO'):
            result = sc.smart_correct("same text here", app)

        assert result == "same text here"
        msgs = [r.message for r in caplog.records]
        assert any(
            m.startswith('[SMART][cloud') and 'ms] no change' in m
            for m in msgs
        )

    def test_ollama_timeout_logs_backend_only(self, monkeypatch, caplog):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'})
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s, model: (None, True),
        )

        with caplog.at_level('INFO'):
            result = sc.smart_correct("this call will time out here", app)

        assert result == "this call will time out here"
        msgs = [r.message for r in caplog.records]
        assert '[SMART][ollama] timed out -- returning original' in msgs


# ============================================================================
# keep_alive / think=false in the Ollama request payload (Task 5 / Task 6)
# ============================================================================

class _FakeOllamaResponse:
    def __init__(self, content="corrected text"):
        self.status_code = 200
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": self._content}}


class TestOllamaPayload:
    def test_keep_alive_default_in_payload(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['payload'] = json
            return _FakeOllamaResponse()

        monkeypatch.setattr(sc._session, 'post', fake_post)
        app = _make_app()

        raw, was_timeout = sc._call_ollama("some text", app, "system prompt", 6.0, "qwen2.5:3b")

        assert was_timeout is False
        assert captured['payload']['keep_alive'] == '30m'

    def test_keep_alive_configured_value_used(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['payload'] = json
            return _FakeOllamaResponse()

        monkeypatch.setattr(sc._session, 'post', fake_post)
        app = _make_app(smart_corrections={'keep_alive': '10m'})

        sc._call_ollama("some text", app, "system prompt", 6.0, "qwen2.5:3b")

        assert captured['payload']['keep_alive'] == '10m'

    def test_think_false_in_payload(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured['payload'] = json
            return _FakeOllamaResponse()

        monkeypatch.setattr(sc._session, 'post', fake_post)
        app = _make_app()

        sc._call_ollama("some text", app, "system prompt", 6.0, "qwen2.5:3b")

        assert captured['payload']['think'] is False

    def test_timeout_is_reported(self, monkeypatch):
        def fake_post(url, json=None, timeout=None):
            raise sc.requests.exceptions.Timeout("simulated")

        monkeypatch.setattr(sc._session, 'post', fake_post)
        app = _make_app()

        raw, was_timeout = sc._call_ollama("some text", app, "system prompt", 6.0, "qwen2.5:3b")

        assert raw is None
        assert was_timeout is True


# ============================================================================
# _call_cloud uses cloud_llm.send_ex's structured result (tribunal Fix 8) --
# not a substring match on "timed out" in an "Error: ..." string.
# ============================================================================

class TestCallCloudUsesSendEx:
    def test_success_passes_text_through_not_timeout(self, monkeypatch):
        monkeypatch.setattr(
            sc.cloud_llm, 'send_ex',
            lambda system_prompt, text, app, timeout: ("corrected text", None),
        )
        app = _make_app()

        raw, was_timeout = sc._call_cloud("some text", app, "system prompt", 6.0)

        assert raw == "corrected text"
        assert was_timeout is False

    def test_timeout_error_kind_reports_was_timeout_true(self, monkeypatch):
        monkeypatch.setattr(
            sc.cloud_llm, 'send_ex',
            lambda system_prompt, text, app, timeout: (None, "timeout"),
        )
        app = _make_app()

        raw, was_timeout = sc._call_cloud("some text", app, "system prompt", 6.0)

        assert raw is None
        assert was_timeout is True

    def test_generic_error_kind_reports_was_timeout_false(self, monkeypatch):
        monkeypatch.setattr(
            sc.cloud_llm, 'send_ex',
            lambda system_prompt, text, app, timeout: (None, "error"),
        )
        app = _make_app()

        raw, was_timeout = sc._call_cloud("some text", app, "system prompt", 6.0)

        assert raw is None
        assert was_timeout is False

    def test_response_text_that_happens_to_contain_timed_out_is_not_misclassified(self, monkeypatch):
        """Regression guard for the exact substring-match bug Fix 8
        replaces: a real dictated/corrected sentence that happens to
        contain the words "timed out" must never be misread as a timeout
        just because send_ex reports success."""
        monkeypatch.setattr(
            sc.cloud_llm, 'send_ex',
            lambda system_prompt, text, app, timeout: (
                "The meeting timed out after an hour.", None,
            ),
        )
        app = _make_app()

        raw, was_timeout = sc._call_cloud("some text", app, "system prompt", 6.0)

        assert raw == "The meeting timed out after an hour."
        assert was_timeout is False


# ============================================================================
# warm_up() -- fire-and-forget, gated on backend=='ollama', never raises
# ============================================================================

class TestWarmUp:
    def test_warm_up_never_raises_when_backend_resolution_fails(self, monkeypatch):
        def _boom(app):
            raise RuntimeError("resolution boom")

        monkeypatch.setattr(sc, 'resolve_backend', _boom)
        app = _make_app()

        sc.warm_up(app)  # must not raise

    def test_warm_up_noop_when_backend_not_ollama(self, monkeypatch):
        spawned = []
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: 'cloud')
        monkeypatch.setattr(
            sc.thread_registry, 'spawn',
            lambda name, target, daemon=True: spawned.append(name),
        )
        app = _make_app()

        sc.warm_up(app)

        assert spawned == []

    def test_warm_up_spawns_daemon_thread_when_backend_is_ollama(self, monkeypatch):
        spawned = []
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: 'ollama')
        monkeypatch.setattr(
            sc.thread_registry, 'spawn',
            lambda name, target, daemon=True: spawned.append((name, target, daemon)),
        )
        app = _make_app()

        sc.warm_up(app)

        assert len(spawned) == 1
        name, target, daemon = spawned[0]
        assert name == "smart_corrections.warm_up"
        assert daemon is True
        assert callable(target)

    def test_warm_up_request_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(sc, 'resolve_backend', lambda app: 'ollama')

        def fake_spawn(name, target, daemon=True):
            target()  # run inline -- exercises _do_warm_up's own try/except

        monkeypatch.setattr(sc.thread_registry, 'spawn', fake_spawn)

        def fake_post(url, json=None, timeout=None):
            raise ConnectionError("no server")

        monkeypatch.setattr(sc._session, 'post', fake_post)
        app = _make_app()

        sc.warm_up(app)  # must not raise


# ============================================================================
# One-time-per-session fallback tray notice (Task 3)
# ============================================================================

class TestFallbackNotice:
    @pytest.fixture(autouse=True)
    def _reset_notice_flag(self, monkeypatch):
        monkeypatch.setattr(sc, '_fallback_notice_shown', False)

    def test_notifies_once_when_skipping(self, monkeypatch):
        notified = []
        nm = types.SimpleNamespace(
            show_notification=lambda title, msg, duration=5: notified.append((title, msg))
        )
        app = _make_app(smart_corrections={
            'enabled': True, 'min_words': 1, 'backend': 'auto', 'allow_cloud_fallback': False,
        })
        app.notification_manager = nm
        monkeypatch.setattr(sc, '_ollama_reachable', lambda app: False)

        sc.smart_correct("first call that should skip and notify", app)
        sc.smart_correct("second call that should skip and not notify again", app)

        assert len(notified) == 1
        assert notified[0][0] == "Smart Corrections"
        assert "skipping" in notified[0][1]

    def test_notifies_once_when_routed_to_cloud_via_fallback(self, monkeypatch):
        notified = []
        nm = types.SimpleNamespace(
            show_notification=lambda title, msg, duration=5: notified.append((title, msg))
        )
        app = _make_app(
            smart_corrections={
                'enabled': True, 'min_words': 1, 'backend': 'auto', 'allow_cloud_fallback': True,
            },
            cloud_llm={'enabled': True},
        )
        app.notification_manager = nm
        monkeypatch.setattr(sc, '_ollama_reachable', lambda app: False)
        monkeypatch.setattr(sc.cloud_llm, 'is_enabled', lambda app: True)
        monkeypatch.setattr(
            sc, '_call_cloud',
            lambda text, app, system_prompt, timeout_s: (text, False),
        )

        sc.smart_correct("route this to cloud via fallback please", app)

        assert len(notified) == 1
        assert "using Cloud AI" in notified[0][1]

    def test_no_notification_manager_does_not_raise(self, monkeypatch):
        app = _make_app(smart_corrections={
            'enabled': True, 'min_words': 1, 'backend': 'auto', 'allow_cloud_fallback': False,
        })
        monkeypatch.setattr(sc, '_ollama_reachable', lambda app: False)

        sc.smart_correct("no notifier present, must not raise", app)


# ============================================================================
# _sanitize_output -- pure-function guardrails
# ============================================================================

class TestSanitizeOutput:
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

    def test_word_count_deviation_over_15_percent_returns_original(self):
        original = "one two three four five"  # 5 words
        raw = "one two three four five six seven eight"  # 8 words -> 60% deviation
        result = sc._sanitize_output(raw, original)
        assert result == original

    def test_word_count_deviation_within_15_percent_is_accepted(self):
        # 20 words -> +3 words is exactly 15%.
        original = " ".join(f"w{i}" for i in range(20))
        raw = original + " extra words here"  # 23 words -> exactly 15%
        result = sc._sanitize_output(raw, original)
        assert result == raw

    def test_word_count_deviation_just_over_15_percent_is_rejected(self):
        # 20 words -> +4 words is 20%, just over the boundary.
        original = " ".join(f"w{i}" for i in range(20))
        raw = original + " extra words here now"  # 24 words -> 20%
        result = sc._sanitize_output(raw, original)
        assert result == original

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


# ============================================================================
# <think>...</think> stripping (Task 6 -- qwen3-family reasoning models)
# ============================================================================

class TestThinkTagStripping:
    """_strip_think_blocks/_strip_fences are kept as functions (tested
    directly below, for other callers), but tribunal Fix 2 means
    _sanitize_output no longer calls them destructively -- a <think> tag or
    code fence present in the raw output but absent from the original is
    now a REJECT signal (return original verbatim), not something to strip
    and continue past. Stripping-and-continuing risked deleting real user
    speech mixed in with the artifact."""

    def test_closed_think_block_in_raw_not_in_original_rejects(self):
        raw = "<think>reasoning about the correction here</think>Corrected sentence."
        result = sc._sanitize_output(raw, "corrected sentence")
        assert result == "corrected sentence"

    def test_unclosed_think_block_rejects(self):
        raw = "<think>reasoning that got cut off mid-stream"
        result = sc._sanitize_output(raw, "original text here")
        assert result == "original text here"

    def test_think_tag_present_in_original_too_is_not_rejected_for_that_reason(self):
        # The user's own dictation literally contained the substring
        # "<think>" (e.g. reading code aloud) -- its presence in the output
        # must not by itself trigger a reject. Same word count on both
        # sides so only the artifact-gate behavior is under test here.
        original = "say the tag <think> now"
        raw = "Say the tag <think> now."
        result = sc._sanitize_output(raw, original)
        assert result == "Say the tag <think> now."

    def test_fake_backend_returning_think_wrapped_output_rejects_end_to_end(self, monkeypatch):
        app = _make_app(smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'})
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s, model: (
                "<think>let me consider homophones here</think>Their going to the store.",
                False,
            ),
        )

        result = sc.smart_correct("there going too the store", app)

        assert result == "there going too the store"

    def test_strip_think_blocks_function_still_works_directly(self):
        """Regression guard: the underlying helper itself is unchanged --
        only _sanitize_output stopped calling it."""
        raw = "<think>reasoning</think>Corrected sentence."
        assert sc._strip_think_blocks(raw) == "Corrected sentence."


class TestCodeFenceRejection:
    def test_fence_in_raw_not_in_original_rejects(self):
        raw = "```\nHello there, friend.\n```"
        result = sc._sanitize_output(raw, "hello there friend")
        assert result == "hello there friend"

    def test_fence_present_in_original_too_is_not_rejected_for_that_reason(self):
        # The user dictated something that literally included a code fence
        # (e.g. reading a markdown snippet aloud) -- must not itself reject.
        original = "wrap it in a fence like ``` for markdown"
        raw = "Wrap it in a fence like ``` for markdown."
        result = sc._sanitize_output(raw, original)
        assert result == "Wrap it in a fence like ``` for markdown."

    def test_strip_fences_function_still_works_directly(self):
        """Regression guard: the underlying helper itself is unchanged --
        only _sanitize_output stopped calling it."""
        raw = "```\nHello there, friend.\n```"
        assert sc._strip_fences(raw) == "Hello there, friend."


# ============================================================================
# Anti-over-edit punctuation floor (Task 6)
# ============================================================================

class TestPunctuationFloor:
    def test_llama_failure_signature_is_rejected(self):
        """The observed llama3.2 failure: commas/apostrophes silently
        stripped while the word count barely moves."""
        original = "OK, I think it's fine."
        bad_correction = "OK I think its fine."

        result = sc._sanitize_output(bad_correction, original)

        assert result == original

    def test_legitimate_quote_adding_correction_passes(self):
        original = "Did you read the article called Hidden Costs"
        good_correction = 'Did you read the article called "Hidden Costs"?'

        result = sc._sanitize_output(good_correction, original)

        assert result == good_correction

    def test_legitimate_comma_adding_correction_passes(self):
        original = "I went to the store and bought milk"
        good_correction = "I went to the store, and bought milk."

        result = sc._sanitize_output(good_correction, original)

        assert result == good_correction

    def test_shrink_at_new_015_threshold_is_rejected_by_punct_floor(self):
        # 20 words with a trailing period, corrected drops exactly 3 words
        # (15% shrink, the new threshold) AND the period -- rejected by the
        # punctuation floor itself (word_shrink <= threshold).
        original = " ".join(f"w{i}" for i in range(19)) + " w19."
        bad_correction = " ".join(f"w{i}" for i in range(17))  # 17 words, no period

        result = sc._sanitize_output(bad_correction, original)

        assert result == original

    def test_shrink_just_over_015_threshold_still_rejected_by_deviation_gate(self):
        # Fix 3 invariant: once shrink exceeds the punct-floor threshold
        # (so the floor itself lets it through), the SAME shrink fraction
        # is by construction also over _WORD_DEVIATION_LIMIT (equal
        # thresholds) -- so the deviation gate catches it instead. Net
        # result is unchanged (still rejected), just via the other gate.
        original = " ".join(f"w{i}" for i in range(19)) + " w19."
        bad_correction = " ".join(f"w{i}" for i in range(16))  # 16 words, no period (20% shrink)

        result = sc._sanitize_output(bad_correction, original)

        assert result == original


# ============================================================================
# Disfluency repair mode (repair_disfluencies) -- both gate states
# ============================================================================

class TestDisfluencyRepairSystemPrompt:
    def test_disabled_by_default_prompt_unchanged(self):
        app = _make_app(language='en')
        assert sc._build_system_prompt(app) == sc.SYSTEM_PROMPT
        assert sc._DISFLUENCY_INSTRUCTIONS not in sc._build_system_prompt(app)

    def test_enabled_appends_disfluency_instructions(self):
        app = _make_app(smart_corrections={'repair_disfluencies': True}, language='en')
        prompt = sc._build_system_prompt(app)
        assert sc._DISFLUENCY_INSTRUCTIONS in prompt
        assert "filler words" in prompt
        assert "self-corrections" in prompt


class TestDisfluencyRepairPunctuationFloor:
    """The llama failure signature (TestPunctuationFloor) must still be
    rejected when the gate is off (default), and pass through when on."""

    def test_gate_off_still_rejects_llama_failure_signature(self):
        original = "OK, I think it's fine."
        bad_correction = "OK I think its fine."

        result = sc._sanitize_output(bad_correction, original, repair_disfluencies=False)

        assert result == original

    def test_gate_on_suspends_punctuation_floor(self):
        original = "OK, I think it's fine."
        bad_correction = "OK I think its fine."

        result = sc._sanitize_output(bad_correction, original, repair_disfluencies=True)

        assert result == bad_correction


class TestDisfluencyRepairWordCountGates:
    _ORIGINAL = "one two three four five six seven eight nine ten"  # 10 words

    def test_gate_off_shrink_over_15_percent_rejected(self):
        # 50% shrink (5/10) -- over the default symmetric 15% limit.
        new_text = "one two three four five"
        result = sc._sanitize_output(new_text, self._ORIGINAL, repair_disfluencies=False)
        assert result == self._ORIGINAL

    def test_gate_on_shrink_up_to_50_percent_accepted(self):
        # Same 50% shrink -- accepted once the gate widens the shrink
        # allowance (fillers/self-corrections/fragments legitimately drop
        # this many words).
        new_text = "one two three four five"
        result = sc._sanitize_output(new_text, self._ORIGINAL, repair_disfluencies=True)
        assert result == new_text

    def test_gate_on_shrink_beyond_50_percent_still_rejected(self):
        new_text = "one two three four"  # 60% shrink
        result = sc._sanitize_output(new_text, self._ORIGINAL, repair_disfluencies=True)
        assert result == self._ORIGINAL

    def test_growth_cap_unchanged_regardless_of_gate(self):
        # 50% growth (15/10) exceeds the 15% growth cap whether the gate is
        # on or off -- disfluency repair only ever widens the SHRINK side.
        new_text = (
            "one two three four five six seven eight nine ten "
            "eleven twelve thirteen fourteen fifteen"
        )
        assert sc._sanitize_output(new_text, self._ORIGINAL, repair_disfluencies=False) == self._ORIGINAL
        assert sc._sanitize_output(new_text, self._ORIGINAL, repair_disfluencies=True) == self._ORIGINAL


class TestDisfluencyRepairEndToEnd:
    def test_smart_correct_passes_gate_through_to_sanitizer(self, monkeypatch):
        """Functional check that smart_correct() reads repair_disfluencies
        from config and threads it through to _sanitize_output, using the
        same fake-backend pattern as TestThinkTagStripping."""
        app = _make_app(smart_corrections={
            'enabled': True, 'min_words': 1, 'backend': 'ollama',
            'repair_disfluencies': True,
        })
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s, model: ("OK I think its fine.", False),
        )

        result = sc.smart_correct("OK, I think it's fine.", app)

        # Would be reverted to the original under the default punctuation
        # floor -- confirms the gate reached the sanitizer.
        assert result == "OK I think its fine."


# ============================================================================
# System prompt -- few-shot examples + new permissions (Task 6)
# ============================================================================

class TestSystemPromptFewShot:
    def test_prompt_contains_homophone_misrecognition_example(self):
        assert "angziolotic" in sc.SYSTEM_PROMPT
        assert "anxiolytic" in sc.SYSTEM_PROMPT

    def test_prompt_contains_noop_example(self):
        assert "Send the draft to Sarah." in sc.SYSTEM_PROMPT

    def test_prompt_grants_quote_and_misrecognition_permission(self):
        assert "quotation marks" in sc.SYSTEM_PROMPT
        assert "clearly misrecognitions of the intended word" in sc.SYSTEM_PROMPT

    def test_prompt_still_forbids_paraphrasing(self):
        assert "Do not paraphrase" in sc.SYSTEM_PROMPT


# ============================================================================
# Language-aware system prompt (multilingual dictation Task 4)
# ============================================================================

class TestLanguageAwarePrompt:
    def test_english_uses_system_prompt_verbatim_including_fewshot(self):
        app = _make_app(language='en')
        assert sc._build_system_prompt(app) == sc.SYSTEM_PROMPT

    def test_unset_language_defaults_to_english_prompt(self):
        app = _make_app()  # no language key at all
        assert sc._build_system_prompt(app) == sc.SYSTEM_PROMPT

    def test_auto_drops_english_examples_and_forbids_translation(self):
        app = _make_app(language='auto')
        prompt = sc._build_system_prompt(app)
        assert "angziolotic" not in prompt
        assert "Send the draft to Sarah." not in prompt
        assert "Preserve the language of the input exactly; never translate." in prompt

    def test_specific_language_names_it_and_forbids_translation(self):
        app = _make_app(language='de')
        prompt = sc._build_system_prompt(app)
        assert "angziolotic" not in prompt
        assert "Deutsch" in prompt
        assert "Never translate." in prompt

    def test_specific_language_wording_is_soft_not_forced(self):
        """Tribunal Fix 5: the old wording ("The text is in {lang}. Correct
        it in that language.") FORCES the configured language even when the
        user actually spoke English -- softened to describe an expectation,
        not an assertion, and to explicitly defer to whatever language the
        text actually turns out to be."""
        app = _make_app(language='es')
        prompt = sc._build_system_prompt(app)
        assert "The text is expected to be in Español. Correct it in " \
               "whatever language it is actually in. Never translate." in prompt
        # The old forcing wording must be gone.
        assert "Correct it in that language." not in prompt

    def test_non_english_prompt_keeps_base_instructions(self):
        app = _make_app(language='ja')
        prompt = sc._build_system_prompt(app)
        assert "Do not paraphrase" in prompt
        assert "quotation marks" in prompt


# ============================================================================
# Translation guardrail -- script-ratio check (multilingual dictation Task 4)
# ============================================================================

class TestTranslationGuardrail:
    def test_cjk_original_translated_to_latin_output_is_rejected(self):
        original = "今日は会議に遅れます"
        translated = "I will be late for the meeting today"
        result = sc._sanitize_output(translated, original)
        assert result == original

    def test_latin_original_translated_to_cjk_output_is_rejected(self):
        original = "I will be late for the meeting today"
        translated = "今日は会議に遅れます"
        result = sc._sanitize_output(translated, original)
        assert result == original

    def test_same_script_correction_passes_through(self):
        original = "今日わ会議に遅れます"
        corrected = "今日は会議に遅れます"
        result = sc._sanitize_output(corrected, original)
        assert result == corrected

    def test_fake_backend_returning_translation_is_rejected_end_to_end(self, monkeypatch):
        app = _make_app(smart_corrections={
            'enabled': True, 'min_words': 1, 'backend': 'ollama',
        }, language='ja')
        original_text = "今日は会議に遅れます"

        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s, model: (
                "I will be late for the meeting today", False,
            ),
        )

        result = sc.smart_correct(original_text, app)

        assert result == original_text


# ============================================================================
# Same-script translation guard (tribunal Fix 4) -- script_class() alone
# only catches a script FLIP (e.g. CJK -> Latin, see TestTranslationGuardrail
# above); es/fr/de/pt/it/nl -> English is invisible to it since both sides
# are Latin script. looks_translated_to_english() closes that gap.
# ============================================================================

class TestSameScriptTranslationGuard:
    _CASES = {
        "es": ("el gato esta en la casa y no quiere salir",
               "El gato está en la casa y no quiere salir.",
               "The cat is in the house and does not want to leave."),
        "fr": ("le chat est dans la maison et il ne veut pas sortir",
               "Le chat est dans la maison et il ne veut pas sortir.",
               "The cat is in the house and it does not want to go out."),
        "de": ("die katze ist im haus und will nicht raus",
               "Die Katze ist im Haus und will nicht raus.",
               "The cat is in the house and does not want to go out."),
        "pt": ("o gato esta na casa e nao quer sair",
               "O gato está na casa e não quer sair.",
               "The cat is in the house and does not want to leave."),
        "it": ("il gatto e in casa e non vuole uscire",
               "Il gatto è in casa e non vuole uscire.",
               "The cat is in the house and does not want to go out."),
        "nl": ("de kat is in het huis en wil niet naar buiten",
               "De kat is in het huis en wil niet naar buiten.",
               "The cat is in the house and does not want to go outside."),
    }

    @pytest.mark.parametrize("lang", ["es", "fr", "de", "pt", "it", "nl"])
    def test_real_correction_in_configured_language_does_not_trip(self, lang):
        """A real, legitimate homophone/punctuation correction that STAYS
        in the configured language (e.g. real Spanish -> corrected
        Spanish) must never be flagged as translated."""
        original, correction, _translated = self._CASES[lang]
        result = sc._sanitize_output(correction, original, lang=lang)
        assert result == correction

    @pytest.mark.parametrize("lang", ["es", "fr", "de", "pt", "it", "nl"])
    def test_translation_to_english_is_rejected(self, lang):
        original, _correction, translated = self._CASES[lang]
        result = sc._sanitize_output(translated, original, lang=lang)
        assert result == original

    def test_default_lang_en_skips_the_guard_entirely(self):
        # Default _sanitize_output(raw, original) call (no lang= passed)
        # must not apply the guard -- confirms the "en" default is a
        # genuine skip, not an accidental block on real corrections.
        original, correction, _translated = self._CASES["es"]
        result = sc._sanitize_output(correction, original)
        assert result == correction

    # Word-count-matched pair (0% deviation, no punctuation change) so a
    # skip test isolates the language guard specifically -- not confounded
    # by the unrelated word-count-deviation gate also rejecting the same
    # translated text for its own reasons.
    _MATCHED_ORIGINAL = "el gato esta en la casa"
    _MATCHED_TRANSLATED = "The cat is in the house"

    def test_auto_skips_the_guard(self):
        # Even a full translation must pass through when lang="auto" --
        # the guard is bounded to specific configured Latin-script codes.
        result = sc._sanitize_output(self._MATCHED_TRANSLATED, self._MATCHED_ORIGINAL, lang="auto")
        assert result == self._MATCHED_TRANSLATED

    def test_non_latin_configured_language_skips_the_guard(self):
        # "ja" isn't in SAME_SCRIPT_FUNCTION_WORDS -- must skip cleanly
        # rather than raise, and the script-flip guard (TestTranslationGuardrail)
        # is what actually catches CJK -> English, not this one.
        result = sc._sanitize_output(self._MATCHED_TRANSLATED, self._MATCHED_ORIGINAL, lang="ja")
        assert result == self._MATCHED_TRANSLATED

    def test_smart_correct_threads_configured_language_through_end_to_end(self, monkeypatch):
        original, _correction, translated = self._CASES["de"]
        app = _make_app(
            smart_corrections={'enabled': True, 'min_words': 1, 'backend': 'ollama'},
            language='de',
        )
        monkeypatch.setattr(sc, '_resolve_backend_detailed', lambda app: ('ollama', False, None))
        monkeypatch.setattr(
            sc, '_call_ollama',
            lambda text, app, system_prompt, timeout_s, model: (translated, False),
        )

        result = sc.smart_correct(original, app)

        assert result == original


# ============================================================================
# Probe cache invalidation on call failure (tribunal Fix 6)
# ============================================================================

class TestProbeCacheInvalidation:
    @pytest.fixture(autouse=True)
    def _clean_caches(self):
        sc._probe_cache.clear()
        sc._last_known_reachable.clear()
        yield
        sc._probe_cache.clear()
        sc._last_known_reachable.clear()

    def test_timeout_invalidates_probe_cache_for_that_host(self, monkeypatch):
        app = _make_app()
        host = sc._ollama_host(app)
        sc._probe_cache[host] = (time.monotonic(), True)  # stale "up"

        def fake_post(url, json=None, timeout=None):
            raise sc.requests.exceptions.Timeout("simulated")
        monkeypatch.setattr(sc._session, 'post', fake_post)

        sc._call_ollama("text", app, "system prompt", 6.0, "qwen2.5:3b")

        assert host not in sc._probe_cache

    def test_generic_failure_invalidates_probe_cache_for_that_host(self, monkeypatch):
        app = _make_app()
        host = sc._ollama_host(app)
        sc._probe_cache[host] = (time.monotonic(), True)  # stale "up"

        def fake_post(url, json=None, timeout=None):
            raise ConnectionError("no server")
        monkeypatch.setattr(sc._session, 'post', fake_post)

        sc._call_ollama("text", app, "system prompt", 6.0, "qwen2.5:3b")

        assert host not in sc._probe_cache

    def test_successful_call_does_not_touch_probe_cache(self, monkeypatch):
        app = _make_app()
        host = sc._ollama_host(app)
        sc._probe_cache[host] = (time.monotonic(), True)

        monkeypatch.setattr(sc._session, 'post', lambda url, json=None, timeout=None: _FakeOllamaResponse())

        sc._call_ollama("text", app, "system prompt", 6.0, "qwen2.5:3b")

        assert host in sc._probe_cache  # untouched by success


# ============================================================================
# Per-outage privacy re-notify (tribunal Fix 7)
# ============================================================================

class _FakeProbeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class TestPerOutageNotice:
    @pytest.fixture(autouse=True)
    def _clean_state(self, monkeypatch):
        monkeypatch.setattr(sc, '_fallback_notice_shown', False)
        sc._probe_cache.clear()
        sc._last_known_reachable.clear()
        yield
        sc._probe_cache.clear()
        sc._last_known_reachable.clear()

    def test_recovery_probe_after_down_resets_notice_flag(self, monkeypatch):
        app = _make_app()

        monkeypatch.setattr(sc._session, 'get', lambda url, timeout=None: _FakeProbeResponse(500))
        assert sc._ollama_reachable(app) is False
        sc._fallback_notice_shown = True  # simulate the notice having fired during the outage

        sc._probe_cache.pop(sc._ollama_host(app), None)  # force a fresh probe, not a TTL cache hit
        monkeypatch.setattr(sc._session, 'get', lambda url, timeout=None: _FakeProbeResponse(200))
        assert sc._ollama_reachable(app) is True

        assert sc._fallback_notice_shown is False

    def test_first_ever_probe_being_up_does_not_touch_the_flag(self, monkeypatch):
        # No prior "down" observation for this host -- a first-ever "up"
        # probe is not a recovery, so it must not reset anything.
        app = _make_app()
        sc._fallback_notice_shown = True
        monkeypatch.setattr(sc._session, 'get', lambda url, timeout=None: _FakeProbeResponse(200))

        sc._ollama_reachable(app)

        assert sc._fallback_notice_shown is True

    def test_staying_down_across_probes_does_not_reset(self, monkeypatch):
        app = _make_app()
        monkeypatch.setattr(sc._session, 'get', lambda url, timeout=None: _FakeProbeResponse(500))
        assert sc._ollama_reachable(app) is False
        sc._fallback_notice_shown = True

        sc._probe_cache.pop(sc._ollama_host(app), None)  # force a second live probe
        assert sc._ollama_reachable(app) is False

        assert sc._fallback_notice_shown is True  # still down -- not a recovery

    def test_end_to_end_down_notice_up_down_second_notice(self, monkeypatch):
        """Full outage/recovery/outage cycle through smart_correct() itself
        -- exactly the tribunal's stated net behavior: each distinct
        local-AI outage produces exactly one notice."""
        notified = []
        nm = types.SimpleNamespace(
            show_notification=lambda title, msg, duration=5: notified.append(msg)
        )
        app = _make_app(smart_corrections={
            'enabled': True, 'min_words': 1, 'backend': 'auto', 'allow_cloud_fallback': False,
        })
        app.notification_manager = nm
        host = sc._ollama_host(app)

        # Phase 1: Ollama down -- smart_correct notifies once.
        monkeypatch.setattr(sc._session, 'get', lambda url, timeout=None: _FakeProbeResponse(500))
        sc.smart_correct("first outage call", app)
        assert len(notified) == 1

        # Phase 2: Ollama recovers (force a fresh probe past the TTL cache).
        sc._probe_cache.pop(host, None)
        monkeypatch.setattr(sc._session, 'get', lambda url, timeout=None: _FakeProbeResponse(200))
        assert sc._ollama_reachable(app) is True
        assert sc._fallback_notice_shown is False

        # Phase 3: Ollama goes down again -- must notify a SECOND time,
        # not stay silent because the flag already fired once before.
        sc._probe_cache.pop(host, None)
        monkeypatch.setattr(sc._session, 'get', lambda url, timeout=None: _FakeProbeResponse(500))
        sc.smart_correct("second outage call", app)

        assert len(notified) == 2
