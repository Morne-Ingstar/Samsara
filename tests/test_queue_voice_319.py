"""The spoken queue reads canonical file state and asks before writing."""

import importlib
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from samsara.agent_channel import claude_reply
from samsara.agent_channel.queue_voice import QueuePaths, QueueView, prompt_id
from samsara.command_packs import get_enabled_packs
from samsara.plugin_commands import list_commands
from plugins.commands import queue_voice as voice
from plugins.commands import message_claude


SOURCE_NOTE = Path(__file__).parent / "fixtures" / "319" / "queue_note.py"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "Claude"
    queue = root / "queue"
    reports = root / "reports"
    home = tmp_path / "app-home"
    for folder in (queue, queue / "done", queue / "running" / "locks",
                   queue / "accepted", reports, home):
        folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE_NOTE, root / "queue_note.py")
    stub = root / "note_stub.py"
    stub.write_text("print('queue note refreshed')\n", encoding="utf-8")
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(home))
    paths = QueuePaths(queue, reports, Path(sys.executable), home, stub)
    return SimpleNamespace(queue=queue, reports=reports, home=home, paths=paths)


def prompt(space, pid, *, folder=None, owns="samsara/example.py", order=""):
    folder = folder or space.queue
    path = folder / f"{pid}_example.md"
    path.write_text(f"AGENT: codex - Sol\nOWNS: {owns}\nORDER: {order}\n# Prompt {pid}\n",
                    encoding="utf-8")
    return path


def report(space, pid, status="DONE", summary="The requested work finished.", stamp=None):
    directory = space.reports / pid
    directory.mkdir()
    path = directory / "FINAL.md"
    path.write_text(f"MODEL: test\n\n## Result\n\n{summary}\n\nSTATUS: {status}\n",
                    encoding="utf-8")
    if stamp is not None:
        os.utime(path, ns=(stamp, stamp))
    return path


def test_finished_since_is_newest_first_and_pages_three(workspace):
    base = 1_700_000_000_000_000_000
    for index, pid in enumerate(("901", "902", "903", "904")):
        report(workspace, pid, stamp=base + index * 1_000_000_000)
    last = workspace.reports / "904" / "FINAL.md"
    last.write_text("MODEL: test\nPREMISES\n- CONFIRMED: input.\nPART 2 — fix\n"
                    "- The status now derives from the report.\nSTATUS: DONE\n", encoding="utf-8")
    os.utime(last, ns=(base + 3_000_000_000, base + 3_000_000_000))
    view = QueueView(workspace.paths)
    spoken, count = view.finished()
    assert count == 3
    assert spoken.index("904, DONE") < spoken.index("903, DONE") < spoken.index("902, DONE")
    assert "The status now derives from the report." in spoken
    assert "901" not in spoken
    more, count = view.more()
    assert count == 1 and "901, DONE" in more
    again, count = view.finished()
    assert count == 0 and again.startswith("Nothing new since")
    assert (workspace.home / "queue_voice_state.json").is_file()


def test_runnable_respects_after_and_owns_overlap(workspace):
    prompt(workspace, "309", folder=workspace.queue / "done")
    prompt(workspace, "901", owns="samsara/shared.py", order="AFTER 309 is reconciled")
    prompt(workspace, "902", owns="samsara/other.py", order="AFTER 309 is reconciled")
    (workspace.queue / "902_example.md").write_text(
        "AGENT: codex - Sol\nOWNS: samsara/other.py\nAFTER: 309\n"
        "ORDER: Parallel-safe with other prompts.\n# Prompt 902\n", encoding="utf-8")
    prompt(workspace, "903", folder=workspace.queue / "running", owns="samsara/shared.py")
    prompt(workspace, "904", owns="samsara/finished.py", order="AFTER 309 is reconciled")
    report(workspace, "904")
    view = QueueView(workspace.paths)
    spoken, count = view.runnable()
    assert count == 1
    assert "902, ready" in spoken
    assert "901" not in spoken
    assert "904" not in spoken
    running, count = view.running()
    assert count == 1 and "903, running, codex - Sol" in running


def test_lock_only_owns_blocks_runnable_prompt(workspace):
    prompt(workspace, "901", owns="samsara/shared.py")
    lock = workspace.queue / "running" / "locks" / "903.lock"
    lock.write_text('{"owns": ["samsara/shared.py"]}', encoding="utf-8")
    spoken, count = QueueView(workspace.paths).runnable()
    assert count == 0 and "Nothing ready" in spoken


def test_report_status_uses_queue_note_and_spoken_form(workspace):
    path = prompt(workspace, "312")
    report(workspace, "312", status="BLOCKED", summary="Owner pick required. " + "detail " * 90)
    view = QueueView(workspace.paths)
    assert view.note.report_state("312", path)[0] == "blocked"
    first = view.read_report("312")
    assert first.startswith("312, BLOCKED.")
    assert len(first.split()) <= 60
    rest = view.report_followup("read the rest")
    assert rest and len(rest.split()) <= 60
    assert prompt_id("three one two") == "312"


