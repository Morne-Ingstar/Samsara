"""Provider wire calls, route provenance and decoder races; no real accounts/COM/network."""
from contextlib import contextmanager
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import execution_policy as ep, effect_policy as effects, message_delivery as delivery
from samsara import message_adapters as adapters, plugin_commands
from samsara.command_registry import DispatchState
from samsara.commands import CommandExecutor
from plugins.commands import private_messages, ask_ollama


class Timer:
    def __init__(self, seconds, callback, timers):
        self.seconds, self.callback, self.cancelled = seconds, callback, False
        timers.append(self)

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    registry = dict(plugin_commands._REGISTRY)
    modules = {k: dict(v) for k, v in plugin_commands._MODULE_ENTRIES.items()}
    plugin_commands._reinstall_module_commands(private_messages)
    ask_ollama.clear_pending_action()
    ep._speech.last_onset = ep._speech.last_dispatch_start = 0
    app = SimpleNamespace(config={"execution_policy": {"model_tool_allowlist": ["slack message"]}}, _ava_cmd_generation=7,
        audio_coordinator=SimpleNamespace(is_speaking=False), _show_outcome_chip=Mock(), save_config=Mock())
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"commands": {"submit": {"type": "press", "key": "enter"}}}))
    executor = CommandExecutor(path, plugins_dir=tmp_path / "absent", app=app)
    app.command_executor = executor
    timers = []
    original_stage = ep.stage_cancel_window
    monkeypatch.setattr(ep, "stage_cancel_window", lambda *a, **kw: original_stage(
        *a, **dict(kw, timer_factory=lambda s, f: Timer(s, f, timers))))
    monkeypatch.setattr(delivery, "_launch", lambda fn: fn())
    monkeypatch.setattr(delivery, "_speak", Mock(return_value=False))
    monkeypatch.setattr(ep, "listen_for_confirmation", Mock())
    message = adapters.Message("slack", "workspace/sender", "dm-id", ("person-id",), "Pat",
                               "one two three four five six seven eight nine ten", person=True)
    current = [message]
    sends = []
    def send(msg):
        sends.append(msg)
        return adapters.Receipt("sent", "provider-id")
    def stage(msg=None, route=ep.Route.EXACT, setup=False, probe=None):
        inv = ep.Invocation("slack message", {"remainder": "Pat saying fixture message"}, route, 7)
        return delivery.stage(app, inv, msg or current[0], probe or (lambda: current[0]), send, setup=setup)
    yield SimpleNamespace(app=app, executor=executor, timers=timers, message=message,
                          current=current, sends=sends, stage=stage, monkeypatch=monkeypatch)
    ask_ollama.clear_pending_action()
    ep._speech.last_onset = ep._speech.last_dispatch_start = 0
    plugin_commands._REGISTRY.clear()
    plugin_commands._REGISTRY.update(registry)
    plugin_commands._MODULE_ENTRIES.clear()
    plugin_commands._MODULE_ENTRIES.update(modules)


def trust(rig):
    msg = rig.current[0]
    rig.app.config["messaging"] = {"trusted_slack": [{"provider": msg.provider, "account": msg.account,
        "conversation": msg.conversation, "recipients": list(msg.recipients)}]}


def test_known_dm_has_one_second_grace_and_sent_receipt(rig):
    trust(rig)
    result = rig.stage()
    assert result.detail["tier"] == "T2"
    assert [t.seconds for t in rig.timers] == [1.0]
    assert not rig.sends
    delivery._speak.assert_called_once()
    assert "one two three four five six seven eight" in delivery._speak.call_args.args[1]
    rig.timers[-1].fire()
    assert rig.sends == [rig.message]
    assert list(rig.app._message_attempts.values()) == [adapters.Receipt("sent", "provider-id")]


@pytest.mark.parametrize("reply", ["no", "cancel", "stop", "no thanks"])
def test_no_cancel_during_decoder_lag(rig, reply):
    trust(rig)
    ep.note_utterance_start()
    rig.stage()
    expired = rig.timers[-1]
    ep.note_speech_onset(rig.app)
    expired.callback()  # Timeout callback already queued before Timer.cancel.
    assert ep.pending_operation().held
    assert ep.answer_pending(rig.app, reply) == "rejected"
    rig.timers[-1].callback()
    assert not rig.sends


def test_onset_during_slow_timeout_probe_wins(rig):
    trust(rig)
    ep.note_utterance_start()
    def probe():
        ep.note_speech_onset(rig.app)
        return rig.current[0]
    rig.stage(probe=probe)
    rig.timers[0].fire()
    assert not rig.sends
    assert ep.pending_operation().held
    assert ep.answer_pending(rig.app, "no") == "rejected"


