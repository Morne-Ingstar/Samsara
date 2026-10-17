"""Selection transport for the existing Ava proposal pipeline; no app bootstrap."""
from __future__ import annotations

import re

from samsara.ava_edit import intent, session

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
    original, target = capture()
    if not original:
        # Owner-specified wording, also visible on both existing chip surfaces.
        session._chip(app, "edit_refused", {"reason": "Nothing selected"})
        session._say(app, "Nothing selected")
        return False
    cleanup = intent._first_word(instruction) in _CLEANUP_VERBS
    instruction = DEFAULT_INSTRUCTION if cleanup else instruction
    session._chip(app, "edit_thinking")
    proposal = session.propose(
        original, instruction, app,
        offline=not session.is_available(app) or not session.enabled(app),
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
    return clipboard.paste_with_preservation(
        staged.proposal.rewritten,
        before_paste=lambda: _target_matches(target) and session.pending() is staged,
    )


def register_commands(module_name: str) -> None:
    """Discoverable metadata + thin handlers; Ava and command mode share session."""
    from samsara.plugin_commands import command

    groups = intent.selection_phrases()
    groups.update({"apply": ("apply", "apply the edit"), "discard": ("discard", "discard the edit")})
    for instruction, aliases in groups.items():
        def make_handler(utterance):
            def handle(app, _remainder):
                if _remainder:
                    return False
                return session.handle_utterance(app, utterance)
            handle.__module__ = module_name
            handle.__qualname__ = "edit_" + utterance.replace(" ", "_")
            # Owner-editable stand-in, surfaced by the catalogue/reference.
            handle.__doc__ = "Ava: propose an edit, then say apply or discard."
            return handle

        handler = make_handler(instruction)
        command(instruction, aliases=list(aliases), pack="core", ai_visible=False,
                risk_class="reversible", ai_composable=False,
                side_effects=["clipboard", "keystrokes", "ui"],
                preconditions=[], voice_triggerable=True, param_schema={},
                reversible=True, preview_template="")(handler)


def command_for_utterance(text: str) -> "str | None":
    instruction = intent.selection_instruction(text)
    if instruction is not None:
        return instruction
    if session.pending() is not None:
        if intent.is_apply_utterance(text):
            return "apply"
        if intent.is_discard_utterance(text):
            return "discard"
    return None