def test_accept_is_confirmed_then_writes_empty_marker_and_calls_note(workspace, monkeypatch):
    prompt(workspace, "312")
    report(workspace, "312")
    view = QueueView(workspace.paths)
    spoken = []
    chips = []
    app = SimpleNamespace(_queue_voice_view=view, config={},
                          audio_coordinator=SimpleNamespace(
                              speak=lambda text, **kw: spoken.append((text, kw))),
                          _show_outcome_chip=lambda text, kind: chips.append((text, kind)))
    pending = {}
    monkeypatch.setattr(voice.execution_policy, "current_generation", lambda _app: 1)
    monkeypatch.setattr(voice.execution_policy, "stage_pending",
                        lambda _app, inv, question, **kw: pending.update(
                            inv=inv, question=question, approve=kw["on_approve"]))
    monkeypatch.setattr(voice.execution_policy, "authorize",
                        lambda _inv, **_kw: voice.execution_policy.Allowed("confirmed"))
    assert voice.accept(app, "three one two") is voice.DispatchState.QUEUED
    marker = workspace.queue / "accepted" / "312"
    assert not marker.exists()
    assert pending["question"] == "Accept 312?"
    assert pending["inv"].command_id == "accept prompt"
    pending["approve"](None)
    assert marker.is_file() and marker.stat().st_size == 0
    assert "queue note refreshed" in spoken[-1][0]
    assert all(kw["category"] == "ava_response" for _text, kw in spoken)
    assert chips[-1][0] == "queue: confirm"


def test_note_failure_uses_last_error_line(workspace):
    workspace.paths.note_command.write_text(
        "import sys\nprint('first error', file=sys.stderr)\n"
        "print('last error', file=sys.stderr)\nsys.exit(2)\n", encoding="utf-8")
    assert QueueView(workspace.paths).queue_note() == "queue note failed: last error"


def test_read_report_followups_use_captioned_claude_seam(workspace):
    report(workspace, "312", summary="A concise summary. " + "Further details " * 70)
    view = QueueView(workspace.paths)
    view.read_report("312")
    spoken = []
    app = SimpleNamespace(_queue_voice_view=view,
                          audio_coordinator=SimpleNamespace(
                              speak=lambda text, **kw: spoken.append((text, kw))))
    assert claude_reply.followup(app, "read the rest")
    assert spoken and spoken[-1][1]["category"] == "ava_response"
    assert claude_reply.followup(app, "repeat that")
    assert spoken[-1][0] == spoken[-2][0]


def test_queue_commands_are_available_by_default_and_writes_are_classified():
    importlib.reload(voice)  # other command tests clear the global plugin registry
    importlib.reload(message_claude)
    entries = {entry["phrase"]: entry for entry in list_commands()
               if entry["source"] == "plugins.commands.queue_voice"}
    assert entries
    assert all(len(phrase.split()) > 1 for entry in entries.values()
               for phrase in [entry["phrase"], *entry["aliases"]] if phrase != "more")
    assert entries["read more"]["aliases"] == [
        "read next", "next three", "more", "continue reading"]
    assert {entry["pack"] for entry in entries.values()} <= get_enabled_packs({})
    assert entries["accept prompt"]["risk_class"] == "write"
    assert entries["queue note"]["risk_class"] == "write"
    assert entries["what finished"]["risk_class"] == "safe"
    followups = {entry["phrase"]: entry for entry in list_commands()
                 if entry["phrase"] in {"read the rest", "repeat that"}}
    assert set(followups) == {"read the rest", "repeat that"}
    assert {entry["pack"] for entry in followups.values()} == {"core"}


def test_paging_hint_uses_registered_phrases_and_every_read_more_alias_resolves(workspace):
    importlib.reload(voice)
    from samsara import plugin_commands
    from samsara.command_registry import CommandMatcher

    registered = next(entry for entry in plugin_commands._MODULE_ENTRIES[voice.__name__].values()
                      if entry["func"] is voice.more)
    matcher = CommandMatcher()
    matcher.load_plugins(plugin_commands._MODULE_ENTRIES[voice.__name__])
    matcher.freeze()
    for phrase in [registered["phrase"], *registered["aliases"]]:
        match, _remainder = matcher.match(phrase)
        assert match is not None and match.phrase == registered["phrase"]

    for pid in ("901", "902", "903", "904"):
        report(workspace, pid)
    view = QueueView(workspace.paths)
    spoken = []
    app = SimpleNamespace(
        _queue_voice_view=view,
        audio_coordinator=SimpleNamespace(speak=lambda text, **_kw: spoken.append(text)),
    )
    voice.what_finished(app)
    expected = f"Say {registered['phrase']} or {registered['aliases'][0]} for the next three."
    assert expected in spoken[-1]
    assert registered["phrase"] in spoken[-1]
    assert matcher.match(registered["phrase"])[0].phrase == registered["phrase"]
