"""598: pure seed inputs, container ownership and structural validation."""
from copy import deepcopy

import pytest

from samsara.config_seed import build_default_config, structural_error


def _containers(value):
    if isinstance(value, (dict, list)):
        yield id(value)
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            yield from _containers(child)


def test_factory_preserves_inputs_and_owns_every_nested_container(tmp_path):
    macros = {"bindings": [{"keys": ["f9"]}]}
    notifications = {"reminders": [{"days": [1, 3]}]}
    before = deepcopy((macros, notifications))
    inputs = dict(home=tmp_path, macro_defaults=macros,
                  notification_defaults=notifications)
    first = build_default_config(**inputs)
    second = build_default_config(**inputs)

    assert first == second
    assert first["key_macros"] == macros
    assert first["notifications"] == notifications
    assert first["smart_actions"]["brain_dump_path"] == str(
        tmp_path / "Documents" / "Samsara Brain Dump.md")
    assert first["smart_actions"]["allowed_directories"] == [str(tmp_path / "Documents")]
    assert set(_containers(first)).isdisjoint(_containers(second))
    assert set(_containers(first)).isdisjoint(_containers([macros, notifications]))
    # 940: 919 ships no personal profiles; retain the list-ownership mutation.
    assert first["wake_profiles"] == second["wake_profiles"] == []
    first["wake_profiles"].append({"id": "synthetic", "enabled": False})
    first["key_macros"]["bindings"][0]["keys"].append("f10")
    first["notifications"]["reminders"].clear()
    assert second["wake_profiles"] == []
    assert (macros, notifications) == before
    assert structural_error(second) is None


@pytest.mark.parametrize("key, expected", [
    ("wake_word_config", "dict"), ("wake_profiles", "list"),
    ("command_mode", "dict"), ("ducking", "dict"),
    ("smart_corrections", "dict"), ("wake_targets", "list"),
    ("ai_command_mode", "dict"),
])
def test_structural_error_rejects_wrong_container(key, expected):
    assert structural_error({key: None}) == (
        f"top-level '{key}' must be {expected} when present, got NoneType")


def test_structural_error_accepts_partial_and_unknown_keys():
    assert structural_error({}) is None
    assert structural_error({"future": [1]}) is None
    assert structural_error([]) == (
        "top-level config must be a JSON object (dict), got list")
