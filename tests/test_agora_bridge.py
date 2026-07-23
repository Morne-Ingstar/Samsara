"""Tests for samsara/agora_bridge.py -- the Ava-to-Agora intake client.

Unit tests mock the HTTP layer (never require a live Agora). The
signature-interop test and the optional live round-trip test cross into
F:\\Projects F\\Agora via sys.path and skip cleanly (with a clear reason)
when that repo is absent or its pieces don't cooperate in this
environment -- see each test's own skip conditions.
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samsara.agora_bridge as ab

AGORA_REPO = Path(r"F:\Projects F\Agora")


@pytest.fixture(autouse=True)
def _fresh_token_cache():
    """Every test gets its own in-memory token cache -- otherwise one
    test's cached token/mtime could leak into the next."""
    ab._token_cache = ab._TokenCache()
    yield
    ab._token_cache = ab._TokenCache()


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "api_token.txt"
    path.write_text("test-token-abc123_XYZ-789", encoding="utf-8")
    monkeypatch.setenv(ab.TOKEN_FILE_ENV_VAR, str(path))
    return path


def _http_error(status: int, payload: dict) -> "urllib.error.HTTPError":
    import urllib.error
    import io
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        url=ab.DEFAULT_BASE_URL + ab.INTAKE_PATH, code=status, msg="err",
        hdrs=None, fp=io.BytesIO(body),
    )


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# =============================================================================
# 1. Signature interop -- the load-bearing test.
# =============================================================================

def _load_bridge_signing():
    if not AGORA_REPO.exists():
        pytest.skip(f"Agora repo not found at {AGORA_REPO} -- skipping interop check")
    sys.path.insert(0, str(AGORA_REPO))
    try:
        import bridge_signing  # noqa: PLC0415
    except Exception as exc:
        pytest.skip(f"Could not import Agora's bridge_signing.py: {exc}")
    return bridge_signing


class TestSignatureInterop:
    def test_sign_request_matches_agora_reference_implementation(self, token_file):
        bridge_signing = _load_bridge_signing()
        token = ab._token_cache.get()
        request_id = str(uuid.uuid4())
        body = json.dumps({"utterance": "hello -> world"}, ensure_ascii=False).encode("utf-8")
        timestamp = "2026-07-23T12:00:00Z"

        mine = ab._sign(
            token, method="POST", path=ab.INTAKE_PATH,
            event_id=ab._canonical_event_id(request_id), timestamp=timestamp, body=body,
        )
        theirs = bridge_signing.sign_request(
            token, direction="intake", method="POST", path=ab.INTAKE_PATH,
            event_id=request_id, timestamp=timestamp, body=body,
        )
        assert mine == theirs

    def test_agora_verify_request_accepts_a_signature_this_client_produced(self, token_file):
        bridge_signing = _load_bridge_signing()
        token = ab._token_cache.get()
        request_id = str(uuid.uuid4())
        body = json.dumps({"utterance": "Fix the boot cost. → Do it."}, ensure_ascii=False).encode("utf-8")
        timestamp = "2026-07-23T12:00:00Z"
        canonical_id = ab._canonical_event_id(request_id)

        signature = ab._sign(token, method="POST", path=ab.INTAKE_PATH, event_id=canonical_id, timestamp=timestamp, body=body)
        headers = {
            ab._EVENT_ID_HEADER: canonical_id,
            ab._TIMESTAMP_HEADER: timestamp,
            ab._SIGNATURE_HEADER: signature,
        }
        verification = bridge_signing.verify_request(
            token, direction="intake", method="POST", path=ab.INTAKE_PATH,
            headers=headers, body=body,
            now=datetime(2026, 7, 23, 12, 0, 5, tzinfo=timezone.utc),
        )
        assert verification.accepted is True
        assert verification.error is None
        assert verification.canonical_event_id == canonical_id

    def test_wrong_key_is_rejected_by_agora_verify_request(self, token_file):
        """Sanity check the interop test itself isn't vacuously true --
        signing with the CALLBACK direction key (this client only ever
        signs intake) must NOT verify as a valid intake signature."""
        bridge_signing = _load_bridge_signing()
        token = ab._token_cache.get()
        request_id = str(uuid.uuid4())
        body = b'{"utterance":"x"}'
        timestamp = "2026-07-23T12:00:00Z"
        canonical_id = ab._canonical_event_id(request_id)

        wrong_signature = bridge_signing.sign_request(
            token, direction="callback", method="POST", path=ab.INTAKE_PATH,
            event_id=request_id, timestamp=timestamp, body=body,
        )
        headers = {
            ab._EVENT_ID_HEADER: canonical_id,
            ab._TIMESTAMP_HEADER: timestamp,
            ab._SIGNATURE_HEADER: wrong_signature,
        }
        verification = bridge_signing.verify_request(
            token, direction="intake", method="POST", path=ab.INTAKE_PATH,
            headers=headers, body=body,
            now=datetime(2026, 7, 23, 12, 0, 5, tzinfo=timezone.utc),
        )
        assert verification.accepted is False
        assert verification.error == "invalid_signature"