def test_hold_timeout_cancels_silence_instead_of_sending(rig):
    trust(rig)
    rig.stage()
    ep.note_speech_onset(rig.app)
    rig.timers[-1].fire()
    assert not rig.sends


def test_send_now_skips_grace_without_duplicate(rig):
    trust(rig)
    rig.stage()
    timer = rig.timers[-1]
    assert ep.answer_pending(rig.app, "send now") == "approved"
    timer.callback()
    assert len(rig.sends) == 1
    assert rig.stage().state is DispatchState.REJECTED


@pytest.mark.parametrize("source", [ep.ANSWER_MODEL, "echo"])
def test_non_user_approval_refused(rig, source):
    trust(rig)
    rig.stage()
    assert ep.answer_pending(rig.app, "send now", source=source) == "refused:model"
    assert not rig.sends


@pytest.mark.parametrize("trusted", [False, True])
def test_spoken_readback_cannot_approve_itself(rig, trusted):
    if trusted:
        trust(rig)
    rig.stage()
    rig.app.audio_coordinator.is_speaking = True
    assert ep.answer_pending(rig.app, "yes") == "refused:readback active"
    assert not rig.sends
    assert ep.answer_pending(rig.app, "no") == "rejected"


@pytest.mark.parametrize("field,value", [("account", "other-account"), ("conversation", "other-dm"),
    ("recipients", ("other-person",)), ("body", "changed body"), ("revision", "new setup")])
def test_target_or_body_change_refuses(rig, field, value):
    trust(rig)
    rig.stage()
    rig.current[0] = replace(rig.message, **{field: value})
    assert ep.answer_pending(rig.app, "send now").startswith("refused:")
    assert not rig.sends


@pytest.mark.parametrize("route", list(ep.Route))
def test_first_contact_and_non_user_routes_have_one_bound_question(rig, route):
    result = rig.stage(route=route)
    assert result.detail["tier"] == "T3"
    assert not rig.timers
    assert not isinstance(ep.pending_operation(), ep.TimedOperation)
    assert ep.answer_pending(rig.app, "yes") == "approved"
    assert len(rig.sends) == 1


def test_setup_trust_is_not_granted_by_first_send_approval(rig):
    rig.stage()
    ep.answer_pending(rig.app, "yes")
    assert "messaging" not in rig.app.config


@pytest.mark.parametrize("kind", ["sent", "unknown", "raise"])
def test_no_blind_retry_even_after_provider_exception(rig, kind):
    calls = []
    def send(msg):
        calls.append(msg)
        if kind == "raise":
            raise TimeoutError()
        return adapters.Receipt(kind)
    inv = ep.Invocation("slack message", {"remainder": "fixture"}, generation=7)
    delivery.stage(rig.app, inv, rig.message, lambda: rig.message, send)
    ep.answer_pending(rig.app, "yes")
    assert len(calls) == 1
    again = delivery.stage(rig.app, inv, rig.message, lambda: rig.message, send)
    assert again.state is DispatchState.REJECTED
    assert len(calls) == 1


def email_message():
    return adapters.Message("outlook", "sender@example.invalid", "draft-id", ("to@example.invalid",),
        "Outlook", "one two three four five six seven eight nine ten", ("cc@example.invalid",),
        ("bcc@example.invalid",), "Fixture subject", (("fixture.txt", "content-hash"),))


def test_email_preview_full_readback_and_one_question(rig):
    msg = email_message()
    rig.current[0] = msg
    trust(rig)  # Trust never downgrades email.
    assert rig.stage().detail["tier"] == "T3"
    op = ep.pending_operation()
    for text in (msg.account, *msg.recipients, *msg.cc, *msg.bcc, msg.subject, "fixture.txt",
                 "one two three four five six seven eight"):
        assert text in op.prompt
    assert msg.body not in op.prompt
    assert ep.answer_pending(rig.app, "read full message") == "extended"
    assert msg.body in delivery._speak.call_args.args[1]
    assert ep.pending_operation() is op
    assert not rig.timers
    assert ep.answer_pending(rig.app, "yes") == "approved"
    assert rig.sends == [msg]


@pytest.mark.parametrize("field,value", [("cc", ("other@example.invalid",)), ("bcc", ()),
    ("subject", "new subject"), ("attachments", (("fixture.txt", "changed bytes"),))])
def test_email_preview_changes_invalidate_approval(rig, field, value):
    rig.current[0] = email_message()
    rig.stage()
    rig.current[0] = replace(rig.current[0], **{field: value})
    assert ep.answer_pending(rig.app, "yes").startswith("refused:")
    assert not rig.sends


