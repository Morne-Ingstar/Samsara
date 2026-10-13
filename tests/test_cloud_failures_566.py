"""566: cloud failure metadata and readiness, entirely fake transports."""
import json
import logging
from types import SimpleNamespace

import pytest
import requests

from samsara import ai_preferences, ava_readiness, cloud_llm
from plugins.commands import ask_ollama
from tests.test_ava_one_answer_536 import harness  # isolated fake app fixture


def app_for(provider="openrouter"):
    config = {"ava": {"provider_policy": "cloud"}, "ava_command_session": {"enabled": True},
              "cloud_llm": {"enabled": True, "provider": provider, "api_key": "synthetic"}}
    identity = ai_preferences.provider_for(config, "cloud")
    config["ava"]["consent"] = ai_preferences.accept_consent(
        config, features=("ava",), provider=identity, cloud=True, accepted_at="2026-10-13T00:00:00Z")
    return SimpleNamespace(config=config)


def response(status, message="", code=None, retry=None):
    result = requests.Response()
    result.status_code = status
    result.headers = {"Retry-After": retry} if retry is not None else {}
    result._content = json.dumps({"error": {"code": status if code is None else code,
                                             "message": message}}).encode()
    return result


CASES = [(401, "Invalid credentials", "bad_key"),
         (402, "Insufficient credits", "out_of_credit"),
         (402, "Key limit exceeded", "out_of_credit"),
         (403, "Blocked by moderation", "blocked"),
         (403, "Budget exceeded", "blocked"),
         (403, "Invalid API key", "bad_key"),
         (404, "No endpoints found matching data policy", "model_unavailable"),
         (429, "Rate limit exceeded", "rate_limited"),
         (500, "Internal server error", "provider_down"),
         (502, "Provider failed", "provider_down"),
         (503, "No available provider meets routing requirements", "provider_down")]


@pytest.mark.parametrize("status,message,kind", CASES)
@pytest.mark.parametrize("method", ["send", "send_json"])
def test_real_transport_status_and_private_metadata(monkeypatch, caplog, status, message, kind, method):
    # Synthetic arbitrary content is used only in memory, never written to evidence.
    private = "private-" + chr(113) * 35
    resp = response(status, message + " " + private, retry="12")
    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", lambda *a, **kw: resp)
    with caplog.at_level(logging.WARNING):
        result = getattr(cloud_llm, method)(private, private, app_for())
    assert isinstance(result, str) and result.startswith("Error:")
    failure = result.failure
    assert (failure.kind, failure.provider, failure.http_status, failure.retry_after_seconds) == (
        kind, "openrouter", status, 12)
    assert failure.provider_error_code == status
    assert len(failure.provider_error_message) <= 100
    assert private not in str(result) + repr(failure) + caplog.text
    assert [r.getMessage() for r in caplog.records] == [f"[CLOUD-FAILURE] kind={kind} status={status}"]


@pytest.mark.parametrize("provider", list(cloud_llm.BUILTIN_PROVIDERS))
@pytest.mark.parametrize("status,kind", [(401, "bad_key"), (402, "out_of_credit"),
    (404, "model_unavailable"), (429, "rate_limited"), (503, "provider_down")])
def test_all_providers_and_probe_share_failure(monkeypatch, provider, status, kind):
    resp = response(status, retry="7")
    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", lambda *a, **kw: resp)
    app = app_for(provider)
    result = cloud_llm.send("Synthetic system", "Synthetic question", app)
    identity, failure = ava_readiness.probe_configured_provider(app, http_get=lambda *a, **kw: resp)
    assert identity == provider and failure == result.failure
    assert failure.kind == kind and failure.retry_after_seconds == 7


@pytest.mark.parametrize("exception,kind", [(requests.Timeout, "timeout"),
    (requests.ConnectionError, "unreachable"), (ValueError, "error")])
@pytest.mark.parametrize("method", ["send", "send_json"])
def test_exceptions_are_structured_and_never_echoed(monkeypatch, caplog, exception, kind, method):
    private = "private-" + chr(114) * 35
    def fail(*a, **kw):
        raise exception(private)
    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", fail)
    result = getattr(cloud_llm, method)("Synthetic system", "Synthetic question", app_for())
    assert result.failure.kind == kind
    assert result.failure.http_status is None
    assert private not in str(result) + repr(result.failure) + caplog.text


@pytest.mark.parametrize("retry,expected", [("3.5", 3.5), ("0", 0), ("-1", 0),
    ("nan", None), ("inf", None), ("invalid", None),
    ("Wed, 21 Oct 2015 07:28:00 GMT", 15)])
def test_retry_after_numeric_date_and_invalid(monkeypatch, retry, expected):
    monkeypatch.setattr(cloud_llm.time, "time", lambda: 1445412465)
    assert cloud_llm.failure_from_response("openrouter", response(429, retry=retry)).retry_after_seconds == expected


def test_unknown_body_codes_and_metadata_are_discarded():
    private = chr(115) * 35
    resp = response(403, private, code=private)
    failure = cloud_llm.failure_from_response("openrouter", resp)
    assert failure.kind == "blocked" and failure.provider_error_code is None
    assert private not in repr(failure)
    resp._content = b"not json"
    assert cloud_llm.failure_from_response("openrouter", resp).kind == "blocked"


