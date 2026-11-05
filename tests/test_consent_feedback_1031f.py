"""Both consent displays, actual reply lifecycle and speech priority; inert effects."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama
from samsara import ava_pending, consent_feedback, effect_policy as effects, execution_policy as ep
from samsara.live_surface.feedback import Feedback, FeedbackKind, FeedbackPurpose, schedule_feedback
from samsara.live_surface.model import Notice, NoticeKind
from samsara.message_adapters import Message
from samsara.ui.indicator_message_layout import message_lines
from samsara.live_surface.placement import Edge
from tests.test_message_durations_475 import display
from tests.test_indicator_messages_fit_775 import displays


@pytest.fixture(autouse=True)
def clean_pending():
    ask_ollama.clear_pending_action()
    ep._speech.last_onset = ep._speech.last_dispatch_start = 0
    yield
    op = ep.pending_operation()
    if op is not None:
        op.cancel(chip=False)
        ep._release_slot(op)
    ask_ollama.clear_pending_action()


def email():
    return Message("outlook", "sender@example.invalid", "draft-id",
        ("pat@example.invalid",), "Pat", "Synthetic test message with eight safe preview words only",
        cc=("lee@example.invalid",), bcc=("sam@example.invalid",),
        subject="Synthetic planning example", attachments=(("example.pdf", "hash"),))


def install(app, tier="T3", clock=None):
    app._ava_cmd_generation = 7
    message = email() if tier == "T3" else Message("slack", "example/sender", "dm-id",
        ("person-id",), "Pat", "Synthetic test message with eight safe preview words only", person=True)
    inv = ep.Invocation("send outlook email" if tier == "T3" else "slack message",
        route=ep.Route.EXACT, generation=7, effect_target=message)
    if tier == "T2":
        op = ep.stage_cancel_window(app, inv, "Pat", 1, Mock(), bind_foreground=False,
                                    start=False, timer_factory=Mock())
        op.handling = effects.PRIVATE_SEND
    else:
        op = ep.PendingOperation(inv, message.preview() + ". Send?", app=app,
                                  **({"clock": clock} if clock else {}))
        ep._install_record(app, {"type": "action", "op": op})
    return op


@pytest.mark.parametrize("tier", ["T2", "T3"])
def test_shared_handoff_shows_bound_action_and_recipient_without_private_history(display, tier):
    d = display
    d.app.history_db = SimpleNamespace(add=Mock())
    schedule_feedback(d.app, "Synthetic older error", "error", None)
    d.flush()
    d.app.history_db.add.reset_mock()
    op = install(d.app, tier)
    schedule_feedback(d.app, "generic pending words", "pending", 100)
    d.flush()
    text = d.text()
    assert "Pat" in text or "pat@example.invalid" in text
    assert "Synthetic test message" in text
    if tier == "T2":
        assert "Sending to Pat" in text and "send now" in text
        assert "no guaranteed recall" in text and "yes to confirm" not in text
    else:
        assert "To:" in text and "CC:" in text and "BCC:" in text
        assert "Subject:" in text and "example.pdf" in text
        assert "No answer cancels" in text and "Cancel before it runs" in text
    assert d.deadline() is None
    d.app.history_db.add.assert_not_called()
    op.cancel(chip=False)
    if d.c:
        d.i.reconcile_interactions()
    else:
        d.expire()
    d.flush()
    assert d.text() in ("", "Synthetic older error")  # Full restores its retained error.


def test_reply_microphone_keeps_recipient_and_timeout_cancels_without_commit(display, monkeypatch):
    d = display
    app = d.app
    monkeypatch.setattr(ava_pending.time, "monotonic", lambda: d.clock[0])
    monkeypatch.setattr(ava_pending.thread_registry, "timer", lambda *_a, **_kw: Mock())
    spoken = []
    def speak(_app, text, **kwargs):
        spoken.append((text, kwargs))
        return True
    monkeypatch.setattr(ask_ollama, "speak", speak)
    app.config["tts"] = {"feedback_level": "silent"}
    app.audio_coordinator = SimpleNamespace(cancel_speech=Mock())
    app._ensure_wake_consumer = Mock()
    app._release_wake_consumer = Mock()
    app.app_state, app.wake_word_triggered = "asleep", False
    op = install(app, clock=lambda: d.clock[0])
    committed = Mock()
    op._on_approve = committed
    ep.listen_for_confirmation(app, op.prompt)
    app.audio_coordinator.cancel_speech.assert_called_once()
    assert "pat@example.invalid" in spoken[0][0]
    spoken[0][1]["on_done"]()
    d.flush()
    assert app._ava_question.opened
    assert "pat@example.invalid" in d.text()
    assert "Say yes to confirm" in d.text() and "30 s" in d.text()
    assert app._ava_question.duration == 30
    d.clock[0] = 31
    ava_pending._tick(app, app._ava_question)
    d.flush()
    assert app._ava_question is None
    assert op.approved is False and ep.pending_operation() is None
    assert "Cancelled" in d.text()
    committed.assert_not_called()
    app._release_wake_consumer.assert_called_once_with("ava_question")


@pytest.mark.parametrize("tier", ["T2", "T3"])
@pytest.mark.parametrize("scale", [1, 1.5])
def test_complete_consent_text_fits_full_and_compact(displays, qapp, tier, scale):
    d = displays(Edge.BOTTOM, .5, scale)
    app = SimpleNamespace(config={})
    op = install(app, tier)
    purpose = FeedbackPurpose.RUNNING if tier == "T2" else FeedbackPurpose.QUESTION
    feedback = Feedback(FeedbackKind.RUNNING if tier == "T2" else FeedbackKind.QUESTION,
                        purpose, "policy:" + op.op_id, alive=lambda: op.approved is None)
    label = consent_feedback.pending_label(app, "pending", feedback)
    notice = Notice(NoticeKind.RUNNING if tier == "T2" else NoticeKind.PENDING,
        label, None, feedback=feedback.kind, purpose=purpose, owner=feedback.owner)
    d.full.refresh(replace(d.view, notice=notice), animate=False)
    d.controller._place_widget()
    d.compact.show_feedback(label, "pending", None, feedback)
    qapp.processEvents()
    band = d.full._band
    assert not band.elided
    assert ''.join(band.label.text().split()) == ''.join(label.split())
    assert d.full.rect().contains(band.geometry())
    assert band.rect().contains(band.label.geometry())
    assert label in d.full.accessibleDescription()
    assert label in d.compact.accessibleDescription()
    rect = d.compact._layout()[3]
    lines, elided = message_lines(label, d.compact._chip_font(), rect.width()-24,
                                  max_lines=consent_feedback.compact_max_lines(d.compact))
    assert not elided and len(lines) > 2
    assert ''.join(''.join(lines).split()) == ''.join(label.split())
    assert rect.height() >= len(lines)*d.compact.fontMetrics().lineSpacing()
    assert d.area.contains(d.full.geometry()) and d.area.contains(d.compact.geometry())


def test_compact_consent_expansion_keeps_current_retry_button_space(displays, qapp):
    d = displays(Edge.BOTTOM, .5, 1)
    d.compact.show_retry_notice("Synthetic missed words", Mock())
    qapp.processEvents()
    assert d.compact._report_width() == 100  # n05: retain master's Try again width.
    assert consent_feedback.compact_max_lines(d.compact) is None
    d.compact.clear_retry_notice()
    qapp.processEvents()
    assert consent_feedback.compact_max_lines(d.compact) == 2


def test_old_question_cannot_supply_a_successor_preview():
    app = SimpleNamespace(config={})
    old = install(app)
    feedback = Feedback(FeedbackKind.QUESTION, FeedbackPurpose.QUESTION, "policy:" + old.op_id)
    install(app, "T2")
    assert consent_feedback.pending_label(app, "old handoff", feedback) == "old handoff"


def test_confirmation_interrupts_old_speech_and_full_readback_ignores_answer_length(monkeypatch):
    from samsara import message_delivery
    events = []
    app = SimpleNamespace(config={"ava": {"max_response_length": 20}},
        audio_coordinator=SimpleNamespace(
            cancel_speech=lambda: events.append("cancel"),
            speak=lambda text, **kw: events.append((text, kw)) or SimpleNamespace(utterance_id="spoken")))
    monkeypatch.setattr(ask_ollama.ava_turn_queue, "can_deliver", lambda _app: True)
    monkeypatch.setattr(ask_ollama, "get_max_response_length", lambda _app: 20)
    body = ("Synthetic full readback. " * 30).rstrip()
    assert message_delivery._speak(app, body)
    assert events[0] == "cancel" and events[1][0] == body
    assert events[1][1]["category"] == "confirmation"
    assert events[1][1]["interruptible"] is True


@pytest.mark.parametrize("tier", [effects.IMMEDIATE, effects.LOCAL_EDIT])
def test_ordinary_results_are_never_relabelled_as_pending(display, tier):
    d = display
    install(d.app)
    schedule_feedback(d.app, "Synthetic ordinary result", "success", 1800,
        source="command_executed", detail={"state": "completed", "tier": tier.tier})
    d.flush()
    assert d.text() == "Synthetic ordinary result"
    assert d.deadline() is not None
