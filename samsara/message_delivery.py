"""1031c: one shared bound operation, one provider attempt, honest receipts."""
import copy
from dataclasses import replace
import threading

from samsara.message_adapters import Message, OutlookAdapter, SlackAdapter, Receipt


_request = threading.local()
_attempt_lock = threading.Lock()
_COMMANDS = {"slack message", "trust slack conversation", "send outlook email"}


def finish_resolution(op, approved):
    from samsara import execution_policy as ep
    op._event.set()
    question = getattr(op._app, "_ava_question", None)
    if (question is not None and question.operation is op
            and isinstance(question.runner, ep._ConfirmationListener)):
        from samsara import ava_pending
        ava_pending._clear(op._app, question, "Confirmed." if approved else "Cancelled.")
    callback = op._on_approve if approved else op._on_reject
    if callback is not None:
        try:
            callback(op)
        except Exception:
            ep.logger.error("[POLICY] pending callback failed")  # Never expose provider/body exceptions.
    return True


def run_timed_operation(op, reason, *, release=True):
    from samsara import execution_policy as ep
    why = op.refusal(op._app)
    if why is not None:
        if release:
            ep._release_slot(op)
        if why != "already answered":
            op.cancel(op._app, why)
        return False
    if op.handling.mode == "short_send_grace":
        return resolve_grace(op, reason, release=release)
    if release:
        ep._release_slot(op)
    op.outcome_reason = reason
    ep.logger.info("[POLICY] cancel window ran %s (%s) after %.2fs", op.invocation.command_id,
                   reason, op.delay_s)
    return op.resolve(True)


def resolve_grace(op, reason, *, release=True):
    """Serialize the send claim with onset, then call the worker outside locks.

    A slow provider probe may finish after a cancellation started. Re-read the
    speech edge after that probe; the old timeout must not win over the onset.
    """
    from samsara import execution_policy as ep
    with ep._speech.lock:
        speaking = ep._speech.last_onset > ep._speech.last_dispatch_start > 0
        if reason == "window elapsed" and (op.held or speaking):
            op.hold()
            return False
        with op._lock:
            if op.approved is not None:
                return False
            op.approved = True
            op.outcome_reason = reason
    if release:
        ep._release_slot(op)
    op._disarm()
    return op._finish_resolution(True)


def deferred(inv):
    from samsara import execution_policy as ep
    entry = ep._plugin_entry(inv.command_id) or {}
    return (inv.effect_target is None and inv.command_id in _COMMANDS
            and entry.get("source") == "plugins.commands.private_messages")


def remember(app, inv, decision):
    from samsara import execution_policy as ep
    _request.value = ((app, copy.deepcopy(inv)) if deferred(inv)
                      and isinstance(decision, ep.Allowed) else None)


def take(app, command):
    previous = getattr(_request, "value", None)
    _request.value = None
    if previous and previous[0] is app and previous[1].command_id == command:
        return previous[1]
    return None  # Direct/model calls cannot manufacture an exact-user invocation.


def handling(message, app, route):
    from samsara import effect_policy as effects
    trusted = getattr(app, "config", {}).get("messaging", {}).get("trusted_slack", [])
    scope = {"provider": message.provider, "account": message.account,
             "conversation": message.conversation, "recipients": list(message.recipients)}
    if (message.provider == "slack" and message.person and not message.attachments
            and route in {"exact", "grammar"} and scope in trusted):
        return effects.PRIVATE_SEND
    return effects.CONFIRM


def _speak(app, text, **kwargs):
    from plugins.commands import ask_ollama
    return ask_ollama.speak(app, text, category="confirmation", **kwargs)


def _reply_filter(app, reply, source, capture_path):
    from samsara import execution_policy as ep
    # Cancellation remains usable while TTS or a decoder is still busy.
    if reply == ep.REPLY_NO or source == ep.ANSWER_DIALOG:
        return None
    question = getattr(app, "_ava_question", None)
    if (getattr(getattr(app, "audio_coordinator", None), "is_speaking", False)
            or (question is not None and not question.opened)):
        return "refused:readback active"
    return None


def _result(dispatch_state, command, **detail):
    from samsara.command_registry import DispatchResult, DispatchState
    return DispatchResult(getattr(DispatchState, dispatch_state), command, command, detail)


def _launch(fn):
    from samsara.runtime import thread_registry
    thread_registry.spawn("message.send", fn, daemon=True)


