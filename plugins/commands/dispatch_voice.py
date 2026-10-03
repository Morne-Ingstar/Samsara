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
from samsara.ava_memory import log_conversation
from samsara.log import get_logger

_SEND = re.compile(r"^(?:send|dispatch)\b", re.I)
_DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
           "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_SPOKEN = {
    "number": "Which prompt number?", "identity": "To Luna, Sol, Astra or Claude Code?",
    "absent": "I don't see that prompt in the queue.",
    "choose_ready": "Which prompt: {ids}?", "choose_exact": "{ids}?",
    "absent_ready": "I don't see that prompt in the queue. Ready: {ids}.",
    "queue_unavailable": "I can't read the queue folder.",
    "expired": "That dispatch question expired. Say the full request again.",
    "repeat": "Say that again, I lost the end.",
    "plan_waiting": "The dispatch plan is already waiting for approval.",
    "plan_target": "The pending plan is for {identity}. Say no, then send again.",
}
_FOLLOW_UP_S = 30.0
_LETTER_HEARINGS = {"8": "a", "eight": "a", "hey": "a", "be": "b",
                    "sea": "c", "the": "d"}
_IDENTITIES = {
    "sol": "sol", "soul": "sol", "sole": "sol", "seoul": "sol",
    "the soul": "sol", "the sun": "sol", "s o l": "sol",
    "the sun s o l": "sol", "solomon": "sol",
    "luna": "luna", "lunar": "luna", "loona": "luna",
    "astra": "astra", "astro": "astra", "aster": "astra", "astrid": "astra",
    "claude code": "claude code", "cloud code": "claude code", "claude": "claude code",
}
_STOP = threading.Event()
logger = get_logger(__name__)
_FEED_INIT_LOCK = threading.Lock()
_EVENT_STATES = frozenset({"draft", "queued", "preparing", "running", "reported",
                           "blocked", "failed", "cancelled"})


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).split())


def _identity(text: str) -> str | None:
    return _IDENTITIES.get(_words(text))


def _prompt_id(text: str) -> str | None:
    words = _words(text).split()
    if not words:
        return None
    suffix = ""
    if len(words) > 1 and (words[-1] in {"b", "bee"} or
                           len(words[-1]) == 1 and words[-1].isalpha()):
        suffix = "b" if words[-1] == "bee" else words[-1]
        words = words[:-1]
    if len(words) == 1 and re.fullmatch(r"\d{2,3}[a-z]?", words[0]):
        return prompt_id(words[0] + suffix)
    digits = [word if len(word) == 1 and word.isdigit() else _DIGITS.get(word)
              for word in words]
    if len(digits) not in (2, 3) or any(digit is None for digit in digits):
        return None
    return prompt_id("".join(digits) + suffix)


def _send_parts(text: str) -> tuple[str, str | None] | None:
    words = _words(text)
    match = re.match(r"^(?:send|dispatch)\s+(?:(?:prompt|number)\s+)*(.+)$", words)
    if not match:
        return None
    body = match.group(1)
    parts = re.split(r"\bto\b", body, maxsplit=1)
    if len(parts) == 2:
        left, right = parts
        return left, _identity(right)
    for alias in sorted(_IDENTITIES, key=len, reverse=True):
        if body.endswith(" " + alias):
            return body[:-(len(alias) + 1)], _IDENTITIES[alias]
    return body, None


def parse_send(text: str) -> tuple[str | None, str | None] | None:
    """Canonical queue id and identity, including bounded Whisper spellings."""
    parts = _send_parts(text)
    return (_prompt_id(parts[0]), parts[1]) if parts is not None else None


def _ready_ids(app) -> tuple[str, ...] | None:
    """Read queue-root IDs with QueueView's existing filename and label parser."""
    try:
        view = queue_voice._view(app)
        return tuple(sorted({view.note.label(path).lower()
                             for path in view._prompt_files(view.paths.queue_root)}))
    except Exception as exc:
        logger.warning("[DISPATCH] ready queue unavailable: %s", type(exc).__name__)
        return None


def _number_hint(text: str) -> tuple[str, str]:
    """Return heard digits and optional suffix; allow a few Whisper letter forms."""
    from plugins.commands.window_switcher import PHONETIC

    words = _words(text).split()
    if not words:
        return "", ""
    if len(words) == 1 and re.fullmatch(r"\d{4}", words[0]) and words[0][-1] in {"8", "3"}:
        return words[0][:-1], {"8": "a", "3": "b"}[words[0][-1]]
    suffix = ""
    last = words[-1]
    if last in _LETTER_HEARINGS or last in PHONETIC or (len(last) == 1 and last.isalpha()):
        suffix = _LETTER_HEARINGS.get(last) or PHONETIC.get(last, last).lower()
        words.pop()
    elif last == "3" and len(words) > 1:
        prior = "".join(word if word.isdigit() else _DIGITS.get(word, "") for word in words[:-1])
        if len(prior) >= 3:
            suffix = "b"
            words.pop()
    values = [word if word.isdigit() else _DIGITS.get(word) for word in words]
    if any(value is None for value in values):
        return "", ""
    digits = "".join(values)
    return digits, suffix


