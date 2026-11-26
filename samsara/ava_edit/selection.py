"""Selection transport for the existing Ava proposal pipeline; no app bootstrap."""
from __future__ import annotations

import re

from samsara.ava_edit import intent, model, sanitize, session

# PLACEHOLDER COPY (1288): cleanup model instruction.
DEFAULT_INSTRUCTION = (
    "Fix dictation artifacts only: spurious periods and capitals from pauses, "
    "merge fragments into sentences, fix punctuation and obvious misheard words "
    "only when the intended word sounds the same or nearly the same. "
    "A period followed by a capital letter that only marks a pause in speech is "
    "an artifact: remove it and join the two parts into one sentence when the "
    "result is grammatical. Keep real sentence ends, names, and literal text. "
    "Keep every word in its original order, apart from those sound corrections "
    "or equivalent contractions. Do not reword, shorten, change tone, or add information."
)
_CLEANUP_VERBS = {"clean", "tidy", "fix", "correct", "punctuate"}


def check_transform(raw, original, instruction, operation):
    """Reuse output hygiene; semantic edits have their own character bounds.

    Similarity, preserving a question, and preserving line structure describe
    copy editing, not translation or summarization. All earlier output-hygiene
    guards still apply. Generic edits and cleanup never enter this branch.
    """
    refusal = sanitize.check(raw, original, instruction)
    semantic = {"model answered instead of editing", "rewrite grew too much",
                "model returned a structured answer"}
    if refusal and refusal not in semantic and not refusal.startswith("not derived from the text"):
        return refusal
    ceiling = len(original) * (3 if operation == "translate" else 1)
    if len(sanitize.clean(raw)) > ceiling:
        return "translation grew too much" if operation == "translate" else "summary is longer than the selection"
    return None


def _notice(app, text):
    session._chip(app, "edit_refused", {"reason": text})
    session._say(app, text)


def read_back(app, original):
    """Queue sentence chunks on the current voice and its existing interrupt.

    The coordinator's generation changes on both cancel_speech and ordinary
    speech interruption. Edge may call on_done even on cancellation; the
    generation guard prevents that callback from continuing the reading.
    """
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is None:
        session._say(app, original)
        return
    generation = coordinator._speech_generation
    chunks = iter(re.findall(r".*?(?:[.!?](?:\s+|$)|\n+|$)", original, re.S))

    def next_sentence():
        if coordinator._speech_generation != generation:
            return
        chunk = next((part for part in chunks if part.strip()), None)
        if chunk is not None:
            coordinator.speak(chunk, category="dictation_readback", on_done=next_sentence)

    next_sentence()


class TranslationReply:
    """Deterministic runner; ava_pending owns the sole reply window/lifetime."""

    def __init__(self, app, original, target):
        from samsara import execution_policy
        self.app, self.original, self.target = app, original, target
        self.generation = execution_policy.capture_generation(app)
        self.coordinator = getattr(app, "audio_coordinator", None)
        self.speech_generation = getattr(self.coordinator, "_speech_generation", None)

    def eligible(self):
        from samsara import execution_policy
        question = getattr(self.app, "_ava_question", None)
        speaking = question is not None and question.runner is self and not question.opened
        return (execution_policy.is_current(self.app, self.generation)
                and session.enabled(self.app)
                and (not speaking or getattr(self.coordinator, "_speech_generation", None)
                     == self.speech_generation))

    def answer(self, text):
        # consume clears capture and question ownership before invoking us.
        if intent.is_discard_utterance(text):
            return
        action = intent.selection_action(text)
        language = action[1] if action and action[0] == "translate" else intent._normalize(text)
        if language not in intent.LANGUAGES:
            _notice(self.app, "Language not recognized.")  # Stand-in copy (635).
            return

        def translate():
            if self.eligible():
                _transform(self.app, self.original, self.target, "translate", language,
                           generation=self.generation)

        session.thread_registry.spawn("selection-translate", translate, daemon=True)


def ask_language(app, original, target):
    from samsara import ava_pending
    return ava_pending.ask(app, TranslationReply(app, original, target), "selection_language",
                           "Into which language?", ())


