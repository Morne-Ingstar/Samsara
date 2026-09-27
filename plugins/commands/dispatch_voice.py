"""Voice drafting and confirmed approval of Agora prompt dispatches."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from samsara import agora_bridge, execution_policy
from samsara.agent_channel.dispatch_voice import DispatchVoice
from samsara.agent_channel.queue_voice import prompt_id
from samsara.command_registry import DispatchState
from samsara.execution_policy import Invocation, Route
from samsara.plugin_commands import command
from samsara.runtime import thread_registry
from plugins.commands import queue_voice

_SEND = re.compile(r"^send\s+(.+?)\s+to\s+(sol|luna|astra|claude code)[.!?]?\s*$", re.I)
_STOP = threading.Event()


@dataclass
class FeedState:
    since: str | None = None
    seen: deque = field(default_factory=lambda: deque(maxlen=512))
    spoken_states: set[tuple[str, str]] = field(default_factory=set)
    speech_queue: deque = field(default_factory=deque)
    active_speech: object | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    blocked: dict[str, dict] = field(default_factory=dict)
    needs_chip: bool = False


@dataclass(frozen=True)
class SpeechItem:
    text: str
    task_id: str = ""
    event_state: str = ""
    event: bool = False


def match_send(text: str) -> bool:
    return bool(_SEND.fullmatch((text or "").strip()))


def _client(app) -> DispatchVoice:
    return getattr(app, "_dispatch_voice_client", None) or DispatchVoice()


def _feed_state(app) -> FeedState:
    state = getattr(app, "_dispatch_voice_feed", None)
    if state is None:
        state = FeedState()
        app._dispatch_voice_feed = state
    return state


def _start_next_speech(app, state: FeedState) -> None:
    with state.lock:
        if state.active_speech is not None or not state.speech_queue or _STOP.is_set():
            return
        item = state.speech_queue.popleft()
        token = object()
        state.active_speech = token

    def done():
        with state.lock:
            if state.active_speech is not token:
                return
            state.active_speech = None
        _start_next_speech(app, state)

    try:
        coordinator = getattr(app, "audio_coordinator", None)
        if coordinator is not None:
            handle = coordinator.speak(item.text, category="ava_response",
                                       queue_mode="append", interruptible=True,
                                       on_done=done)
            if str(getattr(handle, "utterance_id", "")).startswith("noop-"):
                done()
        else:
            queue_voice._say(app, item.text)
            done()
    except Exception:
        done()


def _say(app, text: str, chip: str | None = None, *,
         task_id: str = "", event_state: str = "", is_event: bool = False,
         state: FeedState | None = None) -> None:
    state = state or _feed_state(app)
    with state.lock:
        key = (task_id, event_state)
        if task_id and event_state:
            if key in state.spoken_states:
                return
            state.spoken_states.add(key)
        if is_event and task_id:
            # Progress can be superseded; preserve plan and approval readbacks.
            superseded = {"preparing": {"running", "reported", "blocked"},
                          "running": {"reported", "blocked"}}
            state.speech_queue = deque(item for item in state.speech_queue
                                       if not (item.event and item.task_id == task_id
                                               and event_state in superseded.get(item.event_state, set())))
        state.speech_queue.append(SpeechItem(text, task_id, event_state, is_event))
    show = getattr(app, "_show_outcome_chip", None)
    if show is not None and chip:
        show(chip, "info")
    _start_next_speech(app, state)


def _failure(exc: Exception, prompt: str = "") -> str:
    if isinstance(exc, agora_bridge.AgoraUnreachableError):
        return "Agora isn't running"
    if isinstance(exc, agora_bridge.AgoraDispatchHTTPError):
        if exc.status == 404:
            return f"{prompt} isn't in the queue" if prompt else "That prompt isn't in the queue"
        if exc.status == 409:
            return "that plan changed since you heard it"
        return exc.spoken or f"Agora refused: {exc.error}"
    return "Agora couldn't complete that request"


def _show_needs_chip(app, state: FeedState) -> None:
    if not state.needs_chip:
        return
    show = getattr(app, "_show_outcome_chip", None)
    if show is not None:
        show("needs you", "pending", None)


def poll_once(app, client: DispatchVoice, state: FeedState) -> list[dict]:
    events = client.events(state.since)
    fresh = []
    with state.lock:
        for event in sorted(events, key=lambda row: (row.get("at", ""), row.get("task_id", ""))):
            key = (event.get("task_id"), event.get("state"), event.get("at"), event.get("spoken"))
            if key in state.seen:
                continue
            state.seen.append(key)
            at = event.get("at")
            if at and (state.since is None or at > state.since):
                state.since = at
            task_id = event.get("task_id", "")
            if event.get("state") == "blocked":
                state.blocked[task_id] = event
                state.needs_chip = True
            else:
                state.blocked.pop(task_id, None)
            fresh.append(event)
    # Only the newest state for each task in this tick needs a spoken update.
    latest = {event.get("task_id"): event for event in fresh}
    for event in fresh:
        if latest[event.get("task_id")] is not event:
            continue
        spoken = event.get("spoken")
        if spoken:
            # Feed text must never become a voice confirmation utterance.
            if spoken.strip().lower().rstrip(".!?") in {"approve", "yes", "reject", "no"}:
                spoken = "Agora reported a status update."
            _say(app, spoken, task_id=event.get("task_id", ""),
                 event_state=event.get("state", ""), is_event=True, state=state)
    _show_needs_chip(app, state)
    return events


def start_services(app) -> None:
    config = (getattr(app, "config", {}) or {}).get("dispatch_voice", {}) or {}
    if not config.get("enabled", True) or getattr(app, "_dispatch_voice_poller", None):
        return
    state = _feed_state(app)
    if state.since is None:
        state.since = datetime.now(timezone.utc).isoformat()
    client = _client(app)
    interval = max(1.0, float(config.get("poll_s", 5)))
    thread_registry.on_shutdown(_STOP.set)

    def run():
        while not _STOP.wait(interval):
            try:
                poll_once(app, client, state)
            except Exception:
                pass  # unavailable Agora is reported by explicit voice requests

    app._dispatch_voice_poller = thread_registry.spawn("dispatch.voice.events", run)


@command("send prompt", pack="core", risk_class="write", reversible=True,
         ai_visible=False, side_effects=["network", "audio", "ui"],
         preview_template="Draft a prompt dispatch for approval")
def send_prompt(app, remainder=""):
    if not (getattr(app, "config", {}) or {}).get("dispatch_voice", {}).get("enabled", True):
        _say(app, "Voice dispatch is off in settings.")
        return DispatchState.FAILED
    match = _SEND.fullmatch("send " + (remainder or "").strip())
    if match is None:
        _say(app, "Say send, a prompt number, to Sol, Luna, Astra, or Claude Code.")
        return DispatchState.FAILED
    pid = prompt_id(match.group(1))
    identity = match.group(2).lower()
    if pid is None:
        _say(app, "Say a prompt number.")
        return DispatchState.FAILED
    client = _client(app)
    try:
        # Agora binds effort to the queue prompt; the spoken command does not specify it.
        draft = client.draft(pid, identity.title())
    except Exception as exc:
        _say(app, _failure(exc, pid))
        return DispatchState.FAILED
    question = f"{draft.spoken}. Approve to start?"
    generation = execution_policy.current_generation(app)
    inv = Invocation("send prompt", {"remainder": remainder}, Route.GRAMMAR,
                     generation, source_text="send " + remainder)

    def approved(_op):
        decision = execution_policy.authorize(inv, app=app, confirmed=True)
        if not isinstance(decision, execution_policy.Allowed):
            _say(app, "cancelled, nothing started")
            return
        try:
            result = client.approve(draft.task_id, draft.plan_hash)
            _say(app, result.get("spoken") or f"{pid} approved for {identity.title()}.",
                 task_id=draft.task_id, event_state=result.get("state", "queued"))
        except Exception as exc:
            _say(app, _failure(exc, pid))

    def rejected(_op):
        _say(app, "cancelled, nothing started")

    op = execution_policy.stage_pending(app, inv, question, on_approve=approved,
                                        on_reject=rejected, record_type="action", ttl_s=60,
                                        extra={"task_id": draft.task_id,
                                               "plan_hash": draft.plan_hash})

    def expire():
        if execution_policy.pending_operation() is op and op.approved is None:
            execution_policy._release_slot(op)
            op.cancel(app, "expired", chip=False)

    thread_registry.timer("dispatch.voice.expiry", 60.05, expire, daemon=True)
    _say(app, question + " Say approve or no.", "dispatch: confirm",
         task_id=draft.task_id, event_state="draft")
    return DispatchState.QUEUED


@command("what needs me", pack="core", risk_class="safe", reversible=True,
         side_effects=["network", "audio", "ui"])
def what_needs_me(app, _remainder=""):
    client = _client(app)
    try:
        drafts = client.pending()
        events = client.events()
    except Exception as exc:
        _say(app, _failure(exc))
        return DispatchState.FAILED
    latest = {}
    for event in sorted(events, key=lambda row: row.get("at", "")):
        latest[event.get("task_id")] = event
    blocked = [row for row in latest.values() if row.get("state") == "blocked"]
    items = [f"{row['prompt_id']} awaiting approval: {row['summary']}." for row in drafts]
    items += [row.get("spoken", f"{row.get('prompt_id')} needs you.") for row in blocked]
    view = queue_voice._view(app)
    view._items = items
    view._index = 0
    text, count = view._page() if items else ("Nothing needs you right now.", 0)
    _say(app, queue_voice._next_page_hint(view, text), f"needs you: {len(items)}")
    state = getattr(app, "_dispatch_voice_feed", None)
    if state is not None:
        state.needs_chip = False
    return True
