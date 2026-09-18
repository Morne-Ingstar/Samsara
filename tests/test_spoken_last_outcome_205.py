"""Prompt 205: the last outcome is available as speech and a chip."""

import time
import types
from collections import deque

import pytest

from samsara import diagnostics, outcome_ring
from samsara.intent import resolve as resolve_module
from samsara import plugin_commands
from plugins.commands import core_utils


class _Speaker:
    def __init__(self):
        self.calls = []

    def speak(self, text, **kwargs):
        self.calls.append((text, kwargs))


def _app(record):
    app = types.SimpleNamespace(
        _outcome_ring=deque([record], maxlen=8),
        audio_coordinator=_Speaker(),
        chips=[],
    )

    def show(label, kind, ttl="default", **kwargs):
        app.chips.append((label, kind, ttl, kwargs))

    app._show_outcome_chip = show
    return app


def _record(label, kind="success", source="", canonical_id=""):
    return outcome_ring.record(label, kind, time.time() + 1.0, source, canonical_id)


@pytest.fixture(autouse=True)
def _clear_diagnostics():
    diagnostics.clear()
    yield
    diagnostics.clear()


def _heard(text="show windows"):
    diagnostics.record(diagnostics.DiagRecord(
        mode="command", audio_s=1.0, model_name="small", device="cpu",
        compute_type="int8", text=text,
    ))


def test_command_is_registered_in_catalog_path_and_never_model_composable():
    plugin_commands._reinstall_module_commands(core_utils)
    entry = plugin_commands._REGISTRY["why didn't that work"]
    assert entry["func"] is core_utils.explain_last_outcome
    assert entry["ai_visible"] is False
    assert entry["ai_composable"] is False
    assert {"what just happened", "what did you hear"} <= set(entry["aliases"])

    rows = [{
        "phrase": entry["phrase"], "aliases": entry["aliases"],
        "source": "plugin", "pack": entry["pack"],
        "description": entry["func"].__doc__ or "last outcome",
    }]
    from samsara import command_catalog
    records = command_catalog.catalog_from_registry_rows(rows)
    assert records[0]["phrase"] == "why didnt that work"


@pytest.mark.parametrize("record, expected", [
    (_record("typed"), "typed"),
    (_record("✓ show windows", source="command_executed",
             canonical_id="builtin.show_windows"), "ran show windows"),
    (_record("refused: focus lock", kind="warning", source="command_failed"),
     "refused: focus lock"),
    (_record("Ava: offline", kind="warning"), "Ava: offline"),
])
def test_each_standard_outcome_is_spoken_and_shown(record, expected):
    _heard("show windows")
    app = _app(record)
    assert core_utils.explain_last_outcome(app) is True
    assert expected in app.audio_coordinator.calls[0][0]
    assert expected in app.chips[0][0]


def test_miss_speaks_nearest_similarity_command(monkeypatch):
    class FakeResolver:
        by_id = {"builtin.show_windows": {"phrase": "show windows"}}

        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, heard):
            assert heard == "show windos"
            return types.SimpleNamespace(
                tier="similarity", suggestions=("builtin.show_windows",),
                canonical_id="builtin.show_windows",
            )

    monkeypatch.setattr(resolve_module, "IntentResolver", FakeResolver)
    _heard("show windos")
    app = _app(_record("MISS", kind="error", source="command_miss"))
    core_utils.explain_last_outcome(app)
    spoken = app.audio_coordinator.calls[0][0]
    assert 'Heard "show windos"' in spoken
    assert "MISS" in spoken
    assert "Nearest command at similarity tier: show windows" in spoken
    assert app.chips[0][0] == spoken


def test_report_chip_does_not_become_the_next_outcome():
    record = _record("typed")
    app = _app(record)
    app._outcome_ring.append(_record(
        "Heard \"old\". typed.", kind="accent",
        source=core_utils._LAST_OUTCOME_REPORT_SOURCE,
    ))
    assert core_utils._last_outcome_record(app) == record