def _transform(app, original, target, operation, argument, *, generation=None):
    from samsara import execution_policy
    if generation is None:
        generation = execution_policy.capture_generation(app)
    # is_available() is a staged-source predicate, not a backend probe.
    if not session.enabled(app) or model.resolve_backend(app) is None:
        _notice(app, "Translation needs Ava's model" if operation == "translate"
                else "Summarization needs Ava's model")
        return False
    if operation == "translate" and not argument:
        ask_language(app, original, target)
        return True
    instruction = (f"Translate the passage into {argument.title()}. Return only the translation."
                   if operation == "translate" else
                   "Summarize the passage concisely. Return only the summary.")
    session._chip(app, "edit_thinking")
    proposal = session.propose(original, instruction, app, transform=operation)
    if not execution_policy.is_current(app, generation) or not session.enabled(app):
        return False
    return session.stage(app, proposal, selection_target=target,
                         selection_mode="below" if argument == "below" else "replace")


def deterministic_cleanup(text: str, *, vocabulary=()) -> str:
    """Repair explicit pause artifacts using optional Dictionary vocabulary."""
    from samsara.ava_edit.pause_cleanup import repair
    return repair(text, vocabulary)


def should_capture(app, instruction: str) -> bool:
    request = intent.selection_request(instruction)
    return request in {"selection", "bare"}


def _target_matches(target) -> bool:
    from samsara import clipboard
    return bool(target and target[0] and target[2]) and clipboard._paste_target_matches(target)


def capture():
    """Remember foreground AND control, and refuse a copy-time focus race."""
    from samsara import clipboard, teach_patterns
    target = clipboard._paste_target()
    if not target or not target[0] or not target[2]:
        return None, None
    original = teach_patterns.grab_selection_block()
    if not _target_matches(target):
        return None, None
    return original, target


def _window_process(hwnd):
    import ctypes
    from samsara import clipboard
    if not hwnd or clipboard._user32 is None:
        return None
    pid = ctypes.c_ulong()
    if clipboard._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return pid.value or None
    return None


def _foreground_matches(target, process):
    from samsara import clipboard
    current = clipboard._paste_target()
    return bool(target and current and current[0] == target[0]
                and process and _window_process(current[0]) == process)


_CONSOLE_CLASSES = {'consolewindowclass', 'cascadia_hosting_window_class', 'virtualconsoleclass'}


def _window_class(hwnd):
    import ctypes
    from samsara import clipboard
    if not hwnd or clipboard._user32 is None:
        return ''
    label = ctypes.create_unicode_buffer(256)
    if clipboard._user32.GetClassNameW(hwnd, label, len(label)):
        return label.value.lower()
    return ''


def capture_clipboard_result(target):
    """Explicit fallback; keep ingress focus through the Ctrl+C transaction."""
    from samsara import teach_patterns
    from samsara.ava_edit.targets import SelectionCapture
    from samsara.injection_safety import window_integrity

    if not target or not target[0]:
        return SelectionCapture('failed', target=target)
    if _window_class(target[0]) in _CONSOLE_CLASSES:
        return SelectionCapture('protected', target=target)
    process = _window_process(target[0])
    if not process or not window_integrity(target[0]).confirmed_ok:
        return SelectionCapture('failed', target=target)

    def current():
        return _foreground_matches(target, process) and (
            not target[2] or _target_matches(target))

    result = teach_patterns.grab_selection_block_result(before_copy=current)
    if result.state != 'selected' or not current():
        return SelectionCapture('failed', target=target)
    return SelectionCapture('selected', result.text, target, clipboard_process=process)


def capture_result(app=None):
    """Safe passive snapshot; unsupported controls are explicitly unknown.

    Only ordinary Win32 Edit controls have a reviewed native adapter here.
    Rich/custom/terminal controls refuse until a safe range adapter is supplied.
    There is no speculative Ctrl+C and no inferred empty selection on timeout.
    """
    from samsara import clipboard
    from samsara.ava_edit.targets import SelectionCapture
    target = clipboard._paste_target()
    if not target or not target[0] or not target[2]:
        return SelectionCapture(target=target)
    try:
        draft = _draft_selection(app, target)
        if draft is not None:
            return draft
        return _native_selection(target)
    except Exception:
        return SelectionCapture('failed', target=target)