def test_email_silence_and_stale_generation_never_send(rig):
    rig.current[0] = email_message()
    rig.stage()
    ep.pending_operation().deadline = -1
    assert ep.answer_pending(rig.app, "yes") == "refused:expired"
    assert not rig.sends
    rig.stage()
    rig.app._ava_cmd_generation += 1
    assert ep.answer_pending(rig.app, "yes") == "refused:stale"
    assert not rig.sends


def test_direct_handler_call_cannot_forge_user_origin(rig):
    result = private_messages.slack_message(rig.app, "Pat saying hello")
    assert result.state is DispatchState.REJECTED


def test_plugin_executor_preserves_model_route_and_checks_allowlist(rig):
    rig.app.config["execution_policy"] = {"model_tool_allowlist": []}
    denied = rig.executor.execute_canonical("slack message", rig.app, args={"remainder": "Pat saying hi"},
        route=ep.Route.MODEL, generation=7)
    assert denied.state is DispatchState.REJECTED
    seen = []
    rig.monkeypatch.setattr(delivery, "slack", lambda app, inv, *a, **kw: seen.append(inv) or True)
    rig.app.config["execution_policy"]["model_tool_allowlist"] = ["slack message"]
    rig.executor.execute_canonical("slack message", rig.app, args={"remainder": "Pat saying hi"},
        route=ep.Route.MODEL, generation=7)
    assert seen[0].route is ep.Route.MODEL
    assert seen[0].generation == 7


def slack_settings():
    return dict(name="Pat", team="T1", sender="U1", recipient="U2", conversation="D1", token_env="SLACK_TEST_ENV")


def slack_request(calls, channel=None, response=None):
    def request(method, args):
        calls.append((method, args))
        if method == "auth.test":
            return dict(ok=True, team_id="T1", user_id="U1")
        if method == "conversations.info":
            return {"ok": True, "channel": channel or dict(id="D1", user="U2", is_im=True)}
        return response if response is not None else dict(ok=True, channel="D1", ts="123.456",
                                                          message=dict(text="fixture body", user="U1"))
    return request


def test_slack_adapter_exact_provider_binding_and_single_wire_send():
    calls = []
    adapter = adapters.SlackAdapter(slack_settings(), slack_request(calls))
    msg = adapter.snapshot("fixture body")
    assert msg.scope == ("slack", "T1/U1", "D1", ("U2",))
    assert adapter.send(msg) == adapters.Receipt("sent", "123.456")
    writes = [args for method, args in calls if method == "chat.postMessage"]
    assert len(writes) == 1
    assert writes[0]["channel"] == "D1" and writes[0]["text"] == "fixture body"
    assert writes[0]["mrkdwn"] is False and writes[0]["parse"] == "none"


@pytest.mark.parametrize("channel", [dict(id="D1", user="U3", is_im=True),
    dict(id="D1", user="U2", is_im=False), dict(id="D2", user="U2", is_im=True)])
def test_slack_adapter_never_guesses_peer_or_group(channel):
    calls = []
    adapter = adapters.SlackAdapter(slack_settings(), slack_request(calls, channel=channel))
    with pytest.raises(ValueError):
        adapter.snapshot("fixture")
    assert not any(method == "chat.postMessage" for method, args in calls)


@pytest.mark.parametrize("response", [{"ok": True}, dict(ok=True, channel="D2", ts="123",
    message=dict(text="fixture body", user="U1")), dict(ok=True, channel="D1", ts="123",
    message=dict(text="changed", user="U1"))])
def test_slack_receipt_must_prove_channel_sender_and_body(response):
    adapter = adapters.SlackAdapter(slack_settings(), slack_request([], response=response))
    assert adapter.send(adapter.snapshot("fixture body")).state == "unknown"


def outlook_fixture():
    recipients = [SimpleNamespace(Type=t, Resolved=True,
        AddressEntry=SimpleNamespace(Type="SMTP", Address=a))
        for t, a in [(1, "to@example.invalid"), (2, "cc@example.invalid"), (3, "bcc@example.invalid")]]
    attachment = SimpleNamespace(Type=1, FileName="fixture.txt",
        PropertyAccessor=SimpleNamespace(GetProperty=lambda key: b"fixture bytes"))
    item = SimpleNamespace(Class=43, Sent=False, SendUsingAccount=SimpleNamespace(SmtpAddress="from@example.invalid"),
        Recipients=SimpleNamespace(Count=3, Item=lambda i: recipients[i-1]),
        Attachments=SimpleNamespace(Count=1, Item=lambda i: attachment), Body="fixture body", HTMLBody="<p>fixture body</p>",
        BodyFormat=2, Subject="fixture subject", EntryID="fixture-id", Send=Mock())
    @contextmanager
    def context(hwnd):
        assert hwnd == 42
        yield item
    return adapters.OutlookAdapter(42, context), item, recipients, attachment


