"""Queue 54: the DICTATE commit word ("end") and hands-free dispatch logging.

Evidence: ~/.samsara/logs/samsara.log, 2026-09-14 22:31-22:41, live hands-free.
Never imports dictation (the app may be running): dictation.py methods are
compiled out of the source with ast, the same way test_wake_session_policy does.
"""
import ast
import logging
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import session_modes, transcript_gates
from samsara.command_registry import CommandMatcher
from samsara.session_modes import (
    CommandDispatchResult,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
    is_dictate_commit,
    split_trailing_dictate_commit,
    strip_redecoded_commit_word,
)

ROOT = Path(__file__).resolve().parents[1]
GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))
BAD = UtteranceSignals(has_contiguous_speech=None, compression_ratios=())


def _manager(redecode=None, command_dispatch=None):
    inject = Mock(side_effect=lambda text, guard=None: text)
    mgr = SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=Mock(return_value="notepad.exe"),
        foreground_hwnd_resolver=Mock(return_value=4242),
        inject_fn=inject,
        remove_chars_fn=Mock(),
        command_dispatch_fn=command_dispatch or Mock(return_value=CommandDispatchResult(matched=False)),
        agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True,
        commit_redecode_fn=redecode,
        clock=lambda: 1000.0,
    )
    mgr.force_mode(SessionMode.DICTATE)
    return mgr, inject


# ---------------------------------------------------------------------------
# A. The commit word
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["End.", "end", "End", "end,", "END", "End!", "  end.  "])
def test_whole_utterance_end_variants_commit_and_never_reach_the_text(word):
    mgr, inject = _manager()
    mgr.dispatch_utterance("This was brought up a while ago", GOOD)
    outcome = mgr.dispatch_utterance(word, GOOD)
    assert outcome.kind == "dictate_committed"
    pasted = inject.call_args[0][0]
    assert pasted == "This was brought up a while ago"
    assert "end" not in pasted.lower().split()


@pytest.mark.parametrize("chunk", [
    "a while ago, but I don't really know that it got fixed. End.",   # live log 22:37:12
    "a while ago, but I don't really know that it got fixed. end",
    "a while ago, but I don't really know that it got fixed! END,",
    "a while ago, but I don't really know that it got fixed? End!",
])
def test_trailing_end_as_its_own_sentence_commits_without_the_word(chunk):
    mgr, inject = _manager()
    outcome = mgr.dispatch_utterance(chunk, GOOD)
    assert outcome.kind == "dictate_committed"
    assert outcome.detail["commit_word"] == "trailing"
    pasted = inject.call_args[0][0]
    assert pasted.startswith("a while ago, but I don't really know that it got fixed")
    assert "end" not in pasted.lower().replace(".", " ").replace("!", " ").replace("?", " ").split()
    assert mgr.dictate_pending_buffer == ""


def test_trailing_end_is_stripped_from_the_commit_redecode_too():
    """22:37:18: the re-decode heard the consumed "End." as "and" and pasted it."""
    audio = np.zeros(4, dtype=np.float32)
    redecode = Mock(return_value="This was brought up a while ago, but I don't really know that it got fixed and.")
    mgr, inject = _manager(redecode=redecode)
    signals = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,), audio_ref=audio)
    outcome = mgr.dispatch_utterance("This was brought up a while ago, but I don't really know that it got fixed. End.", signals)
    assert outcome.kind == "dictate_committed"
    assert inject.call_args[0][0] == "This was brought up a while ago, but I don't really know that it got fixed."


def test_redecode_tail_is_left_alone_when_no_trailing_word_was_consumed():
    redecode = Mock(return_value="Salt, pepper and")
    mgr, inject = _manager(redecode=redecode)
    mgr.dispatch_utterance("Salt, pepper and", UtteranceSignals(
        has_contiguous_speech=True, compression_ratios=(1.3,), audio_ref=np.zeros(2, dtype=np.float32)))
    mgr.dispatch_utterance("end", GOOD)
    assert inject.call_args[0][0] == "Salt, pepper and"


@pytest.mark.parametrize("chunk", [
    "we finally reached the end.",        # no sentence break before "end"
    "and then the end",
    "It was the end. The End.",           # "The End." is two tokens
    "I got it fixed. And.",               # the "and" decision: never a commit
    "the weekend.",
])
def test_end_inside_prose_is_staged_not_committed(chunk):
    mgr, inject = _manager()
    outcome = mgr.dispatch_utterance(chunk, GOOD)
    assert outcome.kind == "dictate_staged"
    inject.assert_not_called()


@pytest.mark.parametrize("word", ["and", "And.", "And...", "AND"])
def test_the_and_decision_a_lone_and_is_dictation_not_a_commit(word):
    """Decision (queue 54): "and" is not folded into the commit word, neither
    whole-utterance nor trailing. In the owner's logs every lone "And..." that
    committed was a mid-thought pause; a misheard "end" costs one repeat."""
    assert is_dictate_commit(word) is False
    mgr, inject = _manager()
    mgr.dispatch_utterance("And the story went on", GOOD)
    outcome = mgr.dispatch_utterance(word, GOOD)
    assert outcome.kind == "dictate_staged"
    inject.assert_not_called()
    assert "and" not in session_modes._DICTATE_COMMIT_HOMOPHONES


