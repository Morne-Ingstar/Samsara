"""Queue 115: every command explains itself in one plain sentence.

Queue 111 found the catalog's descriptions could not carry a tooltip: 77 of
the 312 safe records just restated the phrase ("Copy" -> "Copy"), and some
ran to 300-character engineering notes written for whoever was going to
maintain the handler. The reference shows this text, and the Home example
strip is meant to show it as a tooltip, so it is read by -- and read ALOUD
to -- someone who cannot see the screen.

These tests hold the standard the owner set:

  * one sentence, saying what the command does from the user's side;
  * never a restatement of its own phrase;
  * under 140 characters, so it fits a tooltip;
  * no module name, file path, function name or config key.

The catalog is GENERATED (tools/gen_command_catalog.py). A plugin's
description is the first sentence of its handler's docstring
(samsara/command_catalog.py:403); a built-in's is the "description" field in
commands.json (command_registry.py:438). So a failure here is fixed in the
handler or in commands.json, never in commands_catalog.json.
"""
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CATALOG = REPO / "commands_catalog.json"
COMMANDS_JSON = REPO / "commands.json"

MAX_LEN = 140
#: The brief's soft target. Not a hard gate -- a handful of commands genuinely
#: need a clause -- but a regression past it is worth seeing.
SOFT_LEN = 90

#: What "engineering language" means, as a pattern list, so a failure says
#: which rule it broke rather than only that it broke one.
ENGINEERING = [
    (re.compile(r"\b\w+\.py\b"), "a .py file name"),
    (re.compile(r"\bsamsara\.\w+"), "a samsara module path"),
    (re.compile(r"\bplugins?\.\w+"), "a plugins module path"),
    (re.compile(r"\b\w+_\w+\("), "a function call"),
    (re.compile(r"\b[a-z]+(?:_[a-z]+)+\b"), "a snake_case identifier"),
    (re.compile(r"\bqueue \d+\b", re.I), "a queue number"),
    (re.compile(r"\bWin32\b|\bUIA\b|\bHWND\b|\bSendInput\b|\bWM_\w+"), "a Win32 name"),
    (re.compile(r"\bctypes\b|\bpyautogui\b|\bPySide6\b|\bQt\b"), "a library name"),
    (re.compile(r"-->|\s->\s"), "an arrow used as prose"),
    (re.compile(r"\bconfig\[|\bconfig\.\w+"), "a config key"),
    (re.compile(r"\bTODO\b|\bFIXME\b|\bXXX\b"), "a code marker"),
]


def _records():
    doc = json.loads(CATALOG.read_text(encoding="utf-8"))
    return doc["commands"] if isinstance(doc, dict) else doc


RECORDS = _records()
IDS = [r["canonical_id"] for r in RECORDS]


@pytest.fixture(scope="module")
def live_specs():
    # Collection of other files can register synthetic commands before this
    # module fixture runs. Discover only repository plugins, then restore them.
    from samsara import plugin_commands
    from tools.gen_command_catalog import generate
    with pytest.MonkeyPatch.context() as clean:
        clean.setattr(plugin_commands, '_REGISTRY', {})
        clean.setattr(plugin_commands, '_shared_matcher', None)
        return generate()[0]


