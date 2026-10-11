"""Ordinary logging privacy: real delivery seams, no app bootstrap or child."""
import ast
import logging
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.log import describe_text, safe_exception

ROOT = Path(__file__).resolve().parents[1]
MARKER = "privacy" + uuid.uuid4().hex
PRIVATE = f"Synthetic {MARKER} sentence."
LEVELS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
DENY = set("text raw_text transcript final_text draft utterance clipboard prompt reply "
           "response remainder original_text command_text corrected _trimmed seg_text "
           "wrong right preview request initial_content title context word replacement "
           "app_name question content dispatch_text answer message captured prepended".split())
PRIVATE_KEYS = DENY | {"heard", "body", "window", "label"}
# This developer-only coordinate trace can contain UIA control names. Its
# compile-time _COORD_DEBUG switch is false; there is no user-facing setting.
OPT_IN_ALLOWLIST = {("plugins/commands/show_numbers.py", "[DPI-COORD] elem")}
# Overlay text is a generated number label, not recognized/dictated words.
NOT_USER_TEXT = {("samsara/ui/numbers_overlay_qt.py", "[OVERLAY-PAINT]")}
# Reviewed message transports or fixed hardware/model diagnostics. These
# exclusions do not authorize free speech in callers of the transports.
NOT_USER_TEXT |= {("dictation.py", "[PREVIEW]"), ("dictation.py", "[CAL]"),
                  ("samsara/audio_ducking.py", "[DUCK] USER NOTICE:"),
                  ("samsara/boot.py", "[QT]"),
                  ("samsara/cloud_llm.py", "retired default"),
                  ("samsara/echo_cancel.py", "[AEC-CAL] Calibration failed:"),
                  ("samsara/ui/mic_setup_wizard_qt.py", "[WIZARD] Wake calibration not saved:")}
LOG_TRANSPORTS = {("dictation.py", "print"),
                  ("samsara/audio_engine/wake_consumer.py", "_log_frame")}


@pytest.fixture(autouse=True)
def ordinary_capture(caplog):
    caplog.set_level(logging.DEBUG)
    yield
    # Check every level and logger, including formatted exceptions.
    assert MARKER not in caplog.text
    assert all(MARKER not in record.getMessage() for record in caplog.records)


def tree(file):
    return ast.parse((ROOT / file).read_text(encoding="utf-8-sig"))


def log_seam(file, event, namespace):
    """Run an actual source logging statement with isolated caller stand-ins."""
    calls = [n for n in ast.walk(tree(file)) if isinstance(n, ast.Call)
             and (isinstance(n.func, ast.Attribute) and n.func.attr in LEVELS
                  or isinstance(n.func, ast.Name) and n.func.id == "safe_exception")
             and any(isinstance(v, ast.Constant) and isinstance(v.value, str)
                     and event in v.value for v in ast.walk(n))]
    assert calls, event
    module = ast.Module(body=[ast.Expr(value=calls[0])], type_ignores=[])
    ast.fix_missing_locations(module)
    # This contains only the selected logger expression, never module/class
    # bootstrap. A distinct filename identifies the isolated expression.
    exec(compile(module, "<511 isolated logging statement>", "exec"), dict(
        logger=logging.getLogger("Samsara"), log=logging.getLogger("Samsara"),
        describe_text=describe_text, safe_exception=safe_exception,
        logging=logging, sys=sys, **namespace))


@pytest.mark.parametrize("ok", [True, False], ids=["ordinary-hold", "failed-paste"])
def test_hold_delivery(ok, monkeypatch, caplog):
    from samsara import paste
    app = SimpleNamespace(
        config={}, _foreground_wants_typed_injection=Mock(return_value=False),
        _flight_foreground_process_name=Mock(return_value="notepad.exe"),
        _record_undoable_paste=Mock(), adaptive_learner=Mock(),
        play_sound=Mock(), _show_outcome_chip=Mock())
    monkeypatch.setattr(paste.injection_safety, "window_integrity", lambda: None)
    monkeypatch.setattr(paste, "flight_foreground_process_name", lambda: "notepad.exe")
    monkeypatch.setattr(paste.flight_recorder, "record", Mock())
    def delivery(text, **kwargs):
        assert text == PRIVATE
        assert kwargs["before_paste"]()
        return ok
    result = paste.paste_preserving_clipboard(
        app, PRIVATE, paste_with_preservation_fn=delivery,
        type_text_unicode_fn=Mock(), get_foreground_hwnd_fn=lambda: 101)
    assert result is ok
    if ok:
        log_seam("dictation.py", "[TEXT] ", {"text": PRIVATE})
        assert "[TEXT]" in caplog.text
    else:
        assert "delivery failed" in caplog.text


