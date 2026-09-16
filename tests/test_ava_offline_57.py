"""Queue 57 -- Ava answers were never spoken.

Covers:
  * command_mode.tts_char_limit scoped to command acknowledgements (Ava
    speech exempt; 0 = no limit);
  * a model that cannot answer: no success feedback, an explicit offline
    message naming the configured provider, an offline chip;
  * readiness ready -> offline -> ready without a restart;
  * AVA entry refused (with a spoken reason) while the provider is offline;
  * name capture only from explicit forms, reversible by voice;
  * generic alias fast paths no longer swallow ordinary questions.

Never imports dictation (Samsara may be running): the two DictationApp
methods exercised are compiled from source, as tests/test_execution_policy.py
does.
"""

import ast
import logging
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.commands import ask_ollama  # noqa: E402
from samsara import ava_profile, ava_readiness, cloud_llm  # noqa: E402
from samsara.tts.coordinator import (  # noqa: E402
    AudioCoordinator, TTS_CHAR_LIMIT_EXEMPT_CATEGORIES, command_mode_char_limit,
)
from samsara.tts.engine_base import SpeechHandle  # noqa: E402

CHECK = chr(0x2713)
LONG_ANSWER = ("Loneliness is hard, and it makes sense to feel it late at night. "
               "If you want, tell me what's on your mind, or we can talk about "
               "something else entirely for a while.")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture(autouse=True)
def _fresh_readiness(monkeypatch):
    ava_readiness.tracker.reset()
    with ava_readiness.tracker._lock:
        ava_readiness.tracker._listeners.clear()
    monkeypatch.setattr(ava_readiness, "_monitor", None)
    monkeypatch.setattr(ask_ollama, "_ollama_health_state", "down")
    yield
    ava_readiness.tracker.reset()


@pytest.fixture
def turn_log():
    handler = _ListHandler()
    ask_ollama.logger.addHandler(handler)
    old_level = ask_ollama.logger.level
    ask_ollama.logger.setLevel(logging.DEBUG)
    yield handler.records
    ask_ollama.logger.removeHandler(handler)
    ask_ollama.logger.setLevel(old_level)


def _engine():
    engine = MagicMock()
    engine.speak.return_value = SpeechHandle(utterance_id="spoken-1")
    engine.get_engine_state.return_value = "idle"
    return engine


def _app(*, cloud=True, command_mode_active=True, tts_char_limit=50):
    app = MagicMock()
    app.config = {
        "ollama": {"enabled": True, "host": "http://localhost:11434"},
        "cloud_llm": {"enabled": cloud, "provider": "deepseek", "api_key": "test-not-a-key" if cloud else ""},
        "command_mode": {"tts_char_limit": tts_char_limit},
        "tts": {"speed": 1.0, "volume": 0.8, "voice_id": None},
        "language": "en",
    }
    app.command_mode_active = command_mode_active
    app.engine = _engine()
    app.audio_coordinator = AudioCoordinator(app, app.engine)
    app.play_sound = Mock()
    app._ava_memory = MagicMock()
    app._ava_memory.get_messages.return_value = [{"role": "user", "content": "x"}]
    app._ava_turn_outcome = None
    return app


def _spoken(app):
    return [c.args[0] for c in app.engine.speak.call_args_list]


@pytest.fixture
def inline_worker(monkeypatch):
    # thread_registry is one shared module: run only the Ava worker inline;
    # the coordinator's interrupt-poll thread must not run (it loops while
    # SPEAKING, and the mock engine never finishes speaking).
    def _spawn(name, fn, *a, **k):
        if name == "ask_ollama._worker":
            return fn()
        return None

    monkeypatch.setattr(ask_ollama.thread_registry, "spawn", _spawn)
    monkeypatch.setattr(ask_ollama, "_check_teaching_intent", lambda app, text: False)


# ---------------------------------------------------------------------------
# A. the limit is scoped to command acknowledgements
# ---------------------------------------------------------------------------

