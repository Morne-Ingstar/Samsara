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
    first = desc.split()[0] if desc else ""
    assert re.match(r"^[A-Z][A-Za-z-]*(s|es|ies)$", first), (
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

    def test_the_record_count_is_unchanged(self):
        assert len(RECORDS) == 487

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