def test_dropped_signoff(monkeypatch, caplog):
    from samsara import hallucination_filter as hf
    segment = SimpleNamespace(text=PRIVATE, avg_logprob=-2.0, no_speech_prob=0.95)
    # Force only the phrase classifier; confidence/duration gates stay real.
    monkeypatch.setattr(hf, "_is_phrase_sequence", lambda tokens: True)
    reason = hf.signoff_failure_reason(PRIVATE, [segment], duration_s=0.5)
    assert reason
    log_seam("dictation.py", "[HALLUCINATION] dropped:",
             {"text": PRIVATE, "_reason": reason})
    assert "[HALLUCINATION] dropped:" in caplog.text


def test_command_free_text(monkeypatch, caplog):
    from samsara import commands
    executor = object.__new__(commands.CommandExecutor)
    handler = Mock(side_effect=ValueError(PRIVATE))
    entry = SimpleNamespace(phrase="synthetic command", source="plugin", handler=handler)
    executor._app = SimpleNamespace()
    executor.commands = {}
    monkeypatch.setattr(executor, "_registry_entry", lambda cid: entry)
    monkeypatch.setattr(commands.execution_policy, "authorize", lambda *a, **k: object())
    result = executor.execute_canonical(
        entry.phrase, args={"remainder": PRIVATE}, source_text=PRIVATE, generation=1)
    handler.assert_called_once_with(executor._app, PRIVATE)
    assert result.state is commands.DispatchState.FAILED
    assert "[PLUGIN] Executing: synthetic command" in caplog.text
    assert caplog.records[-1].exc_info


def test_error_message_cannot_echo_private_input(caplog):
    try:
        raise ValueError(PRIVATE)
    except ValueError as error:
        log_seam("dictation.py", "[ERROR] Transcription failed:", {"e": error})
    assert "ValueError" in caplog.text
    assert caplog.records and all(record.exc_info for record in caplog.records)
    assert "Traceback (most recent call last)" in caplog.text
    assert "test_error_message_cannot_echo_private_input" in caplog.text
    assert MARKER not in logging.Formatter().formatException(caplog.records[-1].exc_info)


def test_crash_hooks_cannot_echo_exception_or_object(monkeypatch, caplog):
    from samsara import boot
    monkeypatch.setattr(threading, "excepthook", Mock())
    monkeypatch.setattr(sys, "unraisablehook", Mock())
    thread_hook, unraisable_hook = boot.install_exception_hooks(
        logging.getLogger("Samsara.boot"))
    error = ValueError(PRIVATE)
    thread_hook(SimpleNamespace(exc_type=ValueError, exc_value=error,
                                exc_traceback=None, thread=SimpleNamespace(name="511-worker")))
    unraisable_hook(SimpleNamespace(exc_type=ValueError, exc_value=error,
                                   exc_traceback=None, err_msg=None, object=PRIVATE))
    log_seam("dictation.py", "Uncaught exception:",
             {"exc_type": ValueError, "exc_value": error, "exc_tb": None})
    assert "511-worker" in caplog.text and "ValueError" in caplog.text