class TestCharLimitScope:
    def test_ava_answer_is_spoken_in_command_mode(self):
        app = _app()
        handle = app.audio_coordinator.speak(LONG_ANSWER, category="ava_response")
        assert handle.utterance_id != "noop-cmd-mode"
        assert _spoken(app) == [LONG_ANSWER]

    @pytest.mark.parametrize("category", ["ava_response", "ava_status", "ava_command_session"])
    def test_ava_categories_are_exempt(self, category):
        assert category in TTS_CHAR_LIMIT_EXEMPT_CATEGORIES

    def test_command_acknowledgement_is_still_limited(self):
        app = _app()
        handle = app.audio_coordinator.speak("A" * 60, category="agent_response")
        assert handle.utterance_id == "noop-cmd-mode"
        assert _spoken(app) == []

    def test_zero_means_no_limit(self):
        app = _app(tts_char_limit=0)
        app.audio_coordinator.speak("A" * 600, category="agent_response")
        assert _spoken(app) == ["A" * 600]

    @pytest.mark.parametrize("raw, effective", [
        (50, 50), (10, 10), (0, None), (-3, 50), ("junk", 50), (None, 50),
    ])
    def test_effective_limit(self, raw, effective):
        assert command_mode_char_limit({"command_mode": {"tts_char_limit": raw}}) == effective

    def test_missing_config_uses_default(self):
        assert command_mode_char_limit({}) == 50

    def test_ask_ollama_speak_uses_the_exempt_category(self):
        app = _app()
        assert ask_ollama.speak(app, LONG_ANSWER) is True
        assert app.engine.speak.call_args.kwargs["category"] == "ava_response"
        assert _spoken(app) == [LONG_ANSWER]


# ---------------------------------------------------------------------------
# B. honest failure path
# ---------------------------------------------------------------------------

def _compile_dictation_methods(*names):
    source = ROOT / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {m.name for m in methods} == set(names)
    namespace = {"logger": logging.getLogger("ava57_test")}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), "exec"), namespace)
    return {name: namespace[name] for name in names}


def _session_done_harness(app):
    """Bind dictation's real _on_ava_session_request_done onto the stub."""
    import collections
    fns = _compile_dictation_methods("_on_ava_session_request_done")
    app._on_ava_session_request_done = fns["_on_ava_session_request_done"].__get__(app)
    app._show_outcome_chip = Mock()
    app._touch_session_activity = Mock()
    app._ava_session_dispatch_lock = threading.Lock()
    app._ava_session_dispatch_queue = collections.deque(maxlen=3)
    app._ava_session_request_in_flight = True
    return app


@pytest.mark.parametrize("error_text, expected_sentence, expected_short", [
    ("Error: Could not connect to the cloud LLM provider.",
     "Ava is offline. I can't reach DeepSeek.", "unreachable"),
    ("Error: Cloud LLM request failed: 401 Client Error: Unauthorized for url: "
     "https://api.deepseek.com/v1/chat/completions",
     "Ava is offline. DeepSeek rejected the API key. Check it in Settings.", "bad API key"),
    ("Error: Cloud LLM request timed out after 30s.",
     "Ava is offline. DeepSeek did not answer in time.", "timed out"),
    ("Error: Cloud LLM request failed: 429 Client Error: Too Many Requests for url: "
     "https://api.deepseek.com/v1/chat/completions",
     "Ava is offline. DeepSeek is rate limiting requests. Try again in a minute.", "rate limited"),
])
def test_model_unreachable_gives_offline_message_and_chip_never_success(
        monkeypatch, inline_worker, turn_log, error_text, expected_sentence, expected_short):
    app = _session_done_harness(_app())
    monkeypatch.setattr(cloud_llm, "send", lambda *a, **k: error_text)
    ollama_probe = Mock(return_value=False)
    monkeypatch.setattr(ask_ollama, "_check_ollama_available", ollama_probe)

    ask_ollama.handle_ask_ava(app, "I'm lonely.", on_done=app._on_ava_session_request_done,
                              generation=None)

    spoken = _spoken(app)
    assert spoken == [expected_sentence], spoken
    assert not any("Ollama" in s for s in spoken), "cloud is configured: never blame Ollama"
    ollama_probe.assert_not_called()           # no blocking fallback probe when Ollama is not known up
    sounds = [c.args[0] for c in app.play_sound.call_args_list]
    assert "success" not in sounds

    # the chip the session actually shows
    app._show_outcome_chip.assert_called_once()
    label, kind = app._show_outcome_chip.call_args.args
    assert kind == "error"
    assert label.endswith(f"Ava offline: {expected_short}")
    assert CHECK not in label

    snap = ava_readiness.readiness_for(app)
    assert snap.offline and snap.provider == "deepseek"

    turn = [r for r in turn_log if r.getMessage().startswith("[AVA-TURN]")]
    assert len(turn) == 1
    msg = turn[0].getMessage()
    assert "outcome=failed" in msg and "provider=deepseek" in msg and "latency_ms=" in msg
    assert "failure=" in msg and "spoken" in msg
    assert turn[0].levelno == logging.WARNING


