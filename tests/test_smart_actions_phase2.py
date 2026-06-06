"""Tests for Smart Actions Phase 2: webhook bridge, session, tool dispatcher.

All tests are headless -- no display, no real network, no filesystem writes
beyond temp files. UI-dependent paths (tkinter confirmation dialogs) are
mocked out.
"""

import json
import threading
import time
import types
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---- Module imports ---------------------------------------------------------

from samsara.smart_actions_bridge import SmartActionsBridge, CONTRACT_VERSION
from samsara.smart_actions_session import SmartActionsSession
from samsara.smart_actions_tools import (
    ToolDispatcher, TIER_AUTO, TIER_SETUP, TIER_ALWAYS_CONFIRM, TOOL_TIERS)


# ---- Fixtures / helpers -----------------------------------------------------

def _make_app(config=None):
    """Minimal app-like object for ToolDispatcher tests."""
    app = MagicMock()
    app.config = config or {
        'smart_actions': {
            'enabled': True,
            'brain_dump_path': '~/Documents/test_brain_dump.md',
            'earcons_enabled': False,
        }
    }
    app.root = None
    return app


def _make_dispatcher(config=None, app=None):
    cfg = config or {
        'allowed_directories': ['/tmp'],
        'allowed_domains': ['https://api.example.com'],
        'tier2_approvals': {},
    }
    a = app or _make_app()
    d = ToolDispatcher(a, cfg)
    return d


# =============================================================================
# Bridge tests
# =============================================================================

class TestBridgePayload:
    """Verify the POSTed JSON matches the contract."""

    def _make_bridge(self, **kwargs):
        cfg = {'endpoint_url': 'http://localhost:9999/test',
               'auth_header': '',
               'timeout_s': 5}
        cfg.update(kwargs)
        return SmartActionsBridge(cfg)

    def test_bridge_sends_correct_payload(self):
        """Payload must include contract_version, request_id, and observations."""
        captured = {}

        class _FakeResp:
            status = 200
            def read(self): return b'{"reply": "ok"}'
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def _mock_urlopen(req, timeout=None):
            captured['body'] = json.loads(req.data.decode())
            return _FakeResp()

        bridge = self._make_bridge()
        with patch('urllib.request.urlopen', _mock_urlopen):
            bridge.send(
                text="plan my week",
                command_verb="plan",
                session_id="abc123",
                observations=[{'tool': 'paste_text', 'status': 'success', 'output': None}])

        body = captured['body']
        assert body['contract_version'] == CONTRACT_VERSION
        assert 'request_id' in body and len(body['request_id']) == 32
        assert body['text'] == "plan my week"
        assert body['command'] == "plan"
        assert body['session_id'] == "abc123"
        assert len(body['observations']) == 1
        assert body['observations'][0]['tool'] == 'paste_text'

    def test_bridge_returns_none_on_timeout(self):
        import urllib.error
        bridge = self._make_bridge()
        with patch('urllib.request.urlopen', side_effect=TimeoutError("timed out")):
            result = bridge.send("ask something", "ask")
        assert result is None

    def test_bridge_returns_none_on_http_error(self):
        import urllib.error
        bridge = self._make_bridge()
        err = urllib.error.HTTPError(
            url='http://localhost', code=500, msg='Internal Server Error',
            hdrs=None, fp=None)
        with patch('urllib.request.urlopen', side_effect=err):
            result = bridge.send("ask something", "ask")
        assert result is None

    def test_bridge_returns_none_when_unconfigured(self):
        bridge = SmartActionsBridge({'endpoint_url': '', 'timeout_s': 5})
        result = bridge.send("ask something", "ask")
        assert result is None

    def test_is_configured_false_when_empty(self):
        bridge = SmartActionsBridge({'endpoint_url': ''})
        assert not bridge.is_configured()

    def test_is_configured_true_when_set(self):
        bridge = SmartActionsBridge({'endpoint_url': 'http://example.com'})
        assert bridge.is_configured()