def test_exception_chain_and_os_metadata(caplog):
    original = None
    try:
        try:
            raise OSError(5, PRIVATE, "synthetic.txt")
        except OSError as error:
            raise ValueError(PRIVATE) from error
    except ValueError as error:
        original = error
        safe_exception(logging.getLogger("Samsara"), "Synthetic failure")
    record = caplog.records[-1]
    rendered = logging.Formatter().format(record)
    assert record.exc_info[2] is original.__traceback__
    assert record.exc_info[1] is not original
    assert str(original) == PRIVATE
    assert MARKER not in rendered
    assert "ValueError" in rendered and type(original.__cause__).__name__ in rendered
    assert "errno=5" in rendered and "file=synthetic.txt" in rendered
    assert "test_exception_chain_and_os_metadata" in rendered


def test_exception_group_keeps_child_frames_and_types(caplog):
    try:
        try:
            raise ValueError(PRIVATE)
        except ValueError as error:
            group = ExceptionGroup(PRIVATE, [error, error])
        raise group
    except ExceptionGroup:
        safe_exception(logging.getLogger("Samsara"), "Synthetic grouped failure")
    rendered = logging.Formatter().format(caplog.records[-1])
    assert MARKER not in rendered
    assert "ExceptionGroup" in rendered and "ValueError" in rendered
    assert "test_exception_group_keeps_child_frames_and_types" in rendered


def test_hands_free_chunk(caplog):
    from samsara.session_modes import SessionModeManager, SessionMode, UtteranceSignals
    manager = SessionModeManager(
        abort_phrases=[], foreground_exe_resolver=lambda: "notepad.exe",
        inject_fn=Mock(return_value=True), remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(), agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True)
    manager.reset(SessionMode.DICTATE)
    text = "dictate " + PRIVATE
    manager.dispatch_utterance(text, UtteranceSignals(
        has_contiguous_speech=False, compression_ratios=(3.0,), transcript_confident=False))
    assert MARKER in manager.dictate_pending_buffer
    log_seam("samsara/session_modes.py", "[SESSION] paced fragment kept as dictation:",
             {"text": text})
    assert "[SESSION] paced fragment kept as dictation:" in caplog.text
    assert describe_text({"text": PRIVATE, "chars": len(PRIVATE),
                          "mode": "dictate", "target_process": "notepad.exe"}) == {
        "text": describe_text(PRIVATE), "chars": len(PRIVATE),
        "mode": "dictate", "target_process": "notepad.exe"}


@pytest.mark.parametrize("provider", ["ollama", "cloud"])
def test_ava_request_with_stubbed_model(provider, monkeypatch, caplog):
    from plugins.commands import ask_ollama as ava
    private_writer = Mock()
    monkeypatch.setattr(ava, "log_conversation", private_writer)
    model = Mock(return_value=SimpleNamespace(
        text=PRIVATE, ok=True, search=None, latency_ms=17,
        provider=provider, fallback_from=None))
    ava._turn_local.reply = model(PRIVATE)
    ava._turn_local.speech = []
    ava._turn_local.spoken_text = []
    ava._turn_local.working_context = ""
    ava._log_menu(PRIVATE, ["next tab"])
    ava._log_turn(SimpleNamespace(config={}), PRIVATE, outcome="answered")
    assert "[AVA-MENU]" in caplog.text and "[AVA-TURN]" in caplog.text
    assert "latency_ms=17" in caplog.text
    assert describe_text(PRIVATE) in caplog.text
    assert private_writer.call_args.kwargs["user_text"] == PRIVATE


def test_spoken_tts_text(monkeypatch, caplog):
    from samsara.tts.coordinator import AudioCoordinator, SPEAKING
    engine = Mock()
    app = SimpleNamespace(config={"tts": {"enabled": True}})
    coordinator = AudioCoordinator(app, engine)
    # Keep the real speak/transition/log paths; suppress device/timer effects.
    monkeypatch.setattr(coordinator, "_on_transition", Mock())
    coordinator.speak(PRIVATE, category="readback")
    assert coordinator.get_state() == SPEAKING
    assert engine.speak.call_args.args[0] == PRIVATE
    assert "context=" in caplog.text and describe_text(PRIVATE) in caplog.text


