"""Queue 15: every tutorial "try saying ..." example comes from the command
catalog -- read-only first, never destructive -- and the tutorial degrades to
an honest line with a Help link when no catalog is available."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import command_catalog as cc  # noqa: E402
from samsara.command_packs import PACKS  # noqa: E402
from samsara.ui import tutorial_qt as tut  # noqa: E402


def _page_labels(window):
    """Every QLabel on every tutorial page (pages other than the current step
    are not parented to the window, so window.findChildren misses them)."""
    from PySide6.QtWidgets import QLabel
    return [lbl for page in window._pages for lbl in page.findChildren(QLabel)]


@pytest.fixture(scope="module")
def catalog():
    records = cc.load_catalog_json()
    assert records
    return records


@pytest.mark.parametrize("config", [
    {},                                                       # default packs
    {"command_packs": {pid: True for pid in PACKS}},          # everything on
])
def test_examples_are_real_never_destructive_and_prefer_read(catalog, config):
    by_phrase = {cc.canonical_phrase(r): r for r in catalog}
    examples = tut._command_examples(catalog, config)
    assert examples
    for phrase in examples:
        record = by_phrase[phrase]
        assert record["risk"] != "destructive"
        assert not record["whole_utterance"]
        assert not any(a["required"] for a in record["args"])
    reads = [r for r in catalog if r["risk"] == "read"]
    assert by_phrase[examples[0]]["risk"] == "read" if reads else True


def test_no_destructive_command_is_ever_picked_even_when_it_is_all_there_is(catalog):
    destructive = [r for r in catalog if r["risk"] == "destructive"]
    assert destructive
    assert cc.pick_examples(destructive, 10) == []
    assert cc.pick_examples(catalog, len(catalog)) and all(
        r["risk"] != "destructive" for r in cc.pick_examples(catalog, len(catalog)))


def test_disabled_packs_are_not_suggested(catalog):
    picked = cc.pick_examples(catalog, 50, enabled_packs={"core"})
    assert picked and all(r["pack"] == "core" for r in picked)


def test_instruction_and_hint_quote_the_examples(catalog):
    examples = tut._command_examples(catalog, {})
    text = tut._command_instruction(examples, "ctrl+alt+c", "jarvis", True)
    assert f'"{examples[0]}"' in text and f'"jarvis, {examples[0]}"' in text
    assert all(p in tut._command_hint(examples) for p in examples)


def test_done_page_names_commands_only_through_the_catalog(catalog):
    more = tut._more_text(catalog)
    help_phrase = tut._catalog_phrase(catalog, tut._HELP_COMMAND_ID)
    assert help_phrase and f'"{help_phrase}"' in more
    numbers = tut._catalog_phrase(catalog, tut._NUMBERS_COMMAND_ID)
    assert numbers and f'"{numbers}"' in tut._pointer_text(catalog)
    # A vanished id drops the sentence instead of naming a dead command.
    assert '"' not in tut._more_text([]) and '"' not in tut._pointer_text([])


def test_degraded_text_is_honest_and_links_to_help():
    assert tut._command_examples(None, {}) is None
    for text in (tut._command_instruction(None, "ctrl+alt+c", "jarvis", False), tut._command_hint(None)):
        assert "unavailable" in text and f'href="{tut.HELP_LINK}"' in text


def test_tutorial_window_degrades_without_a_catalog(qapp, monkeypatch):
    from PySide6.QtWidgets import QLabel

    monkeypatch.setattr(cc, "load_catalog_json", lambda path=None: None)
    opened = []
    monkeypatch.setattr(tut, "open_support_tab", lambda app: opened.append(app))
    app = types.SimpleNamespace(config={})
    window = tut.TutorialWindow(app)
    try:
        labels = [lbl for lbl in _page_labels(window) if tut.HELP_LINK in lbl.text()]
        assert labels, "the command step must say examples are unavailable, with a Help link"
        labels[0].linkActivated.emit(tut.HELP_LINK)
        assert opened == [app]
    finally:
        window.close()
        window.deleteLater()


def test_tutorial_window_uses_live_registry_examples(qapp):
    from tools.dump_command_metadata import build_executor
    from PySide6.QtWidgets import QLabel

    executor = build_executor()
    app = types.SimpleNamespace(config={}, command_executor=executor)
    window = tut.TutorialWindow(app)
    try:
        records = cc.catalog_from_registry_rows(executor._matcher.list_commands())
        expected = tut._command_examples(records, {})
        text = " ".join(lbl.text() for lbl in _page_labels(window))
        assert expected and all(f"'{p}'" in text or f'"{p}"' in text for p in expected)
    finally:
        window.close()
        window.deleteLater()
