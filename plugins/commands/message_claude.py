"""Send a message to the Claude DESKTOP app -- and prove it by observation.

Demo step 4: "tell claude the mode switch fix landed and ask what's next".
Astra review 02_conversational_architecture.md section 3, worked example
"send a message to the Claude app": the target is the desktop app and the
conversation it shows -- not the Claude API, not the ARC inbox that
quick_ask.py writes -- and "sent" means a new message node was OBSERVED in
that conversation, never "I pressed Ctrl+V and Enter".

Two phases, two registered commands:

    claude message prepare   (write)
        "tell claude <text>" / "ask claude <text>" / "message claude <text>" /
        "send claude <text>". Resolves the Claude desktop window and its
        composer, creates an OWNED DRAFT (id, body hash, window handle, the
        conversation title as shown in the window) and stages the policy's
        pending confirmation for the submit: the app asks "Send to Claude:
        <first 60 chars>?". Returns DispatchResult QUEUED with that question.

    claude message submit    (destructive, irreversible -> the policy asks)
        Runs only on the confirmed path ("yes"). Re-verifies window and
        conversation title against the draft, focuses the window, pastes the
        body with the clipboard-preserving paste, READS THE COMPOSER BACK
        through UIA and requires it to equal the body, activates the Send
        control (UIA Invoke), then waits up to OBSERVE_TIMEOUT_S for a new
        message node whose text starts with the body. Observed -> COMPLETED
        "sent: observed". Not observed -> FAILED with state "unknown" and the
        draft kept; a second submit of that draft is refused (no retries on
        unknown: a retry could duplicate the message).

UIA control names, from tools/probe_claude_window.py against the live app
(claude.exe, Electron): the conversation document is the
DocumentControl(AutomationId='RootWebArea') whose Name is "<title> - Claude";
the composer is EditControl(Name='Prompt', ClassName='tiptap ProseMirror')
with Value and Text patterns; the send control is ButtonControl(Name='Send',
Invoke pattern) in the sibling group right of the composer (a 'Submit'
button elsewhere belongs to question forms -- never that one); messages live
under GroupControl(Name='Chat messages') as GroupControl(Name='Message N')
with the text in TextControl children.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from samsara.command_registry import DispatchResult, DispatchState
from samsara.plugin_commands import command
from samsara import execution_policy
from samsara.execution_policy import Invocation, Route
from samsara.log import get_logger

logger = get_logger(__name__)

OBSERVE_TIMEOUT_S = 5.0
COMPOSER_WAIT_S = 3.0
PREVIEW_CHARS = 60

# The ONLY rewrites allowed (prompt: light local shaping, <= 5 idioms).
# A trailing idiom becomes its own sentence; everything before it is sent
# as spoken (first letter capitalised, a full stop added).
IDIOMS = {
    "and ask what's next": "What should I do next?",
    "and ask what is next": "What should I do next?",
    "and ask what to do next": "What should I do next?",
    "and ask for next steps": "What are the next steps?",
    "and ask if it's done": "Is it done?",
}


# ---------------------------------------------------------------------------
# Grammar / shaping (pure)
# ---------------------------------------------------------------------------

def shape_body(text: str) -> str:
    """'the mode switch fix landed and ask what's next'
       -> 'The mode switch fix landed. What should I do next?'
    Anything without a listed idiom is returned exactly as spoken."""
    raw = (text or "").strip()
    lowered = raw.lower().rstrip(".!? ")
    for idiom, replacement in IDIOMS.items():
        if lowered.endswith(idiom):
            head = raw[: len(lowered) - len(idiom)].rstrip(" ,;.!?")
            if not head:
                return replacement
            head = head[0].upper() + head[1:]
            return f"{head}. {replacement}"
    return raw


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def preview(body: str) -> str:
    return body if len(body) <= PREVIEW_CHARS else body[:PREVIEW_CHARS].rstrip() + "..."


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


# ---------------------------------------------------------------------------
# Drafts (owned, in-process)
# ---------------------------------------------------------------------------

@dataclass
class Draft:
    id: str
    body: str
    hash: str
    hwnd: int
    window_title: str
    conversation: str
    created: float = field(default_factory=time.time)
    state: str = "prepared"          # prepared | sent | unknown | failed
    outcome: str = ""

    def preview(self) -> str:
        return preview(self.body)


_drafts: dict = {}
_drafts_lock = threading.Lock()


def get_draft(draft_id: str) -> Optional[Draft]:
    with _drafts_lock:
        return _drafts.get(draft_id)


def _store(draft: Draft) -> None:
    with _drafts_lock:
        _drafts[draft.id] = draft


def clear_drafts() -> None:
    with _drafts_lock:
        _drafts.clear()


# ---------------------------------------------------------------------------
# The adapter: everything that touches the Claude window. One object so the
# tests can replace it wholesale.
# ---------------------------------------------------------------------------

class ClaudeUiaAdapter:
    """Live implementation over `uiautomation` + the window helpers the app
    already uses (app_verbs.resolve_window, windows.get_all_movable_windows,
    window_switcher._force_focus, clipboard.paste_with_preservation)."""

    PROCESS_PREFIX = "claude"

    # -- window ---------------------------------------------------------
    def resolve_window(self):
        """(hwnd, window_title, conversation_title) of the Claude desktop
        app, or None. app_verbs' fuzzy resolver first, validated by process
        (a browser tab titled 'Claude' is not the app); then a plain scan."""
        import psutil  # noqa: PLC0415
        try:
            from plugins.commands.app_verbs import resolve_window  # noqa: PLC0415
            match = resolve_window("claude")
        except Exception as exc:
            logger.debug("[CLAUDE] resolve_window unavailable: %s", exc)
            match = None
        if match is not None and (match[2] or "").lower().startswith(self.PROCESS_PREFIX):
            hwnd, title = match[0], match[1]
        else:
            hwnd = title = None
            try:
                from plugins.commands.windows import get_all_movable_windows  # noqa: PLC0415
                for h, t, pid in get_all_movable_windows():
                    try:
                        if psutil.Process(pid).name().lower().startswith(self.PROCESS_PREFIX):
                            hwnd, title = h, t
                            break
                    except Exception:
                        continue
            except Exception as exc:
                logger.debug("[CLAUDE] window scan failed: %s", exc)
        if hwnd is None:
            return None
        return hwnd, title or "", self.conversation_title(hwnd)

    def _window_control(self, hwnd):
        import uiautomation as uia  # noqa: PLC0415
        return uia.ControlFromHandle(hwnd)

    def conversation_title(self, hwnd) -> str:
        """Name of the deepest RootWebArea document: '<title> - Claude'."""
        try:
            import uiautomation as uia  # noqa: PLC0415
            win = self._window_control(hwnd)
            best = ""
            for doc in _walk(win, lambda c: c.ControlTypeName == "DocumentControl"
                             and c.AutomationId == "RootWebArea", limit=8):
                name = doc.Name or ""
                if len(name) > len(best):
                    best = name
            return best
        except Exception as exc:
            logger.debug("[CLAUDE] conversation title unavailable: %s", exc)
            return ""

    def focus(self, hwnd) -> bool:
        from plugins.commands.window_switcher import _force_focus  # noqa: PLC0415
        return bool(_force_focus(hwnd))

    def is_foreground(self, hwnd) -> bool:
        import win32gui  # noqa: PLC0415
        return win32gui.GetForegroundWindow() == hwnd

    # -- composer -------------------------------------------------------
    def find_composer(self, hwnd):
        import uiautomation as uia  # noqa: PLC0415
        win = self._window_control(hwnd)
        composer = uia.EditControl(searchFromControl=win, searchDepth=60, Name="Prompt")
        return composer if composer.Exists(COMPOSER_WAIT_S, 0.25) else None

    def read_composer(self, composer) -> str:
        import uiautomation as uia  # noqa: PLC0415
        try:
            value = composer.GetPattern(uia.PatternId.ValuePattern)
            if value is not None and value.Value is not None:
                return value.Value
        except Exception:
            pass
        try:
            text = composer.GetPattern(uia.PatternId.TextPattern)
            if text is not None:
                return text.DocumentRange.GetText(-1)
        except Exception:
            pass
        return "".join((c.Name or "") for c in _walk(composer, lambda c: c.ControlTypeName == "TextControl")
                       if c.ClassName != "ProseMirror-trailingBreak")

    def paste(self, body: str, hwnd) -> bool:
        from samsara.clipboard import paste_with_preservation  # noqa: PLC0415
        return bool(paste_with_preservation(body, before_paste=lambda: self.is_foreground(hwnd)))

    # -- send -----------------------------------------------------------
    def find_send(self, composer):
        """The ButtonControl named 'Send' in the composer's own prompt group
        (never a 'Submit' from a question form)."""
        import uiautomation as uia  # noqa: PLC0415
        scope = composer
        for _ in range(3):
            parent = scope.GetParentControl()
            if parent is None:
                break
            scope = parent
        button = uia.ButtonControl(searchFromControl=scope, searchDepth=6, Name="Send")
        return button if button.Exists(1.0, 0.2) else None

    def invoke(self, button) -> bool:
        import uiautomation as uia  # noqa: PLC0415
        pattern = button.GetPattern(uia.PatternId.InvokePattern)
        if pattern is None:
            return False
        pattern.Invoke()
        return True

    # -- observation ----------------------------------------------------
    def message_texts(self, hwnd) -> list:
        """Text of the last few 'Message N' groups under 'Chat messages'."""
        import uiautomation as uia  # noqa: PLC0415
        win = self._window_control(hwnd)
        chat = uia.GroupControl(searchFromControl=win, searchDepth=60, Name="Chat messages")
        if not chat.Exists(0.5, 0.25):
            return []
        groups = [g for g in _walk(chat, lambda c: c.ControlTypeName == "GroupControl"
                                   and (c.Name or "").startswith("Message "), limit=200)]
        texts = []
        for g in groups[-4:]:
            parts = [(c.Name or "") for c in _walk(g, lambda c: c.ControlTypeName == "TextControl", limit=400)]
            texts.append(_norm(" ".join(parts)))
        return texts


def _walk(root, predicate, limit=2000):
    """Depth-first generator over a UIA subtree (bounded)."""
    stack = [root]
    seen = 0
    while stack and seen < limit:
        ctrl = stack.pop()
        seen += 1
        try:
            if predicate(ctrl):
                yield ctrl
            children = ctrl.GetChildren()
        except Exception:
            children = []
        stack.extend(reversed(children))


_adapter: Optional[ClaudeUiaAdapter] = None


def _get_adapter():
    global _adapter
    if _adapter is None:
        _adapter = ClaudeUiaAdapter()
    return _adapter


def set_adapter(adapter) -> None:
    """Tests (and a future dry-run) swap the window adapter here."""
    global _adapter
    _adapter = adapter


# ---------------------------------------------------------------------------
# Feedback helpers
# ---------------------------------------------------------------------------

def _speak(app, text):
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        try:
            coordinator.speak(text, category="confirmation", interruptible=False)
            return
        except TypeError:
            coordinator.speak(text)
            return
        except Exception as exc:
            logger.debug("[CLAUDE] speak failed: %s", exc)
    print(f"[CLAUDE] {text}")


def _chip(app, label, kind):
    show = getattr(app, "_show_outcome_chip", None)
    if show is None:
        return
    try:
        show(label, kind)
    except Exception as exc:
        logger.debug("[CLAUDE] chip failed: %s", exc)


def _failed(app, draft, reason, state="failed", chip="error", extra=None):
    if draft is not None:
        draft.state = state
        draft.outcome = reason
    logger.warning("[CLAUDE] %s", reason)
    print(f"[CLAUDE] {reason}")
    _chip(app, "Outcome unknown" if state == "unknown" else "Not sent", chip)
    detail = {"reason": reason, "state": state, "draft_id": getattr(draft, "id", None)}
    if extra:
        detail.update(extra)
    return DispatchResult(DispatchState.FAILED, "claude message submit", "claude message submit", detail)


# ---------------------------------------------------------------------------
# Phase 1: prepare
# ---------------------------------------------------------------------------

@command("claude message prepare",
         aliases=["tell claude", "ask claude", "message claude", "send claude"],
         pack="ai", risk_class="write", reversible=True, side_effects=["ui"],
         side_effect_category="draft")
def handle_claude_message_prepare(app, remainder):
    """Create an owned draft for the Claude desktop app and ask 'Send?'."""
    body = shape_body(remainder)
    if not body:
        _speak(app, "Tell Claude what?")
        return DispatchResult(DispatchState.FAILED, "claude message prepare", "claude message prepare",
                              {"reason": "empty message"})

    adapter = _get_adapter()
    window = adapter.resolve_window()
    if window is None:
        _speak(app, "The Claude app is not running.")
        _chip(app, "Claude app not running", "error")
        return DispatchResult(DispatchState.FAILED, "claude message prepare", "claude message prepare",
                              {"reason": "Claude app not running"})
    hwnd, title, conversation = window

    composer = adapter.find_composer(hwnd)
    if composer is None:
        _speak(app, "I can't find the message box in the Claude app.")
        _chip(app, "Claude composer not found", "error")
        return DispatchResult(DispatchState.FAILED, "claude message prepare", "claude message prepare",
                              {"reason": "composer not found", "window": title})

    draft = Draft(id=uuid.uuid4().hex[:8], body=body, hash=body_hash(body), hwnd=hwnd,
                  window_title=title, conversation=conversation)
    _store(draft)

    question = f"Send to Claude: {draft.preview()}?"
    generation = execution_policy.current_generation(app)
    inv = Invocation("claude message submit", {"remainder": draft.id}, Route.EXACT, generation,
                     prompt=question, source_text=remainder)

    def _approve(op, _draft_id=draft.id, _inv=inv):
        # The policy's confirmed path: re-authorize (generation re-check) and
        # only then submit -- the same shape commands.py uses for built-ins.
        decision = execution_policy.authorize(_inv, app=app, confirmed=True)
        if not isinstance(decision, execution_policy.Allowed):
            _speak(app, "That request expired. Not sent.")
            return
        handle_claude_message_submit(app, _draft_id)

    execution_policy.stage_pending(app, inv, question, on_approve=_approve, record_type="action",
                                   extra={"draft_id": draft.id})
    _speak(app, question + " Say yes to send, or ava cancel.")
    _chip(app, "Send?", "pending")
    logger.info("[CLAUDE] draft %s prepared for %r (%s): %s", draft.id, conversation or title, draft.hash, question)
    return DispatchResult(DispatchState.QUEUED, "claude message prepare", "claude message prepare", {
        "question": question, "draft_id": draft.id, "body": body, "hash": draft.hash,
        "window": title, "conversation": conversation, "awaiting_confirmation": True,
    })


# ---------------------------------------------------------------------------
# Phase 2: submit (confirmed path only)
# ---------------------------------------------------------------------------

@command("claude message submit", pack="ai", risk_class="destructive", reversible=False,
         side_effects=["message_sent"], side_effect_category="external_write",
         param_schema={"remainder": {"type": "str", "required": True}})
def handle_claude_message_submit(app, remainder):
    """Submit a prepared draft. Reached only after the policy's confirmation
    ('yes' -> stage_pending approve, or the executor's own NeedsConfirmation
    for a spoken 'claude message submit <id>'). Never retries an unknown
    outcome: a second submit of the same draft is refused."""
    draft_id = (remainder or "").strip()
    draft = get_draft(draft_id)
    if draft is None:
        return _failed(app, None, f"no such draft '{draft_id}'")
    if draft.state == "unknown":
        _chip(app, "Outcome unknown", "warning")
        return DispatchResult(DispatchState.REJECTED, "claude message submit", "claude message submit", {
            "reason": "outcome of the previous submit is unknown -- not resubmitting; check the Claude window",
            "state": "unknown", "draft_id": draft.id})
    if draft.state == "sent":
        return DispatchResult(DispatchState.REJECTED, "claude message submit", "claude message submit",
                              {"reason": "already sent", "state": "sent", "draft_id": draft.id})

    adapter = _get_adapter()
    window = adapter.resolve_window()
    if window is None:
        return _failed(app, draft, "Claude app not running, not sent")
    hwnd, title, conversation = window
    if hwnd != draft.hwnd or _norm(conversation) != _norm(draft.conversation):
        return _failed(app, draft, "conversation changed, not sent",
                       extra={"expected": draft.conversation, "found": conversation})

    if not adapter.focus(hwnd):
        return _failed(app, draft, "could not focus the Claude window, not sent")
    composer = adapter.find_composer(hwnd)
    if composer is None:
        return _failed(app, draft, "composer not found, not sent")

    if not adapter.paste(draft.body, hwnd):
        return _failed(app, draft, "paste refused (focus guard), not sent")
    time.sleep(0.15)
    seen = adapter.read_composer(composer)
    if _norm(seen) != _norm(draft.body):
        return _failed(app, draft, "composer text differs from the draft; left in the composer, not sent",
                       extra={"composer": seen[:120]})

    button = adapter.find_send(composer)
    if button is None or not adapter.invoke(button):
        return _failed(app, draft, "send control not found; body left in the composer, not sent")
    submitted_at = time.time()

    deadline = time.monotonic() + OBSERVE_TIMEOUT_S
    head = _norm(draft.body)[:40].lower()
    while True:
        for text in adapter.message_texts(hwnd):
            if text.lower().startswith(head) or head in text.lower():
                draft.state = "sent"
                draft.outcome = "sent: observed"
                _chip(app, "Sent to Claude", "success")
                _speak(app, "Sent.")
                logger.info("[CLAUDE] draft %s observed in conversation %r", draft.id, conversation)
                return DispatchResult(DispatchState.COMPLETED, "claude message submit", "claude message submit", {
                    "state": "sent", "detail": "sent: observed", "draft_id": draft.id,
                    "observed_after_s": round(time.time() - submitted_at, 2)})
        if time.monotonic() >= deadline:
            break
        time.sleep(0.3)

    _speak(app, "I sent it but could not see it appear. Check the Claude window; I will not resend.")
    return _failed(app, draft, "submitted, not observed -- do not retry automatically", state="unknown",
                   chip="warning", extra={"draft_kept": True})
