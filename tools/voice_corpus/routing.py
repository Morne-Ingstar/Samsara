"""Production text routing probes with inert effects; no plugin execution.

These are separate from hold capture: a held prose utterance does not dispatch
media/draft commands. Contextual reply and session routing are labelled probes.
"""
import json
from pathlib import Path
from types import SimpleNamespace as NS


def route(text):
    from samsara.kept_dictations import is_keep
    from samsara.execution_policy import classify_reply
    from samsara.session_modes import SessionModeManager, SessionMode, UtteranceSignals
    if is_keep(text):
        return "keep"
    reply = classify_reply(text)
    if reply is not None:
        return "pending_reply:" + str(reply)
    # Exercise the real session controller with a throwaway staged draft.
    manager = SessionModeManager(abort_phrases=[], foreground_exe_resolver=lambda: "fake.exe",
        inject_fn=lambda *a, **k: True, remove_chars_fn=lambda *a: None,
        command_dispatch_fn=lambda *a, **k: False, agent_dispatch_fn=lambda *a, **k: None,
        buffer_dictate_until_commit=True, stop_fn=lambda reason: {}, speak_fn=lambda *a: None)
    manager.reset(SessionMode.DICTATE)
    signals = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.,), transcript_confident=True)
    manager.dispatch_utterance("A harmless sample draft", signals)
    # Restore has a meaningful precondition: it follows a clear in this probe.
    from samsara.result_messages import CLEAR_RESTORE_PHRASES
    from samsara.session_modes import normalize_utterance
    if normalize_utterance(text) in CLEAR_RESTORE_PHRASES:
        manager.dispatch_utterance("clear draft", signals)
    outcome = manager.dispatch_utterance(text, signals)
    if outcome.kind not in ("dictate_staged", "dictate_injected", "dictate_committed"):
        return "session:" + outcome.kind
    # Match only public repo command metadata; never load or run plugins.
    from samsara.command_registry import CommandMatcher
    from samsara.command_catalog import canonical_phrase
    root = Path(__file__).resolve().parents[2]
    records = json.loads((root / "commands_catalog.json").read_text(encoding="utf-8"))["commands"]
    allowed_sources = ("plugins/commands/music.py:", "plugins/commands/selection_edit.py:",
                       "samsara/ava_edit/draft_review.py:")
    records = [r for r in records if r.get("source", "").startswith(allowed_sources)]
    matcher = CommandMatcher()
    matcher.load_builtins({canonical_phrase(r): dict(type="corpus_probe", aliases=r["aliases"]) for r in records})
    ids = {canonical_phrase(r): r["canonical_id"] for r in records}
    for row in records:
        matcher._entries[canonical_phrase(row)].handler = NS(whole_utterance=row.get("whole_utterance", False))
    matcher.freeze()
    match = matcher.match(text)
    return "catalog:" + ids[match[0].phrase] if match and match[0] else "prose"


def check(reference, observed):
    expected = route(reference)
    actual = route(observed)
    return dict(expected_route=expected, actual_route=actual,
                command_routed_correctly=expected != "prose" and actual == expected,
                scope="production text routing with seeded context; effects are fake")
