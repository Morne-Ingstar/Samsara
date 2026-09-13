"""Guidance audit 2026-09-13 row 142: the cheat sheet says which packs are
off. Pure helpers, no widgets."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.command_packs import PACKS, get_enabled_packs  # noqa: E402
from samsara.ui import command_cheatsheet_qt as cs  # noqa: E402


def test_disabled_packs_follow_the_config():
    cfg = {"command_packs": {pid: False for pid in PACKS}}
    disabled = cs._disabled_packs(cfg)
    assert disabled == set(PACKS) - get_enabled_packs(cfg)
    assert "core" not in disabled                     # always_on packs never show as off
    assert cs._disabled_packs({"command_packs": {}}) == set(PACKS) - get_enabled_packs({"command_packs": {}})


def test_rows_are_annotated_without_mutating_the_source():
    rows = [{"phrase": "x", "pack": "macros"}, {"phrase": "y", "pack": "core"}]
    out = cs._annotate_disabled(rows, {"macros"})
    assert [r["pack_disabled"] for r in out] == [True, False]
    assert "pack_disabled" not in rows[0]
