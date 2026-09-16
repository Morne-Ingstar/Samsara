"""Regression test for the Ava command session's shortlist size cap.

Ava Front Door P1: the old ai_command_mode.menu_limit / _capped_menu
mechanism (tests/test_ai_command_mode_menu_cap.py, deleted) truncated a
flat, whole-registry menu passed to the LLM prompt, logging a WARNING when
truncation happened. That "fixed-menu prompt path" is an explicit spec
DROP item -- the new waterfall's stage (c) never sends the whole registry
at all; _build_shortlist sends only the top `shortlist_size` matches for
THAT utterance.

Queue 107: _build_shortlist is now CommandExecutor.ava_menu(limit=...), the
same menu source the Ava conversation path uses, so this exercises the REAL
registry rather than a fake list of phrases -- the cap has to hold against
the commands this app actually has, and every name it returns has to be one
the executor would accept.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara import ava_command_session
from samsara.commands import CommandExecutor


def _load_real_plugins():
    """tests/conftest.py clears the plugin registry for every test; these
    cases are about the real registry the shortlist is drawn from, so the
    app's own plugin commands are put back (queue 107)."""
    import importlib
    import pkgutil

    import plugins.commands as package
    from samsara import plugin_commands

    for info in pkgutil.iter_modules(package.__path__):
        try:
            module = importlib.import_module(f"plugins.commands.{info.name}")
        except Exception:
            continue
        plugin_commands._reinstall_module_commands(module)


@pytest.fixture(scope="module")
def executor():
    _load_real_plugins()
    return CommandExecutor()


@pytest.fixture(autouse=True)
def _real_plugins_present(executor):
    _load_real_plugins()
    yield


def _app(executor):
    return SimpleNamespace(command_executor=executor, config={})


class TestShortlistSizeCap:
    def test_shortlist_never_exceeds_configured_size(self, executor):
        shortlist = ava_command_session._build_shortlist(
            _app(executor), "do something", {"shortlist_size": 12})
        assert 0 < len(shortlist) <= 12

    def test_shortlist_respects_a_larger_configured_size(self, executor):
        shortlist = ava_command_session._build_shortlist(
            _app(executor), "do something", {"shortlist_size": 50})
        assert 12 < len(shortlist) <= 50

    def test_shortlist_shorter_than_cap_when_the_cap_exceeds_the_registry(self, executor):
        huge = ava_command_session._build_shortlist(
            _app(executor), "do something", {"shortlist_size": 10_000})
        assert len(huge) == len(set(huge))
        assert len(huge) < 10_000

    def test_default_shortlist_size_used_when_absent(self, executor):
        shortlist = ava_command_session._build_shortlist(_app(executor), "do something", {})
        assert len(shortlist) == ava_command_session._DEFAULTS["shortlist_size"]

    def test_an_executor_without_the_menu_yields_nothing_rather_than_a_guess(self):
        app = SimpleNamespace(command_executor=SimpleNamespace(commands={}), config={})
        assert ava_command_session._build_shortlist(app, "do something", {}) == []

    def test_the_shortlist_is_ranked_by_relevance_not_the_alphabet(self, executor):
        shortlist = ava_command_session._build_shortlist(
            _app(executor), "turn the volume up", {"shortlist_size": 12})
        assert "volume up" in shortlist
        assert shortlist != sorted(shortlist)
