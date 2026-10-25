"""776: explicit, draft-scoped recovery from an unpreservable clipboard.

The attempt context crosses the legacy bool-returning injection callback without
global last-error state. Consent is single-use, bound to a document revision,
target window and clipboard sequence; none of these receipts are persisted.
"""
from contextvars import ContextVar
from dataclasses import dataclass


IMAGE_MESSAGE = "Clipboard has a large image Samsara can't put back."
BITMAP_MESSAGE = "Clipboard has an image Samsara can't put back."
CHOICE_SUFFIX = ' Say "type it" or "paste anyway" (image lost), or click.'
CHOICE_MESSAGE = IMAGE_MESSAGE + CHOICE_SUFFIX
LOSS_MESSAGE = "Image removed from clipboard."
_attempt = ContextVar("clipboard_delivery_attempt", default=None)


@dataclass
class Attempt:
    mode: str = "normal"
    sequence: int | None = None
    reason: str = ""
    image: bool = False
    replaced: bool = False


def current_attempt():
    return _attempt.get()


def fail(reason, *, image=False, sequence=None):
    receipt = current_attempt()
    if receipt is not None:
        receipt.reason, receipt.image = reason, image
        if receipt.mode == "normal":
            receipt.sequence = sequence


def clipboard_failure_message():
    from samsara import clipboard as cb
    try:
        if cb._user32 is not None and cb._user32.GetOpenClipboardWindow():
            return "Paste blocked - clipboard held by another app"
    except Exception:
        pass
    return "Text wasn't inserted. Draft kept."


def draft_key(manager):
    document = manager.draft_document
    return document.document_id, document.revision


def inject_commit(manager, text, guard, target_mode, *, inject_fn=None):
    receipt = current_attempt() or Attempt()
    manager._clipboard_attempt = receipt
    token = _attempt.set(receipt)
    try:
        return (inject_fn or manager._inject_fn)(text, guard)
    finally:
        _attempt.reset(token)
        if receipt.image and receipt.mode == "normal":
            manager._clipboard_choice = (draft_key(manager), manager._dictate_target_process,
                                         manager._dictate_target_hwnd, receipt.sequence, target_mode,
                                         receipt.reason)


def failed_commit(manager, text, foreground, hwnd):
    from samsara.session_modes import DispatchOutcome, check_focus_lock
    after_process, after_hwnd = manager._foreground_exe_resolver(), manager._foreground_hwnd_resolver()
    if not check_focus_lock(foreground, after_process) or after_hwnd != hwnd:
        if manager._on_focus_lock_revert:
            manager._on_focus_lock_revert()
        return DispatchOutcome(kind="dictate_commit_blocked_focus_lock", detail={
            "pending_chars": len(text), "target_process": foreground, "foreground": after_process,
            "target_hwnd": hwnd, "foreground_hwnd": after_hwnd,
            "clipboard_image_removed": manager._clipboard_attempt.replaced})
    receipt = manager._clipboard_attempt
    if receipt.image and receipt.mode == "normal":
        return DispatchOutcome(kind="dictate_clipboard_choice", detail={
            "pending_chars": len(text), "reason": receipt.reason + CHOICE_SUFFIX})
    reason = receipt.reason or clipboard_failure_message()
    if receipt.replaced:
        reason = LOSS_MESSAGE + " Text wasn't inserted. Draft kept."
    if manager._on_switch_dispatch_error:
        manager._on_switch_dispatch_error(RuntimeError(reason))
    return DispatchOutcome(kind="dictate_commit_failed", detail={
        "pending_chars": len(text), "error": reason, "clipboard_reason": True})


def complete_commit(manager, detail):
    if manager._clipboard_attempt.replaced:
        detail["clipboard_image_removed"] = True
    manager._clipboard_choice = None


