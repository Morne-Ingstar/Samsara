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


# ============================================================================
# <think>...</think> stripping (Task 6 -- qwen3-family reasoning models)
# ============================================================================

class TestThinkTagStripping:
    def test_strips_closed_think_block(self):
        raw = "<think>reasoning about the correction here</think>Corrected sentence."
        result = sc._sanitize_output(raw, "corrected sentence")
        assert result == "Corrected sentence."
        assert "<think>" not in result

    def test_strips_unclosed_think_block_falls_back_to_original(self):
        raw = "<think>reasoning that got cut off mid-stream"
        result = sc._sanitize_output(raw, "original text here")
        assert result == "original text here"

    def test_fake_backend_returning_think_wrapped_output(self, monkeypatch):
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

        assert result == "Their going to the store."
        assert "<think>" not in result


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

    def test_gate_off_shrink_over_40_percent_rejected(self):
        # 50% shrink (5/10) -- over the default symmetric 40% limit.
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
        # 50% growth (15/10) exceeds the 40% growth cap whether the gate is
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
