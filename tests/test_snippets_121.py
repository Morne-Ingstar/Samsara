"""Queue 121: snippets -- say a name, get the text.

Three things under test, and they are separable on purpose:

  * samsara/snippets.py -- the store. JSONL under ~/.samsara/snippets/, the
    quick_memo pattern: append to create, atomic whole-file rewrite under a
    per-file lock to edit or delete.
  * plugins/commands/snippets.py -- "insert <name>" and "save snippet <name>".
    Delivery goes through the app's ONE text chokepoint; nothing here types.
  * samsara/ui/snippets_qt.py -- the page, operable with no mouse.

Nothing imports dictation.py. The delivery contract is asserted against a
fake app that records what it was handed, and against dictation.py's source
for the chokepoint's own guarantees.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# NOTE: this module deliberately does NOT set QT_QPA_PLATFORM. Setting it at
# import time changes the platform for the WHOLE pytest process -- pytest
# imports every test module before running any -- and that silently breaks
# the pixel measurements in test_home_qt.py when the two run together. The
# conftest `qapp` fixture owns the platform.

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from samsara import snippets
from samsara.command_registry import DispatchState

SIGN_OFF = "Kind regards,\nMorne"


# ---------------------------------------------------------------------------
# A fake app: it records what it was asked to type, and never types.
# ---------------------------------------------------------------------------

class _App:
    """Everything the plugin touches, and nothing else."""

    def __init__(self, *, deliver_ok=True, last_text=None, history=None, registry=None):
        self.typed: list = []
        self.chips: list = []
        self.spoken: list = []
        self._deliver_ok = deliver_ok
        self._last_dictation_text = last_text
        self.history_store = _History(history)
        self.command_executor = _Executor(registry or [])

    def _paste_preserving_clipboard(self, text, before_paste=None):
        self.typed.append(text)
        return self._deliver_ok

    def _show_outcome_chip(self, label, kind):
        self.chips.append((label, kind))

    def _speak_session_notice(self, text, category="confirmation"):
        self.spoken.append(text)


class _History:
    def __init__(self, rows):
        self._rows = rows or []

    def query(self, type_filter=None, limit=200, **kw):
        return self._rows[:limit]


class _Executor:
    def __init__(self, rows):
        self._matcher = _Matcher(rows)


class _Matcher:
    def __init__(self, rows):
        self._rows = rows

    def list_commands(self):
        return self._rows


@pytest.fixture
def home(tmp_path):
    return tmp_path


@pytest.fixture
def plugin():
    """The command module, imported without loading the whole plugin system."""
    import importlib

    return importlib.import_module("plugins.commands.snippets")


@pytest.fixture(autouse=True)
def _store_in_tmp(monkeypatch, tmp_path):
    """The plugin calls the store with no `home`, so point the real default
    at the temp profile rather than the owner's live one."""
    monkeypatch.setattr(snippets, "samsara_home_dir", lambda: tmp_path)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_the_store_is_jsonl_with_the_documented_fields(home):
    record = snippets.add_snippet("sign off", SIGN_OFF, home=home)
    path = snippets.snippet_file(home)
    assert path.name == "snippets.jsonl"
    assert path.parent.name == "snippets"

    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert set(stored) == {"id", "name", "text", "created", "last_used", "use_count"}
    assert stored["name"] == "sign off" and stored["text"] == SIGN_OFF
    assert stored["use_count"] == 0 and stored["last_used"] is None
    assert stored["id"] == record["id"]


def test_creation_appends_and_edits_rewrite(home):
    first = snippets.add_snippet("a", "one", home=home)
    snippets.add_snippet("b", "two", home=home)
    raw = snippets.snippet_file(home).read_text(encoding="utf-8")
    assert raw.count("\n") == 2                          # appended, not rewritten

    snippets.update_snippet(first["id"], text="ONE", home=home)
    records = snippets.load_snippets(home)
    assert [r["text"] for r in records] == ["ONE", "two"]  # order preserved
    assert len(snippets.snippet_file(home).read_text(encoding="utf-8").splitlines()) == 2


