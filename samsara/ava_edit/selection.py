"""Selection transport for the existing Ava proposal pipeline; no app bootstrap."""
from __future__ import annotations

import re

from samsara.ava_edit import intent, model, sanitize, session

# PLACEHOLDER COPY (1036a): cleanup model instruction.
DEFAULT_INSTRUCTION = (
    "Fix dictation artifacts only: spurious periods and capitals from pauses, "
    "merge fragments into sentences, fix punctuation and obvious homophones. "
    "A period followed by a capital letter that only marks a pause in speech is "
    "an artifact: remove it and join the two parts into one sentence when the "
    "result is grammatical. "
    "Do not reword, shorten, change tone, or add information."
)
_CLEANUP_VERBS = {"clean", "tidy", "fix", "correct", "punctuate"}
_LOWERCASE_WORDS = frozenset(
    "a an the and or but so to of in on at for from with as by into is are was "
    "were be been being am have has had do does did can could would should "
    "will went bought store some this that it my your our their then "
    "live helps help tells tell instead which who because these done ready "
    "working reviewed finished available".split())
_CLAUSE_VERBS = frozenset(
    "am is are was were have has had do does did will would can could should "
    "may might must live lives help helps tell tells want wants need needs "
    "went go goes bought buy buys saw see sees waved wave waves arrived "
    "left stayed finished stopped like likes know knows think thinks said told "
    "use uses work works let lets make makes feel feels hear hears watch watches"
    .split())
_UNFINISHED = frozenset(
    "to of in on at for with from into by as and or but because that which who "
    "the a an my your our their this these is are was were has have had will "
    "would can could should must".split())
_CONTINUATIONS = frozenset("and but or so then because instead being".split())


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


def deterministic_cleanup(text: str) -> str:
    """Explicit cleanup's offline pause repair; retains protected text.

    Require an unfinished left clause or a compatible continuation on the
    right. Shortness alone is not evidence that a sentence end was a pause.
    Ambiguous independent clauses stay separate; this is no grammar parser.
    The text-only helper extends these 747 detectors to interior ellipses,
    evidenced fragments and stray casing, without changing hold delivery.
    """
    def join_pause(left, following):
        fragment = re.split(r"[.!?\r\n]", following, maxsplit=1)[0]
        words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", fragment.lower())
        previous = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", left.lower())
        if not words or not previous or not re.match(r"[A-Za-z]", following):
            return False
        ending = previous[-1]
        # Titles, initials/initialisms, ellipses and questions are hard bounds.
        if (ending in {"mr", "mrs", "ms", "dr", "prof", "etc", "vs", "e", "g"}
                or (len(ending) == 1 and ending != "i") or left.endswith(".")
                or re.match(r"[^.!?\r\n]*\?", following)
                or previous[0] in {"is", "are", "was", "were", "do", "does", "did",
                                    "can", "could", "will", "would", "should",
                                    "what", "why", "how", "when", "where"}):
            return False
        first = words[0]
        # An explicit new subject normally starts a real independent clause.
        independent = (first in {"i", "you", "he", "she", "it", "we", "they", "there"}
                       or (following[0].isupper() and first not in _LOWERCASE_WORDS
                           and len(words) > 1
                           and words[1] in _CLAUSE_VERBS))
        if independent:
            return False
        if ending in _UNFINISHED or (ending == "so" and previous == ["so"]):
            return True
        # Catenative verbs license an object followed by a bare infinitive:
        # "helps me. Live ..."; an arbitrary verb + pronoun does not.
        if (ending in {"me", "you", "him", "her", "us", "them"}
                and len(previous) > 1
                and previous[-2] in {"help", "helps", "helped", "let", "lets",
                                     "make", "makes", "made", "hear", "hears",
                                     "see", "sees", "watch", "watches", "feel", "feels"}
                and first in (_CLAUSE_VERBS - {"am", "is", "are", "was", "were",
                                              "has", "had", "does", "did"}) | {"to"}):
            return True
        noun_phrase = (len(previous) > 1
                       and previous[0] in {"a", "an", "the", "my", "your", "our",
                                           "their", "this", "these", "whatever"}
                       and not any(word in _CLAUSE_VERBS for word in previous))
        continuation = (following[0].islower() or first in _CONTINUATIONS
                        or (first in _CLAUSE_VERBS and noun_phrase))
        if first == "instead" and words[1:2] != ["of"]:
            return False
        if (first == "then" and len(words) > 1
                and words[1] in {"i", "you", "he", "she", "it", "we", "they"}):
            return False
        return continuation and (len(previous) <= 5 or noun_phrase)

    from samsara.ava_edit.pause_cleanup import repair
    return repair(text, join_pause, _LOWERCASE_WORDS)


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
        return SelectionCapture()
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
    label = ctypes.create_unicode_buffer(256)
    if not api.GetClassNameW(focus, label, len(label)) or label.value.lower() != 'edit':
        return SelectionCapture(target=target)
    if not window_integrity(focus).confirmed_ok:
        return SelectionCapture(target=target)
    # ES_PASSWORD / ES_READONLY. Do not read protected or non-editable fields.
    from ctypes import wintypes
    get_style = api.GetWindowLongW
    get_style.argtypes, get_style.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
    if get_style(focus, -16) & (0x20 | 0x800):
        return SelectionCapture(target=target)

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
    # Owner-editable stand-in wording; this is a pending chip, not a dismissal.
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
    session._chip(app, "edit_proposed", {"changes": staged.proposal.change_count})
    session._say(app, reason)
    return result


def _paste(staged) -> bool:
    from samsara import clipboard, teach_patterns
    target = staged.selection_target
    if not _target_matches(target):
        return False
    current = teach_patterns.grab_selection_block()
    if current != staged.proposal.original:
        return False
    def prepare():
        if not _target_matches(target) or session.pending() is not staged:
            return False
        if staged.request is not None:
            from samsara.ava_edit import targets
            app = session._worker_app
            if (app is None or not targets.request_valid(app, staged.request.snapshot)
                    or not targets.source_valid(app, staged.bound_target)):
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
        if canonical.split()[0] in intent.EDIT_VERBS and canonical.endswith(" this"):
            canonical = canonical[:-5] + " selection"
        groups[canonical] = groups.get(canonical, ()) + aliases
    groups.update({"apply": ("apply", "apply the edit"), "discard": ("discard", "discard the edit")})
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
        if instruction.split()[0] in intent.EDIT_VERBS and instruction.endswith(" this"):
            instruction = instruction[:-5] + " selection"
        return instruction
    if session.pending() is not None:
        if intent.is_apply_utterance(text):
            return "apply"
        if intent.is_discard_utterance(text):
            return "discard"
    return None
