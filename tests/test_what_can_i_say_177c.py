"""Focused-app command-reference coverage for queue 177c."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from samsara import command_catalog  # noqa: E402
from samsara.command_packs import EXE_TO_PACK, PACKS, pack_for_exe  # noqa: E402
from samsara.ui import command_cheatsheet_qt as cheatsheet  # noqa: E402
from samsara.ui.command_marquee import EXAMPLE_ALLOWED_PACKS  # noqa: E402


def test_what_can_i_say_resolves_in_catalog():
    records = command_catalog.load_catalog_json()
    assert records is not None
    assert any("what can i say" in record["aliases"] for record in records)


def test_pack_for_exe_is_explicit_and_safe_for_unknown_apps():
    assert pack_for_exe("chrome.exe") == "browsers"
    assert pack_for_exe("not-an-app.exe") is None
    assert pack_for_exe(None) is None
    assert set(EXE_TO_PACK.values()) <= set(PACKS)


def test_scoped_window_has_only_focused_and_universal_packs_and_resets(qapp, tmp_path):
    window = cheatsheet._CheatSheetWindow(
        lambda phrase: None, lambda: [], tmp_path / "palette.json"
    )
    try:
        unscoped_ids = {row["canonical_id"] for row in window._all}
        assert unscoped_ids

        allowed = set(EXAMPLE_ALLOWED_PACKS) | {"browsers"}
        window.set_pack_scope(allowed)
        scoped_ids = {row["canonical_id"] for row in window._all}
        assert scoped_ids
        assert all(row["pack"] in allowed for row in window._all)
        assert any(row["pack"] == "browsers" for row in window._all)

        window.set_pack_scope(None)
        assert {row["canonical_id"] for row in window._all} == unscoped_ids
    finally:
        window.deleteLater()


def test_handler_posts_focused_scope_and_speaks_feedback(monkeypatch):
    from plugins.commands import core_utils
    from samsara import handlers
    from samsara.ui import qt_runtime

    sheet = Mock()
    audio = Mock()
    app = SimpleNamespace(cheat_sheet=sheet, audio_coordinator=audio)
    monkeypatch.setattr(handlers, "_get_foreground_exe_lower", lambda: "chrome.exe")
    monkeypatch.setattr(qt_runtime, "post", lambda callback: callback())

    assert core_utils.what_can_i_say(app) is True
    sheet.show_scoped.assert_called_once_with(set(EXAMPLE_ALLOWED_PACKS) | {"browsers"})
    assert audio.speak.call_args.args[0] == "Showing what you can say here."