@pytest.mark.parametrize("event,file,namespace", [
    ("Pre-buffering trailing speech:", "dictation.py", {"initial_content": PRIVATE}),
    ("[DICTATE] Initial content:", "dictation.py", {"initial_content": PRIVATE}),
    ("[CORRECTIONS] undo:", "samsara/correction_queue.py",
     {"entry": {"wrong": PRIVATE, "right": PRIVATE}}),
    ("Reminder toast action failed", "samsara/ui/reminder_toast.py",
     {"row": SimpleNamespace(title=PRIVATE), "e": ValueError(PRIVATE)}),
], ids=["M1", "M2", "M4", "M5"])
def test_reviewed_leak_seams(event, file, namespace, caplog):
    try:
        raise ValueError(PRIVATE)
    except ValueError:
        log_seam(file, event, namespace)
    assert caplog.records


class BrokenStringError(Exception):
    def __str__(self):
        raise RuntimeError(PRIVATE)


class SyntheticBaseError(BaseException):
    pass


def unusual_exception(case):
    if case == "broken-str":
        return BrokenStringError(PRIVATE)
    if case == "int-filename":
        return OSError(9, PRIVATE, 123)
    if case == "non-string-notes":
        error = ValueError(PRIVATE)
        error.__notes__ = [123, PRIVATE, BrokenStringError()]
        return error
    if case == "no-args":
        return ValueError()
    return SyntheticBaseError(PRIVATE)


@pytest.mark.parametrize("case", ["broken-str", "int-filename", "non-string-notes",
                                 "no-args", "base-exception"])
def test_exception_helpers_never_interrupt_recovery(case, caplog):
    error = unusual_exception(case)
    summary = describe_text(error)
    assert isinstance(summary, str) and type(error).__name__ in summary
    recovered = False
    try:
        raise error
    except BaseException:
        safe_exception(logging.getLogger("Samsara"), "Synthetic recovery: %s", summary)
        recovered = True
    assert recovered
    record = caplog.records[-1]
    assert record.exc_info[2] is error.__traceback__
    rendered = logging.Formatter().format(record)
    assert type(error).__name__ in rendered
    assert "test_exception_helpers_never_interrupt_recovery" in rendered
    assert MARKER not in rendered


def test_outcome_identifiers_and_enum_values_survive():
    from samsara.session_modes import SessionMode
    identifiers = dict(phrase="builtin.open", reason="stale", pack="navigation",
                       source="voice clear", kind="command_rejected", lane="command",
                       origin="model", stage="inject", where="draft",
                       reverted_to=SessionMode.DICTATE, commit_outcome="dictation_committed",
                       mode_retained=SessionMode.DICTATE)
    expected = dict(identifiers, reverted_to=SessionMode.DICTATE.value,
                    mode_retained=SessionMode.DICTATE.value)
    assert describe_text(identifiers) == expected
    for key in PRIVATE_KEYS | {"target"}:
        assert MARKER not in str(describe_text({key: PRIVATE}))
    assert MARKER not in str(describe_text({"reason": "no window found for " + PRIVATE}))


def source_modules():
    # Repository-native paths also work in plain exports, without git or
    # reading external files. Superset of the tracked shipped Python tree.
    yield from ROOT.glob("*.py")
    yield from (ROOT / "samsara").rglob("*.py")
    yield from (ROOT / "plugins" / "commands").glob("*.py")


def unsafe_names(node):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "format_exception", "format_exc"}:
            return {"exception traceback can contain user text"}
        if isinstance(node.func, ast.Name) and node.func.id in {
                "describe_text", "len", "type", "sentence_mark_density", "_preview"}:
            return set()
        if isinstance(node.func, ast.Attribute) and node.func.attr == "count":
            return set()
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            key = node.args[0]
            if isinstance(key, ast.Constant) and key.value in PRIVATE_KEYS:
                return {"private key " + key.value}
        if isinstance(node.func, ast.Attribute):
            found = unsafe_names(node.func.value)
            for argument in node.args:
                found |= unsafe_names(argument)
            return found
    if isinstance(node, ast.Attribute):
        if node.attr in DENY or node.attr.endswith(("_text", "_transcript")):
            return {node.attr}
        if node.attr not in {"text", "buffer", "detail"}:
            return set()
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        if node.slice.value in PRIVATE_KEYS:
            return {"private key " + node.slice.value}
        if node.slice.value in {"chars", "source", "type", "index", "reason"}:
            return set()
    found = {node.id} & DENY if isinstance(node, ast.Name) else set()
    for child in ast.iter_child_nodes(node):
        found |= unsafe_names(child)
    return found