# =============================================================================
# 2. Token re-read on 401 and on mtime change.
# =============================================================================

class TestTokenReread:
    def test_token_cached_across_calls(self, token_file):
        first = ab._token_cache.get()
        second = ab._token_cache.get()
        assert first == second == "test-token-abc123_XYZ-789"

    def test_token_rereads_on_mtime_change(self, token_file):
        original = ab._token_cache.get()
        assert original == "test-token-abc123_XYZ-789"

        import time
        time.sleep(0.01)
        token_file.write_text("rotated-token-999", encoding="utf-8")
        # Ensure a distinguishable mtime even on coarse filesystem clocks.
        new_time = token_file.stat().st_mtime + 1
        import os
        os.utime(token_file, (new_time, new_time))

        updated = ab._token_cache.get()
        assert updated == "rotated-token-999"

    def test_send_intent_rereads_token_once_on_401_then_succeeds(self, token_file):
        calls = []

        def fake_post(url, body, headers, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise _http_error(401, {"accepted": False, "error": "invalid_signature"})
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-1", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            result = ab.send_intent("hello", base_url="http://127.0.0.1:9", timeout=1.0)

        assert len(calls) == 2
        assert result.task_id == "AG-1"

    def test_send_intent_raises_auth_error_after_second_401(self, token_file):
        def fake_post(url, body, headers, timeout):
            raise _http_error(401, {"accepted": False, "error": "invalid_signature"})

        with patch.object(ab, "_post", side_effect=fake_post):
            with pytest.raises(ab.AgoraAuthError):
                ab.send_intent("hello", base_url="http://127.0.0.1:9", timeout=1.0)

    def test_missing_token_file_raises_typed_error(self, tmp_path, monkeypatch):
        missing = tmp_path / "does_not_exist.txt"
        monkeypatch.setenv(ab.TOKEN_FILE_ENV_VAR, str(missing))
        with pytest.raises(ab.AgoraTokenError):
            ab._token_cache.get()

    def test_empty_token_file_raises_typed_error(self, tmp_path, monkeypatch):
        path = tmp_path / "empty.txt"
        path.write_text("", encoding="utf-8")
        monkeypatch.setenv(ab.TOKEN_FILE_ENV_VAR, str(path))
        with pytest.raises(ab.AgoraTokenError):
            ab._token_cache.get()


# =============================================================================
# 3. Envelope shape + hash correctness (non-ASCII fixture).
# =============================================================================

NON_ASCII_UTTERANCE = "Have Code fix the boot cost → then notify José."


class TestEnvelopeShape:
    def test_utterance_sha256_matches_plain_utf8_sha256(self):
        import hashlib
        expected = hashlib.sha256(NON_ASCII_UTTERANCE.encode("utf-8")).hexdigest()
        assert ab._utterance_sha256(NON_ASCII_UTTERANCE) == expected

    def test_envelope_has_exactly_the_eight_contract_keys_no_utterance_sha256(self, token_file):
        captured = {}

        def fake_post(url, body, headers, timeout):
            captured["body"] = json.loads(body.decode("utf-8"))
            captured["headers"] = headers
            captured["url"] = url
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-2", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            ab.send_intent(NON_ASCII_UTTERANCE, base_url="http://127.0.0.1:9", timeout=1.0)

        body = captured["body"]
        assert set(body.keys()) == {
            "contract_version", "request_id", "utterance", "intent_context",
            "supersedes_request_id", "sent_at", "expires_at",
            "require_reconfirmation_if_delayed",
        }
        assert "utterance_sha256" not in body
        assert body["utterance"] == NON_ASCII_UTTERANCE
        assert body["contract_version"] == 1
        assert body["require_reconfirmation_if_delayed"] is True
        assert body["intent_context"] is None
        assert body["supersedes_request_id"] is None
        uuid.UUID(body["request_id"])  # does not raise

    def test_expires_at_is_30_seconds_after_sent_at(self, token_file):
        captured = {}

        def fake_post(url, body, headers, timeout):
            captured["body"] = json.loads(body.decode("utf-8"))
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-3", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            ab.send_intent("hi", base_url="http://127.0.0.1:9", timeout=1.0)

        sent_at = datetime.strptime(captured["body"]["sent_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        expires_at = datetime.strptime(captured["body"]["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert (expires_at - sent_at).total_seconds() == 30.0

    def test_header_timestamp_equals_body_sent_at(self, token_file):
        captured = {}

        def fake_post(url, body, headers, timeout):
            captured["body"] = json.loads(body.decode("utf-8"))
            captured["headers"] = headers
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-4", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            ab.send_intent("hi", base_url="http://127.0.0.1:9", timeout=1.0)

        assert captured["headers"][ab._TIMESTAMP_HEADER] == captured["body"]["sent_at"]

    def test_event_id_header_equals_body_request_id(self, token_file):
        captured = {}

        def fake_post(url, body, headers, timeout):
            captured["body"] = json.loads(body.decode("utf-8"))
            captured["headers"] = headers
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-5", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            ab.send_intent("hi", base_url="http://127.0.0.1:9", timeout=1.0)

        assert captured["headers"][ab._EVENT_ID_HEADER] == captured["body"]["request_id"]

    def test_oversized_utterance_rejected_locally_before_any_send(self, token_file):
        huge = "x" * (ab._MAX_UTTERANCE_BYTES + 1)
        with patch.object(ab, "_post") as fake_post:
            with pytest.raises(ab.AgoraValidationError):
                ab.send_intent(huge, base_url="http://127.0.0.1:9", timeout=1.0)
        fake_post.assert_not_called()

    def test_empty_utterance_rejected(self, token_file):
        with pytest.raises(ab.AgoraValidationError):
            ab.send_intent("   ", base_url="http://127.0.0.1:9", timeout=1.0)


# =============================================================================
# 4. Collision path + resend_with_fresh_uuid.
# =============================================================================

class TestCollision:
    def test_409_raises_typed_collision_error(self, token_file):
        def fake_post(url, body, headers, timeout):
            raise _http_error(409, {"accepted": False, "error": "request_id_collision"})

        with patch.object(ab, "_post", side_effect=fake_post):
            with pytest.raises(ab.AgoraCollisionError) as excinfo:
                ab.send_intent("distinct intent", base_url="http://127.0.0.1:9", timeout=1.0)

        assert excinfo.value.utterance == "distinct intent"
        assert excinfo.value.utterance_sha256 == ab._utterance_sha256("distinct intent")

    def test_resend_with_fresh_uuid_uses_a_new_uuid_same_utterance_and_hash(self, token_file):
        def fake_post(url, body, headers, timeout):
            raise _http_error(409, {"accepted": False, "error": "request_id_collision"})

        with patch.object(ab, "_post", side_effect=fake_post):
            with pytest.raises(ab.AgoraCollisionError) as excinfo:
                ab.send_intent("distinct intent", base_url="http://127.0.0.1:9", timeout=1.0)
        collision = excinfo.value

        captured = {}

        def fake_post_success(url, body, headers, timeout):
            captured["body"] = json.loads(body.decode("utf-8"))
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-6", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post_success):
            result = ab.resend_with_fresh_uuid(collision, base_url="http://127.0.0.1:9", timeout=1.0)

        assert result.request_id != collision.request_id
        assert captured["body"]["request_id"] != collision.request_id
        assert result.utterance == collision.utterance == "distinct intent"
        assert result.utterance_sha256 == collision.utterance_sha256


# =============================================================================
# 5. Retry buffer: unreachable -> bounded retry -> gives up cleanly.
# =============================================================================

class TestRetryBuffer:
    def test_retries_on_unreachable_with_same_request_id_and_fresh_timestamps(self, token_file):
        seen_request_ids = []
        seen_timestamps = []
        attempts = {"n": 0}

        def fake_post(url, body, headers, timeout):
            attempts["n"] += 1
            parsed = json.loads(body.decode("utf-8"))
            seen_request_ids.append(parsed["request_id"])
            seen_timestamps.append(parsed["sent_at"])
            if attempts["n"] < 3:
                import urllib.error
                raise urllib.error.URLError("connection refused")
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-7", "state": "draft"})

        fake_clock = {"t": 0.0}
        sleeps = []

        # _rfc3339_now() truncates to whole seconds -- a real mocked retry
        # loop runs in milliseconds, so without forcing distinct wall-clock
        # reads, all attempts would coincidentally land in the same
        # second. Advance a fake "wall clock" one second per attempt so
        # the "fresh timestamp every attempt" assertion is meaningful
        # rather than incidentally true/false based on execution speed.
        base = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)

        def fake_rfc3339_now():
            moment = base + timedelta(seconds=attempts["n"])
            return moment, ab._format_rfc3339(moment)

        with patch.object(ab, "_post", side_effect=fake_post), \
             patch.object(ab, "_rfc3339_now", side_effect=fake_rfc3339_now):
            result = ab._send_with_retry(
                "retry me", request_id=str(uuid.uuid4()), base_url="http://127.0.0.1:9", timeout=1.0,
                intent_context=None, supersedes_request_id=None, require_reconfirmation_if_delayed=True,
                _now_fn=lambda: fake_clock["t"], _sleep_fn=lambda s: (sleeps.append(s), fake_clock.__setitem__("t", fake_clock["t"] + s)),
            )

        assert result.task_id == "AG-7"
        assert attempts["n"] == 3
        assert len(set(seen_request_ids)) == 1  # same request_id every attempt
        assert len(set(seen_timestamps)) == 3   # fresh timestamp every attempt
        assert len(sleeps) == 2

    def test_gives_up_after_30_second_budget_with_typed_error(self, token_file):
        import urllib.error

        def fake_post(url, body, headers, timeout):
            raise urllib.error.URLError("connection refused")

        fake_clock = {"t": 0.0}

        def fake_sleep(seconds):
            fake_clock["t"] += seconds

        with patch.object(ab, "_post", side_effect=fake_post):
            with pytest.raises(ab.AgoraRetryExhaustedError):
                ab._send_with_retry(
                    "never lands", request_id=str(uuid.uuid4()), base_url="http://127.0.0.1:9", timeout=1.0,
                    intent_context=None, supersedes_request_id=None, require_reconfirmation_if_delayed=True,
                    _now_fn=lambda: fake_clock["t"], _sleep_fn=fake_sleep,
                )

        assert fake_clock["t"] >= ab.RETRY_TOTAL_BUDGET_SECONDS

    def test_retry_exhausted_is_also_an_unreachable_error(self, token_file):
        assert issubclass(ab.AgoraRetryExhaustedError, ab.AgoraUnreachableError)

    def test_send_intent_public_api_also_retries(self, token_file):
        """send_intent() itself (not just the internal _send_with_retry
        helper) must apply the retry buffer -- this is the caller-facing
        contract, not an implementation detail."""
        import urllib.error
        attempts = {"n": 0}

        def fake_post(url, body, headers, timeout):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise urllib.error.URLError("connection refused")
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-8", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post), \
             patch.object(ab.time, "sleep", return_value=None):
            result = ab.send_intent("hi", base_url="http://127.0.0.1:9", timeout=1.0)

        assert result.task_id == "AG-8"
        assert attempts["n"] == 2

    def test_single_connection_refused_without_retry_budget_raises_unreachable(self, token_file):
        """Lower-level _send_once (no retry loop) surfaces a plain
        AgoraUnreachableError, not the retry-exhausted subtype."""
        import urllib.error

        def fake_post(url, body, headers, timeout):
            raise urllib.error.URLError("connection refused")

        with patch.object(ab, "_post", side_effect=fake_post):
            with pytest.raises(ab.AgoraUnreachableError) as excinfo:
                ab._send_once(
                    "hi", request_id=str(uuid.uuid4()), base_url="http://127.0.0.1:9", timeout=1.0,
                    intent_context=None, supersedes_request_id=None, require_reconfirmation_if_delayed=True,
                )
        assert not isinstance(excinfo.value, ab.AgoraRetryExhaustedError)


# =============================================================================
# Response mapping sanity (accepted / duplicate) -- not explicitly a
# numbered requirement but directly named in the task's send_intent spec.
# =============================================================================

class TestResponseMapping:
    def test_accepted_maps_to_duplicate_false(self, token_file):
        def fake_post(url, body, headers, timeout):
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-9", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            result = ab.send_intent("hi", base_url="http://127.0.0.1:9", timeout=1.0)
        assert result.task_id == "AG-9"
        assert result.duplicate is False
        assert result.state == "draft"

    def test_duplicate_maps_to_duplicate_true(self, token_file):
        def fake_post(url, body, headers, timeout):
            return _FakeResponse({"accepted": True, "duplicate": True, "task_id": "AG-10", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            result = ab.send_intent("hi", base_url="http://127.0.0.1:9", timeout=1.0)
        assert result.task_id == "AG-10"
        assert result.duplicate is True

    def test_other_error_status_raises_generic_typed_error(self, token_file):
        def fake_post(url, body, headers, timeout):
            raise _http_error(410, {"accepted": False, "error": "external_intent_expired"})

        with patch.object(ab, "_post", side_effect=fake_post):
            with pytest.raises(ab.AgoraIntentError) as excinfo:
                ab.send_intent("hi", base_url="http://127.0.0.1:9", timeout=1.0)
        assert excinfo.value.status == 410
        assert excinfo.value.error_code == "external_intent_expired"


# =============================================================================
# CLI smoke tests
# =============================================================================

class TestCli:
    def test_cli_success_prints_result_and_exits_0(self, token_file, capsys):
        def fake_post(url, body, headers, timeout):
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-11", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            code = ab._cli(["hello there"])
        out = capsys.readouterr().out
        assert code == 0
        assert "AG-11" in out

    def test_cli_error_exits_1(self, token_file, capsys):
        def fake_post(url, body, headers, timeout):
            raise _http_error(409, {"accepted": False, "error": "request_id_collision"})

        with patch.object(ab, "_post", side_effect=fake_post):
            code = ab._cli(["hello there"])
        err = capsys.readouterr().err
        assert code == 1
        assert "request_id" in err

    def test_cli_resend_collision_flag_recovers(self, token_file, capsys):
        calls = {"n": 0}

        def fake_post(url, body, headers, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(409, {"accepted": False, "error": "request_id_collision"})
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-12", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            code = ab._cli(["hello there", "--resend-collision"])
        out = capsys.readouterr().out
        assert code == 0
        assert "AG-12" in out

    def test_cli_request_id_flag_reused_instead_of_minted(self, token_file, capsys):
        """B4: --request-id lets a caller (e.g. a retry script) supply its
        own UUID instead of send_intent() minting a fresh one each call --
        the request body's request_id must be exactly the one passed."""
        fixed_id = "11111111-1111-4111-8111-111111111111"
        seen = {}

        def fake_post(url, body, headers, timeout):
            seen["request_id"] = json.loads(body)["request_id"]
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-13", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            code = ab._cli(["hello there", "--request-id", fixed_id])
        out = capsys.readouterr().out
        assert code == 0
        assert seen["request_id"] == fixed_id
        assert f"request_id={fixed_id}" in out

    def test_cli_without_request_id_still_mints_a_fresh_uuid(self, token_file, capsys):
        """No regression: omitting --request-id must keep send_intent()'s
        existing mint-a-fresh-UUID-when-omitted behavior."""
        def fake_post(url, body, headers, timeout):
            return _FakeResponse({"accepted": True, "duplicate": False, "task_id": "AG-14", "state": "draft"})

        with patch.object(ab, "_post", side_effect=fake_post):
            code = ab._cli(["hello there"])
        out = capsys.readouterr().out
        assert code == 0
        assert "AG-14" in out


# =============================================================================
# Optional live check -- only if the Agora repo exists and cooperates.
# =============================================================================

@pytest.mark.slow
def test_live_round_trip_against_real_bridge_runtime(tmp_path, monkeypatch):
    if not AGORA_REPO.exists():
        pytest.skip(f"Agora repo not found at {AGORA_REPO}")
    sys.path.insert(0, str(AGORA_REPO))
    try:
        from bridge_runtime import BridgeRuntime
    except Exception as exc:
        pytest.skip(f"Could not import Agora's bridge_runtime.py: {exc}")

    token_path = tmp_path / "agora_live_token" / "api_token.txt"
    try:
        runtime = BridgeRuntime(port=0, token_path=token_path)
        warning = runtime.prepare()
        if warning:
            pytest.skip(f"Agora token provisioning did not succeed in this environment: {warning}")
        start_warning = runtime.start()
        if start_warning:
            pytest.skip(f"Agora listener did not start in this environment: {start_warning}")
    except Exception as exc:
        pytest.skip(f"Booting a real BridgeRuntime was not viable in this environment: {exc}")

    try:
        port = runtime.listener.port
        monkeypatch.setenv(ab.TOKEN_FILE_ENV_VAR, str(token_path))
        ab._token_cache = ab._TokenCache()

        result = ab.send_intent(
            "Have Samsara say hello to Agora.",
            base_url=f"http://127.0.0.1:{port}",
            timeout=5.0,
        )
        # The response IS the proof: Agora's listener only returns these
        # fields from create_external_intent_draft()'s own freshly
        # inserted tasks row (bridge_listener.do_POST), so state=="draft"
        # and a real task_id together constitute "a draft task row
        # appeared" without this test needing its own DB connection into
        # Agora's store.
        assert result.state == "draft"
        assert result.task_id
        assert result.duplicate is False

        # A same-UUID, same-utterance resend must report duplicate=True
        # against the same task_id -- proves the round trip is idempotent
        # end to end, not just "a 200 came back."
        again = ab.send_intent(
            "Have Samsara say hello to Agora.",
            base_url=f"http://127.0.0.1:{port}",
            timeout=5.0,
            request_id=result.request_id,
        )
        assert again.duplicate is True
        assert again.task_id == result.task_id
    finally:
        runtime.stop()