def _draft_selection(app, target):
    """Reuse the revisioned Full/Compact correction selection, when focused."""
    import ctypes
    import os
    from samsara import clipboard
    from samsara.ava_edit.targets import SelectionCapture
    if app is None or clipboard._user32 is None:
        return None
    pid = ctypes.c_ulong()
    clipboard._user32.GetWindowThreadProcessId(target[0], ctypes.byref(pid))
    if pid.value != os.getpid():
        return None
    manager = getattr(app, '_session_mode_manager', None)
    if manager is None:
        return None
    pending = manager.pending_word_correction()
    coords = pending.get('selection') if pending else None
    if not coords:
        return None
    from samsara.session_corrections import _selection_text
    document = manager.draft_document
    text = _selection_text(document, coords)
    if text is None:
        return SelectionCapture('failed', target=target)
    start = 0
    for segment in document.segments:
        if segment.id == coords['segment_id']:
            start += coords['start']
            break
        start += len(segment.text)
    return SelectionCapture('selected', text, target, draft_range=(start, start + len(text)))


def _native_selection(target):
    import ctypes
    from samsara import clipboard
    from samsara.injection_safety import window_integrity
    from samsara.ava_edit.targets import SelectionCapture
    api = clipboard._user32
    if api is None:
        return SelectionCapture(target=target)
    focus = target[2]
    control = _window_class(focus)
    if not control:
        return SelectionCapture('failed', target=target)
    if control in _CONSOLE_CLASSES:
        # In terminals Ctrl+C can interrupt a process.
        return SelectionCapture('protected', target=target)
    if control != 'edit':
        return SelectionCapture(target=target)
    if not window_integrity(focus).confirmed_ok:
        return SelectionCapture('protected', target=target)
    # ES_PASSWORD / ES_READONLY. Do not read protected or non-editable fields.
    from ctypes import wintypes
    get_style = api.GetWindowLongW
    get_style.argtypes, get_style.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
    if get_style(focus, -16) & (0x20 | 0x800):
        return SelectionCapture('protected', target=target)

    def send(message, wparam=0, lparam=0):
        if not _target_matches(target):
            raise RuntimeError('source changed')
        result = ctypes.c_size_t()
        if not api.SendMessageTimeoutW(focus, message, wparam, lparam, 0x23, 200,
                                       ctypes.byref(result)):
            raise RuntimeError('source unavailable')
        return result.value

    start, end = ctypes.c_ulong(), ctypes.c_ulong()
    send(0xB0, ctypes.addressof(start), ctypes.addressof(end))  # EM_GETSEL
    if start.value == end.value:
        return SelectionCapture('verified_empty', target=target, span=(start.value, end.value))
    length = send(0xE)  # WM_GETTEXTLENGTH (UTF-16 units)
    if length > 20_000 or end.value > length:
        return SelectionCapture('failed', target=target)
    buffer = ctypes.create_unicode_buffer(length + 1)
    copied = send(0xD, length + 1, ctypes.addressof(buffer))  # WM_GETTEXT
    after_start, after_end = ctypes.c_ulong(), ctypes.c_ulong()
    send(0xB0, ctypes.addressof(after_start), ctypes.addressof(after_end))
    if (copied != length or (start.value, end.value) != (after_start.value, after_end.value)
            or not _target_matches(target)):
        return SelectionCapture('failed', target=target)
    raw = buffer.value.encode('utf-16-le')
    text = raw[start.value * 2:end.value * 2].decode('utf-16-le')
    return SelectionCapture('selected', text, target, (start.value, end.value))