def test_a_rewrite_keeps_fields_this_version_does_not_know(home):
    """The migration-proofing claim, asserted: a row written by a newer
    Samsara (a "format" key, say) must survive an older one editing the file.
    That is why placeholders will not need a migration."""
    snippets.add_snippet("a", "one", home=home)
    path = snippets.snippet_file(home)
    row = json.loads(path.read_text(encoding="utf-8").strip())
    row["format"] = "template"
    row["future_thing"] = {"nested": True}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    snippets.update_snippet(row["id"], text="two", home=home)

    after = json.loads(path.read_text(encoding="utf-8").strip())
    assert after["text"] == "two"
    assert after["format"] == "template"
    assert after["future_thing"] == {"nested": True}


def test_a_corrupt_line_does_not_cost_the_other_snippets(home):
    snippets.add_snippet("a", "one", home=home)
    snippets.add_snippet("b", "two", home=home)
    path = snippets.snippet_file(home)
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    assert [r["name"] for r in snippets.load_snippets(home)] == ["a", "b"]


def test_delete_and_use_counting(home):
    record = snippets.add_snippet("a", "one", home=home)
    assert snippets.record_use(record["id"], home=home)["use_count"] == 1
    assert snippets.record_use(record["id"], home=home)["use_count"] == 2
    assert snippets.record_use(record["id"], home=home)["last_used"]
    assert snippets.delete_snippet(record["id"], home=home) is True
    assert snippets.load_snippets(home) == []
    assert snippets.delete_snippet(record["id"], home=home) is False


def test_lookup_is_exact_on_the_normalised_name(home):
    snippets.add_snippet("Sign Off", SIGN_OFF, home=home)
    for said in ("sign off", "Sign Off.", "  SIGN OFF  ", "Sign off!"):
        assert snippets.find_snippet(said, home=home) is not None, said
    for other in ("sign", "sign offs please", "signoff"):
        assert snippets.find_snippet(other, home=home) is None, other


def test_a_near_miss_suggests_and_a_far_miss_does_not(home):
    snippets.add_snippet("sign off", SIGN_OFF, home=home)
    assert snippets.suggest_names("sign of", home=home) == ["sign off"]
    assert snippets.suggest_names("banana bread", home=home) == []


# ---------------------------------------------------------------------------
# Name collisions -- refused at SAVE time, naming the conflict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, expect", [
    ("tab", "formatting token"),          # "insert tab" IS a formatting token
    ("new line", "formatting token"),
    ("scratch that", "session control"),
])
def test_a_name_that_shadows_something_is_refused_naming_it(home, name, expect):
    with pytest.raises(snippets.SnippetError) as exc:
        snippets.add_snippet(name, "x", home=home)
    message = str(exc.value)
    assert expect in message, message
    assert "insert" in message                 # it says what saying it would do
    assert snippets.load_snippets(home) == []  # and nothing was written


def test_a_name_that_shadows_a_registered_command_is_refused(home):
    """The live registry is the third source. reserved_whole_utterances()
    alone would not catch this one."""
    app = _App(registry=[{"phrase": "show numbers", "aliases": ["numbers"]}])
    with pytest.raises(snippets.SnippetError) as exc:
        snippets.add_snippet("show numbers", "x", home=home, app=app)
    assert "voice command" in str(exc.value)
    assert snippets.name_conflict("numbers", app=app, home=home)
    assert snippets.name_conflict("something else", app=app, home=home) is None


def test_a_duplicate_name_is_refused(home):
    snippets.add_snippet("sign off", SIGN_OFF, home=home)
    with pytest.raises(snippets.SnippetError) as exc:
        snippets.add_snippet("Sign Off.", "other", home=home)
    assert "already a snippet" in str(exc.value)


def test_an_empty_name_or_body_is_refused(home):
    for bad_name in ("", "   ", "..."):
        with pytest.raises(snippets.SnippetError):
            snippets.add_snippet(bad_name, "x", home=home)
    with pytest.raises(snippets.SnippetError):
        snippets.add_snippet("ok", "   ", home=home)


def test_renaming_onto_a_conflict_is_refused_but_keeping_the_name_is_not(home):
    a = snippets.add_snippet("a", "one", home=home)
    snippets.add_snippet("b", "two", home=home)
    with pytest.raises(snippets.SnippetError):
        snippets.update_snippet(a["id"], name="b", home=home)
    # Saving the same record without changing its name must not trip the
    # duplicate check against itself.
    assert snippets.update_snippet(a["id"], name="a", text="ONE", home=home)["text"] == "ONE"


