"""Isolation proof for the Phase 1 mobile companion subsystem.

Exercises samsara/mobile/supervisor.py + bridge.py directly -- no GUI, no
audio engine, no full Samsara boot -- and proves:

  1. The HTTP subprocess round-trips a /ping through the IPC bridge.
  2. Killing the subprocess externally is detected by the supervisor, and
     the in-process bridge (and therefore the host) survives.
  3. A malformed request and a handler exception both come back as JSON
     errors, and the bridge's dispatch thread survives both.
  4. A pre-bound HTTP port makes start() disable the feature gracefully,
     without raising.
  5. stop() leaves no orphan subprocess and releases the IPC socket.

Run with: F:\\envs\\sami\\python.exe tools\\mobile_isolation_test.py
"""

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from samsara.mobile.bridge import Bridge
from samsara.mobile.supervisor import Supervisor, WATCHDOG_POLL_SECONDS

# Distinct ports per check so failures/timing in one check can't collide
# with sockets still settling in another.
HTTP_PORT_CHECK_1_2 = 18742
HTTP_PORT_CHECK_4 = 18743
HTTP_PORT_CHECK_5 = 18744

HTTP_REQUEST_TIMEOUT_SECONDS = 5.0


def _ping(port, token):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping?token={token}", timeout=HTTP_REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_1_and_2_ping_then_kill():
    """Checks 1 and 2: ping round-trip, then external kill is survived."""
    supervisor = Supervisor(http_port=HTTP_PORT_CHECK_1_2, http_host="127.0.0.1")
    try:
        started = supervisor.start()
        assert started, "supervisor.start() should succeed with a free port"

        reply = _ping(HTTP_PORT_CHECK_1_2, supervisor.http_token)
        assert reply.get("ok") is True, f"expected ok:true, got {reply}"
        assert "pong" in reply, f"expected 'pong' in reply, got {reply}"
        print(f"  [1] PASS: /ping round-tripped through subprocess -> bridge: {reply}")

        proc = supervisor.process
        bridge = supervisor.bridge
        proc.kill()
        proc.wait(timeout=5)

        # Give the watchdog at least one poll tick to notice.
        time.sleep(WATCHDOG_POLL_SECONDS + 0.5)

        assert supervisor.enabled is False, "supervisor should mark itself disabled after subprocess death"
        assert bridge.is_alive(), "in-process bridge dispatch thread must survive subprocess death (host survives)"
        print("  [2] PASS: subprocess death detected; in-process bridge still alive (host survives)")
    finally:
        supervisor.stop()


def check_3_malformed_and_handler_exception():
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
        print(f"  [3a] PASS: malformed request returned JSON error: {malformed_reply}")

        # Well-formed request whose handler raises.
        request = {"token": token, "action": "boom", "params": {}}
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            sock.settimeout(5)
            raw = sock.recv(4096)
        boom_reply = json.loads(raw.decode("utf-8"))
        assert boom_reply.get("ok") is False, f"expected ok:false for a raising handler, got {boom_reply}"
        assert bridge.is_alive(), "bridge dispatch thread must survive a handler exception"
        print(f"  [3b] PASS: handler exception returned JSON error, dispatch thread alive: {boom_reply}")
    finally:
        bridge.stop()


def check_4_port_in_use_disables_gracefully():
    """Check 4: pre-bound HTTP port causes graceful, non-raising feature disable.

    HTTPServer sets SO_REUSEADDR on its listening socket, and on Windows
    SO_REUSEADDR permits binding straight on top of another SO_REUSEADDR (or
    plain) listener instead of failing -- so a naive pre-bind here would not
    actually simulate "port in use". SO_EXCLUSIVEADDRUSE is the Windows flag
    that genuinely reserves the address against any other socket, reuse or
    not, which is what's needed to make the subprocess's bind fail.
    """
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("127.0.0.1", HTTP_PORT_CHECK_4))
    blocker.listen(1)

    supervisor = Supervisor(http_port=HTTP_PORT_CHECK_4, http_host="127.0.0.1")
    try:
        started = supervisor.start()  # must not raise
        assert started is False, "start() should return False when the HTTP port is already in use"
        assert supervisor.enabled is False, "supervisor should report disabled after a port-in-use failure"
        print("  [4] PASS: port-in-use caused graceful disable, start() raised nothing")
    finally:
        supervisor.stop()
        blocker.close()


def check_5_stop_cleans_up():
    """Check 5: stop() leaves no orphan subprocess and releases the IPC socket."""
    supervisor = Supervisor(http_port=HTTP_PORT_CHECK_5, http_host="127.0.0.1")
    started = supervisor.start()
    assert started, "supervisor.start() should succeed with a free port"

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
    print(f"  [5] PASS: subprocess gone (exit code {proc.returncode}), IPC port {ipc_port} released")


def main():
    checks = [
        check_1_and_2_ping_then_kill,
        check_3_malformed_and_handler_exception,
        check_4_port_in_use_disables_gracefully,
        check_5_stop_cleans_up,
    ]
    failures = []
    for check in checks:
        try:
            check()
        except Exception as e:
            failures.append((check.__name__, e))
            print(f"  FAIL in {check.__name__}: {e}")

    print()
    if failures:
        print(f"RESULT: {len(failures)} failure(s) out of {len(checks)} check group(s)")
        return 1
    print("RESULT: all isolation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