def _resolve_ids(heard: str, ready: tuple[str, ...],
                 choices: tuple[str, ...] = ()) -> tuple[str, ...]:
    available = tuple(pid for pid in choices if pid in ready) if choices else ready
    exact = _prompt_id(heard)
    if exact in available:
        return (exact,)
    digits, suffix = _number_hint(heard)
    if not digits and not suffix:
        return ()
    if not suffix:
        return tuple(pid for pid in available if pid.startswith(digits)) if digits else ()
    matches = tuple(pid for pid in available if pid == digits + suffix)
    if matches:
        return matches
    if not digits:
        return tuple(pid for pid in available if pid.endswith(suffix))
    return tuple(pid for pid in available if pid.endswith(suffix)
                 and _digits_in_order(digits, re.match(r"\d+", pid).group()))


def _digits_in_order(heard: str, candidate: str) -> bool:
    variants = {heard}
    variants.update(heard[:index] + heard[index + 1:]
                    for index in range(1, len(heard)) if heard[index] == heard[index - 1])
    for variant in variants:
        positions = iter(candidate)
        if all(any(digit == value for value in positions) for digit in variant):
            return True
    return False


def _list_ids(ids: tuple[str, ...]) -> str:
    if len(ids) == 1:
        return ids[0]
    return ", ".join(ids[:-1]) + " or " + ids[-1]


def _number_question(ready: tuple[str, ...], choices: tuple[str, ...] = ()) -> str:
    if choices:
        return _SPOKEN["choose_exact"].format(ids=_list_ids(choices))
    if ready:
        return _SPOKEN["choose_ready"].format(ids=_list_ids(ready[:4]))
    return _SPOKEN["number"]


def _absent_line(ready: tuple[str, ...]) -> str:
    return (_SPOKEN["absent_ready"].format(ids=_list_ids(ready[:4]))
            if ready else _SPOKEN["absent"])


def _stage_follow_up(state: FeedState, pid: str | None, identity: str | None,
                     choices: tuple[str, ...] = (), retry: int = 0) -> None:
    state.follow_up = (pid, identity)
    state.follow_up_choices = choices
    state.follow_up_deadline = time.monotonic() + _FOLLOW_UP_S
    state.follow_up_retry = retry


def _looks_like_reply(text: str) -> bool:
    return bool(_identity(text) or _prompt_id(text) or _number_hint(text) != ("", ""))


def awaiting_follow_up(app, text: str = "") -> bool:
    """Forward a live reply, or an expired ID/identity for a spoken refusal."""
    state = getattr(app, "_dispatch_voice_feed", None)
    return bool(state and state.follow_up is not None
                and (time.monotonic() <= state.follow_up_deadline or _looks_like_reply(text)))


def is_dispatch_shaped(text: str) -> bool:
    raw = (text or "").strip()
    words = _words(raw)
    return bool(re.match(r"^(?:send|dispatch)\b.*\bto\b", words)
                or re.match(r"^send\s+\d", words)
                or re.match(r"^send\s+(?:prompt\s+)?(?:number\s+)?(?:zero|one|two|three|four|five|six|seven|eight|nine)\b", words)
                or re.match(r"^count\s+number\s+\d", words))


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
    follow_up: tuple[str | None, str | None] | None = None
    follow_up_choices: tuple[str, ...] = ()
    follow_up_deadline: float = 0.0
    follow_up_retry: int = 0
    task_meta: dict[str, tuple[str, str]] = field(default_factory=dict)
    recent_prompt: str | None = None
    recent_identity: str | None = None


@dataclass(frozen=True)
class SpeechItem:
    text: str
    task_id: str = ""
    event_state: str = ""
    event: bool = False


def match_send(text: str) -> bool:
    parsed = parse_send(text)
    return parsed is not None and all(parsed)


def _remember(app, prompt: str, identity: str, event: str) -> None:
    """The model sees what Samsara actually did, never a guessed action."""
    try:
        from plugins.commands.ask_ollama import ensure_ava_memory
        stamp = datetime.now().strftime("%H:%M")
        target = f"{prompt} to {identity.title()}" if identity else prompt
        ensure_ava_memory(app).add_action_event(
            f"[action] {target}: {event} at {stamp}")
    except Exception as exc:
        logger.warning("[DISPATCH] Ava action history unavailable: %s", type(exc).__name__)


