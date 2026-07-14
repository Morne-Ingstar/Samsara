"""Regression tests for the Tasks plugin's local-only privacy boundary."""
from types import SimpleNamespace

import pytest

from plugins.commands import tasks as tasks_plugin
from samsara import tasks_store
from samsara.config_schema import SETTINGS_SCHEMA


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks_store, "_TASKS_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tasks_store, "_data", {"tasks": [], "next_id": 1})
    return tasks_store


def test_add_task_persists_locally_without_network(isolated_store, monkeypatch):
    """Even a stale legacy opt-in config cannot transmit task text."""
    import urllib.request

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Tasks plugin attempted a network request")

    monkeypatch.setattr(urllib.request, "urlopen", fail_if_called)
    app = SimpleNamespace(
        config={"tasks": {"sync_to_arcana": True}},
        audio_coordinator=None,
        tts_engine=None,
    )

    assert tasks_plugin.handle_add_to_list(app, remainder="water the plants") is True
    assert [task["text"] for task in isolated_store.get_active()] == ["water the plants"]


def test_tasks_plugin_exposes_no_sync_hook():
    assert not hasattr(tasks_plugin, "_post_task_bg")
    assert not hasattr(tasks_plugin, "_arcana_config")


def test_task_sync_setting_no_longer_exists():
    assert "tasks.sync_to_arcana" not in SETTINGS_SCHEMA