def propose_and_stage(app, instruction: str) -> bool:
    parts = intent.selection_edit_parts(instruction)
    if parts and not parts[1]:
        verb = parts[0].split()[0].title()
        _notice(app, f"{verb} it how? For example: {verb.lower()} the selection shorter.")
        return False
    action = intent.selection_action(instruction)
    original, target = capture()
    if not original or not original.strip():
        if action and action[0] == "read":
            # 822: when the selection router owns "read that back", let the
            # accessibility handler fall back to the last delivered dictation.
            from plugins.commands.core_utils import read_that_back
            return read_that_back(
                app, _selection_checked=True, _source_text=instruction)
        # Owner-specified wording, also visible on both existing chip surfaces.
        session._chip(app, "edit_refused", {"reason": "Select some text first"})
        session._say(app, "Select some text first")
        return False
    if action:
        operation, argument = action
        if operation == "read":
            read_back(app, original)
            return True
        return _transform(app, original, target, operation, argument)
    # Preserve the explicit capture API used by existing selection callers.
    # Both request routes use actions; this adapter retains its clipboard
    # capture contract and the same authorized proposal/stage engine (1036b).
    cleanup, instruction = intent.cleanup_instruction(instruction)
    if not cleanup and (not session.enabled(app) or model.resolve_backend(app) is None):
        _notice(app, "This edit needs Ava. Turn Ava on and choose a model first.")
        return False
    session._chip(app, "edit_thinking")
    proposal = session.propose(original, instruction, app,
        offline=cleanup and not session.enabled(app), cleanup=cleanup)
    return session.stage(app, proposal, selection_target=target)


def apply(app, staged) -> dict:
    """Keep the proposal on any refusal; paste only into the same selection."""
    # The proposal stays pending so the user can return and apply it.
    reason = "Return to the original selection and say apply."
    result = {"applied": False, "reason": reason, "chars": 0}
    try:
        sent = _paste(staged)
    except Exception:
        sent = False
    if sent:
        with session._lock:
            if session._pending is staged:
                session.clear()
        session._chip(app, "edit_applied", {"changes": staged.proposal.change_count})
        session._log_history(app, staged.proposal)
        return {"applied": True, "chars": len(staged.proposal.rewritten), "reason": ""}
    # PLACEHOLDER COPY (1287): fit the refusal in the Compact chip.
    session._chip(app, "edit_refused", {"reason": "Return to the selection"})
    session._say(app, reason)
    return result


def _paste(staged) -> bool:
    from samsara import clipboard, teach_patterns
    from samsara.ava_edit import targets
    target = staged.selection_target

    def source_matches():
        if staged.request is None:
            return _target_matches(target)
        app = session._worker_app
        return (app is not None and targets.request_valid(app, staged.request.snapshot)
                and targets.source_valid(app, staged.bound_target))

    if not source_matches():
        return False
    # Legacy read/translation adapters have no bound native range or PID.
    # Keep their existing copy check. A bound edit uses its own source guard.
    if (staged.request is None
            and teach_patterns.grab_selection_block() != staged.proposal.original):
        return False
    if not teach_patterns._wait_for_copy_modifiers():
        return False

    def prepare():
        if session.pending() is not staged or not source_matches() or teach_patterns._copy_modifiers_down():
            return False
        if staged.selection_mode == "below":
            import pyautogui
            # Collapse at the selection's right edge even for backward ranges,
            # then move to line end. The newline travels in the preserved paste.
            for key in ("right", "end"):
                pyautogui.press(key)
                if not _target_matches(target) or session.pending() is not staged:
                    return False
        return True

    payload = ("\n" if staged.selection_mode == "below" else "") + staged.proposal.rewritten
    return clipboard.paste_with_preservation(payload, before_paste=prepare)