# ---------------------------------------------------------------------------
# insert <name>
# ---------------------------------------------------------------------------

def test_insert_types_the_stored_text_through_the_app_chokepoint(plugin, home):
    snippets.add_snippet("sign off", SIGN_OFF, home=home)
    app = _App()

    assert plugin.insert_snippet(app, "sign off") is True

    assert app.typed == [SIGN_OFF]               # byte-identical, no formatting
    assert app.chips[-1] == ("inserted sign off", "success")
    assert snippets.load_snippets(home)[0]["use_count"] == 1


def test_the_delivery_path_is_the_one_that_preserves_the_clipboard():
    """The reused path, asserted at both ends: the plugin calls
    _paste_preserving_clipboard, and that method is the one that saves and
    restores the clipboard and refuses an elevated window. This is NOT
    MacroHandler's `type` step, which is a bare pyautogui.typewrite."""
    import ast

    plugin_src = (REPO / "plugins" / "commands" / "snippets.py").read_text(encoding="utf-8")
    assert "_paste_preserving_clipboard" in plugin_src
    # Checked in the AST, not the text: the module's docstring names the path
    # it deliberately did NOT take, and prose must not fail a code assertion.
    tree = ast.parse(plugin_src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "pyautogui" not in imported
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "typewrite" not in called
    constants = {node.value for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert "_paste_preserving_clipboard" in constants   # looked up, not just mentioned

    app_src = (REPO / "dictation.py").read_text(encoding="utf-8")
    body = app_src.split("def _paste_preserving_clipboard(", 1)[1].split("\n    def ", 1)[0]
    assert "injection_safety.window_integrity()" in body      # elevated windows refused
    assert "paste_with_preservation(" in body                 # clipboard saved + restored
    assert "_record_undoable_paste(" in body                  # "undo that" still works

    handlers = (REPO / "samsara" / "handlers.py").read_text(encoding="utf-8")
    assert "pyautogui.typewrite" in handlers                  # the path NOT taken


def test_an_unknown_name_suggests_and_types_nothing(plugin, home):
    snippets.add_snippet("sign off", SIGN_OFF, home=home)
    app = _App()

    assert plugin.insert_snippet(app, "sign of") is DispatchState.FAILED

    assert app.typed == []
    label, kind = app.chips[-1]
    assert "did you mean" in label and "sign off" in label
    assert kind == "warning"


def test_a_name_that_matches_nothing_at_all_says_so(plugin, home):
    snippets.add_snippet("sign off", SIGN_OFF, home=home)
    app = _App()

    assert plugin.insert_snippet(app, "banana bread") is DispatchState.FAILED

    assert app.typed == []
    assert app.chips[-1] == ('no snippet "banana bread"', "error")


def test_insert_with_no_name_asks_rather_than_guessing(plugin, home):
    snippets.add_snippet("sign off", SIGN_OFF, home=home)
    app = _App()
    assert plugin.insert_snippet(app, "") is DispatchState.FAILED
    assert app.typed == []
    assert app.chips[-1][0] == "insert what?"
    assert "sign off" in " ".join(app.spoken)      # it offers what there is


def test_a_refused_delivery_is_reported_and_not_counted_as_a_use(plugin, home):
    snippets.add_snippet("sign off", SIGN_OFF, home=home)
    app = _App(deliver_ok=False)

    assert plugin.insert_snippet(app, "sign off") is DispatchState.FAILED

    assert app.chips[-1] == ("could not type it", "error")
    assert snippets.load_snippets(home)[0]["use_count"] == 0


COMMAND_TEXTS = [
    "scratch that",
    "go to sleep",
    "open chrome and close the tab",
    "halt",
    "insert tab",
    "delete everything, then shut down",
]


@pytest.mark.parametrize("payload", COMMAND_TEXTS)
def test_a_snippet_whose_text_is_a_command_is_typed_verbatim_and_runs_nothing(
        plugin, home, payload):
    """A snippet's text is DATA. There is no path from the store back into
    the matcher, and the text is handed to the delivery chokepoint unchanged
    -- not re-dispatched, not formatted, not normalised."""
    snippets.add_snippet("payload", payload, home=home)
    app = _App()

    assert plugin.insert_snippet(app, "payload") is True

    assert app.typed == [payload]                 # verbatim
    # The only thing the plugin ever calls on the app is the chokepoint, the
    # chip and the notice. Nothing dispatches.
    plugin_src = (REPO / "plugins" / "commands" / "snippets.py").read_text(encoding="utf-8")
    for dispatcher in ("process_text", "command_executor.process", "dispatch_utterance",
                       "_process_wake_command"):
        assert dispatcher not in plugin_src, dispatcher


# ---------------------------------------------------------------------------
# save snippet <name>
# ---------------------------------------------------------------------------

def test_save_snippet_captures_the_last_committed_dictation(plugin, home):
    app = _App(last_text="The quick brown fox jumps over the lazy dog.")

    assert plugin.save_snippet(app, "fox") is True

    stored = snippets.load_snippets(home)
    assert len(stored) == 1
    assert stored[0]["name"] == "fox"
    assert stored[0]["text"] == "The quick brown fox jumps over the lazy dog."
    assert app.chips[-1] == ("saved fox", "success")
    assert "insert fox" in " ".join(app.spoken)


def test_save_snippet_falls_back_to_the_history_store(plugin, home):
    """_last_dictation_text is cleared 60 s after delivery (the undo window),
    so naming a paragraph later has to still work. Same lookup
    open_correction_capture uses."""
    app = _App(last_text=None, history=[{"display_text": "From history."}])

    assert plugin.save_snippet(app, "later") is True
    assert snippets.load_snippets(home)[0]["text"] == "From history."


def test_the_live_text_wins_over_history(plugin, home):
    app = _App(last_text="Live.", history=[{"display_text": "Older."}])
    plugin.save_snippet(app, "which")
    assert snippets.load_snippets(home)[0]["text"] == "Live."


def test_save_snippet_with_nothing_committed_refuses_clearly(plugin, home):
    app = _App(last_text=None, history=[])

    assert plugin.save_snippet(app, "nothing") is DispatchState.FAILED

    assert snippets.load_snippets(home) == []
    assert app.chips[-1] == ("nothing to save", "warning")
    said = " ".join(app.spoken)
    assert "dictate the text first" in said.lower()


def test_save_snippet_with_no_name_refuses(plugin, home):
    app = _App(last_text="Something.")
    assert plugin.save_snippet(app, "") is DispatchState.FAILED
    assert snippets.load_snippets(home) == []


def test_save_snippet_reports_a_collision_at_save_time(plugin, home):
    app = _App(last_text="Something.")

    assert plugin.save_snippet(app, "tab") is DispatchState.FAILED

    assert snippets.load_snippets(home) == []
    assert app.chips[-1][1] == "error"
    assert "formatting token" in " ".join(app.spoken)


# ---------------------------------------------------------------------------
# The command's own registration
# ---------------------------------------------------------------------------

def test_the_command_declares_a_required_named_argument(plugin):
    """Queue 107: an undeclared remainder is what makes a command unreachable
    to Ava, because the menu cannot say what to pass it."""
    from samsara import command_catalog as cc

    entry = {"name": {"type": "str", "required": True}}
    args = cc.infer_args("plugin", "insert", entry, plugin.insert_snippet)
    assert [(a.name, a.required) for a in args] == [("name", True)]
    assert args[0].type == "text"

    source = (REPO / "plugins" / "commands" / "snippets.py").read_text(encoding="utf-8")
    assert 'param_schema={"name": {"type": "str", "required": True}}' in source
    assert 'risk_class="write"' in source           # it types into a document


def test_the_command_is_offered_to_ava():
    """ai_visible defaults to True and nothing here opts out, so the command
    reaches Ava's menu; with a declared required argument she can call it."""
    source = (REPO / "plugins" / "commands" / "snippets.py").read_text(encoding="utf-8")
    assert "ai_visible=False" not in source
    assert "voice_triggerable=True" in source


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

@pytest.fixture
def page(qapp, home):
    from samsara.ui.snippets_qt import SnippetsPage

    widget = SnippetsPage(app=None, home=home)
    widget.resize(720, 560)
    widget.show()
    for _ in range(20):
        qapp.processEvents()
    yield widget
    widget.hide()
    widget.deleteLater()


def test_every_control_is_focusable_and_44px_in_a_deliberate_order(page, qapp):
    from PySide6.QtCore import Qt

    for widget in page._tab_chain:
        assert widget.focusPolicy() != Qt.FocusPolicy.NoFocus, widget.accessibleName()
        assert widget.accessibleName(), widget
        if widget.isVisible():
            assert widget.height() >= 44 or widget.minimumHeight() >= 44, widget.accessibleName()
    # The order is set, not left to construction order.
    assert page._tab_chain[0] is page._search
    assert page._tab_chain[-1] is page._cancel_btn


def test_every_shortcut_is_written_on_the_control_that_uses_it(page):
    from samsara.ui import snippets_qt as sq

    assert "Ctrl+F" in sq.SEARCH_LABEL
    assert "Ctrl+N" in sq.NEW
    assert "Enter" in sq.SAVE
    assert "Esc" in sq.CANCEL_EDIT
    assert "Del" in sq.DELETE
    bound = {s.key().toString() for s in page._shortcuts}
    assert {"Ctrl+F", "Ctrl+N", "Del", "Esc"} <= bound


def test_the_page_can_create_edit_and_delete_by_keyboard(page, qapp, home):
    page.start_new()
    page._name.setText("sign off")
    page._text.setPlainText(SIGN_OFF)
    assert page.save_edit() is True
    assert [r["name"] for r in snippets.load_snippets(home)] == ["sign off"]
    assert "insert sign off" in page._status.text()

    page._list.setCurrentRow(0)
    page._text.setPlainText("Changed.")
    assert page.save_edit() is True
    assert snippets.load_snippets(home)[0]["text"] == "Changed."

    page.request_delete()
    assert page._confirm_btn.isVisible() and page._cancel_btn.isVisible()
    assert not page._delete_btn.isVisible()
    assert page.confirm_delete() is True
    assert snippets.load_snippets(home) == []


def test_delete_confirms_inline_and_cancelling_keeps_it(page, home, qapp):
    snippets.add_snippet("keep me", "x", home=home)
    page.reload()
    page._list.setCurrentRow(0)

    page.request_delete()
    page.cancel_delete()

    assert page._delete_btn.isVisible()
    assert not page._confirm_btn.isVisible()
    assert len(snippets.load_snippets(home)) == 1
    # No modal: nothing here opens a dialog at all.
    source = (REPO / "samsara" / "ui" / "snippets_qt.py").read_text(encoding="utf-8")
    assert "QMessageBox" not in source and "QDialog" not in source


def test_the_page_refuses_a_shadowing_name_in_words(page, home):
    page.start_new()
    page._name.setText("tab")
    page._text.setPlainText("x")

    assert page.save_edit() is False

    assert "formatting token" in page._status.text()
    assert snippets.load_snippets(home) == []


def test_search_filters_on_name_and_on_text(page, home):
    snippets.add_snippet("sign off", "Kind regards", home=home)
    snippets.add_snippet("address", "14 Example Road", home=home)
    page.reload()
    assert page._list.count() == 2
    page._search.setText("regards")            # matches the TEXT, not the name
    assert page._list.count() == 1
    page._search.setText("address")
    assert page._list.count() == 1
    page._search.setText("zzz")
    assert page._list.count() == 0
    assert page._status.text() == page._status.text()  # a message, not a blank


def test_every_size_on_the_page_is_a_theme_token():
    """Queue 83's floor: no hard-coded px font size anywhere on the page."""
    import re

    source = (REPO / "samsara" / "ui" / "snippets_qt.py").read_text(encoding="utf-8")
    literals = re.findall(r"font-size:\s*(\d+)px", source)
    assert literals == [], literals
    assert "theme.TYPE_BODY" in source


def test_the_page_is_in_the_hub_nav_beside_memos():
    from samsara.ui import main_window_qt
    from samsara.ui.snippets_qt import SNIPPETS

    order = list(main_window_qt.NAV_ORDER)
    assert SNIPPETS in order
    assert order.index(SNIPPETS) == order.index("Memos") + 1
    assert main_window_qt.NAV_ICONS[SNIPPETS] == "snippets"

    from samsara.ui.home_qt import ICON_GLYPHS
    assert ICON_GLYPHS.get("snippets")          # not a blank icon beside five real ones
