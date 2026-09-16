"""Hostile proposal regression tests for queue 150."""

from samsara.ai_capability import get_capability_snapshot, validate_proposal
from samsara.execution_policy import DEFAULT_MAX_STR_LEN


def _snapshot():
    return {
        "commands": {
            "set count": {
                "param_schema": {
                    "count": {"type": "int", "required": True, "min": 0},
                },
            },
            "name thing": {
                "param_schema": {
                    "name": {"type": "str", "required": True},
                },
            },
        },
        "all_action_ids": ["set count", "name thing"],
    }


def _proposal(action_id, params):
    return {"steps": [{"action_id": action_id, "params": params}]}


def test_int_param_rejects_boolean():
    result = validate_proposal(_proposal("set count", {"count": True}), _snapshot())
    assert result["valid"] is False
    assert any("must be an int" in error for error in result["errors"])


def test_int_param_rejects_fraction_and_numeric_string():
    for value in (7.5, "7"):
        result = validate_proposal(_proposal("set count", {"count": value}), _snapshot())
        assert result["valid"] is False
        assert any("must be an int" in error for error in result["errors"])


def test_undeclared_param_is_rejected():
    result = validate_proposal(
        _proposal("set count", {"count": 7, "unexpected": "value"}), _snapshot())
    assert result["valid"] is False
    assert any("undeclared parameters" in error for error in result["errors"])


def test_str_param_rejects_non_string_and_oversized_string():
    for value in ({"nested": "object"}, "x" * (DEFAULT_MAX_STR_LEN + 1)):
        result = validate_proposal(_proposal("name thing", {"name": value}), _snapshot())
        assert result["valid"] is False
    assert "max length" in validate_proposal(
        _proposal("name thing", {"name": "x" * (DEFAULT_MAX_STR_LEN + 1)}), _snapshot()
    )["errors"][0]


def test_list_action_id_returns_invalid_result_instead_of_raising():
    result = validate_proposal(_proposal([], {}), _snapshot())
    assert result == {
        "valid": False,
        "errors": ["step 0: 'action_id' must be a string, got list"],
    }


def test_param_schema_edit_changes_snapshot_version():
    class Matcher:
        def __init__(self, schema):
            self.schema = schema

        def list_commands(self):
            return [{
                "phrase": "set count",
                "ai_visible": True,
                "ai_composable": True,
                "risk_class": "safe",
                "side_effects": [],
                "param_schema": self.schema,
            }]

    before = get_capability_snapshot(Matcher({"count": {"type": "int", "min": 0}}))
    after = get_capability_snapshot(Matcher({"count": {"type": "int", "min": 1}}))
    assert before["version"] != after["version"]