def test_outlook_adapter_binds_all_fields_and_hashes_actual_attachment():
    adapter, item, recipients, attachment = outlook_fixture()
    msg = adapter.snapshot()
    assert msg.recipients == ("to@example.invalid",) and msg.cc and msg.bcc
    assert msg.attachments == (("fixture.txt", adapters.hashlib.sha256(b"fixture bytes").hexdigest()),)
    attachment.PropertyAccessor.GetProperty = lambda key: b"changed bytes"
    assert adapter.send(msg).state == "not_sent"
    item.Send.assert_not_called()


@pytest.mark.parametrize("observed", [False, True])
def test_outlook_send_only_claims_sent_if_provider_observed_it(observed):
    adapter, item, _, _ = outlook_fixture()
    msg = adapter.snapshot()
    item.Send.side_effect = lambda: setattr(item, "Sent", observed)
    assert adapter.send(msg).state == ("sent" if observed else "unknown")
    item.Send.assert_called_once()


def test_outlook_unresolved_recipient_and_implicit_account_are_refused():
    adapter, item, recipients, _ = outlook_fixture()
    recipients[0].Resolved = False
    with pytest.raises(ValueError):
        adapter.snapshot()
    recipients[0].Resolved = True
    item.SendUsingAccount = None
    with pytest.raises(ValueError):
        adapter.snapshot()
    item.Send.assert_not_called()


def test_raw_email_submit_has_complete_preview_and_unknown_no_retry(rig):
    msg = email_message()
    target = effects.SubmitTarget("email", 42, "outlook.exe", "composer", "revision", message=msg)
    rig.monkeypatch.setattr(effects, "resolve_submit_target", lambda app=None: target)
    rig.monkeypatch.setattr(ep, "foreground_window_identity", lambda: {"hwnd": 42, "process": "outlook.exe"})
    from samsara import commands
    run = Mock(return_value=True)
    rig.monkeypatch.setattr(commands, "get_handler", lambda kind: SimpleNamespace(execute=run))
    assert rig.executor.execute_canonical("submit", rig.app).state is DispatchState.QUEUED
    assert msg.bcc[0] in ep.pending_operation().prompt
    assert ep.answer_pending(rig.app, "yes") == "approved"
    assert run.call_count == 1
    assert list(rig.app._message_attempts.values())[0].state == "unknown"
    rig.executor.execute_canonical("submit", rig.app)
    ep.answer_pending(rig.app, "yes")
    assert run.call_count == 1


@pytest.mark.parametrize("process", ["outlook.exe", "olk.exe", "thunderbird.exe"])
def test_unsupported_email_preview_never_sends_keys(rig, process):
    target = effects.SubmitTarget("email", 42, process, "composer", "revision")
    rig.monkeypatch.setattr(effects, "resolve_submit_target", lambda app=None: target)
    assert rig.executor.execute_canonical("submit", rig.app).state is DispatchState.REJECTED


def test_email_click_preview_and_bound_latch(rig):
    from samsara.click_effect_policy import ClickTarget, dispatch
    msg = email_message()
    destination = effects.SubmitTarget("email", 42, "outlook.exe", "composer", "revision", message=msg)
    target = ClickTarget(effects.CONFIRM, destination, "button", "v1", "Send")
    run = Mock(return_value=True)
    # Use a registered raw key id so this unit test exercises real authorization.
    inv = ep.Invocation("key:return", generation=7)
    assert dispatch(rig.app, target, lambda: target, run, invocation=inv).state is DispatchState.QUEUED
    assert msg.cc[0] in ep.pending_operation().prompt and msg.bcc[0] in ep.pending_operation().prompt
    ep.answer_pending(rig.app, "yes")
    assert run.call_count == 1
    dispatch(rig.app, target, lambda: target, run, invocation=inv)
    ep.answer_pending(rig.app, "yes")
    assert run.call_count == 1


def test_ambiguous_contact_never_resolves_by_label(rig):
    row = slack_settings()
    rig.app.config["messaging"] = {"slack": [row, dict(row, conversation="D2")]}
    with pytest.raises(ValueError):
        delivery.slack_adapter(rig.app, "Pat")


