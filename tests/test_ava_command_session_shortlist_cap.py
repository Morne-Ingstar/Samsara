"""Regression test for the Ava command session's shortlist size cap.

Ava Front Door P1: the old ai_command_mode.menu_limit / _capped_menu
mechanism (tests/test_ai_command_mode_menu_cap.py, deleted) truncated a
flat, whole-registry menu passed to the LLM prompt, logging a WARNING when
truncation happened. That "fixed-menu prompt path" is an explicit spec
DROP item ("separate resolver+plan executor" / fixed-menu prompt path in
the behavior inventory) -- the new waterfall's stage (c) never sends the
whole registry at all; _build_shortlist sends only the top
`shortlist_size` fuzzy-scored matches for THAT utterance. This is the
nearest equivalent regression guard: shortlist_size still bounds prompt
size even against a large registry, just via a different (per-utterance,
relevance-ranked) mechanism rather than a flat truncation + warning log.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara import ava_command_session


def _make_app(num_commands=288):
    commands = [
        {"phrase": f"command {i:03d}", "aliases": [], "source": "plugin", "ai_visible": True}
        for i in range(num_commands)
    ]
    matcher = SimpleNamespace(list_commands=lambda: commands)
    command_executor = SimpleNamespace(_matcher=matcher, commands={})
    return SimpleNamespace(command_executor=command_executor)


class TestShortlistSizeCap:
    def test_shortlist_never_exceeds_configured_size(self):
        app = _make_app(num_commands=288)
        cfg = {"shortlist_size": 12}
        shortlist = ava_command_session._build_shortlist(app, "do something", cfg)
        assert len(shortlist) <= 12

    def test_shortlist_respects_a_larger_configured_size(self):
        app = _make_app(num_commands=288)
        cfg = {"shortlist_size": 50}
        shortlist = ava_command_session._build_shortlist(app, "do something", cfg)
        assert len(shortlist) <= 50

    def test_shortlist_shorter_than_cap_when_registry_is_small(self):
        app = _make_app(num_commands=5)
        cfg = {"shortlist_size": 12}
        shortlist = ava_command_session._build_shortlist(app, "do something", cfg)
        assert len(shortlist) == 5

    def test_default_shortlist_size_used_when_absent(self):
        app = _make_app(num_commands=288)
        shortlist = ava_command_session._build_shortlist(app, "do something", {})
        assert len(shortlist) == ava_command_session._DEFAULTS["shortlist_size"]
