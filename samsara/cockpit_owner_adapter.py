"""Native Cockpit credential, available only inside a checked client call.

This is a same-user process boundary, not a sandbox against arbitrary Python.
The model/command interfaces never receive the key or the private call permit.
"""
from contextvars import ContextVar
import os
from pathlib import Path
import re

from samsara.cockpit_client import CockpitError

NATIVE_HEADER = "X-Cockpit-Native-Owner"
_client_call = ContextVar("cockpit_owner_call", default=None)


def native_key_path():
    root = os.environ.get("LOCALAPPDATA")
    return Path(root) / "Cockpit" / "native-owner.key" if root else None


class NativeOwnerAdapter:
    def __init__(self, *, key_path=None, request=None):
        self._path = Path(key_path) if key_path is not None else native_key_path()
        self._request = request

    @property
    def available(self):
        return self._path is not None and self._path.is_file()

    def _send(self, path, body):
        # Never retain the credential on the adapter or reuse it across calls.
        try:
            with self._path.open("r", encoding="ascii") as source:
                key = source.read(128)
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
                raise ValueError
        except (OSError, ValueError, AttributeError):
            raise CockpitError("Cockpit voice approval is not available yet.") from None
        from samsara.cockpit_client import CockpitClient
        return CockpitClient(request=self._request)._json(
            "POST", path, body, headers={NATIVE_HEADER: key})

    def confirm(self, binding, operation):
        permit = _client_call.get()
        if permit is None or permit[0] is not self or permit[1] is not operation or permit[2] != binding:
            raise CockpitError("A current owner approval is required.")
        return self._send("/api/changes/confirm", {
            key: binding[key] for key in ("proposal_id", "proposal_hash", "request_id")})

    def cancel(self):
        if self.available:
            self._send("/api/changes/cancel", {})


def owner_adapter():
    """Install the native channel without reading any credential at construction."""
    return NativeOwnerAdapter()