def test_trailing_commit_refused_by_the_gate_stages_the_whole_chunk(caplog):
    mgr, inject = _manager()
    caplog.set_level(logging.INFO, logger=session_modes.log.name)
    outcome = mgr.dispatch_utterance("It got fixed. End.", BAD)
    assert outcome.kind == "dictate_staged"
    assert mgr.dictate_pending_buffer == "It got fixed. End."
    inject.assert_not_called()
    assert any("trailing commit word refused" in r.message for r in caplog.records)


def test_split_and_strip_helpers():
    assert split_trailing_dictate_commit("Fixed. End.") == "Fixed."
    assert split_trailing_dictate_commit("End.") is None          # whole utterance is is_dictate_commit's job
    assert split_trailing_dictate_commit("Fixed, end.") is None
    assert strip_redecoded_commit_word("it got fixed and.") == "it got fixed."
    assert strip_redecoded_commit_word("it got fixed. End.") == "it got fixed."
    assert strip_redecoded_commit_word("it got fixed, end") == "it got fixed"
    assert strip_redecoded_commit_word("it got fixed") == "it got fixed"


# ---------------------------------------------------------------------------
# B. "show windows" and dispatch logging
# ---------------------------------------------------------------------------

def _decorated_commands(path):
    """(phrase, aliases, pack) for every @command in a plugin source file."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "command":
                phrase = ast.literal_eval(dec.args[0])
                kw = {k.arg: ast.literal_eval(k.value) for k in dec.keywords if k.arg in ("aliases", "pack")}
                out.append((phrase, kw.get("aliases", []), kw.get("pack", "core")))
    return out


def _matcher(enabled_packs=None):
    registry = {}
    for stem in ("show_numbers", "window_switcher"):
        for phrase, aliases, pack in _decorated_commands(ROOT / "plugins" / "commands" / f"{stem}.py"):
            entry = {"func": None, "phrase": phrase, "aliases": aliases, "pack": pack}
            for name in [phrase, *aliases]:
                registry[name] = entry
    m = CommandMatcher()
    if enabled_packs is not None:
        m.set_enabled_packs(enabled_packs)
    m.load_plugins(registry)
    m.freeze()
    return m


def test_show_windows_resolves_to_window_switcher_not_show_numbers_show_alias():
    m = _matcher()
    for said in ("show windows", "Show windows.", "label windows"):
        entry, remainder = m.match(said)
        assert entry.phrase == "show windows", said
        assert remainder == ""
    # Queue 58: with window-management disabled, "show windows" is a miss that
    # names the pack -- the bare "show" alias (show_numbers) no longer claims it.
    disabled = _matcher(enabled_packs={"accessibility"})
    assert disabled.match("show windows") == (None, '')
    assert disabled.disabled_pack_for("show windows") == "window-management"


def test_show_windows_in_command_mode_dispatches_to_window_switcher_show_windows():
    m = _matcher()

    def dispatch(text):
        entry, _ = m.match(text)
        return CommandDispatchResult(matched=entry is not None, phrase=entry and entry.phrase, state="completed")

    mgr, _ = _manager(command_dispatch=Mock(side_effect=dispatch))
    mgr.force_mode(SessionMode.COMMAND)
    outcome = mgr.dispatch_utterance("show windows", GOOD)
    assert outcome.kind == "command_executed"
    assert outcome.detail["phrase"] == "show windows"
    app = _app()
    assert app._command_canonical_id(outcome.detail["phrase"]) == "window_switcher.show_windows"
    assert app._command_canonical_id("show numbers") == "show_numbers.show_numbers"


_METHODS = ("_handle_command_mode_utterance", "_is_dictate_context_echo",
            "_log_cmd_utt_dropped", "_log_command_dispatch", "_command_canonical_id")


def _load_methods():
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DictationApp")
    nodes = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in _METHODS]
    assert {n.name for n in nodes} == set(_METHODS)
    module = types.ModuleType("dictation_extract_54")
    module.__dict__.update(
        np=np, logger=logging.getLogger("dictation_extract_54"), SessionMode=SessionMode,
        resample_audio=lambda audio, *a: audio,
        # Queue 128 moved these pure, Qt/model-free gates out of dictation.
        # Import their production module instead of duplicating its AST seam.
        _drop_trailing_garbage_segments=transcript_gates._drop_trailing_garbage_segments,
        _trim_trailing_garbage_run=transcript_gates._trim_trailing_garbage_run,
        _is_hallucinated_segments=transcript_gates._is_hallucinated_segments,
        _is_quality_exhausted=transcript_gates._is_quality_exhausted,
        _sanitise_context_tail=transcript_gates._sanitise_context_tail,
        _CONTEXT_TAIL_CHARS=transcript_gates._CONTEXT_TAIL_CHARS,
        _CONTEXT_ECHO_CHIP=transcript_gates._CONTEXT_ECHO_CHIP,
        _is_context_echo=transcript_gates._is_context_echo,
        is_scratch_that=session_modes.is_scratch_that,
        is_dictate_commit=session_modes.is_dictate_commit,
        is_recover_draft=session_modes.is_recover_draft,
        match_switch_word=session_modes.match_switch_word,
        # This extraction tests transcript dispatch/logging, not queue-69's
        # execution-policy window lifecycle. A no-window adapter is the same
        # safe seam the app uses when that optional module is unavailable.
        _cancel_window_module=lambda: None,
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "dictation.py", "exec"), module.__dict__)
    return module


_MOD = _load_methods()


def _app(mode=SessionMode.COMMAND, text="", ghost=False):
    app = SimpleNamespace(
        _transcription_owners=SimpleNamespace(claim=lambda lane: 1, release=lambda lane, token: None),
        _vad_reset=lambda: None, model_rate=16000, model_lock=__import__("threading").Lock(),
        _command_mode_ghost_tap=ghost, config={"command_mode": {"tts_char_limit": 50}},
        command_mode_active=True, voice_training_window=SimpleNamespace(apply_corrections=lambda t: t),
        get_transcription_params=lambda include_vocabulary=False: {},
        _filter_dictation_language=lambda t, info: t, play_sound=Mock(),
    )
    manager = SimpleNamespace(mode=mode, dispatch_utterance=Mock(), dictate_context_tail=lambda: "")
    app._session_mode_manager = manager
    app._ensure_session_mode_manager = lambda: manager
    segs = [SimpleNamespace(text=text)] if text else []
    app.model = SimpleNamespace(transcribe=lambda audio, **kw: (iter(segs), SimpleNamespace(language="en")))
    for name in _METHODS:
        fn = _MOD.__dict__[name]
        setattr(app, name, types.MethodType(fn, app) if name != "_log_cmd_utt_dropped" else fn)
    return app


def _drops(caplog):
    return [r.message for r in caplog.records if "[CMD-UTT] dropped" in r.message]


@pytest.mark.parametrize("case, seconds, text, ghost, reason", [
    ("too short", 0.2, "", False, "too_short"),
    ("empty decode", 1.0, "", False, "empty_transcription"),
    ("ghost tap", 1.0, "show windows", True, "ghost_tap"),
])
def test_utterance_that_reaches_no_dispatcher_logs_why_with_the_mode(caplog, case, seconds, text, ghost, reason):
    caplog.set_level(logging.INFO, logger="dictation_extract_54")
    app = _app(text=text, ghost=ghost)
    app._handle_command_mode_utterance([np.zeros(int(16000 * seconds), dtype=np.float32)], 16000)
    app._session_mode_manager.dispatch_utterance.assert_not_called()
    assert _drops(caplog) == [f"[CMD-UTT] dropped reason={reason} mode=command duration={seconds:.1f}s"], case
    if seconds >= 0.3:
        assert "[CMD-UTT] capture mode=command duration=1.0s" in [r.message for r in caplog.records]


def test_dispatch_exception_before_dispatch_logs_a_drop(caplog):
    caplog.set_level(logging.INFO, logger="dictation_extract_54")
    app = _app(mode=SessionMode.DICTATE, text="hello")

    def boom(audio, **kw):
        raise RuntimeError("model gone")
    app.model = SimpleNamespace(transcribe=boom)
    app._handle_command_mode_utterance([np.zeros(16000, dtype=np.float32)], 16000)
    assert _drops(caplog) == ["[CMD-UTT] dropped reason=error:RuntimeError mode=dictate duration=1.0s"]


def test_command_dispatch_log_names_canonical_id_and_a_silent_confirmation(caplog, monkeypatch):
    """22:35:06: "show windows" was held for a confirmation whose 55-char
    prompt command mode never spoke; the log only said command_executed."""
    import samsara.execution_policy as policy
    op = SimpleNamespace(invocation=SimpleNamespace(command_id="show windows"), prompt="x" * 55)
    monkeypatch.setattr(policy, "pending_operation", lambda: op)
    caplog.set_level(logging.INFO, logger="dictation_extract_54")
    app = _app()
    app._log_command_dispatch("show windows", "show windows", "queued")
    messages = [r.message for r in caplog.records]
    assert ("[CMD-DISPATCH] mode=command utterance='show windows' resolved='show windows' "
            "canonical=window_switcher.show_windows state=queued awaiting_confirmation=True") in messages
    # Queue 58: confirmation questions are exempt from tts_char_limit, so the
    # "will not be spoken" warning is gone.
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_command_miss_is_logged_with_mode(caplog, monkeypatch):
    import samsara.execution_policy as policy
    monkeypatch.setattr(policy, "pending_operation", lambda: None)
    caplog.set_level(logging.INFO, logger="dictation_extract_54")
    app = _app(mode=SessionMode.DICTATE)
    app._log_command_dispatch("flibber", None, "miss")
    assert ("[CMD-DISPATCH] mode=dictate utterance='flibber' resolved=None canonical=None "
            "state=miss awaiting_confirmation=False") in [r.message for r in caplog.records]