def test_answer_is_spoken_in_command_mode_and_chip_is_success(monkeypatch, inline_worker, turn_log):
    app = _session_done_harness(_app(command_mode_active=True))
    monkeypatch.setattr(cloud_llm, "send", lambda *a, **k: LONG_ANSWER)

    ask_ollama.handle_ask_ava(app, "I'm lonely.", on_done=app._on_ava_session_request_done)

    assert _spoken(app) == [LONG_ANSWER]
    label, kind = app._show_outcome_chip.call_args.args
    assert (label, kind) == (f"Ava {CHECK}", "success")
    assert ava_readiness.readiness_for(app).ready
    msg = next(r.getMessage() for r in turn_log if r.getMessage().startswith("[AVA-TURN]"))
    assert "outcome=answered" in msg and "provider=deepseek" in msg
    assert f"response_chars={len(LONG_ANSWER)}" in msg
    assert "spoken" in msg and "NOT spoken" not in msg


def test_answer_that_could_not_be_spoken_is_not_a_success(monkeypatch, inline_worker, turn_log):
    app = _session_done_harness(_app())
    app.audio_coordinator = Mock()
    app.audio_coordinator.speak.return_value = SpeechHandle(utterance_id="noop-cmd-mode")
    monkeypatch.setattr(cloud_llm, "send", lambda *a, **k: LONG_ANSWER)

    ask_ollama.handle_ask_ava(app, "hello there", on_done=app._on_ava_session_request_done)

    label, kind = app._show_outcome_chip.call_args.args
    assert kind == "warning" and "not spoken" in label
    msg = next(r.getMessage() for r in turn_log if r.getMessage().startswith("[AVA-TURN]"))
    assert "NOT spoken" in msg and "tts_char_limit" in msg


def test_stale_request_resolves_pending_chip_without_success(monkeypatch, inline_worker):
    app = _session_done_harness(_app())
    monkeypatch.setattr(cloud_llm, "send", lambda *a, **k: LONG_ANSWER)
    monkeypatch.setattr(ask_ollama.execution_policy, "is_current", lambda app_, gen: False)

    ask_ollama.handle_ask_ava(app, "hello", on_done=app._on_ava_session_request_done, generation=1)

    label, kind = app._show_outcome_chip.call_args.args
    assert kind != "success" and CHECK not in label


def test_local_ollama_down_names_ollama(monkeypatch, inline_worker):
    app = _session_done_harness(_app(cloud=False))
    monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda host, timeout=3: False)

    ask_ollama.handle_ask_ava(app, "hello", on_done=app._on_ava_session_request_done)

    assert _spoken(app) == ["Ava is offline. Ollama is not running on this computer."]
    label, kind = app._show_outcome_chip.call_args.args
    assert kind == "error"


def test_disabled_plugin_says_so(inline_worker):
    app = _session_done_harness(_app())
    app.config["ollama"]["enabled"] = False
    ask_ollama.handle_ask_ava(app, "hello", on_done=app._on_ava_session_request_done)
    assert _spoken(app) == ["Ava is turned off in Settings."]
    assert app._show_outcome_chip.call_args.args[1] == "error"


def test_handle_response_sentinel_speaks_the_real_reason():
    app = _app()
    ava_readiness.tracker.record_turn("deepseek", ava_readiness.BAD_KEY)
    ask_ollama.handle_response(app, ask_ollama.MODEL_UNAVAILABLE)
    assert _spoken(app) == ["Ava is offline. DeepSeek rejected the API key. Check it in Settings."]


