"""Webhook bridge for Smart Actions Phase 2.

POSTs voice requests to a user-configured AI agent endpoint and returns
the parsed response. All security decisions (tier, scope) stay LOCAL and
are never delegated to the remote endpoint.

contract_version is bumped on breaking schema changes so agents can detect
incompatible callers.
"""

import json
import logging
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CONTRACT_VERSION = 1
SAMSARA_VERSION = "0.9.8"


def _default_available_tools() -> List[str]:
    return [
        'paste_text', 'append_to_brain_dump', 'show_notification',
        'append_to_file', 'webhook_trigger', 'calendar_create',
        'email_draft', 'send_email', 'delete_file', 'run_shell_command',
    ]


class SmartActionsBridge:
    """HTTP bridge to an external AI agent endpoint."""

    def __init__(self, config: dict):
        self._endpoint_url: str = config.get('endpoint_url', '').strip()
        self._auth_header: str = config.get('auth_header', '').strip()
        self._timeout_s: int = int(config.get('timeout_s', 30))

    def is_configured(self) -> bool:
        return bool(self._endpoint_url)

    def send(
        self,
        text: str,
        command_verb: str,
        session_id: Optional[str] = None,
        context: Optional[List[dict]] = None,
        observations: Optional[List[dict]] = None,
        available_tools: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """POST to the configured endpoint.

        Returns the parsed JSON response dict, or None on any failure (network,
        timeout, bad JSON). Never raises -- caller handles the None case.

        SECURITY: The returned dict may contain tool_calls. The caller MUST
        determine execution tier from local TOOL_TIERS, never from any 'tier'
        field in the response.
        """
        if not self._endpoint_url:
            return None

        payload = {
            'contract_version': CONTRACT_VERSION,
            'request_id': uuid.uuid4().hex,
            'text': text,
            'command': command_verb,
            'session_id': session_id or '',
            'context': list(context) if context else [],
            'observations': list(observations) if observations else [],
            'available_tools': available_tools if available_tools is not None
                               else _default_available_tools(),
            'samsara_version': SAMSARA_VERSION,
        }

        body = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if self._auth_header:
            headers['Authorization'] = self._auth_header

        try:
            req = urllib.request.Request(
                self._endpoint_url, data=body, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                raw = resp.read().decode('utf-8')
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            logger.error("[BRIDGE] HTTP %s from %s: %s", e.code, self._endpoint_url, e)
        except urllib.error.URLError as e:
            logger.error("[BRIDGE] Network error reaching %s: %s", self._endpoint_url, e)
        except TimeoutError:
            logger.error("[BRIDGE] Timed out after %ss", self._timeout_s)
        except json.JSONDecodeError as e:
            logger.error("[BRIDGE] Invalid JSON response: %s", e)
        except Exception as e:
            logger.error("[BRIDGE] Unexpected error: %s", e)
        return None

    def test_connection(self, timeout_s: int = 5) -> tuple:
        """POST a minimal ping to the endpoint. Returns (ok: bool, message: str)."""
        if not self._endpoint_url:
            return False, "No endpoint URL configured"

        payload = {
            'contract_version': CONTRACT_VERSION,
            'request_id': uuid.uuid4().hex,
            'text': 'ping',
            'command': 'test',
        }
        body = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
        if self._auth_header:
            headers['Authorization'] = self._auth_header

        try:
            req = urllib.request.Request(
                self._endpoint_url, data=body, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return True, f"Connected (HTTP {resp.status})"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            return False, f"Unreachable: {e.reason}"
        except TimeoutError:
            return False, f"Timed out after {timeout_s}s"
        except Exception as e:
            return False, str(e)
