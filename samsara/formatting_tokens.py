"""Inline formatting tokens for DICTATE output.

During dictation, a small fixed set of spoken phrases become literal
formatting characters in the delivered text:

    "new line"                -> "\n"
    "new paragraph"           -> "\n\n"
    "insert tab"              -> "\t"
    "bullet" / "bullet point" -> "\n• " (own line, trailing space)
    "open quote" / "close quote" -> '"' (straight quote)
    "open paren" / "close paren" -> "(" / ")"
    "asterisk"                -> "*"
    "double asterisk"         -> "**"

and one TRAILING modifier, as the final words of the text it is given, wraps
that whole text:

    "... in quotes"    -> "\"...\""
    "... in asterisks" -> "*...*"
    "... in bold"      -> "**...**"
    "... in parens"    -> "(...)"

WHAT THE TRAILING WRAP'S SCOPE ACTUALLY IS (queue 100). The wrap covers
exactly the text handed to ONE call, and delivery sites are required to make
that call exactly once, immediately before delivery. What that means follows
from where the single call sits, and the lanes differ:

  * Buffered hands-free DICTATE (session_modes, buffer_dictate_until_commit)
    stages silence-bounded chunks WITHOUT formatting them and formats the
    joined draft once at commit, so the wrap's scope is THE WHOLE STAGED
    DRAFT. "Thank you." / pause / "In quotes." wraps the draft -> '"Thank
    you."'. That is deliberate, not incidental: someone speaking in fragments
    means the fragments.
  * Every other lane (hotkey dictation, wake-session dictation,
    immediate-inject DICTATE, streaming) delivers one utterance at a time, so
    the scope is that utterance. A trigger with nothing before it in its own
    text has nothing to wrap and is left as LITERAL WORDS -- never half a
    wrap, never a silently dropped utterance.

An earlier version of this docstring listed "wrapping text from a PREVIOUS
utterance" as deferred because it "needs the session buffer". That is no
longer true: the staged draft IS that buffer, and the buffered lane already
wraps across chunks by construction.

A wrap is emitted by _wrap() and nowhere else, and _wrap() interpolates its
opening and closing delimiter in one expression -- half a wrap is not a state
it can be in, rather than something guarded against case by case. (The
EXPLICIT "open quote"/"close quote"/"open paren"/"close paren" tokens are a
different feature: each is one delimiter the speaker asked for by name, and
one on its own is correct output, not a partial wrap.)

IDEMPOTENCE (queue 100). apply_formatting_tokens(apply_formatting_tokens(t))
== apply_formatting_tokens(t) for every t. It has to be: a
focus-lock-suppressed chunk is stored already-formatted and
session_modes.retype_last_suppressed re-injects it through the same delivery
pipeline, so the pass really does run twice on that path. The trigger's tail
class below therefore accepts only characters a SPEAKER can put there --
spaces and sentence punctuation, never the \n/\t that exist only because
_substitute already ran. Before this was fixed, "hello in quotes new line"
became "hello in quotes\n" on the first pass (trigger mid-text, so literal)
and then '"hello"' on the second, because the substituted newline had moved
the trigger into a trailing position that was never in what was spoken.

Scope is DICTATE output only (hotkey dictation, the session DICTATE lane,
wake-session dictation) -- callers own that restriction; this module has no
opinion about modes. Escape prefix and numbered lists are deferred to a
later release -- not implemented here. So is an escape for someone who
really wants to end a sentence with the words "in quotes".

"star" is deliberately NOT an alias for "asterisk": this transform runs on
all dictated prose, where "five star review" and "star wars" are ordinary
words. (samsara/verbatim.py maps "star" -> "*" for terminal/URL targets,
where that trade-off is different.)

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
# given position ("bullet point" before "bullet"; "double asterisk" before
# "asterisk"; "new paragraph" and "new line" don't collide with each other
# but are kept adjacent for readability).
_SIMPLE_TOKENS = (
    ("new paragraph", "\n\n"),
    ("new line", "\n"),
    ("insert tab", "\t"),
    ("bullet point", "\n• "),
    ("bullet", "\n• "),
    ("double asterisk", "**"),
    ("asterisk", "*"),
    ("open quote", '"'),
    ("close quote", '"'),
    ("open paren", "("),
    ("close paren", ")"),
)

# How a token treats the text on either side of it. Tokens not listed are
# "separator": the inserted character is its own separator, so ONE space is
# removed on both sides (the original new line / tab / bullet behaviour).
#   open   keeps the space before; removes the space after it -- and a comma
#          Whisper put right after the spoken phrase ("open quote, hello")
#   close  removes the space before it -- and a comma right before the spoken
#          phrase ("hello, close quote"); keeps the space after
#   pair   asterisks have no separate open/close words: within one utterance
#          each phrase alternates open, close, open, ... so
#          "double asterisk bold double asterisk" -> "**bold**"
_ATTACH = {
    "open quote": "open",
    "close quote": "close",
    "open paren": "open",
    "close paren": "close",
    "asterisk": "pair",
    "double asterisk": "pair",
}

# bullet/bullet point at the very start of the utterance would otherwise
# get a pointless leading newline (nothing above it to separate from).
_START_OF_UTTERANCE_OVERRIDES = {
    "bullet point": "• ",
    "bullet": "• ",
}

_REPLACEMENTS = {phrase: repl for phrase, repl in _SIMPLE_TOKENS}

#: Trailing modifiers: (spoken trigger, text before, text after). One per
#: utterance, final words only; a second one earlier in the utterance is
#: literal text.
TRAILING_WRAPS = (
    ("in quotes", '"', '"'),
    ("in asterisks", "*", "*"),
    ("in bold", "**", "**"),
    ("in parens", "(", ")"),
)
_WRAPS = {trigger: (before, after) for trigger, before, after in TRAILING_WRAPS}


def _build_master_pattern() -> "re.Pattern[str]":
    alternatives = "|".join(
        rf"\b{re.escape(phrase)}\b" for phrase, _ in _SIMPLE_TOKENS
    )
    return re.compile(alternatives, re.IGNORECASE)


_MASTER_PATTERN = _build_master_pattern()

# The trigger must be preceded by whitespace (so there is something before it
# to wrap) and followed only by spaces and sentence punctuation Whisper adds
# ("in quotes." / "in quotes!").
#
# The tail class is spaces and punctuation ONLY -- deliberately not \s. A \n
# or \t after the trigger cannot come from speech; it can only come from an
# earlier _substitute() pass having turned a following "new line"/"insert
# tab" into a control character. Accepting it made this function
# non-idempotent -- see IDEMPOTENCE in the module docstring.
_TRAILING_PATTERN = re.compile(
    r"\s+\b(" + "|".join(re.escape(t) for t, _b, _a in TRAILING_WRAPS)
    + r")\b(?P<tail>[ .!?,;:]*)\Z",
    re.IGNORECASE,
)

# Formatting structure kept OUTSIDE a trailing wrap: leading/trailing line
# breaks, tabs and spaces, and a leading bullet marker. "bullet one in quotes"
# -> '• "one"', never '"• one"' (and never a quote before a line break).
_WRAP_EDGES = re.compile(r"\A([\n\t ]*(?:• )?)(.*?)([\n\t ]*)\Z", re.DOTALL)


def _wrap(body: str, trigger: str, tail: str) -> str:
    """Put `body` inside the delimiter pair `trigger` names. THE ONLY place a
    wrap is emitted, and the only reader of _WRAPS.

    Both delimiters are interpolated in one expression, so "an opening
    delimiter without its closing one" is not a state this function can be in
    -- there is no branch here that emits one without the other. A caller that
    must not wrap (nothing before the trigger) returns before reaching this.

    Formatting structure stays OUTSIDE the pair (see _WRAP_EDGES): leading and
    trailing line breaks, tabs and spaces, and a leading bullet marker.

    `tail` is the whitespace run that followed the trigger in the input --
    normally the single space add_trailing_space had just appended. It is
    re-emitted AFTER the closing delimiter. The trigger's own punctuation
    belongs to the trigger and is consumed, but swallowing the separator too
    meant every wrapped commit ran into the next one ('"Thank you."And then').
    """
    before, after = _WRAPS[trigger.lower()]
    prefix, core, suffix = _WRAP_EDGES.match(body).groups()
    return f"{prefix}{before}{core}{after}{suffix}{tail}"


def _substitute(text: str) -> str:
    """Inline token substitution (see the module docstring). Identity
    fast-path: text with no matches is returned unchanged (same object)."""
    if not text:
        return text

    matches = list(_MASTER_PATTERN.finditer(text))
    if not matches:
        return text

    out = []
    pos = 0
    trim_leading_next = False      # False | "separator" | "open"
    pair_counts = {}
    for m in matches:
        start, end = m.span()
        canonical = m.group(0).lower()
        attach = _ATTACH.get(canonical, "separator")
        if attach == "pair":
            n = pair_counts.get(canonical, 0)
            pair_counts[canonical] = n + 1
            attach = "open" if n % 2 == 0 else "close"

        segment = text[pos:start]
        if trim_leading_next == "open" and segment.startswith(","):
            segment = segment[1:]
        if trim_leading_next and segment.startswith(" "):
            segment = segment[1:]
        if attach in ("separator", "close") and segment.endswith(" "):
            segment = segment[:-1]
        if attach == "close" and segment.endswith(","):
            segment = segment[:-1]
        out.append(segment)

        if start == 0 and canonical in _START_OF_UTTERANCE_OVERRIDES:
            replacement = _START_OF_UTTERANCE_OVERRIDES[canonical]
        else:
            replacement = _REPLACEMENTS[canonical]
        out.append(replacement)

        pos = end
        trim_leading_next = {"separator": "separator", "open": "open"}.get(attach, False)

    tail = text[pos:]
    if trim_leading_next == "open" and tail.startswith(","):
        tail = tail[1:]
    if trim_leading_next and tail.startswith(" "):
        tail = tail[1:]
    out.append(tail)

    return "".join(out)


def apply_formatting_tokens(text: str) -> str:
    """Substitute spoken formatting tokens with literal formatting chars,
    then apply a trailing wrap modifier if the utterance ends with one.

    Case-insensitive, word-boundary matched. Separator tokens (new line,
    tab, bullet) remove a single space immediately before and after them, so
    "hello new line world" becomes "hello\\nworld". Opening tokens remove the
    space after them and closing tokens the space before them, so
    "he said open quote hello close quote" becomes 'he said "hello"'.

    Trailing modifier ORDER: the inline tokens are substituted in the rest of
    the text first, and the wrap goes around that result last -- minus its
    edge formatting (see _WRAP_EDGES). The trigger's own trailing punctuation
    is part of the trigger; punctuation the user spoke BEFORE the trigger
    stays where it is; the whitespace run after the trigger is kept and
    re-emitted outside the wrap. Text that is only a trigger has nothing to
    wrap and is left as literal words.

    Scope of "the text": see WHAT THE TRAILING WRAP'S SCOPE ACTUALLY IS in the
    module docstring. Delivery sites call this exactly once, immediately
    before delivery; on the buffered hands-free lane that one call sees the
    whole staged draft, elsewhere it sees one utterance.

    IDEMPOTENT: f(f(t)) == f(t) for every t (module docstring, IDEMPOTENCE).

    Identity fast-path: text with no tokens and no trigger is returned
    unchanged (same object), no copy made.
    """
    if not text:
        return text

    trailing = _TRAILING_PATTERN.search(text)
    if trailing is None or not text[:trailing.start()].strip():
        return _substitute(text)

    # Only the whitespace RUN at the very end of the trigger's tail survives;
    # the punctuation in front of it is part of the trigger.
    tail = trailing.group("tail")
    return _wrap(_substitute(text[:trailing.start()]),
                 trailing.group(1),
                 tail[len(tail.rstrip()):])


def apply_formatting_tokens_if_enabled(text: str, enabled: bool) -> str:
    """Thin gate for delivery sites: skip the call entirely when disabled.
    Kept separate from apply_formatting_tokens so that function stays a
    pure, config-free text transform; callers resolve `enabled` from
    config themselves (formatting_tokens.enabled)."""
    if not enabled:
        return text
    return apply_formatting_tokens(text)
