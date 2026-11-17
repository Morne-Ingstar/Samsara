"""1242: real confirmation routes separate private visual and spoken previews."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama
from samsara import ava_pending, commands, effect_policy as effects
from samsara import execution_policy as ep, message_delivery as delivery
from samsara.command_registry import DispatchState
from tests.test_message_adapters_1031c import email_message, rig, trust


_LISTEN = ep.listen_for_confirmation


def speech_route(rig, monkeypatch, microphone=False):
    monkeypatch.setattr(ep, "listen_for_confirmation", _LISTEN)
    spoken = Mock(return_value=False)
    monkeypatch.setattr(ask_ollama, "speak", spoken)
    monkeypatch.setattr(ava_pending.thread_registry, "timer", Mock())
    if microphone:
        rig.app._ensure_wake_consumer = Mock()
        rig.app._release_wake_consumer = Mock()
    return spoken


@pytest.mark.parametrize("provider", ["outlook", "slack"])
@pytest.mark.parametrize("subject", ["Project check-in", "", " \t "])
@pytest.mark.parametrize("microphone", [False, True])
def test_short_confirmation_and_explicit_full_readback(rig, monkeypatch, provider, subject, microphone):
    spoken = speech_route(rig, monkeypatch, microphone)
    msg = replace(email_message() if provider == "outlook" else rig.message, subject=subject)
    rig.current[0] = msg
    result = rig.stage()
    assert result.state is DispatchState.QUEUED and result.detail["tier"] == "T3"
    op = ep.pending_operation()
    assert msg.preview() in op.prompt
    short = spoken.call_args.args[1]
    assert msg.spoken_preview() in short
    for recipient in (*msg.recipients, *msg.cc, *msg.bcc):
        assert recipient in short
    assert f"Subject: {subject.strip() or 'none'}" in short
    assert "one two three" not in short and msg.account not in short
    assert "fixture.txt" not in short
    assert "read full message" in short and "yes to confirm or no to cancel" in short
    if microphone:
        assert msg.preview() in rig.app._ava_question.text
        assert msg.preview() in rig.app._ava_question.label
        assert rig.app._ava_question.opened
    assert not rig.sends and not rig.timers
    delivery._speak.assert_not_called()
    assert ep.answer_pending(rig.app, "read full message") == "extended"
    assert delivery._speak.call_args.args[1] == msg.preview(full=True)
    assert ep.pending_operation() is op and not rig.sends
    assert ep.answer_pending(rig.app, "yes") == "approved"
    assert rig.sends == [msg]


def test_trusted_dm_waits_for_short_speech_then_keeps_one_second_grace(rig):
    trust(rig)
    delivery._speak.return_value = True
    assert rig.stage().detail["tier"] == "T2"
    short = delivery._speak.call_args.args[1]
    assert "Pat" in short and "person-id" in short and "Subject: none" in short
    assert "one two three" not in short and "cancel or send now" in short
    assert not rig.timers and not rig.sends
    delivery._speak.call_args.kwargs["on_done"]()
    assert [timer.seconds for timer in rig.timers] == [1.0]
    assert ep.answer_pending(rig.app, "read full message") == "extended"
    assert delivery._speak.call_args.args[1] == rig.message.preview(full=True)
    assert ep.answer_pending(rig.app, "no") == "rejected"
    rig.timers[-1].callback()
    assert not rig.sends


@pytest.mark.parametrize("route", ["input", "click"])
@pytest.mark.parametrize("change", [False, True])
def test_native_email_routes_speak_short_and_bind_hidden_body(rig, monkeypatch, route, change):
    spoken = speech_route(rig, monkeypatch)
    msg = email_message()
    current = [effects.SubmitTarget("email", 42, "outlook.exe", "composer", "revision", message=msg)]
    monkeypatch.setattr(effects, "resolve_submit_target", lambda app=None: current[0])
    monkeypatch.setattr(ep, "foreground_window_identity", lambda: {"hwnd": 42, "process": "outlook.exe"})
    run = Mock(return_value=True)
    if route == "input":
        monkeypatch.setattr(commands, "get_handler", lambda kind: SimpleNamespace(execute=run))
        result = rig.executor.execute_canonical("submit", rig.app)
    else:
        from samsara.click_effect_policy import ClickTarget, dispatch
        target = ClickTarget(effects.CONFIRM, current[0], "button", "v1", "Send")
        result = dispatch(rig.app, target, lambda: replace(target, destination=current[0]), run,
                          invocation=ep.Invocation("key:return", generation=7))
    assert result.state is DispatchState.QUEUED
    op = ep.pending_operation()
    assert msg.preview() in op.prompt
    assert msg.spoken_preview() in spoken.call_args.args[1]
    assert "one two three" not in spoken.call_args.args[1]
    assert not run.called
    if change:
        # Same visible eight words; the unsaid remainder still invalidates consent.
        current[0] = replace(current[0], message=replace(msg, body=msg.body + " changed"))
    reply = ep.answer_pending(rig.app, "yes")
    assert reply.startswith("refused:") if change else reply == "approved"
    assert run.call_count == int(not change)


def test_trust_setup_keeps_exact_account_and_conversation_question(rig, monkeypatch):
    spoken = speech_route(rig, monkeypatch)
    rig.stage(setup=True)
    op = ep.pending_operation()
    assert not hasattr(op, "message_spoken_prompt")
    assert spoken.call_args.args[1] == op.prompt + " Say yes to confirm or no to cancel."
    assert rig.message.account in op.prompt and rig.message.conversation in op.prompt
    assert ep.answer_pending(rig.app, "no") == "rejected"
