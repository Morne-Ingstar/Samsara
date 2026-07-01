"""Samsara Mobile Companion subsystem (Phase 1: subprocess + IPC bridge).

This package performs NO I/O at import time. The LAN-facing HTTP server
(server_proc.py) runs as a separate supervised subprocess and talks to this
process only through the loopback IPC bridge (bridge.py). All sockets and
the subprocess itself are created explicitly by supervisor.Supervisor.start(),
which the app calls after its own startup completes.

Submodules are intentionally not re-exported here -- import them directly
(e.g. `from samsara.mobile.supervisor import Supervisor`) so that importing
this package can never have a side effect beyond defining names.
"""