def route_ava_utterance(app, text: str) -> bool:
    """Claim dispatch and near misses before the model; return False otherwise."""
    state = _feed_state(app)
    raw = (text or "").strip()
    words = _words(raw)
    expired = state.follow_up is not None and time.monotonic() > state.follow_up_deadline
    follow_up = state.follow_up if not expired else None
    choices, retry = state.follow_up_choices, state.follow_up_retry
    state.follow_up = None
    state.follow_up_choices = ()
    if expired and not is_dispatch_shaped(raw) and _looks_like_reply(raw):
        _say(app, _SPOKEN["expired"], lane="near-miss",
             user_text=raw, action="follow-up expired")
        return True
    if follow_up is not None and not is_dispatch_shaped(raw):
        pid, identity = follow_up
        ready = _ready_ids(app)
        if ready is None:
            _say(app, _SPOKEN["queue_unavailable"], lane="refused",
                 user_text=raw, action="queue unavailable")
            return True
        ids = _resolve_ids(raw, ready, choices) if pid is None else (pid,)
        if identity is None:
            identity = _identity(raw)
        if len(ids) == 1 and identity:
            send_prompt(app, f"{ids[0]} to {identity}", heard=raw)
            return True
        if len(ids) > 1:
            choices = ids
        if not ids and pid is None and _number_hint(raw) != ("", ""):
            question = _absent_line(ready)
        else:
            question = (_number_question(ready, choices) if not ids else _SPOKEN["identity"])
        if retry == 0:
            _stage_follow_up(state, pid, identity, choices, retry=1)
        _say(app, question, lane="near-miss", user_text=raw, action="follow-up unresolved")
        return True
    clarification = _identity(raw)
    if state.recent_prompt and clarification:
        response = (_SPOKEN["plan_waiting"]
                    if clarification == state.recent_identity else
                    _SPOKEN["plan_target"].format(identity=state.recent_identity.title()))
        _say(app, response,
             lane="dispatch", user_text=raw, action="clarification")
        return True
    if words == "what needs me":
        what_needs_me(app)
        return True
    if raw.casefold().startswith("send ") and raw.endswith("-") and len(words.split()) <= 3:
        _remember(app, "dispatch request", "", "asked to repeat an incomplete request")
        _say(app, _SPOKEN["repeat"], lane="near-miss",
             user_text=raw, action="repeat requested")
        return True
    parts = _send_parts(raw)
    if parts is None and not is_dispatch_shaped(raw):
        return False
    heard_id, identity = parts or (words.removeprefix("count number "), None)
    ready = _ready_ids(app)
    if ready is None:
        _say(app, _SPOKEN["queue_unavailable"], lane="refused",
             user_text=raw, action="queue unavailable")
        return True
    ids = _resolve_ids(heard_id, ready)
    if not ids and heard_id:
        response = _absent_line(ready)
        _say(app, response, lane="near-miss", user_text=raw, action="prompt absent")
        return True
    pid = ids[0] if len(ids) == 1 else None
    if len(ids) > 1 or not pid or not identity:
        _stage_follow_up(state, pid, identity, ids if len(ids) > 1 else ())
        question = (_number_question(ready, ids if len(ids) > 1 else ())
                    if not pid else _SPOKEN["identity"])
        _remember(app, pid or "dispatch request", identity or "",
                  "asked for a prompt number" if not pid else "asked for an identity")
        _say(app, question, lane="near-miss", user_text=raw, action="clarification")
        return True
    send_prompt(app, f"{pid} to {identity}", heard=raw)
    return True


def _client(app) -> DispatchVoice:
    return getattr(app, "_dispatch_voice_client", None) or DispatchVoice()


