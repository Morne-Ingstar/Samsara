"""Tests proving the Tasks plugin makes no network requests.

Regression covered: plugins/commands/tasks.py used to POST every voice-added
task's text to https://morneis.com/api/add by default, under a "sync to
Arcana" label, with no Settings control ever exposed to see or change this.
That endpoint accepts no authentication or user/device identifier, so it
could never have routed data to an individual user's account in the first
place -- the "sync to my Arcana account" premise was invalid regardless of
the default. The fix removes the network call outright rather than adding a
consent toggle for a feature that cannot do what its label claims.

These tests assert the negative directly: urllib.request.urlopen is patched
to raise if called at all, across every task command and every config shape,
so any reintroduced network call fails loudly instead of needing a specific
config value to trigger.
"""
import sys
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.commands import tasks as tasks_plugin
from samsara import tasks_store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point tasks_store at a throwaway file and reset its in-memory state,
    so tests don't share the module-level singleton's data across runs."""
    monkeypatch.setattr(tasks_store, "_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tasks_store, "_data", {"tasks": [], "next_id": 1})
    yield tasks_store


def _app(config=None):
    """A minimal app stand-in. config defaults to {} rather than containing
    any 'tasks' key, so tests explicitly control what config shape (if any)
    is present -- including legacy configs that still carry a leftover
    sync_to_arcana / arcana_api key from a pre-0.21.1 install."""
    app = Mock(spec=["config", "audio_coordinator", "tts_engine"])
    app.config = config if config is not None else {}
    app.audio_coordinator = None
    app.tts_engine = None
    return app


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any attempt to open a network connection during these tests is a bug
    the whole module exists to catch -- fail hard instead of hitting a real
    socket or silently succeeding against a mock."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("Tasks plugin attempted a network request")
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden, raising=False)


# ---------------------------------------------------------------------------
# 1. Adding a task never makes a network request, under any config shape
# ---------------------------------------------------------------------------

def test_add_task_no_config_makes_no_request(isolated_store):
    app = _app(config={})
    tasks_plugin.handle_add_to_list(app, remainder="buy groceries")
    assert [t["text"] for t in isolated_store.get_active()] == ["buy groceries"]


def test_add_task_with_legacy_sync_true_makes_no_request(isolated_store):
    """A config left over from a pre-0.21.1 install with sync_to_arcana=True
    on disk must not cause a request -- the code path that read this key
    no longer exists."""
    app = _app(config={"tasks": {"sync_to_arcana": True, "arcana_api": "https://morneis.com/api/add"}})
    tasks_plugin.handle_add_to_list(app, remainder="email the landlord")
    assert [t["text"] for t in isolated_store.get_active()] == ["email the landlord"]


def test_add_task_with_legacy_sync_false_makes_no_request(isolated_store):
    app = _app(config={"tasks": {"sync_to_arcana": False}})
    tasks_plugin.handle_add_to_list(app, remainder="call the dentist")
    assert [t["text"] for t in isolated_store.get_active()] == ["call the dentist"]


# ---------------------------------------------------------------------------
# 2. No code path in the module can reach the network -- structural check
# ---------------------------------------------------------------------------

def test_module_has_no_arcana_or_network_code():
    """Belt-and-suspenders: assert the removed surface is actually gone
    from the module namespace, not just unreachable at runtime. Does not
    scan the docstring -- it legitimately documents the removed behavior
    (including the old URL) as history."""
    assert not hasattr(tasks_plugin, "_post_task_bg")
    assert not hasattr(tasks_plugin, "_arcana_config")
    assert not hasattr(tasks_plugin, "urllib")
    assert not hasattr(tasks_plugin, "json")
    assert not hasattr(tasks_plugin, "thread_registry")


# ---------------------------------------------------------------------------
# 3. Local task-list behavior is fully preserved
# ---------------------------------------------------------------------------

def test_show_hide_tasks_no_request(isolated_store):
    app = _app()
    with patch.object(tasks_plugin, "_get_overlay") as mock_overlay:
        tasks_plugin.handle_show_tasks(app)
        tasks_plugin.handle_hide_tasks(app)
    # No urlopen call happened (enforced by the autouse fixture).
    mock_overlay.assert_called_once()


def test_complete_and_remove_task_no_request(isolated_store):
    app = _app()
    tasks_plugin.handle_add_to_list(app, remainder="water the plants")
    tasks_plugin.handle_complete_task(app, remainder="1")
    completed = isolated_store.get_all()
    assert any(t["text"] == "water the plants" and t.get("completed") for t in completed)

    app2 = _app()
    tasks_plugin.handle_add_to_list(app2, remainder="renew passport")
    tasks_plugin.handle_remove_task(app2, remainder="1")
    assert not any(t["text"] == "renew passport" for t in isolated_store.get_active())


def test_clear_completed_and_read_tasks_no_request(isolated_store):
    app = _app()
    tasks_plugin.handle_add_to_list(app, remainder="buy stamps")
    tasks_plugin.handle_complete_task(app, remainder="1")
    tasks_plugin.handle_clear_completed(app)
    assert isolated_store.get_all() == []

    app2 = _app()
    tasks_plugin.handle_add_to_list(app2, remainder="pick up prescription")
    tasks_plugin.handle_read_tasks(app2)
    assert [t["text"] for t in isolated_store.get_active()] == ["pick up prescription"]


# ---------------------------------------------------------------------------
# 4. Network failure (belt-and-suspenders): even if something raised, the
#    already-saved local task must not be lost. There is no longer a network
#    call to fail, so this now just confirms local persistence is unaffected
#    by the removal.
# ---------------------------------------------------------------------------

def test_local_task_persists_regardless_of_network_state(isolated_store):
    app = _app(config={"tasks": {"sync_to_arcana": True}})
    tasks_plugin.handle_add_to_list(app, remainder="renew passport")
    active = isolated_store.get_active()
    assert len(active) == 1
    assert active[0]["text"] == "renew passport"