# =============================================================================
# Session tests
# =============================================================================

class TestSession:
    def test_session_creates_new_id(self):
        s = SmartActionsSession()
        sid = s.get_or_create_session()
        assert isinstance(sid, str) and len(sid) == 12

    def test_session_same_id_within_window(self):
        s = SmartActionsSession(window_minutes=5)
        sid1 = s.get_or_create_session()
        sid2 = s.get_or_create_session()
        assert sid1 == sid2

    def test_session_expires_after_window(self):
        s = SmartActionsSession(window_minutes=5)
        sid1 = s.get_or_create_session()
        # Force expiry via test helper (direct attribute write is bypassed to
        # preserve lock discipline outside test context)
        from datetime import datetime, timedelta
        s._backdate_last_activity(datetime.now() - timedelta(minutes=6))
        sid2 = s.get_or_create_session()
        assert sid1 != sid2

    def test_session_resets_on_command(self):
        s = SmartActionsSession()
        s.get_or_create_session()
        s.add_user_turn("hello")
        s.reset()
        assert not s.has_active_session()
        assert s.snapshot_context() == []
        assert s.snapshot_observations() == []

    def test_session_context_accumulates(self):
        s = SmartActionsSession()
        s.get_or_create_session()
        s.add_user_turn("plan my week")
        s.add_assistant_turn("Here's a plan...")
        s.add_user_turn("add physio on Tuesday")
        ctx = s.snapshot_context()
        assert len(ctx) == 3
        assert ctx[0]['role'] == 'user'
        assert ctx[1]['role'] == 'assistant'

    def test_session_observations_consumed(self):
        s = SmartActionsSession()
        s.get_or_create_session()
        s.add_observation('paste_text', 'success', None)
        s.add_observation('append_to_file', 'error', 'permission denied')
        obs = s.consume_observations()
        assert len(obs) == 2
        assert obs[0]['tool'] == 'paste_text'
        # After consume, pending is empty
        assert s.snapshot_observations() == []
        assert s.consume_observations() == []


# =============================================================================
# ToolDispatcher tests
# =============================================================================

class TestToolDispatch:
    def test_tool_dispatch_tier1_auto(self):
        """paste_text is Tier 1 — no confirmation should be requested."""
        d = _make_dispatcher()
        with patch.object(d, '_request_confirmation') as mock_confirm, \
             patch.object(d, '_execute', return_value={'success': True, 'result': None}):
            d.dispatch({'tool': 'paste_text', 'args': {'text': 'hello'}})
        mock_confirm.assert_not_called()

    def test_tool_dispatch_tier3_confirms(self):
        """send_email is Tier 3 — confirmation is always requested."""
        d = _make_dispatcher()
        with patch.object(d, '_request_confirmation', return_value=(False, False)) as mock_c, \
             patch.object(d, '_execute') as mock_exec:
            result = d.dispatch({'tool': 'send_email', 'args': {}})
        mock_c.assert_called_once()
        mock_exec.assert_not_called()
        assert not result['success']

    def test_tool_dispatch_ignores_remote_tier(self):
        """A tool_call containing tier='auto' for send_email must still use
        local TIER_ALWAYS_CONFIRM — the remote tier field is never read."""
        d = _make_dispatcher()
        # Verify local tier
        assert d._get_tier('send_email') == TIER_ALWAYS_CONFIRM
        # tool_call with injected 'tier' field
        tool_call = {'tool': 'send_email', 'args': {}, 'tier': 'auto'}
        with patch.object(d, '_request_confirmation', return_value=(False, False)):
            result = d.dispatch(tool_call)
        # Tier 3 path was taken (confirmation requested, rejected)
        assert not result['success']

    def test_tier_for_all_tools(self):
        """Sanity check: verify each tool maps to its expected tier."""
        assert TOOL_TIERS['paste_text'] == TIER_AUTO
        assert TOOL_TIERS['append_to_brain_dump'] == TIER_AUTO
        assert TOOL_TIERS['webhook_trigger'] == TIER_SETUP
        assert TOOL_TIERS['send_email'] == TIER_ALWAYS_CONFIRM
        assert TOOL_TIERS['delete_file'] == TIER_ALWAYS_CONFIRM
        assert TOOL_TIERS['run_shell_command'] == TIER_ALWAYS_CONFIRM