def answer(manager, text, *, allowed=True, expected=None):
    """Return None unless this is a complete reply to a current image offer."""
    from samsara.session_modes import DispatchOutcome, SessionMode, normalize_utterance
    mode = {"type it": "type", "paste anyway": "paste"}.get(normalize_utterance(text))
    pending = getattr(manager, "_clipboard_choice", None)
    if mode is None or pending is None or not allowed:
        return None
    with manager._dispatch_lock:
        key, process, hwnd, sequence, target_mode, _ = pending
        # A second queued click/voice reply cannot reuse the same consent.
        if (getattr(manager, "_clipboard_choice", None) is not pending
                or expected is not None and pending is not expected):
            return DispatchOutcome(kind="dictate_commit_failed", detail={
                "error": "Choice already used. Draft kept.", "clipboard_reason": True})
        manager._clipboard_choice = None
        if (draft_key(manager) != key or not manager.dictate_pending_buffer
                or manager.mode is not SessionMode.DICTATE and not manager._parked_hold
                or manager._foreground_exe_resolver() != process
                or manager._foreground_hwnd_resolver() != hwnd):
            return DispatchOutcome(kind="dictate_commit_failed", detail={
                "error": "Draft or target changed. Say finish again. Draft kept.", "clipboard_reason": True})
        receipt = Attempt(mode=mode, sequence=sequence)
        token = _attempt.set(receipt)
        try:
            result = manager._commit_dictate_buffer(target_mode=target_mode)
        finally:
            _attempt.reset(token)
        # Typing can fail without changing the clipboard; allow another explicit reply.
        if result.kind != "dictate_committed" and not receipt.replaced and draft_key(manager) == key:
            manager._clipboard_choice = pending
        return result


def bind_compact(app):
    indicator = getattr(app, "listening_indicator", None)
    if indicator is not None:
        indicator._clipboard_recovery_app = app


def open_choices(owner, app):
    """Show actual nonactivating controls; click never steals the editor focus."""
    manager = getattr(app, "_session_mode_manager", None)
    if manager is None or getattr(manager, "_clipboard_choice", None) is None:
        return False
    pending = manager._clipboard_choice
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
    from samsara.ui import theme
    old = getattr(owner, "_clipboard_choices", None)
    if old is not None:
        old.close()
    panel = QWidget(owner, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                    | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
    panel.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    panel.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    panel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    panel.setAccessibleName("Clipboard image choices")
    panel.setStyleSheet(f"QWidget {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; }}"
                        f"QPushButton {{ background: {theme.BG2}; padding: 8px 16px; border-radius: 6px; }}")
    panel.setFont(theme.qfont(theme.TYPE_BODY))
    layout = QVBoxLayout(panel)
    message = QLabel(pending[-1] + '\n"Paste anyway" removes the image from the clipboard.', panel)
    message.setFont(theme.qfont(theme.TYPE_BODY)); layout.addWidget(message)
    row = QHBoxLayout(); layout.addLayout(row)

    def choose(phrase):
        panel.close()
        from samsara.runtime import thread_registry
        def deliver():
            result = answer(manager, phrase, expected=pending)
            if result is not None:
                app._handle_session_dispatch_outcome(result, "")
        thread_registry.spawn("clipboard-recovery", deliver)

    for label, phrase in (("Type it", "type it"), ("Paste anyway", "paste anyway")):
        button = QPushButton(label, panel)
        button.setFont(theme.qfont(theme.TYPE_BODY)); button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda checked=False, phrase=phrase: choose(phrase))
        row.addWidget(button)
    close = QPushButton("Keep draft", panel); close.setFont(theme.qfont(theme.TYPE_BODY))
    close.setFocusPolicy(Qt.FocusPolicy.NoFocus); close.clicked.connect(panel.close); row.addWidget(close)
    panel.adjustSize()
    area = owner.screen().availableGeometry()
    x = max(area.left()+8, min(owner.geometry().center().x()-panel.width()//2, area.right()-panel.width()-8))
    y = owner.geometry().top()-panel.height()-8
    if y < area.top()+8:
        y = owner.geometry().bottom()+8
    panel.move(x, min(y, area.bottom()-panel.height()-8))
    owner._clipboard_choices = panel
    panel.destroyed.connect(lambda: setattr(owner, "_clipboard_choices", None)
                            if getattr(owner, "_clipboard_choices", None) is panel else None)
    panel.show()
    return True