@pytest.mark.parametrize('failure', [ImportError, TypeError])
def test_capture_registration_survives_optional_wmi_import_failure(monkeypatch, failure):
    """Capture's optional COM backend must not remove its voice commands."""
    import builtins
    import importlib
    import threading
    import types
    from samsara import plugin_commands

    name = 'plugins.commands.workflow_capture'
    parent = importlib.import_module('plugins.commands')
    monkeypatch.setattr(parent, 'workflow_capture', None, raising=False)
    for key in (name, 'samsara_plugin_workflow_capture'):
        # Record even an absent original, so the freshly imported module is
        # removed at teardown rather than escaping into the next test.
        monkeypatch.setitem(sys.modules, key, None)
        monkeypatch.delitem(sys.modules, key)
    monkeypatch.setitem(sys.modules, 'pythoncom', types.ModuleType('pythoncom'))
    monkeypatch.setattr(plugin_commands, '_REGISTRY', {})
    monkeypatch.setattr(plugin_commands, '_MODULE_ENTRIES', {})
    monkeypatch.setattr(plugin_commands, '_LOADED_MODULES', {})
    original_import = builtins.__import__
    attempted = []

    def unavailable(module, *args, **kwargs):
        if module == 'win32com.client':
            attempted.append(module)
            raise failure('synthetic WMI import failure')
        return original_import(module, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', unavailable)
    # This uses the production loader, including its cached-module reuse path.
    capture = plugin_commands._import_plugin(REPO / 'plugins/commands/workflow_capture.py')
    assert attempted == []
    expected = {'start capture', 'stop capture', 'capture status', 'review capture'}
    assert {entry['phrase'] for entry in plugin_commands._REGISTRY.values()} == expected
    plugin_commands._REGISTRY.clear()
    assert plugin_commands._import_plugin(REPO / 'plugins/commands/workflow_capture.py') is capture
    assert {entry['phrase'] for entry in plugin_commands._REGISTRY.values()} == expected
    assert capture._proc_loop_wmi(threading.Event()) is False
    assert attempted == ['win32com.client']


def test_plugin_import_report_counts_partial_and_previously_imported_commands(tmp_path, capsys, monkeypatch):
    from samsara import plugin_commands
    from tools import ci_plugin_import_report as report

    monkeypatch.setattr(plugin_commands, '_REGISTRY', {})
    monkeypatch.setattr(plugin_commands, '_MODULE_ENTRIES', {})
    package = tmp_path / 'counted_plugins'
    package.mkdir()
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'a_failure.py').write_text(
        'from samsara.plugin_commands import command\n'
        '@command("partially registered")\n'
        'def handle(app, remainder): pass\n'
        "raise RuntimeError('synthetic import failure')\n", encoding='utf-8')
    (package / 'b_importer.py').write_text(
        'from counted_plugins import z_commands\n', encoding='utf-8')
    (package / 'z_commands.py').write_text(
        'from samsara.plugin_commands import command\n'
        '@command("count me", aliases=["count alias", "another alias"])\n'
        'def handle(app, remainder): pass\n'
        '@command("count twice")\n'
        'def other(app, remainder): pass\n', encoding='utf-8')
    monkeypatch.setattr(report, 'plugin_modules', lambda root: iter([
        'counted_plugins.a_failure', 'counted_plugins.b_importer', 'counted_plugins.z_commands']))
    try:
        assert report.main(tmp_path) == 0
    finally:
        for name in list(sys.modules):
            if name == 'counted_plugins' or name.startswith('counted_plugins.'):
                del sys.modules[name]
    output = capsys.readouterr().out
    assert 'FAIL counted_plugins.a_failure commands=1' in output
    assert 'RuntimeError: synthetic import failure' in output
    assert 'OK counted_plugins.b_importer commands=0' in output
    assert 'OK counted_plugins.z_commands commands=2' in output


def _normalise(text):
    """Lowercase, drop punctuation, collapse spaces -- so a description is
    compared to its phrase on words alone, not on a full stop."""
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def _phrases(record):
    """Every spoken form, plus the canonical verb+object, normalised."""
    out = {_normalise(a) for a in record.get("aliases", [])}
    verb = record.get("verb", "")
    obj = (record.get("object") or "").replace("_", " ")
    out.add(_normalise(f"{verb} {obj}"))
    out.discard("")
    return out


# ---------------------------------------------------------------------------
# Every record has one
# ---------------------------------------------------------------------------

def test_the_catalog_is_not_empty():
    assert len(RECORDS) > 400, len(RECORDS)


@pytest.mark.parametrize("record", RECORDS, ids=IDS)
def test_every_record_has_a_description(record):
    desc = (record.get("description") or "").strip()
    assert desc, f"{record['canonical_id']} has no description"


@pytest.mark.parametrize("record", RECORDS, ids=IDS)
def test_no_description_merely_restates_its_phrase(record):
    """The 111 finding. "Copy" -> "Copy" tells a first-time user nothing, and
    read aloud it is worse than silence."""
    desc = _normalise(record.get("description"))
    assert desc not in _phrases(record), (
        f"{record['canonical_id']}: the description just restates the phrase "
        f"({record.get('description')!r}). Say what it DOES.")


@pytest.mark.parametrize("record", RECORDS, ids=IDS)
def test_no_description_is_too_long_for_a_tooltip(record):
    desc = record.get("description") or ""
    assert len(desc) <= MAX_LEN, (
        f"{record['canonical_id']}: {len(desc)} characters, cap is {MAX_LEN}. "
        f"Put the detail in the rest of the docstring; the FIRST sentence is "
        f"what the catalog takes.")


@pytest.mark.parametrize("record", RECORDS, ids=IDS)
def test_no_description_uses_engineering_language(record):
    desc = record.get("description") or ""
    broken = [why for pattern, why in ENGINEERING if pattern.search(desc)]
    assert broken == [], (
        f"{record['canonical_id']}: description contains {', '.join(broken)} "
        f"-- {desc!r}. It is shown to the user and read aloud.")