def register_commands(module_name: str) -> None:
    """Discoverable metadata + thin handlers; Ava and command mode share session."""
    from samsara.plugin_commands import command

    groups = {}
    for instruction, aliases in intent.selection_phrases().items():
        canonical = instruction.replace("capitalize", "capitalise").replace("formalize", "formalise")
        groups[canonical] = groups.get(canonical, ()) + aliases
    for name, replies in (("apply", intent.APPLY_UTTERANCES), ("discard", intent.DISCARD_UTTERANCES)):
        # Generic replies are matched by the pending-edit grammar. Registering
        # e.g. "do it" globally would replace unrelated confirmation commands.
        groups[name] = tuple(sorted(reply for reply in replies if reply.split()[0] == name))
    for instruction, aliases in groups.items():
        # The whole-request router selects the new reading path. These two
        # phrases already have catalogue owners; retain their registry entries
        # and the accessibility command's last-dictation aliases.
        if instruction == "read selection":
            aliases = tuple(alias for alias in aliases if alias not in {"read that back", "read this"})
        def make_handler(utterance):
            def handle(app, _remainder):
                request = _remainder or utterance
                if _remainder and command_for_utterance(request) != utterance:
                    return False
                return session.handle_utterance(app, request)
            handle.__module__ = module_name
            handle.__qualname__ = "edit_" + utterance.replace(" ", "_")
            # Owner-editable stand-in, surfaced by the catalogue/reference.
            handle.__doc__ = "Ava: propose an edit, then say apply or discard."
            if utterance == "read selection":
                handle.__doc__ = "Ava: read selected text with the current voice; speak to interrupt."
            elif utterance.startswith("translate selection"):
                handle.__doc__ = "Ava: propose a translation of selected text, then say apply or discard."
            elif utterance.startswith("summarize"):
                handle.__doc__ = ("Ava: propose a summary " +
                                  ("in place of" if utterance.endswith("replace") else "below") +
                                  " selected text, then say apply or discard.")
            if utterance.split()[0] in intent.EDIT_VERBS and utterance.endswith(" selection"):
                # PLACEHOLDER COPY (899): retain the action, decision and cloud disclosure.
                handle.__doc__ = ("Ava: rewrites selection; say apply or discard; "
                                  "cloud sends text to your AI provider.")
                if utterance.split()[0] in _CLEANUP_VERBS:
                    handle.__doc__ += " Simple dictation cleanup also works offline."
            return handle

        handler = make_handler(instruction)
        risk = "read" if instruction == "read selection" else "ui" if instruction == "discard" else "write"
        command(instruction, aliases=list(aliases), pack="core", ai_visible=False,
                risk_class=risk, ai_composable=False,
                side_effects=["clipboard", "keystrokes", "ui"],
                preconditions=[], voice_triggerable=True,
                param_schema=({"remainder": {"type": "str", "required": instruction.split()[0]
                               in {"make", "change", "edit", "reformat", "rewrite", "reword", "rephrase"}}}
                              if instruction.split()[0] in intent.EDIT_VERBS
                              and instruction.endswith(" selection") else {}),
                reversible=instruction == "apply", preview_template="")(handler)


def command_for_utterance(text: str) -> "str | None":
    instruction = intent.selection_instruction(text)
    if instruction is not None:
        instruction = instruction.replace("capitalize", "capitalise").replace("formalize", "formalise")
        return instruction
    if session.pending() is not None:
        if intent.is_apply_utterance(text):
            return "apply"
        if intent.is_discard_utterance(text):
            return "discard"
    return None


def replace_native_range(state, start, end, replacement):
    """Replace one proven range in a plain Unicode Edit control and read it back."""
    import ctypes
    from ctypes import wintypes
    from samsara import delivery_verify as dv

    control = state[1]
    if _window_class(control) != 'edit':
        return None  # Rich controls may have styling this receipt cannot restore.
    api = ctypes.windll.user32
    hwnd = wintypes.HWND(control)
    style = api.GetWindowLongW(hwnd, -16)
    payload = replacement.encode('utf-16-le')
    wanted = state[2][:start * 2] + payload + state[2][end * 2:]
    if (not api.IsWindowUnicode(hwnd) or style & 0x2838  # Numeric, read-only, password, case conversion
            or ('\n' in replacement or '\r' in replacement) and not style & 0x4
            or not 0 <= start < end <= len(state[2]) // 2
            or len(wanted) // 2 > min(dv.MAX_UNITS, dv._send(control, 0x00D5))
            or dv.snapshot() != (state, True)):
        return None
    dv._send(control, 0x00B1, start, end)  # EM_SETSEL
    if dv.snapshot() != ((*state[:3], start, end, state[5]), True):
        return None
    buffer = ctypes.create_unicode_buffer(replacement)
    dv._send(control, 0x00C2, 1, ctypes.addressof(buffer))  # EM_REPLACESEL
    caret = start + len(payload) // 2
    actual = dv.snapshot()
    return actual[0] if actual == ((*state[:2], wanted, caret, caret, state[5]), True) else None