# ---------------------------------------------------------------------------
# readiness: probe classification, ready -> offline -> ready, entry refusal
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status):
        self.status_code = status


class TestProbe:
    @pytest.mark.parametrize("status, kind", [
        (200, None), (401, ava_readiness.BAD_KEY), (403, ava_readiness.BAD_KEY),
        (429, ava_readiness.RATE_LIMITED), (500, ava_readiness.PROVIDER_ERROR),
    ])
    def test_cloud_status(self, status, kind):
        seen = {}

        def get(url, timeout, headers=None):
            seen.update(url=url, timeout=timeout, auth=bool(headers))
            return _Resp(status)

        assert ava_readiness.probe_configured_provider(_app(), http_get=get) == ("deepseek", kind)
        assert seen["url"] == "https://api.deepseek.com/v1/models" and seen["auth"]
        assert seen["timeout"] <= 3

    @pytest.mark.parametrize("exc, kind", [
        (requests.exceptions.ConnectionError(), ava_readiness.UNREACHABLE),
        (requests.exceptions.Timeout(), ava_readiness.TIMEOUT),
    ])
    def test_cloud_network_failures(self, exc, kind):
        def get(*a, **k):
            raise exc
        assert ava_readiness.probe_configured_provider(_app(), http_get=get) == ("deepseek", kind)

    def test_ollama_when_cloud_not_configured(self):
        urls = []
        get = lambda url, timeout, **k: urls.append(url) or _Resp(200)  # noqa: E731
        assert ava_readiness.probe_configured_provider(_app(cloud=False), http_get=get) == ("ollama", None)
        assert urls == ["http://localhost:11434/api/tags"]


def test_readiness_ready_offline_ready_without_restart():
    app = _app()
    script = iter([200, "down", 200])

    def get(url, timeout, headers=None):
        step = next(script)
        if step == "down":
            raise requests.exceptions.ConnectionError()
        return _Resp(step)

    transitions = []
    ava_readiness.tracker.add_listener(lambda old, new: transitions.append((old.state, new.state)))
    monitor = ava_readiness.ReadinessMonitor(
        ava_readiness.tracker, lambda: ava_readiness.probe_configured_provider(app, http_get=get),
        ready_poll_s=0.01, offline_poll_s=0.01)

    states = []
    thread = threading.Thread(target=monitor.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while len(transitions) < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    monitor.stop()
    thread.join(2)
    states = [t[1] for t in transitions[:3]]
    assert states == ["ready", "offline", "ready"], transitions
    # entry is refused only while offline -- checked against the same tracker
    assert ask_ollama.ava_entry_block_reason(app) is None


def test_entry_refused_with_spoken_reason_while_offline_and_allowed_after_recovery():
    app = _app()
    ava_readiness.tracker.record_probe("deepseek", ava_readiness.UNREACHABLE)
    reason = ask_ollama.ava_entry_block_reason(app)
    assert reason == "Ava is offline. I can't reach DeepSeek."
    assert ask_ollama.readiness_chip(app) == ("Ava: offline: unreachable", "error")

    ava_readiness.tracker.record_probe("deepseek", None)
    assert ask_ollama.ava_entry_block_reason(app) is None
    assert ask_ollama.readiness_chip(app) == ("Ava: ready (DeepSeek)", "success")


def test_entry_allowed_before_first_probe_and_after_provider_change():
    app = _app()
    assert ask_ollama.ava_entry_block_reason(app) is None          # unknown: first turn is the check
    assert ask_ollama.readiness_chip(app)[0] == "Ava: checking (DeepSeek)"
    ava_readiness.tracker.record_probe("ollama", ava_readiness.UNREACHABLE)
    # evidence about Ollama says nothing about DeepSeek
    assert ask_ollama.ava_entry_block_reason(app) is None


def test_entry_allowed_when_cloud_offline_but_local_fallback_known_up(monkeypatch):
    app = _app()
    ava_readiness.tracker.record_probe("deepseek", ava_readiness.UNREACHABLE)
    monkeypatch.setattr(ask_ollama, "_ollama_health_state", "up")
    assert ask_ollama.ava_entry_block_reason(app) is None


def test_entry_refusal_is_spoken_past_the_char_limit():
    fns = _compile_dictation_methods("_speak_ava_entry_refusal")
    app = _app(command_mode_active=True)
    fns["_speak_ava_entry_refusal"](app, "Ava is offline. DeepSeek rejected the API key. Check it in Settings.")
    assert _spoken(app) == ["Ava is offline. DeepSeek rejected the API key. Check it in Settings."]
    fns["_speak_ava_entry_refusal"](app, "no agent dispatch function is wired")
    assert _spoken(app)[-1] == "Ava is not available: no agent dispatch function is wired."


# ---------------------------------------------------------------------------
# B. name capture
# ---------------------------------------------------------------------------

@pytest.fixture
def profile_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ava_profile, "_PROFILE_PATH", str(tmp_path / "ava_profile.json"))
    monkeypatch.setattr(ava_profile, "_profile", {})
    return tmp_path / "ava_profile.json"


