"""Isolation guarantees for the mobile companion subsystem (samsara/mobile/).

Promoted from the ad hoc tools/mobile_isolation_test.py script into a
permanent pytest file. Exercises samsara/mobile/bridge.py + supervisor.py
directly -- no GUI, no audio engine, no full Samsara boot -- and locks in:

  1. The HTTP subprocess round-trips a /ping through the IPC bridge.
  2. Killing the subprocess externally is detected by the supervisor, and
     the in-process bridge (and therefore the host) survives.
  3. A malformed request and a handler exception both come back as JSON
     errors, and the bridge's dispatch thread survives both.
  4. A pre-bound HTTP port makes start() disable the feature gracefully,
     without raising.
  5. stop() leaves no orphan subprocess and releases the IPC socket.

These tests bind real loopback sockets and spawn a real subprocess; a
module-scoped autouse fixture skips the whole file (via pytest.skip, not a
failure) in an environment that can't bind a TCP socket at all.
"""

import json
import socket
import time
import urllib.request

import pytest

from samsara.mobile.bridge import Bridge
from samsara.mobile.supervisor import Supervisor, WATCHDOG_POLL_SECONDS

HTTP_REQUEST_TIMEOUT_SECONDS = 5.0


def _free_tcp_port():
    """Reserve an ephemeral loopback port by binding then releasing it.

    Small TOCTOU race window between release and reuse, same as the original
    ad hoc script's fixed test ports -- acceptable for local test runs.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _ping(port, token):
    url = f"http://127.0.0.1:{port}/ping?token={token}"
    with urllib.request.urlopen(url, timeout=HTTP_REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


@pytest.fixture(autouse=True, scope="module")
def _require_loopback_sockets():
    """Skip this whole module if the environment can't bind loopback sockets."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        probe.close()
    except OSError as e:
        pytest.skip(f"environment cannot bind loopback sockets: {e}")


def test_ping_round_trip():
    """Check 1: /ping round-trips through subprocess -> bridge."""
    supervisor = Supervisor(http_port=_free_tcp_port(), http_host="127.0.0.1")
    try:
        assert supervisor.start(), "supervisor.start() should succeed with a free port"
        reply = _ping(supervisor.http_port, supervisor.http_token)
        assert reply.get("ok") is True, f"expected ok:true, got {reply}"
        assert "pong" in reply, f"expected 'pong' in reply, got {reply}"
    finally:
        supervisor.stop()


def test_subprocess_killed_mid_run_leaves_bridge_alive():
    """Check 2: external subprocess death is detected; in-process bridge survives."""
    supervisor = Supervisor(http_port=_free_tcp_port(), http_host="127.0.0.1")
    try:
        assert supervisor.start(), "supervisor.start() should succeed with a free port"
        reply = _ping(supervisor.http_port, supervisor.http_token)
        assert reply.get("ok") is True, f"expected ok:true, got {reply}"

        proc = supervisor.process
        bridge = supervisor.bridge
        proc.kill()
        proc.wait(timeout=5)

        # Give the watchdog at least one poll tick to notice.
        time.sleep(WATCHDOG_POLL_SECONDS + 0.5)

        assert supervisor.enabled is False, "supervisor should mark itself disabled after subprocess death"
        assert bridge.is_alive(), "in-process bridge dispatch thread must survive subprocess death (host survives)"
    finally:
        supervisor.stop()


def test_malformed_request_and_handler_exception_leave_dispatch_alive():
    """Check 3: malformed request and a raising handler both degrade to JSON errors."""
    token = "test-token"
    bridge = Bridge(token)

    def _boom(params):
        raise RuntimeError("simulated handler failure")

    bridge.register("boom", _boom)
    try:
        port = bridge.start(port=0)

        # Malformed request: not valid JSON at all.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"not json at all\n")
            sock.settimeout(5)
            raw = sock.recv(4096)
        malformed_reply = json.loads(raw.decode("utf-8"))
        assert malformed_reply.get("ok") is False, f"expected ok:false for malformed request, got {malformed_reply}"
        assert bridge.is_alive(), "bridge dispatch thread must survive a malformed request"

        # Well-formed request whose handler raises.
        request = {"token": token, "action": "boom", "params": {}}
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            sock.settimeout(5)
            raw = sock.recv(4096)
        boom_reply = json.loads(raw.decode("utf-8"))
        assert boom_reply.get("ok") is False, f"expected ok:false for a raising handler, got {boom_reply}"
        assert bridge.is_alive(), "bridge dispatch thread must survive a handler exception"
    finally:
        bridge.stop()


def test_port_in_use_disables_gracefully():
    """Check 4: pre-bound HTTP port causes graceful, non-raising feature disable.

    HTTPServer sets SO_REUSEADDR on its listening socket, and on Windows
    SO_REUSEADDR permits binding straight on top of another SO_REUSEADDR (or
    plain) listener instead of failing -- so a naive pre-bind here would not
    actually simulate "port in use". SO_EXCLUSIVEADDRUSE is the Windows flag
    that genuinely reserves the address against any other socket, reuse or
    not, which is what's needed to make the subprocess's bind fail.
    """
    if not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        pytest.skip("SO_EXCLUSIVEADDRUSE is Windows-only")

    port = _free_tcp_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)

    supervisor = Supervisor(http_port=port, http_host="127.0.0.1")
    try:
        started = supervisor.start()  # must not raise
        assert started is False, "start() should return False when the HTTP port is already in use"
        assert supervisor.enabled is False, "supervisor should report disabled after a port-in-use failure"
    finally:
        supervisor.stop()
        blocker.close()


def test_stop_releases_port_and_leaves_no_orphan():
    """Check 5: stop() leaves no orphan subprocess and releases the IPC socket."""
    supervisor = Supervisor(http_port=_free_tcp_port(), http_host="127.0.0.1")
    assert supervisor.start(), "supervisor.start() should succeed with a free port"

    proc = supervisor.process
    ipc_port = supervisor.bridge.port

    supervisor.stop()

    assert proc.poll() is not None, "subprocess must have exited after stop() (no orphan)"
    assert supervisor.process is None, "supervisor should drop its subprocess handle after stop()"
    assert supervisor.bridge is None, "supervisor should drop its bridge handle after stop()"

    # If the IPC port is truly released, re-binding it must succeed.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", ipc_port))
    finally:
        probe.close()
