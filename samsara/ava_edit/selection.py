"""Selection transport for the existing Ava proposal pipeline; no app bootstrap."""
from __future__ import annotations

import re

from samsara.ava_edit import intent, model, sanitize, session

DEFAULT_INSTRUCTION = (
    "Fix dictation artifacts only: spurious periods and capitals from pauses, "
    "merge fragments into sentences, fix punctuation and obvious homophones. "
    "Do not reword, shorten, change tone, or add information."
)
_CLEANUP_VERBS = {"clean", "tidy", "fix", "correct", "punctuate"}
_LOWERCASE_WORDS = frozenset(
    "a an the and or but so to of in on at for from with as by into is are was "
    "were be been being am have has had do does did can could would should "
    "will went bought store some this that it my your our their then".split())


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
    """Conservative offline pause repair; retains paragraphs, names and acronyms.

    A short following fragment or lowercase continuation can join a period.
    Only common function words lose capitals inside a sentence; a general
    title-case rule would damage names without a language model.
    """
    text = re.sub(r"[ \t]{2,}", " ", text or "")
    parts = re.split(r"(\.[ \t]+)", text)
    result = parts[0]
    for index in range(1, len(parts), 2):
        separator, following = parts[index:index + 2]
        fragment = re.split(r"[.!?\n]", following, maxsplit=1)[0]
        words = fragment.split()
        previous = re.findall(r"[A-Za-z]+", result)
        abbreviation = previous and previous[-1].lower() in {
            "mr", "mrs", "ms", "dr", "prof", "etc", "vs"}
        merge = words and not abbreviation and (
            len(words) < 4 or words[0][:1].islower())
        if merge:
            if words[0].lower() in _LOWERCASE_WORDS and not words[0].isupper():
                following = following[0].lower() + following[1:]
            separator = " "
        result += separator + following

    def lower_stray(match):
        prefix, word = match.groups()
        if word.lower() in _LOWERCASE_WORDS and not re.search(r"[.!?\n]\s*$", prefix):
            word = word.lower()
        return prefix + word

    return re.sub(r"([^\w]*\s+)([A-Z][a-z]+)\b", lower_stray, result)


def should_capture(app, instruction: str) -> bool:
    request = intent.selection_request(instruction)
    return request == "selection" or (request == "bare" and not session.source_text(app))


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
    cleanup = intent._first_word(instruction) in _CLEANUP_VERBS
    if not cleanup and (not session.enabled(app) or model.resolve_backend(app) is None):
        _notice(app, "This edit needs Ava. Turn Ava on and choose a model first.")
        return False
    instruction = DEFAULT_INSTRUCTION if cleanup else instruction
    session._chip(app, "edit_thinking")
    proposal = session.propose(
        original, instruction, app,
        # 823: staged-draft availability does not describe a captured selection.
        offline=cleanup and not session.enabled(app),
        cleanup=cleanup,
    )
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
                handle.__doc__ = ("Ava: rewrites selected text; then say apply or discard; "
                                  "needs Ava for AI; goes to your AI provider if cloud is chosen.")
                if utterance.split()[0] in _CLEANUP_VERBS:
                    handle.__doc__ += " Simple dictation cleanup also works offline."
            return handle

        handler = make_handler(instruction)
        command(instruction, aliases=list(aliases), pack="core", ai_visible=False,
                risk_class="reversible", ai_composable=False,
                side_effects=["clipboard", "keystrokes", "ui"],
                preconditions=[], voice_triggerable=True,
                param_schema=({"remainder": {"type": "str", "required": instruction.split()[0]
                               in {"make", "change", "edit", "reformat", "rewrite", "reword", "rephrase"}}}
                              if instruction.split()[0] in intent.EDIT_VERBS
                              and instruction.endswith(" selection") else {}),
                reversible=True, preview_template="")(handler)


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