@pytest.mark.parametrize("utterance", [
    "I'm lonely.", "I'm tired", "I'm not sure", "I am lonely", "Im tired.",
    "Hey Ava, I'm lonely.", "I'm in pain", "I'm a bit tired", "I am a mess",
    "I'm an idiot", "I'm going to bed",
])
def test_ordinary_statements_never_set_a_profile_field(utterance):
    assert ava_profile.parse_teaching(utterance) is None


@pytest.mark.parametrize("utterance", [
    "my name is Morne", "My name is Morne.", "call me Morne", "Hey Ava, call me Morne.",
])
def test_explicit_name_forms_set_the_name(utterance):
    assert ava_profile.parse_teaching(utterance) == ("name", "Morne")


@pytest.mark.parametrize("utterance, field", [
    ("I live in Cape Town", "location"), ("I work as a designer", "occupation"),
    ("my pronouns are he/him", "pronouns"),
])
def test_other_explicit_forms_still_work(utterance, field):
    assert ava_profile.parse_teaching(utterance)[0] == field


def _teach(app, text):
    return ask_ollama._check_teaching_intent(app, text)


@pytest.mark.parametrize("set_phrase", ["my name is Morne", "call me Morne"])
@pytest.mark.parametrize("undo_phrase", ["forget my name", "that's not my name", "don't call me that"])
def test_name_set_by_voice_and_reversed_by_voice(profile_store, set_phrase, undo_phrase):
    app = _app()
    app.voice_training_window = None
    assert _teach(app, set_phrase) is True
    assert ava_profile.get("name") == "Morne"
    assert '"Morne"' in profile_store.read_text(encoding="utf-8")
    assert _teach(app, undo_phrase) is True
    assert ava_profile.get("name") is None
    assert "Morne" not in profile_store.read_text(encoding="utf-8")
    assert _spoken(app)[-1] == "Forgotten your name."


@pytest.mark.parametrize("utterance", ["I'm lonely.", "I'm tired", "I'm not sure"])
def test_ordinary_statements_reach_the_model(profile_store, utterance):
    app = _app()
    app.voice_training_window = None
    assert _teach(app, utterance) is False
    assert ava_profile.get_all() == {}
    assert _spoken(app) == []


# ---------------------------------------------------------------------------
# other fast paths: generic alias patterns fall through to the model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("utterance", [
    "What is the capital of France?", "what is love", "forget it",
])
def test_generic_alias_query_and_forget_fall_through_when_nothing_saved(profile_store, monkeypatch, utterance):
    from samsara import ava_corrections
    monkeypatch.setattr(ava_corrections, "get", lambda phrase: None)
    app = _app()
    app.voice_training_window = None
    assert _teach(app, utterance) is False
    assert _spoken(app) == []


def test_saved_alias_query_still_answers_locally(profile_store, monkeypatch):
    from samsara import ava_corrections
    monkeypatch.setattr(ava_corrections, "get",
                        lambda phrase: {"expansion": "the dev checkout"} if phrase == "dev" else None)
    app = _app()
    app.voice_training_window = None
    assert _teach(app, "what is dev") is True
    assert _spoken(app) == ["dev means the dev checkout."]