@pytest.mark.parametrize("record", RECORDS, ids=IDS)
def test_every_description_is_one_sentence_starting_with_a_verb(record):
    """Third person, so the reference reads as a list of what the app does
    rather than a list of orders to the user."""
    desc = (record.get("description") or "").strip()
    # Selection commands deliberately name Ava, followed by an imperative.
    ava = record['canonical_id'].startswith('selection_edit.') and desc.startswith('Ava: ')
    body = desc.removeprefix('Ava: ') if ava else desc
    first = body.split()[0] if body else ""
    grammar = r"^(propose|read)$" if ava else r"^[A-Z][A-Za-z-]*(s|es|ies)$"
    assert re.match(grammar, first), (
        f"{record['canonical_id']}: {first!r} is not a third-person verb "
        f"(\"Opens\", not \"Open\") -- {desc!r}")
    assert desc.endswith((".", "!", "?")), (
        f"{record['canonical_id']}: description is not a sentence -- {desc!r}")


def test_the_soft_length_target_is_mostly_met():
    """Not every command fits 90 characters, but most should. This is the
    canary for the text drifting back towards paragraphs."""
    over = [r["canonical_id"] for r in RECORDS
            if len(r.get("description") or "") > SOFT_LEN]
    assert len(over) <= 10, f"{len(over)} descriptions over {SOFT_LEN} chars: {over[:12]}"


# ---------------------------------------------------------------------------
# The regeneration moved descriptions and nothing else
# ---------------------------------------------------------------------------

class TestOnlyTheDescriptionsMoved:
    """115 rewrote text. It must not have renamed, dropped or re-scoped a
    single command -- a phrase change silently breaks what the user says."""

    def test_the_record_count_is_unchanged(self, live_specs):
        expected = set(IDS)
        actual = {spec.canonical_id for spec in live_specs}
        assert actual == expected, {
            "missing_live": sorted(expected - actual),
            "extra_live": sorted(actual - expected),
        }
        assert len(RECORDS) == len(live_specs)

    def test_no_canonical_id_is_duplicated(self):
        ids = [r["canonical_id"] for r in RECORDS]
        assert len(ids) == len(set(ids))

    def test_the_catalog_is_current(self):
        """`gen_command_catalog.py --check` is the repo's own staleness gate:
        if a docstring changed and nobody regenerated, this fails."""
        import subprocess
        proc = subprocess.run(
            [sys.executable, "tools/gen_command_catalog.py", "--check"],
            cwd=str(REPO), capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, (
            "commands_catalog.json is stale -- run tools/gen_command_catalog.py\n"
            + proc.stdout[-800:] + proc.stderr[-800:])

    @pytest.mark.parametrize("record", RECORDS, ids=IDS)
    def test_every_record_still_has_its_spoken_forms(self, record):
        assert record.get("aliases"), record["canonical_id"]
        assert all(a.strip() for a in record["aliases"])


# ---------------------------------------------------------------------------
# The sources the catalog is generated FROM
# ---------------------------------------------------------------------------

class TestTheSourcesCarryTheText:
    """A description only survives a regeneration if it lives in the source
    the generator reads. These tests fail if somebody edits the catalog by
    hand instead."""

    def test_every_builtin_in_commands_json_has_a_description(self):
        doc = json.loads(COMMANDS_JSON.read_text(encoding="utf-8"))
        table = doc["commands"] if isinstance(doc, dict) and "commands" in doc else doc
        missing = [k for k, v in table.items()
                   if not (v.get("description") or "").strip()]
        assert missing == [], f"commands.json entries with no description: {missing}"

    def test_no_builtin_description_restates_its_key(self):
        doc = json.loads(COMMANDS_JSON.read_text(encoding="utf-8"))
        table = doc["commands"] if isinstance(doc, dict) and "commands" in doc else doc
        bad = [k for k, v in table.items()
               if _normalise(v.get("description")) == _normalise(k)]
        assert bad == [], f"commands.json descriptions that restate their key: {bad}"

    def test_a_plugin_description_comes_from_its_handler_docstring(self):
        """Pins the mechanism, so a future change to command_catalog.py that
        stops reading docstrings is caught here rather than by 487 silently
        reverting to their phrases."""
        from samsara.command_catalog import _first_sentence
        assert _first_sentence("Opens the thing. Then a note.") == "Opens the thing."
        assert _first_sentence("No terminator here") == "No terminator here"
        assert _first_sentence("") == ""
        assert _first_sentence(None) == ""