def test_source_guard():
    failures = []
    coordinate_flag = next(n.value for n in tree("samsara/ui/numbers_overlay_qt.py").body
                           if isinstance(n, ast.Assign) and any(
                               isinstance(t, ast.Name) and t.id == "_COORD_DEBUG"
                               for t in n.targets))
    assert ast.literal_eval(coordinate_flag) is False
    for path in source_modules():
        file = path.relative_to(ROOT).as_posix()
        if file.startswith("samsara/dev/"):
            continue
        module = ast.parse(path.read_text(encoding="utf-8-sig"))
        parents = {child: parent for parent in ast.walk(module)
                   for child in ast.iter_child_nodes(parent)}
        for call in ast.walk(module):
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            is_log = isinstance(fn, ast.Attribute) and fn.attr in LEVELS and (
                not isinstance(fn.value, ast.Name) or fn.value.id != "QMessageBox")
            is_safe_exception = isinstance(fn, ast.Name) and fn.id == "safe_exception"
            is_print = isinstance(fn, ast.Name) and fn.id == "print"
            # print mirroring is local to dictation.py, not a builtins hook.
            if not (is_log or is_safe_exception or is_print and file == "dictation.py"):
                continue
            strings = [v.value for v in ast.walk(call)
                       if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            if any(file == f and any(event in s for s in strings)
                   for f, event in OPT_IN_ALLOWLIST):
                parent = parents.get(call)
                protected = False
                while parent is not None:
                    if isinstance(parent, ast.If):
                        protected |= any(isinstance(n, ast.Name) and n.id == "_COORD_DEBUG"
                                         for n in ast.walk(parent.test))
                    parent = parents.get(parent)
                assert protected, "coordinate diagnostic lost its opt-in guard"
                continue
            if any(file == f and any(event in s for s in strings)
                   for f, event in NOT_USER_TEXT):
                continue
            parent = parents.get(call)
            while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = parents.get(parent)
            if parent is not None and (file, parent.name) in LOG_TRANSPORTS:
                continue
            # Standalone clipboard self-test prints are explicit diagnostics.
            if file == "samsara/clipboard.py" and is_print:
                continue
            names = set().union(*(unsafe_names(arg) for arg in call.args))
            if names:
                failures.append(f"{file}:{call.lineno}: {sorted(names)}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("file,event", [
    ("dictation.py", "Pre-buffering trailing speech:"),
    ("dictation.py", "[DICTATE] Initial content:"),
    ("samsara/tts/coordinator.py", "context="),
    ("samsara/correction_queue.py", "[CORRECTIONS] undo:"),
    ("samsara/ui/reminder_toast.py", "Reminder toast action failed"),
], ids=["M1", "M2", "M3", "M4", "M5"])
def test_reviewed_source_guard(file, event):
    calls = [n for n in ast.walk(tree(file)) if isinstance(n, ast.Call)
             and any(isinstance(v, ast.Constant) and isinstance(v.value, str)
                     and event in v.value for v in ast.walk(n))
             and (isinstance(n.func, ast.Attribute) and n.func.attr in LEVELS
                  or isinstance(n.func, ast.Name) and n.func.id == "safe_exception")]
    assert calls
    assert not any(unsafe_names(arg) for call in calls for arg in call.args)


def test_guard_rejects_new_leaks_and_accepts_counts():
    for expression in ["logger.debug(text)", "logger.info(f'{raw_text}')",
                       "print(reply.text)", "logger.info('%s', prompt)",
                       "logger.info(text.strip())", "logger.info(self._last_dictation_text)"]:
        call = ast.parse(expression).body[0].value
        assert any(unsafe_names(arg) for arg in call.args)
    call = ast.parse("logger.info('%d %s', len(text), describe_text(reply.text))").body[0].value
    assert not any(unsafe_names(arg) for arg in call.args)