def test_schema_is_explicit_config_only_and_defaults_to_no_trust():
    from samsara.config_schema import SETTINGS_SCHEMA
    for key in ("messaging.slack", "messaging.trusted_slack"):
        assert SETTINGS_SCHEMA[key]["default"] == []
        assert SETTINGS_SCHEMA[key]["item_type"] == "dict"
        assert SETTINGS_SCHEMA[key]["settings_ui"] is False


def test_real_plugin_setup_then_message_uses_exact_scope(rig):
    calls = []
    rig.app.config["messaging"] = {"slack": [slack_settings()]}
    rig.monkeypatch.setattr(delivery, "SlackAdapter", lambda cfg: adapters.SlackAdapter(cfg, slack_request(calls)))
    queued = rig.executor.execute_canonical("trust slack conversation", rig.app, args={"remainder": "Pat"})
    assert queued.state is DispatchState.QUEUED
    assert not rig.timers
    assert "T1/U1" in ep.pending_operation().prompt and "D1" in ep.pending_operation().prompt
    assert ep.answer_pending(rig.app, "yes") == "approved"
    rig.app.save_config.assert_called_once()
    assert len(rig.app.config["messaging"]["trusted_slack"]) == 1
    result = rig.executor.execute_canonical("slack message", rig.app,
        args={"remainder": "Pat saying fixture body"})
    assert result.detail["tier"] == "T2"
    rig.timers[-1].fire()
    assert [method for method, args in calls].count("chat.postMessage") == 1


def test_trust_never_downgrades_model_or_other_account(rig):
    trust(rig)
    assert delivery.handling(rig.message, rig.app, ep.Route.MODEL) == effects.CONFIRM
    assert delivery.handling(replace(rig.message, account="other"), rig.app, ep.Route.EXACT) == effects.CONFIRM


def test_slack_default_transport_has_fixed_url_and_no_retry(monkeypatch):
    import io
    monkeypatch.setenv("SLACK_TEST_ENV", "synthetic-fixture")
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append((request, timeout))
            return io.BytesIO(b'{"ok": true, "team_id": "T1", "user_id": "U1"}')
    monkeypatch.setattr(adapters.urllib.request, "build_opener", lambda guard: Opener())
    adapter = adapters.SlackAdapter(slack_settings())
    assert adapter._request("auth.test", {})["user_id"] == "U1"
    assert len(seen) == 1
    assert seen[0][0].full_url == "https://slack.com/api/auth.test" and seen[0][1] == 10
    assert isinstance(json.loads(seen[0][0].data), dict)
    assert adapters._NoRedirect().redirect_request(None, None, None, None, None, None) is None


def test_changed_draft_after_approval_before_worker_never_sends(rig):
    jobs = []
    rig.monkeypatch.setattr(delivery, "_launch", jobs.append)
    rig.stage()
    assert ep.answer_pending(rig.app, "yes") == "approved"
    rig.current[0] = replace(rig.message, body="changed after yes")
    jobs[0]()
    assert not rig.sends


def test_new_command_or_continued_dictation_cancels_dm(rig):
    trust(rig)
    rig.stage()
    ep.note_speech_onset(rig.app)
    assert ep.after_utterance(rig.app, "dictate_staged") == "cancelled"
    rig.timers[-1].callback()
    assert not rig.sends


def test_snapshot_repr_keeps_body_and_subject_out():
    msg = email_message()
    assert msg.body not in repr(msg) and msg.subject not in repr(msg)


def test_wake_email_uses_full_bound_preview_instead_of_generic_return_question(rig, monkeypatch):
    import sys
    msg = email_message()
    target = effects.SubmitTarget("email", 42, "outlook.exe", "composer", "revision", message=msg)
    monkeypatch.setattr(effects, "resolve_submit_target", lambda app=None: target)
    app = rig.app
    app.config["auto_paste"] = True
    app._wake_session_mode = "focus_dictate"
    app._wake_session_target_hwnd, app._wake_session_target_process = 42, "outlook.exe"
    app._wake_session_delivered_text = msg.body
    app._wake_session_started_at = 10
    app.wake_dictation_buffer = []
    app.play_sound, app._emit_wake_trace, app._end_wake_session = Mock(), Mock(), Mock()
    press = Mock()
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(press=press))
    assert effects.submit_wake_profile(app) is False
    op = ep.pending_operation()
    assert msg.bcc[0] in op.prompt and msg.account in op.prompt and msg.attachments[0][0] in op.prompt
    assert not rig.timers
    assert ep.answer_pending(app, "yes") == "approved"
    press.assert_called_once_with("return")
    app._end_wake_session.assert_called_once()
    assert effects.submit_wake_profile(app) is False
    assert press.call_count == 1