def stage(app, inv, message, probe, send, *, timer_factory=None, launch=None, setup=False):
    """Prepare immediately. Never stack the T2 timer with a T3 question.

    `probe` re-reads setup/account/draft; `send` rechecks at the provider seam.
    No body, address, preview or credential is written to an evidence/history file.
    """
    from samsara import execution_policy as ep, effect_policy as effects
    if inv is None:
        return _result("REJECTED", "message", reason="No authorized invocation")
    inv = replace(inv, args=copy.deepcopy(inv.args), effect_target=message)
    expected = ep.argument_hash(inv.command_id, inv.args)
    mode = effects.CONFIRM if setup else handling(message, app, inv.route)
    key = (inv.generation, message.fingerprint)
    attempts = getattr(app, "_message_attempts", None)
    if attempts is None:
        attempts = app._message_attempts = {}
    if key in attempts and not setup:
        return _result("REJECTED", inv.command_id, reason="Already attempted; check the conversation", state=attempts[key].state)

    def refusal():
        if not ep.is_fresh(app, inv.generation):
            return "stale"
        if ep.argument_hash(inv.command_id, inv.args) != expected:
            return "message arguments changed"
        if message != probe():
            return "message, account or destination changed"
        if not setup and mode != handling(message, app, inv.route):
            return "conversation trust changed"
        return None

    def commit():
        try:
            if refusal() or not isinstance(ep.authorize(inv, app=app, confirmed=True), ep.Allowed):
                ep._chip(app, "Message changed; not sent", "warning")  # PLACEHOLDER COPY (1031c)
                return
            if setup:
                send(message)
                return
            with _attempt_lock:
                if key in attempts or not ep.is_fresh(app, inv.generation):
                    return
                attempts[key] = Receipt("unknown")  # Before any provider write, including a raise.
            receipt = send(message)
            if not isinstance(receipt, Receipt):
                receipt = Receipt("unknown")
            attempts[key] = receipt
        except Exception:
            # Never log a provider exception: it can contain credentials or the body.
            receipt = attempts.get(key, Receipt("not_sent"))
        if setup:
            ep._chip(app, "Conversation trust was not saved", "error")  # PLACEHOLDER COPY (1031c)
            return
        labels = {"sent": "Sent: provider observed submission", "not_sent": "Not sent; draft kept",
                  "unknown": "Send outcome unknown; check the conversation before sending again"}
        ep._chip(app, labels.get(receipt.state, labels["unknown"]),
                 "success" if receipt.state == "sent" else "warning")  # PLACEHOLDER COPY (1031c)

    def approve(_op=None):
        (launch or _launch)(commit)

    prompt = message.preview()
    if setup:
        prompt = (f"Trust Slack account {message.account}, recipient {message.label} "
                  f"({', '.join(message.recipients)}), conversation {message.conversation} "
                  "for the one-second private-message grace?")  # PLACEHOLDER COPY (1031c)
    else:
        prompt += ". Send? Say read full message for the full body."  # PLACEHOLDER COPY (1031c)
    if mode == effects.PRIVATE_SEND:
        op = ep.stage_cancel_window(app, inv, message.label, 1.0, approve,
            bind_foreground=False, timer_factory=timer_factory, start=False,
            refusal_guard=refusal, reply_filter=lambda *args: _reply_filter(app, *args))
        op.handling = mode
        preview = message.preview() + ". Say cancel or send now."  # PLACEHOLDER COPY (1031c)
        ep._chip(app, preview, "pending")
        if not _speak(app, preview, on_done=op.start):
            op.start()
    else:
        op = ep.stage_pending(app, inv, prompt, on_approve=approve,
                              refusal_guard=refusal, record_type="action")
        op.voice_reply_filter = lambda *args: _reply_filter(app, *args)
        op.message_readback = lambda: _speak(app, message.preview(full=True))
        ep._chip(app, prompt, "pending")
        ep.listen_for_confirmation(app, prompt)
    op.message_readback = lambda: _speak(app, message.preview(full=True))
    return _result("QUEUED", inv.command_id, tier=mode.tier, send_grace_s=mode.wait_s, op_id=op.op_id)


