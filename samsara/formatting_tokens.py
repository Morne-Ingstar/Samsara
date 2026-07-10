"""Inline formatting tokens for DICTATE output.

During dictation, a small fixed set of spoken phrases become literal
formatting characters in the delivered text:

    "new line"                -> "\n"
    "new paragraph"           -> "\n\n"
    "tab"                     -> "\t"
    "bullet" / "bullet point" -> "\n• " (own line, trailing space)

Scope is DICTATE output only (hotkey dictation, the session DICTATE lane,
wake-session dictation) -- callers own that restriction; this module has no
opinion about modes. Escape prefix and numbered lists are deferred to a
later release -- not implemented here.

PIPELINE POSITION: substitution must run AFTER any LLM correction pass
(smart_correct) and immediately before delivery/paste -- an LLM pass must
never see half-substituted control characters. Callers are responsible for
sequencing; this module is a pure text transform with no knowledge of when
it's called.

apply_formatting_tokens() is a pure function: no config access, no I/O.
Delivery sites decide whether to call it at all (see
apply_formatting_tokens_if_enabled below) based on the
formatting_tokens.enabled config flag.
"""
from __future__ import annotations

import re

# Longest-match-first ordering matters: multi-word phrases must be listed
# before any single-word phrase they share a prefix with, since regex
# alternation tries alternatives in order and takes the first match at a
# given position ("bullet point" before "bullet"; "new paragraph" and
# "new line" don't collide with each other but are kept adjacent for
# readability).
_SIMPLE_TOKENS = (
    ("new paragraph", "\n\n"),
    ("new line", "\n"),
    ("bullet point", "\n• "),
    ("bullet", "\n• "),
)

# bullet/bullet point at the very start of the utterance would otherwise
# get a pointless leading newline (nothing above it to separate from).
_START_OF_UTTERANCE_OVERRIDES = {
    "bullet point": "• ",
    "bullet": "• ",
}

_TAB_REPLACEMENT = "\t"

# "tab" collides with common non-formatting phrases far more than the other
# tokens do ("open a new tab", "press the tab key") -- guarded by hardcoded
# preceding/following-word lists rather than substituting unconditionally.
# Add more guard words here as new collisions are found.
_TAB_PRECEDING_GUARDS = ("new", "browser", "next", "previous", "the", "a")
_TAB_FOLLOWING_GUARDS = ("key",)

_REPLACEMENTS = {phrase: repl for phrase, repl in _SIMPLE_TOKENS}
_REPLACEMENTS["tab"] = _TAB_REPLACEMENT


def _build_master_pattern() -> "re.Pattern[str]":
    simple_alts = "|".join(rf"\b{re.escape(phrase)}\b" for phrase, _ in _SIMPLE_TOKENS)
    # Each guard word gets its OWN negative lookbehind rather than one
    # combined alternation -- Python's re requires fixed-width lookbehind,
    # and the guard words have different lengths ("a" vs "previous").
    precede_lookbehinds = "".join(
        rf"(?<!\b{re.escape(w)}\s)" for w in _TAB_PRECEDING_GUARDS
    )
    follow_alt = "|".join(re.escape(w) for w in _TAB_FOLLOWING_GUARDS)
    tab_pattern = rf"{precede_lookbehinds}\btab\b(?!\s+(?:{follow_alt})\b)"
    return re.compile(rf"{simple_alts}|{tab_pattern}", re.IGNORECASE)


_MASTER_PATTERN = _build_master_pattern()


def apply_formatting_tokens(text: str) -> str:
    """Substitute spoken formatting tokens with literal formatting chars.

    Case-insensitive, word-boundary matched. Removes a single space
    immediately before and after each matched token (the inserted control
    sequence is its own separator) so "hello new line world" becomes
    "hello\\nworld", not "hello \\nworld". Identity fast-path: text with no
    matches is returned unchanged (same object), no copy made.
    """
    if not text:
        return text

    matches = list(_MASTER_PATTERN.finditer(text))
    if not matches:
        return text

    out = []
    pos = 0
    trim_leading_next = False
    for m in matches:
        start, end = m.span()
        segment = text[pos:start]
        if trim_leading_next and segment.startswith(" "):
            segment = segment[1:]
        if segment.endswith(" "):
            segment = segment[:-1]
        out.append(segment)

        canonical = m.group(0).lower()
        if start == 0 and canonical in _START_OF_UTTERANCE_OVERRIDES:
            replacement = _START_OF_UTTERANCE_OVERRIDES[canonical]
        else:
            replacement = _REPLACEMENTS[canonical]
        out.append(replacement)

        pos = end
        trim_leading_next = True

    tail = text[pos:]
    if trim_leading_next and tail.startswith(" "):
        tail = tail[1:]
    out.append(tail)

    return "".join(out)


def apply_formatting_tokens_if_enabled(text: str, enabled: bool) -> str:
    """Thin gate for delivery sites: skip the call entirely when disabled.
    Kept separate from apply_formatting_tokens so that function stays a
    pure, config-free text transform; callers resolve `enabled` from
    config themselves (formatting_tokens.enabled)."""
    if not enabled:
        return text
    return apply_formatting_tokens(text)
