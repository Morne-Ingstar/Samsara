"""Tests for samsara/browser_bridge.py -- the local loopback WebSocket
bridge to the Show Numbers DOM browser extension.

Uses real websockets.sync.client connections against a real BrowserBridge
instance bound to a throwaway test port/origin (never the real PORT/
EXPECTED_ORIGIN constants -- see BrowserBridge.__init__ and
_write_pairing_file's guard), so these tests never touch a real running
Samsara instance's port or SAMSARA_HOME_DIR pairing state.
"""
import json
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from samsara import browser_bridge as bb
from samsara.runtime import thread_registry
from websockets.sync.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, ConnectionClosed

TEST_PORT = 47899
TEST_ORIGIN = "chrome-extension://testtesttesttesttesttesttesttest"


@pytest.fixture
def bridge():
    b = bb.BrowserBridge(port=TEST_PORT, origin=TEST_ORIGIN)
    started = b.start()
    assert started, "test bridge failed to bind -- port may be in use"
    yield b
    b.stop(timeout=2.0)


def _client(origin=TEST_ORIGIN):
    return ws_connect(f"ws://127.0.0.1:{TEST_PORT}", additional_headers={"Origin": origin})


def _handshake(ws):
    ws.send(json.dumps({"type": "hello"}))
    ack = json.loads(ws.recv(timeout=3))
    assert ack["type"] == "hello_ack"
    return ack["token"]


# ---------------------------------------------------------------------------
# Origin allowlist
# ---------------------------------------------------------------------------

def test_mismatched_origin_rejected_during_handshake(bridge):
    with pytest.raises(InvalidStatus):
        with _client(origin="https://evil.example.com"):
            pass


def test_matching_origin_accepted(bridge):
    with _client() as ws:
        token = _handshake(ws)
        assert token
    # The server observes the close on its own recv-events + handler
    # threads, a beat after the client's own close() returns -- poll
    # instead of asserting instantly to avoid a scheduling race.
    assert bridge.wait_until_disconnected(timeout=2.0), (
        "bridge did not observe the client disconnect within timeout"
    )


# ---------------------------------------------------------------------------
# Token handshake
# ---------------------------------------------------------------------------

def test_first_message_other_than_hello_is_fatal(bridge):
    with _client() as ws:
        ws.send(json.dumps({"type": "dismissed"}))  # valid shape, wrong-for-first
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)


def test_message_before_hello_never_marks_connected(bridge):
    with _client() as ws:
        ws.send(json.dumps({"type": "hints", "requestId": 1, "hints": []}))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)
    assert not bridge.is_connected()


def test_wrong_token_closes_connection(bridge):
    with _client() as ws:
        _handshake(ws)
        ws.send(json.dumps({"type": "dismissed", "token": "not-the-real-token"}))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)


def test_correct_token_accepted(bridge):
    with _client() as ws:
        token = _handshake(ws)
        ws.send(json.dumps({"type": "dismissed", "token": token}))
        time.sleep(0.2)  # give the handler thread a moment to process
        # No exception/close means the message was accepted.
        assert bridge.is_connected()


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_malformed_json_rejected(bridge):
    with _client() as ws:
        token = _handshake(ws)
        ws.send("not valid json{{{")
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)


def test_unknown_message_type_rejected(bridge):
    with _client() as ws:
        token = _handshake(ws)
        ws.send(json.dumps({"type": "not_a_real_type", "token": token}))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)


def test_missing_required_field_rejected(bridge):
    with _client() as ws:
        token = _handshake(ws)
        # "hints" type requires requestId + hints; omit hints.
        ws.send(json.dumps({"type": "hints", "requestId": 1, "token": token}))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)


# ---------------------------------------------------------------------------
# Connection replacement (extension reload / browser restart)
# ---------------------------------------------------------------------------

def test_new_connection_replaces_previous(bridge):
    with _client() as ws1:
        _handshake(ws1)
        assert bridge.is_connected()
        with _client() as ws2:
            _handshake(ws2)
            time.sleep(0.2)
            assert bridge.is_connected()
            # ws1 should now be closed by the server.
            with pytest.raises(ConnectionClosed):
                ws1.recv(timeout=2)


# ---------------------------------------------------------------------------
# request_hints / send_selection facade, request-id staleness
# ---------------------------------------------------------------------------

def test_request_hints_returns_none_when_not_connected(bridge):
    assert bridge.request_hints(timeout=0.2) is None
    assert bridge.last_hints_unavailable_reason == "timeout_or_disconnected"