def slack_adapter(app, name):
    rows = getattr(app, "config", {}).get("messaging", {}).get("slack", [])
    matches = [row for row in rows if isinstance(row, dict)
               and str(row.get("name", "")).casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError("Slack contact is missing or ambiguous; set up one exact conversation")
    return SlackAdapter(matches[0])


def slack(app, inv, name, body, *, setup=False):
    if inv is None or not name or (not setup and not body.strip()):
        return _result("REJECTED", "slack message", reason="Say slack message contact saying message")
    try:
        probe = lambda: slack_adapter(app, name).snapshot(body)
        message = probe()
        def send(bound):
            if not setup:
                return slack_adapter(app, name).send(bound)
            scope = {"provider": bound.provider, "account": bound.account,
                     "conversation": bound.conversation, "recipients": list(bound.recipients)}
            cfg = app.config.setdefault("messaging", {})
            trusted = cfg.setdefault("trusted_slack", [])
            if scope not in trusted:
                trusted.append(scope)
                try:
                    app.save_config()
                except Exception:
                    trusted.remove(scope)
                    raise
            from samsara import execution_policy as ep
            ep._chip(app, "This Slack conversation now uses the one-second grace", "success")  # PLACEHOLDER COPY (1031c)
        return stage(app, inv, message, probe, send, setup=setup)
    except Exception:
        return _result("REJECTED", inv.command_id, reason="Slack setup or destination could not be verified")


def outlook(app, inv):
    from samsara import execution_policy as ep
    identity = ep.foreground_window_identity() or {}
    if inv is None or identity.get("process") != "outlook.exe":
        return _result("REJECTED", "send outlook email", reason="Open an unsent classic Outlook draft first")
    adapter = OutlookAdapter(identity["hwnd"])
    try:
        message = adapter.snapshot()
        def probe():
            if ep.foreground_window_identity() != identity:
                raise ValueError("Outlook window changed")
            return adapter.snapshot()
        return stage(app, inv, message, probe, adapter.send)
    except Exception:
        return _result("REJECTED", inv.command_id, reason="Complete Outlook draft preview unavailable; nothing sent")


def readback_reply(app, op, text, source):
    from samsara import execution_policy as ep
    if (source != ep.ANSWER_VOICE or ep._normalize_reply(text) != "read full message"
            or not callable(getattr(op, "message_readback", None))):
        return None
    if not op.is_live(app):
        return "refused:expired or changed"
    op.message_readback()
    return "extended"


def outlook_snapshot(hwnd, process):
    if process != "outlook.exe":
        return None
    try:
        return OutlookAdapter(hwnd).snapshot()
    except Exception:
        return None


def bind_email_question(app, op, message):
    if message is not None:
        op.voice_reply_filter = lambda *args: _reply_filter(app, *args)
        op.message_readback = lambda: _speak(app, message.preview(full=True))


def email_input(app, message, run):
    """An input event has no provider receipt. Keep its outcome explicitly unknown."""
    from samsara import execution_policy as ep
    key = (ep.current_generation(app), message.fingerprint)
    with _attempt_lock:
        attempts = getattr(app, "_message_attempts", None)
        if attempts is None:
            attempts = app._message_attempts = {}
        if key in attempts:
            return _result("REJECTED", "email send", reason="Already attempted; check Outlook", state=attempts[key].state)
        attempts[key] = Receipt("unknown")
    try:
        return run()
    finally:
        ep._chip(app, "Send outcome unknown; check Outlook before sending again", "warning")  # PLACEHOLDER COPY (1031c)


def email_activation(app, target, run):
    import re
    destination = target.destination
    if (destination.kind != "email"
            or destination.process not in {"outlook.exe", "olk.exe", "thunderbird.exe"}
            or not set(re.findall(r"\w+", target.label.casefold())) & {"send", "submit"}):
        return None
    if destination.message is None:
        # 1031b local adapters already bind stable destination IDs. Preserve
        # their bound question; an executable alone cannot override that contract.
        return (None, None) if email_preview_missing(destination) else None
    message = destination.message
    return (lambda: email_input(app, message, run)), message.preview() + ". Send? Say read full message for the full body."


def email_preview_missing(target):
    """Native email needs a preview unless a local adapter binds its identity.

    The predecessor's local adapter seam supplies account/conversation/recipient
    IDs. Keep that seam's gate and final target probe; enrich provider snapshots
    with full readback without inferring destination semantics from an exe name.
    """
    return (target.kind == "email" and target.message is None
            and target.process in {"outlook.exe", "olk.exe", "thunderbird.exe"}
            and not (target.account and target.conversation and target.recipient))