def test_safe_body_codes_and_http_200_error_envelope():
    failure = cloud_llm.failure_from_response("openrouter", response(403, code="invalid_api_key"))
    assert failure.kind == "bad_key" and failure.provider_error_code == "invalid_api_key"
    failure = cloud_llm.failure_from_response("openrouter", response(200, code=402))
    assert failure.kind == "out_of_credit" and failure.http_status == 200


@pytest.mark.parametrize("status,message,kind", CASES)
def test_exact_readiness_states_and_successful_turn_recovery(status, message, kind):
    now = [100.0]
    tracker = ava_readiness.ReadinessTracker(clock=lambda: now[0])
    assert tracker.record_turn("openrouter", None).state == "ready"
    failure = cloud_llm.failure_from_response("openrouter", response(status, message, retry="12"))
    snap = tracker.record_turn("openrouter", failure)
    expected = ("offline" if kind in ("bad_key", "provider_down", "unreachable") else
                "waiting" if kind == "rate_limited" else "needs_attention")
    assert snap.state == expected and snap.failure_kind == kind and snap.failure == failure
    assert "offline" not in snap.sentence().lower() or expected == "offline"
    if expected != "offline":
        assert tracker.record_probe("openrouter", None).state == expected
    assert tracker.record_turn("openrouter", None).state == "ready"


def test_rate_limit_expires_and_rechecks_without_user_action(monkeypatch):
    now, wakes = [100.0], []
    tracker = ava_readiness.ReadinessTracker(clock=lambda: now[0])
    monkeypatch.setattr(ava_readiness, "request_recheck", lambda: wakes.append(True))
    failure = cloud_llm.failure_from_response("openrouter", response(429, retry="12"))
    transitions = []
    tracker.add_listener(lambda old, new: transitions.append(new.state))
    assert tracker.record_turn("openrouter", failure).state == "waiting"
    assert "12 seconds" in tracker.snapshot().sentence()
    now[0] += 11.9
    assert tracker.snapshot().state == "waiting"
    now[0] += 0.1
    assert tracker.snapshot().state == "unknown" and wakes == [True]
    assert tracker.record_probe("openrouter", None).state == "ready"
    assert transitions == ["waiting", "unknown", "ready"]


@pytest.mark.parametrize("kind", ["out_of_credit", "model_unavailable", "blocked", "timeout", "error"])
def test_attention_is_not_cleared_by_public_catalogue_probe(kind):
    tracker = ava_readiness.ReadinessTracker()
    tracker.record_turn("openrouter", cloud_llm.make_failure(kind, "openrouter"))
    monitor = ava_readiness.ReadinessMonitor(tracker, lambda: ("openrouter", None))
    assert monitor.run_once().state == "needs_attention"
    assert tracker.record_turn("openrouter", None).state == "ready"


@pytest.mark.parametrize("status,message,kind", CASES)
def test_actual_ava_turn_uses_kind_and_shows_correct_reason(monkeypatch, harness, status, message, kind):
    app = harness.app
    app.config = app_for().config
    monkeypatch.setattr(ava_readiness, "tracker", ava_readiness.ReadinessTracker())
    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", lambda *a, **kw: response(status, message, retry="12"))
    reply = ask_ollama.ask_model("Synthetic question", app, system="Synthetic system")
    assert reply.failure_kind == kind and reply.failure.http_status == status
    assert ava_readiness.tracker.snapshot().failure == reply.failure
    sentence = ask_ollama.unavailable_sentence(app)
    assert "OpenRouter" in sentence
    assert "12 seconds" in sentence if kind == "rate_limited" else True
    chip = ask_ollama.readiness_chip(app)
    assert ava_readiness.tracker.snapshot().badge_label() in chip[0]
    assert chip[1] == ("error" if kind in ("bad_key", "provider_down") else "warning")


def test_ava_and_warmup_do_not_parse_error_strings(monkeypatch, harness):
    app = harness.app
    app.config = app_for().config
    tracker = ava_readiness.ReadinessTracker()
    monkeypatch.setattr(ava_readiness, "tracker", tracker)
    failure = cloud_llm.make_failure("out_of_credit", "openrouter", 402)
    monkeypatch.setattr(cloud_llm, "send", lambda *a, **kw: cloud_llm.CloudError(failure, "401 timeout connection"))
    assert ask_ollama.ask_model("Synthetic question", app, system="Synthetic system").failure_kind == "out_of_credit"
    assert ava_readiness.warm_configured_provider(app).failure_kind == "out_of_credit"


def test_search_failure_retains_same_structured_metadata(monkeypatch):
    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", lambda *a, **kw: response(429, retry="9"))
    result = cloud_llm.send_web_search("Synthetic system", [], app_for("deepseek"))
    assert result.failure.kind == "rate_limited" and result.failure.retry_after_seconds == 9
    assert ask_ollama._search_failure_kind(result) == "rate_limited"


def test_successful_answer_with_error_prefix_is_not_a_failure(monkeypatch, harness):
    app = harness.app
    app.config = app_for().config
    monkeypatch.setattr(ava_readiness, "tracker", ava_readiness.ReadinessTracker())
    monkeypatch.setattr(cloud_llm, "send", lambda *a, **kw: "Error: is a word in this answer.")
    reply = ask_ollama.ask_model("Synthetic question", app, system="Synthetic system")
    assert reply.ok and ava_readiness.tracker.snapshot().state == "ready"
    assert ava_readiness.warm_configured_provider(app).state == "ready"
