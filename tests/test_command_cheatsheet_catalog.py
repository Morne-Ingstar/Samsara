"""Queue 15: the guidance surfaces are views over the command catalog.

The command cheat sheet, the quick reference and the tutorial must never carry
a hand-written command phrase; their command rows and examples come from
samsara.command_catalog at runtime (built from the LIVE registry rows, with
commands_catalog.json as the fallback), and every surface degrades to an
honest "unavailable" line when no catalog exists.
"""
import io
import json
import sys
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import command_catalog as cc  # noqa: E402
from samsara.ui import command_cheatsheet_qt as cs  # noqa: E402

SURFACES = (
    "samsara/ui/command_cheatsheet_qt.py",
    "samsara/ui/quick_reference_qt.py",
    "samsara/ui/tutorial_qt.py",
)
#: One-word strings that coincide with a catalog alias but are NOT spoken
#: command phrases in these files. Each is justified; multi-word phrases have
#: no allowlist at all.
NOT_COMMAND_PHRASES = {
    "next": "tutorial navigation button label",
    "ava": "section / row / mode NAME in the quick reference, not an utterance",
    "escape": "cancel_hotkey fallback KEY name (pynput key), not an utterance",
    "silence": "continuous_commit_trigger enum VALUE, not an utterance",
    "refresh": "quick reference button label",
}


@pytest.fixture(scope="module")
def catalog():
    records = cc.load_catalog_json()
    assert records, "commands_catalog.json must load for these tests"
    return records


@pytest.fixture(scope="module")
def live_rows():
    from tools.dump_command_metadata import build_executor
    return build_executor()._matcher.list_commands()


def _string_literals(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.STRING:
            yield tok.start[0], tok.string


@pytest.mark.parametrize("path", SURFACES)
def test_no_surface_contains_a_literal_command_phrase(catalog, path):
    aliases = {a for record in catalog for a in record["aliases"]}
    hits = []
    for line, literal in _string_literals(path):
        low = literal.lower()
        bare = low.lstrip("rbuf").strip("'\"").strip()
        for alias in aliases:
            quoted = f"'{alias}'" in low or f'"{alias}"' in low
            if (bare == alias or quoted) and alias not in NOT_COMMAND_PHRASES:
                hits.append((line, alias))
    assert hits == []


def test_allowlist_is_single_words_only():
    assert all(" " not in word for word in NOT_COMMAND_PHRASES)


# ---------------------------------------------------------------------------
# The catalog view itself
# ---------------------------------------------------------------------------

def test_live_rows_build_exactly_the_checked_in_catalog(catalog, live_rows):
    live = {r["canonical_id"]: r for r in cc.catalog_from_registry_rows(live_rows)}
    frozen = {r["canonical_id"]: r for r in catalog}
    assert set(live) == set(frozen)
    for cid, record in live.items():
        for key in ("aliases", "risk", "whole_utterance", "plugin", "pack", "kind"):
            assert record[key] == frozen[cid][key], (cid, key)
        assert [a for a in record["args"] if a["required"]] == \
            [a for a in frozen[cid]["args"] if a["required"]], cid


def test_guidance_catalog_prefers_live_rows_and_falls_back_to_json(catalog, live_rows, tmp_path):
    assert {r["canonical_id"] for r in cc.guidance_catalog(live_rows)} == {r["canonical_id"] for r in catalog}
    assert cc.guidance_catalog(None) == catalog
    assert cc.guidance_catalog([]) == catalog
    missing = tmp_path / "nope.json"
    assert cc.guidance_catalog(None, catalog_path=missing) is None
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert cc.guidance_catalog(None, catalog_path=corrupt) is None
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"version": 1, "commands": [{"canonical_id": "x"}]}), encoding="utf-8")
    assert cc.guidance_catalog(None, catalog_path=invalid) is None


def test_canonical_phrase_and_display_aliases(catalog):
    by_id = {r["canonical_id"]: r for r in catalog}
    for record in catalog:
        phrase = cc.canonical_phrase(record)
        assert phrase in record["aliases"], record["canonical_id"]
        shown = cc.display_aliases(record)
        assert len(shown) <= 3 and phrase not in shown and set(shown) <= set(record["aliases"])
    assert by_id  # non-empty


# ---------------------------------------------------------------------------
# Cheat sheet
# ---------------------------------------------------------------------------

def test_every_cheatsheet_row_resolves_to_a_real_canonical_id(catalog, live_rows):
    ids = {r["canonical_id"] for r in catalog}
    rows = cs.catalog_rows(live_rows)
    assert len(rows) == len(catalog)
    for row in rows:
        assert row["canonical_id"] in ids
        assert row["phrase"] in row["aliases"]
        assert len(row["shown_aliases"]) <= 3


def test_destructive_and_whole_utterance_are_labelled(live_rows):
    rows = cs.catalog_rows(live_rows)
    destructive = [r for r in rows if r["risk"] == "destructive"]
    whole = [r for r in rows if r["whole_utterance"]]
    assert destructive and whole
    assert all("destructive" in cs.row_text(r) for r in destructive)
    assert all("whole utterance" in cs.row_text(r) for r in whole)
    assert not any("destructive" in cs.row_text(r) for r in rows if r["risk"] != "destructive")


def test_window_groups_by_plugin_and_every_item_is_a_catalog_command(qapp, live_rows, tmp_path, catalog):
    from PySide6.QtCore import Qt

    win = cs._CheatSheetWindow(lambda p: None, lambda: live_rows, tmp_path / "palette.json")
    try:
        ids = {r["canonical_id"] for r in catalog}
        assert win._catalog_available and not win._unavailable.isVisibleTo(win)
        # Queue 68: "Live here only" is on by default, so scoped commands that
        # are not live (the window-cube numbers while the cube is hidden) are
        # hidden and counted; unticking lists every catalog command.
        win._category_bar._live_only.setChecked(False)
        items = [win._list.item(i) for i in range(win._list.count())]
        assert len(items) == len(catalog)
        assert all(item.data(Qt.ItemDataRole.UserRole + 1) in ids for item in items)
        groups = win._category_bar._pack_ids[1:]
        assert set(groups) == {r["plugin"] for r in catalog}
        some_plugin = groups[0]
        win._set_category(some_plugin)
        shown = {win._list.item(i).data(Qt.ItemDataRole.UserRole + 1) for i in range(win._list.count())}
        assert shown and all(cid.startswith(some_plugin + ".") for cid in shown)
    finally:
        win.deleteLater()


def test_cheatsheet_degrades_honestly_without_a_catalog(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "load_catalog_json", lambda path=None: None)
    win = cs._CheatSheetWindow(lambda p: None, lambda: [], tmp_path / "palette.json")
    try:
        assert win._catalog_available is False
        assert win._unavailable.isVisibleTo(win) and not win._list.isVisibleTo(win)
        assert "unavailable" in win._unavailable.text() and 'href="' in win._unavailable.text()
        assert win._list.count() == 0
    finally:
        win.deleteLater()


def test_cheatsheet_degrades_when_the_registry_callback_raises(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "load_catalog_json", lambda path=None: None)

    def boom():
        raise RuntimeError("registry not ready")

    win = cs._CheatSheetWindow(lambda p: None, boom, tmp_path / "palette.json")
    try:
        assert win._catalog_available is False
    finally:
        win.deleteLater()