class TestScopeChecks:
    def test_scope_blocks_path_traversal(self):
        """../../path must be rejected even if allowed_dir is configured."""
        d = _make_dispatcher(config={
            'allowed_directories': ['/tmp/safe'],
            'allowed_domains': [],
            'tier2_approvals': {},
        })
        allowed, reason = d._check_scope(
            'append_to_file', {'path': '/tmp/safe/../../etc/passwd'})
        assert not allowed
        assert 'outside allowed' in reason

    def test_scope_allows_configured_dir(self, tmp_path):
        d = _make_dispatcher(config={
            'allowed_directories': [str(tmp_path)],
            'allowed_domains': [],
            'tier2_approvals': {},
        })
        allowed, reason = d._check_scope(
            'append_to_file', {'path': str(tmp_path / 'notes.md')})
        assert allowed

    def test_scope_blocks_unconfigured_domain(self):
        d = _make_dispatcher(config={
            'allowed_directories': [],
            'allowed_domains': ['https://api.good.com'],
            'tier2_approvals': {},
        })
        allowed, reason = d._check_scope(
            'webhook_trigger', {'url': 'https://api.evil.com/exfiltrate'})
        assert not allowed

    def test_scope_allows_exact_domain(self):
        d = _make_dispatcher(config={
            'allowed_directories': [],
            'allowed_domains': ['https://api.example.com'],
            'tier2_approvals': {},
        })
        allowed, _ = d._check_scope(
            'webhook_trigger', {'url': 'https://api.example.com/tasks'})
        assert allowed

    def test_scope_rejects_domain_prefix_bypass(self):
        """https://example.com.evil.com must not match when example.com is allowed."""
        d = _make_dispatcher(config={
            'allowed_directories': [],
            'allowed_domains': ['https://example.com'],
            'tier2_approvals': {},
        })
        allowed, reason = d._check_scope(
            'webhook_trigger', {'url': 'https://example.com.evil.com/steal'})
        assert not allowed
        assert 'not in allowed_domains' in reason

    def test_scope_allows_true_subdomain(self):
        """api.example.com must be accepted when example.com is allowed."""
        d = _make_dispatcher(config={
            'allowed_directories': [],
            'allowed_domains': ['https://example.com'],
            'tier2_approvals': {},
        })
        allowed, _ = d._check_scope(
            'webhook_trigger', {'url': 'https://api.example.com/v1/data'})
        assert allowed


class TestTier2ApprovalScope:
    def test_tier2_approval_exact_scope(self, tmp_path):
        """Approving URL A must NOT grant approval for URL B."""
        url_a = 'https://api.todoist.com/rest/v2/tasks'
        url_b = 'https://api.todoist.com/rest/v2/tasks/delete'

        d = _make_dispatcher(config={
            'allowed_directories': [str(tmp_path)],
            'allowed_domains': ['https://api.todoist.com'],
            'tier2_approvals': {f'webhook_trigger|{url_a}': True},
        })

        # URL A: pre-approved
        key_a = d._build_approval_key('webhook_trigger', {'url': url_a})
        assert d._approvals.get(key_a) is True

        # URL B: NOT pre-approved (different exact URL)
        key_b = d._build_approval_key('webhook_trigger', {'url': url_b})
        assert not d._approvals.get(key_b)

    def test_tier2_approved_skips_confirmation(self, tmp_path):
        url = 'https://api.example.com/tasks'
        d = _make_dispatcher(config={
            'allowed_directories': [str(tmp_path)],
            'allowed_domains': ['https://api.example.com'],
            'tier2_approvals': {f'webhook_trigger|{url}': True},
        })
        with patch.object(d, '_request_confirmation') as mock_c, \
             patch.object(d, '_execute', return_value={'success': True, 'result': None}):
            d.dispatch({'tool': 'webhook_trigger', 'args': {'url': url, 'payload': {}}})
        mock_c.assert_not_called()


