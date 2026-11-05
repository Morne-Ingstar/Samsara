"""Fixed loopback Cockpit v1 client. No redirects, proxy, fallback or UI secret.

Voice confirm/cancel require an independently installed trusted owner adapter.
The browser capability is deliberately neither fetched nor accepted here.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
import threading

import requests

BASE_URL = "http://127.0.0.1:8790"
MAX_RESPONSE = 2_000_000


class CockpitError(ValueError):
    """Safe local disposition; never include a response body/transcript."""


class CockpitClient:
    def __init__(self, *, owner_adapter=None, request=None):
        self._owner = owner_adapter
        self._request_override = request
        self._approval = None
        self._approval_lock = threading.Lock()

    @property
    def can_confirm(self):
        return self._owner is not None and getattr(self._owner, "available", True)

    def _json(self, method, path, body=None, *, headers=None):
        with requests.Session() as session:
            session.trust_env = False
            request = self._request_override or session.request
            try:
                options = dict(json=body, timeout=(3, 30), allow_redirects=False)
                if headers is not None:
                    options["headers"] = headers
                response = request(method, BASE_URL + path, **options)
            except requests.RequestException as exc:
                raise CockpitError("Cockpit response lost or unavailable; recover by request ID.") from exc
            try:
                if response.status_code != 200:
                    raise CockpitError(f"Cockpit refused the request ({response.status_code}).")
                if len(response.content) > MAX_RESPONSE:
                    raise CockpitError("Cockpit response exceeds the safe limit.")
                value = json.loads(response.content)
                if not isinstance(value, dict):
                    raise CockpitError("Invalid Cockpit response.")
                return value
            finally:
                response.close()

    def context(self):
        value = self._json("GET", "/api/context?purpose=memo")
        if (value.get("schema_version") != "cockpit.context.v1" or not value.get("available")
                or not value.get("snapshot_consistent")
                or not value.get("coverage", {}).get("state_complete")):
            raise CockpitError("Cockpit context is incomplete or unavailable.")
        return value

    def preview(self, envelope):
        return self._json("POST", "/api/changes/preview", deepcopy(envelope))

    def briefing(self):
        """Partial coverage is advice, never an empty-success or write authority."""
        value = self._json("GET", "/api/briefing")
        if (value.get('schema_version') != 'cockpit.briefing.v1'
                or value.get('ranking_version') != 1
                or not isinstance(value.get('actionable'), list)
                or not isinstance(value.get('full_order'), list)
                or [r.get('id') for r in value['actionable']] != value['full_order']
                or not isinstance(value.get('speech_items'), list)
                or len(value['speech_items']) > 5
                or not isinstance(value.get('speech_text'), str)):
            raise CockpitError('Cockpit briefing is invalid or unavailable.')
        return value

    def undo_preview(self, receipt_id, request_id):
        for ident in (receipt_id, request_id):
            if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident):
                raise CockpitError('Invalid receipt or request ID.')
        return self._json('POST', '/api/changes/' + receipt_id + '/undo-preview', {'request_id': request_id})

    def recover(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
            raise CockpitError("Invalid request ID.")
        return self._json("GET", "/api/changes/" + request_id)

    def bind_approval(self, binding, operation):
        """Remember the exact unapproved policy object staged by the runner."""
        from samsara import execution_policy as policy
        if (not isinstance(operation, policy.PendingOperation) or operation.approved is not None
                or operation is not policy.pending_operation()
                or operation.invocation.route != policy.Route.EXACT
                or operation.invocation.command_id != "approve cockpit memo batch"
                or operation.invocation.args != binding):
            raise CockpitError("A current owner approval is required.")
        with self._approval_lock:
            self._approval = (operation, deepcopy(binding))

    def confirm(self, binding, operation):
        if not self.can_confirm:
            raise CockpitError("Cockpit voice approval is not available yet.")
        # An actual resolved policy object, never a dict/flag from an extractor.
        from samsara import execution_policy as policy
        if (not isinstance(operation, policy.PendingOperation) or operation.approved is not True
                or operation.invocation.args != binding or operation.is_expired()
                or not operation._event.is_set()
                or operation.invocation.route != policy.Route.EXACT
                or operation.invocation.command_id != "approve cockpit memo batch"
                or not policy.is_fresh(operation._app, operation.generation)):
            raise CockpitError("A current owner approval is required.")
        with self._approval_lock:
            if self._approval is None or self._approval[0] is not operation or self._approval[1] != binding:
                raise CockpitError("A current owner approval is required.")
            self._approval = None  # Consume even if the response is lost.
        from samsara.cockpit_owner_adapter import _client_call
        token = _client_call.set((self._owner, operation, deepcopy(binding)))
        try:
            return self._owner.confirm(deepcopy(binding), operation)
        finally:
            _client_call.reset(token)

    def apply(self, binding, authorization):
        body = {key: binding[key] for key in ("proposal_id", "proposal_hash", "request_id")}
        body["authorization"] = authorization
        return self._json("POST", "/api/changes/apply", body)

    def cancel(self):
        with self._approval_lock:
            self._approval = None
        if self._owner is not None:
            self._owner.cancel()