def test_request_hints_round_trip(bridge):
    with _client() as ws:
        token = _handshake(ws)

        def respond_once():
            msg = json.loads(ws.recv(timeout=3))
            assert msg["type"] == "show_hints"
            ws.send(json.dumps({
                "type": "hints",
                "requestId": msg["requestId"],
                "hints": [{"index": 1, "kind": "button", "rect": {}}],
                "token": token,
            }))

        import threading
        t = threading.Thread(target=respond_once)
        t.start()
        hints = bridge.request_hints(timeout=2.0)
        t.join()
        assert hints == [{"index": 1, "kind": "button", "rect": {}}]


def test_request_hints_no_candidates_is_not_unavailable_timeout(bridge):
    with _client() as ws:
        token = _handshake(ws)

        def respond_once():
            msg = json.loads(ws.recv(timeout=3))
            ws.send(json.dumps({
                "type": "hints_unavailable",
                "requestId": msg["requestId"],
                "reason": "no_candidates",
                "token": token,
            }))

        import threading
        t = threading.Thread(target=respond_once)
        t.start()
        hints = bridge.request_hints(timeout=2.0)
        t.join()
        assert hints is None
        assert bridge.last_hints_unavailable_reason == "no_candidates"


def test_stale_late_response_is_ignored(bridge):
    """A response whose requestId no longer has a waiter (the caller already
    timed out) must not resurrect/affect a later request."""
    with _client() as ws:
        token = _handshake(ws)

        # First request times out client-side (server never replies).
        first = bridge.request_hints(timeout=0.3)
        assert first is None

        # Drain the first show_hints the server sent, then reply late with
        # its (now-stale) requestId.
        stale_req = json.loads(ws.recv(timeout=2))
        ws.send(json.dumps({
            "type": "hints",
            "requestId": stale_req["requestId"],
            "hints": [{"index": 99, "kind": "aria", "rect": {}}],
            "token": token,
        }))
        time.sleep(0.2)

        # A fresh request must get its own fresh response, not the stale one.
        def respond_fresh():
            msg = json.loads(ws.recv(timeout=3))
            ws.send(json.dumps({
                "type": "hints",
                "requestId": msg["requestId"],
                "hints": [{"index": 1, "kind": "button", "rect": {}}],
                "token": token,
            }))

        import threading
        t = threading.Thread(target=respond_fresh)
        t.start()
        second = bridge.request_hints(timeout=2.0)
        t.join()
        assert second == [{"index": 1, "kind": "button", "rect": {}}]


def test_send_selection_ok_true(bridge):
    with _client() as ws:
        token = _handshake(ws)

        def respond_once():
            msg = json.loads(ws.recv(timeout=3))
            assert msg["type"] == "select"
            assert msg["number"] == 3
            assert msg["action"] == "click"
            ws.send(json.dumps({
                "type": "selection_result",
                "requestId": msg["requestId"],
                "ok": True,
                "token": token,
            }))

        import threading
        t = threading.Thread(target=respond_once)
        t.start()
        ok = bridge.send_selection(3, "click", timeout=2.0)
        t.join()
        assert ok is True


def test_send_selection_false_when_not_connected(bridge):
    assert bridge.send_selection(1, "click", timeout=0.2) is False


# ---------------------------------------------------------------------------
# Unsolicited dismissed -> on_dismissed callback
# ---------------------------------------------------------------------------

def test_on_dismissed_callback_fires(bridge):
    fired = []
    bridge.set_on_dismissed(lambda: fired.append(True))
    with _client() as ws:
        token = _handshake(ws)
        ws.send(json.dumps({"type": "dismissed", "token": token}))
        time.sleep(0.3)
    assert fired == [True]


# ---------------------------------------------------------------------------
# Shutdown leaves no thread/port behind
# ---------------------------------------------------------------------------

def test_stop_joins_server_thread_and_frees_port():
    b = bb.BrowserBridge(port=TEST_PORT + 1, origin=TEST_ORIGIN)
    assert b.start()
    thread_name = b._thread.name
    b.stop(timeout=2.0)

    assert not b._thread.is_alive()
    snapshot = thread_registry.snapshot()
    matching = [e for e in snapshot if e["name"] == thread_name]
    assert matching and not matching[0]["alive"], (
        "browser_bridge server thread must be reported not-alive after stop()"
    )

    # Port must be free again -- a second bridge can bind the same port.
    b2 = bb.BrowserBridge(port=TEST_PORT + 1, origin=TEST_ORIGIN)
    assert b2.start()
    b2.stop(timeout=2.0)


def test_stop_before_start_is_safe():
    b = bb.BrowserBridge(port=TEST_PORT + 2, origin=TEST_ORIGIN)
    b.stop(timeout=1.0)  # must not raise


def test_stop_disconnects_active_client(bridge):
    with _client() as ws:
        _handshake(ws)
        assert bridge.is_connected()
        bridge.stop(timeout=2.0)
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=2)
    bridge.start()  # re-start so the fixture's own teardown stop() is a no-op-safe call