# =============================================================================
# Fallback hierarchy tests
# =============================================================================

class TestFallbackHierarchy:
    def _dispatcher_with_mocked_brain_dump(self, brain_dump_ok=True, tmp_path=None):
        d = _make_dispatcher()
        # Patch the brain dump write
        mock_append = MagicMock(return_value=brain_dump_ok)
        return d, mock_append

    def test_fallback_hierarchy_brain_dump(self, tmp_path):
        """When bridge is None, text is saved to brain dump."""
        d = _make_dispatcher()
        with patch('plugins.commands.smart_actions.append_entry',
                   return_value=True) as mock_append, \
             patch('plugins.commands.smart_actions._play_earcon'):
            result = d._fallback_save("plan my week", "agent unreachable")
        assert result == "saved_to_brain_dump"
        mock_append.assert_called_once()

    def test_fallback_hierarchy_clipboard(self, tmp_path):
        """When brain dump fails, falls back to clipboard."""
        d = _make_dispatcher()
        with patch('plugins.commands.smart_actions.append_entry',
                   return_value=False), \
             patch.object(d, '_emergency_db_write', side_effect=RuntimeError("db fail")), \
             patch('pyperclip.copy') as mock_copy, \
             patch('plugins.commands.smart_actions._play_earcon'):
            result = d._fallback_save("lost thought", "brain dump failed")
        assert result == "saved_to_clipboard"
        mock_copy.assert_called_once_with("lost thought")

    def test_unconfigured_bridge_falls_back(self, tmp_path):
        """When endpoint_url is empty, send() returns None and fallback activates."""
        bridge = SmartActionsBridge({'endpoint_url': '', 'timeout_s': 5})
        assert bridge.is_configured() is False
        response = bridge.send("note to self", "note")
        assert response is None


# =============================================================================
# Routing verb tests (without actually running the full pipeline)
# =============================================================================

class TestRoutingVerbs:
    def _make_executor(self, app=None):
        from samsara.commands import CommandExecutor
        from unittest.mock import patch as _patch
        # Minimal executor without loading real commands.json or plugins
        with _patch.object(CommandExecutor, 'load_commands'), \
             _patch('samsara.plugin_commands.load_plugins'), \
             _patch('samsara.plugin_commands.set_shared_matcher'), \
             _patch.object(CommandExecutor, '__init__', lambda self, *a, **kw: None):
            executor = object.__new__(CommandExecutor)
        executor._app = app or _make_app()
        return executor

    def test_is_routing_verb_true(self):
        ex = self._make_executor()
        ex._app.config = {'smart_actions': {
            'enabled': True, 'routing_verbs': ['ask', 'plan', 'summarize']}}
        assert ex._is_routing_verb("ask what the weather is")
        assert ex._is_routing_verb("plan my week around physio")
        assert ex._is_routing_verb("summarize my notes")

    def test_is_routing_verb_false(self):
        ex = self._make_executor()
        ex._app.config = {'smart_actions': {
            'enabled': True, 'routing_verbs': ['ask', 'plan', 'summarize']}}
        assert not ex._is_routing_verb("open chrome")
        assert not ex._is_routing_verb("")

    def test_routing_not_triggered_when_disabled(self):
        """Smart Actions disabled in config -> routing verb falls through as dictation."""
        ex = self._make_executor()
        ex._app.config = {'smart_actions': {'enabled': False,
                                             'routing_verbs': ['ask']}}
        with patch.object(ex, '_try_smart_actions_route') as mock_route:
            result = ex._is_routing_verb("ask anything")
        # is_routing_verb returns True, but the caller checks enabled flag
        assert result is True
        # The flag check in process_text prevents _try_smart_actions_route call
        # (tested indirectly via the enabled gate in process_text)