def _feed_state(app) -> FeedState:
    state = getattr(app, "_dispatch_voice_feed", None)
    if state is None:
        with _FEED_INIT_LOCK:
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
         state: FeedState | None = None, lane: str | None = None,
         user_text: str = "", action: str = "") -> None:
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
    if lane is not None:
        log_conversation(lane=lane, provider="Agora", user_text=user_text,
                         ava_spoken=text, ava_shown=chip or text, action=action or event_state)
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
        with state.lock:
            known_prompt, known_identity = state.task_meta.get(event.get("task_id"), ("", ""))
        prompt = str(event.get("prompt_id") or known_prompt)
        identity = known_identity
        identity = identity or _identity(str(event.get("identity") or ""))
        event_state = str(event.get("state") or "")
        if prompt and event_state in _EVENT_STATES:
            spoken_status = re.search(r"\bSTATUS\s+([A-Z_]+)", str(event.get("spoken") or ""), re.I)
            result = event.get("result")
            status = str(event.get("status") or (result.get("status") if isinstance(result, dict) else "")
                         or (spoken_status.group(1) if spoken_status else ""))
            status_text = f" STATUS {status}" if status else ""
            state_text = {"running": "started", "reported": "finished",
                          "blocked": "blocked; needs me"}.get(event_state, event_state)
            _remember(app, prompt, identity, state_text + status_text)
        if latest[event.get("task_id")] is not event:
            continue
        spoken = event.get("spoken")
        if spoken:
            # Feed text must never become a voice confirmation utterance.
            if spoken.strip().lower().rstrip(".!?") in {"approve", "yes", "reject", "no"}:
                spoken = "Agora reported a status update."
            _say(app, spoken, task_id=event.get("task_id", ""),
                 event_state=event.get("state", ""), is_event=True, state=state,
                 lane="dispatch", action=(event_state if event_state in _EVENT_STATES
                                          else "ignored unrecognized event"))
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
def send_prompt(app, remainder="", *, heard=None):
    if not (getattr(app, "config", {}) or {}).get("dispatch_voice", {}).get("enabled", True):
        _say(app, "Voice dispatch is off in settings.", lane="refused",
             user_text=heard or remainder, action="dispatch disabled")
        return DispatchState.FAILED
    parsed = parse_send("send " + (remainder or "").strip())
    if parsed is None:
        _say(app, _SPOKEN["number"], lane="near-miss",
             user_text=heard or remainder, action="invalid dispatch")
        return DispatchState.FAILED
    pid, identity = parsed
    if pid is None:
        _say(app, _SPOKEN["number"], lane="near-miss",
             user_text=heard or remainder, action="invalid prompt")
        return DispatchState.FAILED
    if identity is None:
        _say(app, _SPOKEN["identity"], lane="near-miss",
             user_text=heard or remainder, action="invalid identity")
        return DispatchState.FAILED
    client = _client(app)
    try:
        # Agora binds effort to the queue prompt; the spoken command does not specify it.
        draft = client.draft(pid, identity.title())
    except Exception as exc:
        _remember(app, pid, identity, "draft refused")
        _say(app, _failure(exc, pid), lane="refused", user_text=heard or remainder,
             action="draft failed")
        return DispatchState.FAILED
    state = _feed_state(app)
    with state.lock:
        state.task_meta[draft.task_id] = (pid, identity)
        state.recent_prompt = pid
        state.recent_identity = identity
    _remember(app, pid, identity, "plan read back")
    question = f"{draft.spoken}. Approve to start?"
    generation = execution_policy.current_generation(app)
    inv = Invocation("send prompt", {"remainder": remainder}, Route.GRAMMAR,
                     generation, source_text="send " + remainder)

    def approved(_op):
        state.recent_prompt = None
        state.recent_identity = None
        decision = execution_policy.authorize(inv, app=app, confirmed=True)
        if not isinstance(decision, execution_policy.Allowed):
            _remember(app, pid, identity, "approval refused")
            _say(app, "cancelled, nothing started", lane="refused",
                 user_text=getattr(_op, "reply_text", ""), action="approval refused")
            return
        try:
            result = client.approve(draft.task_id, draft.plan_hash)
            _remember(app, pid, identity, "approved")
            _say(app, result.get("spoken") or f"{pid} approved for {identity.title()}.",
                 task_id=draft.task_id, event_state=result.get("state", "queued"),
                 lane="dispatch", user_text=getattr(_op, "reply_text", ""), action="approved")
        except Exception as exc:
            _remember(app, pid, identity, "approval failed")
            _say(app, _failure(exc, pid), lane="refused",
                 user_text=getattr(_op, "reply_text", ""), action="approval failed")

    def rejected(_op):
        state.recent_prompt = None
        state.recent_identity = None
        _remember(app, pid, identity, "cancelled")
        _say(app, "cancelled, nothing started", lane="dispatch",
             user_text=getattr(_op, "reply_text", ""), action="cancelled")

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
         task_id=draft.task_id, event_state="draft", lane="dispatch",
         user_text=heard or "send " + remainder, action="plan read back")
    return DispatchState.QUEUED


@command("what needs me", pack="core", risk_class="safe", reversible=True,
         side_effects=["network", "audio", "ui"])
def what_needs_me(app, _remainder=""):
    client = _client(app)
    try:
        drafts = client.pending()
        events = client.events()
    except Exception as exc:
        _say(app, _failure(exc), lane="refused", user_text="what needs me",
             action="needs-me unavailable")
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
    _say(app, queue_voice._next_page_hint(view, text), f"needs you: {len(items)}",
         lane="dispatch", user_text="what needs me", action="needs-me summary")
    state = getattr(app, "_dispatch_voice_feed", None)
    if state is not None:
        state.needs_chip = False
    return True
