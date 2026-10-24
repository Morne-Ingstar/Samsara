"""803: synthetic privacy, browser handoff, crash lifecycle and live UI seams."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse
import json
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from samsara.bug_report import capture, coordinator, crash
from samsara.bug_report.github_issue import issue_draft, MAX_URL_BYTES, PASTE_NOTICE
from samsara.bug_report.preview_qt import ReportPreview
from samsara.bug_report.redact import redact_lines, redact_fault, error_first_line
from samsara.live_surface.feedback import FeedbackKind, schedule_feedback
from samsara.session_modes import (SessionModeManager, SessionMode, CommandDispatchResult,
                                   UtteranceSignals)


PRIVATE = (
    "synthetic dictated sentence", "synthetic transcript", "synthetic Ava question",
    "synthetic agent answer", "synthetic clipboard", "Synthetic Window Title",
    r"C:\Users\SyntheticOwner\Documents\private.txt", "sk-fakeExampleCredential",
    "ghp_fakeExampleCredential", "fake@example.invalid", "Bearer fakeBearerValue",
)


class App:
    def __init__(self, **config):
        self.config = config


@pytest.fixture(autouse=True)
def isolate_coordinator(monkeypatch):
    monkeypatch.setattr(coordinator, "_active", None)


@pytest.mark.parametrize("private", PRIVATE)
def test_redactor_drops_every_forbidden_class_even_inside_known_tags(private):
    lines = [private, f"[INIT] {private}", f"[TTS] feedback level=normal {private}",
             f'[CMD] No command matched: "{private}"',
             f"RuntimeError: 12 chars {private}", f"  {private}"]
    assert redact_lines(lines) == ()
    assert private not in error_first_line(private)


def test_allowlist_accepts_actual_numeric_log_masks_and_drops_masked_prose():
    from samsara.log import describe_text
    masked = describe_text("synthetic sample")
    assert masked == "16 chars, 2 words"
    safe = f'[CMD] No command matched: "{masked}"'
    exception = describe_text(ValueError("synthetic sample"))
    assert redact_lines([safe, exception, "[INIT] Enumerating audio devices..."]) == (
        safe, exception, "[INIT] Enumerating audio devices...")
    assert redact_lines(["2026-10-23 12:00:00,123 - INFO - " + safe]) == (safe,)
    assert redact_lines([safe + "\nsynthetic sample"]) == ()
    assert redact_lines(["[EXIT] Goodbye!"] + ["private"] * 50) == ()


def test_fault_frames_omit_profile_and_validate_shipped_code_names():
    lines = ['Windows fatal exception: access violation',
        r'  File "C:\Users\SyntheticOwner\code\samsara\boot.py", line 588 in begin_session',
        r'  File "C:\Users\SyntheticOwner\private.py", line 9 in private_function',
        r'  File "C:\Users\SyntheticOwner\samsara\boot.py", line 9 in private_function']
    result = "\n".join(redact_fault(lines))
    assert "access violation" in result and '<user>/samsara/boot.py' in result
    assert "SyntheticOwner" not in result and "private" not in result


def test_capture_uses_only_allowlisted_metadata_and_last_log_window(tmp_path):
    app = App(ui={"listening_display": "full"}, command_packs={"hermes": False},
              ava={"provider_policy": "local", "question": PRIVATE[2]},
              cloud_llm={"api_key": PRIVATE[7]}, microphone=PRIVATE[5],
              tasks={"todoist": {"enabled": True, "api_token": PRIVATE[8]}})
    app._bug_report_log_dir = tmp_path
    (tmp_path / "samsara.log").write_text("\n".join([*PRIVATE, "[EXIT] Goodbye!"]), encoding="utf-8")
    failed = capture.failure(PRIVATE[0], "error", "command_failed",
                             {"phrase": "ask hermes", "heard": PRIVATE[2]})
    text = capture.snapshot(app, failed)
    assert "Outcome: command_failed" in text and "Display: Full" in text
    assert "Command: ask_agent.ask_hermes" in text
    assert "Ava" in text and "Todoist" in text and "[EXIT] Goodbye!" in text
    for private in PRIVATE:
        assert private not in text


def test_capture_rejects_forged_ids_source_and_config_names():
    app = App(command_packs={PRIVATE[0]: True}, integrations={PRIVATE[1]: True})
    text = capture.snapshot(app, capture.failure(PRIVATE[0], PRIVATE[1], PRIVATE[2],
                                                {"canonical_id": PRIVATE[3]}))
    assert all(private not in text for private in PRIVATE)


def test_disabled_pack_report_identifies_failure_without_heard_words():
    failed = capture.failure('heard: synthetic question - hermes is off', 'warning', 'command_miss',
                             {'reason': 'pack_disabled', 'pack': 'hermes', 'heard': PRIVATE[0]})
    text = capture.snapshot(App(), failed)
    assert 'Outcome: command_miss' in text
    assert 'Command pack is disabled: hermes' in text
    assert PRIVATE[0] not in text and 'synthetic question' not in text


def test_context_stays_bound_to_clicked_error_after_another_result():
    app = App()
    coordinator.remember(app, "Recording failed", "error", "command_failed")
    coordinator.remember(app, "refused: unclear", "warning", "edit_refused")
    failed = coordinator.context_for(app, "Recording failed", "error")
    assert failed.outcome == "command_failed"
    assert failed.error == "Recording failed"
    assert all(isinstance(key, bytes) for key in app._bug_report_failures)


@pytest.mark.parametrize("body", ["Short report", "界" * 1400, "percent % " * 800])
def test_url_is_bounded_after_percent_encoding_and_fallback_keeps_full_body(body):
    draft = issue_draft(body)
    assert len(draft.url.encode("utf-8")) <= MAX_URL_BYTES
    query = parse_qs(urlparse(draft.url).query)
    if draft.clipboard is None:
        assert query["body"] == [body]
    else:
        assert draft.clipboard == body and "body" not in query
    assert "template" not in query  # Markdown body is used instead of the YAML form.


def test_preview_is_exact_and_inert_until_send_or_copy(qapp, monkeypatch, qt_widget_scope):
    opened, copied = [], []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    monkeypatch.setattr(QApplication, "clipboard", lambda: SimpleNamespace(setText=copied.append))
    with qt_widget_scope(ReportPreview("Synthetic diagnostic")) as window:
        window.show()
        window.activity.setText("Opening Settings")
        assert not opened and not copied
        window.copy.click()
        assert copied == [window.body.toPlainText()] and not opened
        window.send.click()
        assert parse_qs(urlparse(opened[0]).query)["body"] == [window.body.toPlainText()]
        assert "Opening Settings" in window.body.toPlainText()
        for button in (window.send, window.copy, window.cancel):
            assert window.rect().contains(button.geometry())
            assert button.geometry().right() <= window.width()-24


def test_preview_masks_user_pasted_secrets_and_fallback_has_paste_instruction(qapp, monkeypatch, qt_widget_scope):
    opened, copied = [], []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    monkeypatch.setattr(QApplication, "clipboard", lambda: SimpleNamespace(setText=copied.append))
    with qt_widget_scope(ReportPreview("Synthetic diagnostic\n" * 200)) as window:
        window.activity.setText("api_key=exampleSecret fake@example.invalid C:\\Users\\Synthetic\\private")
        assert "exampleSecret" not in window.body.toPlainText()
        assert "fake@example.invalid" not in window.body.toPlainText()
        assert "Synthetic\\private" not in window.body.toPlainText()
        window.send.click()
        assert copied == [window.body.toPlainText()]
        assert window.status.text() == PASTE_NOTICE
        assert "body=" not in opened[0]


def test_cancel_and_browser_failure_do_not_claim_a_submission(qapp, monkeypatch, qt_widget_scope):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or False)
    with qt_widget_scope(ReportPreview("Synthetic diagnostic")) as window:
        window.show()
        window.send.click()
        assert "Could not open" in window.status.text()
        before = len(opened)
        window.cancel.click()
        assert not window.isVisible() and len(opened) == before


def test_crash_marker_handoff_offers_once_and_never_reads_the_new_marker(tmp_path, monkeypatch):
    from samsara import boot
    (tmp_path / boot.SESSION_MARKER).write_text(json.dumps({"pid": 424242, "started": "synthetic"}))
    (tmp_path / "faulthandler.log").write_text('--- Samsara start pid=424242 ---\nWindows fatal exception: access violation\nCurrent thread 0x1234:\n  File "C:/Users/Synthetic/samsara/boot.py", line 588 in begin_session\n\n')
    boot.begin_session(tmp_path)
    catch = crash.CrashCatch(*boot._report_previous_session)
    evidence = catch.take()
    assert evidence is not None and any("access violation" in line for line in evidence)
    assert catch.take() is None
    assert crash.CrashCatch(None).take() is None
    boot.end_session(tmp_path)
    boot.begin_session(tmp_path)
    assert boot._report_previous_session is None
    boot.end_session(tmp_path)


def test_startup_consumes_previous_crash_evidence_once(tmp_path, monkeypatch):
    from samsara import boot
    monkeypatch.setattr(boot, "_report_previous_session", (
        {"pid": 424242}, ["Windows fatal exception: access violation", PRIVATE[0]]))
    monkeypatch.setattr(boot, "_report_log_dir", tmp_path)
    offered = []
    monkeypatch.setattr(crash, "offer", lambda app, catch: offered.append(catch.take()))
    app = App()
    boot.offer_previous_report(app)
    boot.offer_previous_report(app)
    assert offered == [("Windows fatal exception: access violation",)]
    assert app._bug_report_log_dir == tmp_path


def test_report_uses_the_frozen_shared_display_choice():
    from samsara.listening_display import run_display
    app = App(ui={"live_surface": {"enabled": True}})
    assert run_display(app).value == "full"
    app.config = {"ui": {"listening_display": "compact"}}
    assert "Display: Full" in capture.snapshot(app)


def test_command_routing_producer_enumeration_reads_literals_and_fails_closed(tmp_path, monkeypatch):
    from tests import test_outcome_chip as chips
    source = tmp_path / "routing.py"
    monkeypatch.setattr(chips, "_COMMAND_SOURCE", source)
    source.write_text('DispatchOutcome(kind="new_command_literal")\n', encoding="utf-8")
    assert "new_command_literal" in chips._dispatch_outcome_kinds()
    source.write_text('DispatchOutcome(kind=computed)\n', encoding="utf-8")
    with pytest.raises(AssertionError, match="DispatchOutcome built with a non-literal kind"):
        chips._dispatch_outcome_kinds()
    source.unlink()
    with pytest.raises(AssertionError, match="Missing outcome producer"):
        chips._dispatch_outcome_kinds()


def test_actual_crash_offer_opens_same_preview_only_after_report(qapp, monkeypatch, qt_widget_scope):
    previews = []
    monkeypatch.setattr(coordinator, "open_preview", lambda app, report: previews.append(report))
    app = App()
    catch = crash.CrashCatch({"pid": 424242}, ["Windows fatal exception: access violation", PRIVATE[0]])
    window = crash.offer(app, catch)
    with qt_widget_scope(window):
        assert not previews and crash.offer(app, catch) is None
        next(b for b in window.findChildren(QPushButton) if b.text() == "Report").click()
        assert len(previews) == 1 and "access violation" in previews[0]
        assert PRIVATE[0] not in previews[0]


def manager():
    return SessionModeManager(abort_phrases=[], foreground_exe_resolver=lambda: "notepad.exe",
        inject_fn=Mock(return_value=True), remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(return_value=CommandDispatchResult(matched=False)),
        agent_dispatch_fn=Mock())


@pytest.mark.parametrize("text", ["report that", "Report that!", " report   that "])
def test_report_that_routes_only_whole_command_lane_utterances(monkeypatch, text):
    opened = []
    monkeypatch.setattr(coordinator, "report_latest", lambda: opened.append(1) or True)
    session = manager()
    assert session.dispatch_command_utterance(text).kind == "command_executed"
    assert opened == [1]
    session._command_dispatch_fn.assert_not_called()


@pytest.mark.parametrize("text", ["please report that problem", "I will report that", "report that later"])
def test_embedded_phrase_does_not_intercept_commands(monkeypatch, text):
    monkeypatch.setattr(coordinator, "report_latest", lambda: pytest.fail("embedded report routed"))
    session = manager()
    session.dispatch_command_utterance(text)
    session._command_dispatch_fn.assert_called_once_with(text)


def test_report_that_remains_dictation_in_dictate_lane(monkeypatch):
    monkeypatch.setattr(coordinator, "report_latest", lambda: pytest.fail("dictation routed"))
    session = manager()
    session.force_mode(SessionMode.DICTATE)
    outcome = session.dispatch_utterance("report that", UtteranceSignals(True))
    assert outcome.kind in ("dictate_injected", "dictate_staged", "dictate_buffered")
    session._command_dispatch_fn.assert_not_called()


def test_full_and_compact_report_buttons_capture_the_clicked_problem(qapp, monkeypatch, qt_widget_scope):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import NoticeKind
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    opened = []
    monkeypatch.setattr(coordinator, "open_preview", lambda app, text: opened.append(text))
    monkeypatch.setattr("samsara.live_surface.controller.qt_runtime.post", lambda callback: None)
    app = App(ui={"listening_display": "full"})
    controller = LiveSurfaceController(app, widget_factory=lambda ctrl: LiveSurfaceWidget(ctrl, reduced_motion=True))
    controller.start()
    with qt_widget_scope(controller.widget):
        app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
        app._schedule_ui = lambda fn, *args: fn(*args)
        schedule_feedback(app, "Recording failed", "error", None, source="command_failed")
        controller.drain()
        band = controller.widget._chip or controller.widget._band
        assert band.report.isVisible()
        QTest.mouseClick(band.report, Qt.MouseButton.LeftButton)
        assert "Error: Recording failed" in opened[-1]
        assert controller.view().notice.message == "Recording failed"
    app = App(ui={"listening_display": "compact"})
    with qt_widget_scope(ListeningIndicator()) as indicator:
        app.listening_indicator = indicator
        coordinator.remember(app, "Recording failed", "error", "command_failed")
        indicator.show()
        indicator.show_outcome("Recording failed", "error", None)
        qapp.processEvents()
        assert indicator._message_window.report.isVisible()
        QTest.mouseClick(indicator._message_window.report, Qt.MouseButton.LeftButton)
        assert "Outcome: command_failed" in opened[-1]
        assert indicator._active_chip()[0] == "Recording failed"
        indicator.destroy()


def test_home_notice_report_keeps_existing_repair_action(qapp, monkeypatch, qt_widget_scope):
    from samsara.ui.home_notice_qt import build_problem_notice
    from PySide6.QtWidgets import QWidget, QLabel
    actions = []
    card, text, repair = build_problem_notice(8, QWidget, lambda text, **kw: QLabel(text),
        lambda label, **kw: QPushButton(label), lambda: actions.append("repair"),
        lambda: actions.append("report"))
    with qt_widget_scope(card):
        card.findChild(QPushButton, "reportProblemNotice").click()
        repair.click()
        assert actions == ["report", "repair"]


def test_voice_report_uses_visible_problem_then_home_notice_then_latest(monkeypatch):
    from samsara.ui import home_signals, qt_runtime
    opened = []
    monkeypatch.setattr(coordinator, 'open_preview', lambda app, text: opened.append(text))
    monkeypatch.setattr(qt_runtime, 'post', lambda fn: fn())
    monkeypatch.setattr(home_signals, 'problem_notice', lambda app: SimpleNamespace(capability=home_signals.DICTATION))
    app = App()
    class Indicator:
        def _active_chip(self):
            return ('Recording failed', 'error')
    app.listening_indicator = Indicator()
    coordinator.install(app)
    assert coordinator.report_latest()
    assert 'Error: Recording failed' in opened[-1]
    app.listening_indicator = None
    assert coordinator.report_latest()
    assert 'Outcome: notice' in opened[-1]
    monkeypatch.setattr(home_signals, 'problem_notice', lambda app: None)
    app._bug_report_latest = capture.Failure('command_failed')
    assert coordinator.report_latest()
    assert 'Outcome: command_failed' in opened[-1]


def test_startup_handoff_does_not_grow_bootstrap_or_session_modes():
    root = Path(__file__).resolve().parents[1]
    assert len((root / 'samsara/session_modes.py').read_text(encoding='utf-8').splitlines()) <= 3359
    source = (root / 'dictation.py').read_text(encoding='utf-8')
    assert '_samsara_boot.offer_previous_report(self)' in source
