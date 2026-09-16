"""Output-text quality gates for one finished Whisper decode.

Extracted verbatim from dictation.py (queue 128). Every function, constant
and comment below is the code that ran there, moved without alteration -- no
behaviour change, no rewrite. dictation.py re-exports every public name in
this module, so ``dictation._is_hallucinated_segments`` and friends still
resolve for the tests and tools that reach for them by that path.

Two related jobs live here, and they are the two ends of the same loop:

  OUTPUT -- given a decode's segments and joined text, decide whether the
      text is real speech (_is_hallucinated_segments, _is_quality_exhausted,
      _keep_low_confidence_long_chunk, _apply_segment_quality_gates) and trim
      the degenerate tails Whisper emits on near-silence
      (_trim_trailing_garbage_run, _drop_trailing_garbage_segments).

  INPUT -- given the pending draft the DICTATE lane feeds back as
      initial_prompt, clean it before the decoder is conditioned on it
      (_sanitise_context_tail) and refuse a decode that is only that prompt
      talking back (_is_context_echo).

Everything here is pure: text, segment telemetry and the transcribe params
dict in, a verdict out. No app state, no Qt, no model, no I/O. This module
must never import dictation.
"""

import logging
import re
import string

from samsara import diagnostics


# --- Whisper hallucination prevention ("Gate and Reset" architecture) ---
# The threshold below travelled with the two gates that read it; its siblings
# _NO_SPEECH_THRESHOLD and _LOGPROB_THRESHOLD stay in dictation.py, where the
# transcribe-params builders that pass them to faster-whisper live.
_COMPRESSION_RATIO_THRESHOLD = 2.4
                             # faster-whisper's own built-in compression_ratio_threshold default
                             # -- never explicitly passed as a transcribe() kwarg anywhere in this
                             # file (see get_transcription_params), so there's no config value to
                             # read back; this matches both faster-whisper's real internal default
                             # and samsara/diagnostics.py's classify() heuristic. Used by
                             # _is_quality_exhausted (2026-07-10): when faster-whisper's own
                             # temperature fallback ladder exhausts every rung and still can't
                             # meet log_prob_threshold/this compression ceiling, it returns the
                             # final failed attempt anyway rather than nothing -- an 11.7s blank
                             # hotkey hold on 2026-07-10 delivered "Thank you for watching!" this
                             # exact way (every temp 0.0-1.0 failed log_prob_threshold, then
                             # compression_ratio hit 7.125 at temp 0.8) because nothing downstream
                             # checked these signals before delivering the text.


# Well-known Whisper hallucination strings that appear on near-silent audio,
# across languages -- Signature E below. Language-independent by
# construction (checked as a plain case-insensitive substring match, no
# transcription-language dependency), so it extends _is_hallucinated_segments
# rather than needing a separate per-language check. "amara.org" alone
# catches variants not explicitly listed (Amara.org crowd-subtitles the
# phrase on many different source videos/languages).
#
# 2026-07-10: added the ENGLISH "thank you for watching" family -- every
# non-English variant of this exact staple (Japanese/Chinese/Ukrainian/
# Spanish below) was already covered, but the English original was missing
# entirely. An 11.7s blank hotkey hold delivered "Thank you for watching!"
# verbatim because of that gap (see _is_quality_exhausted for the other half
# of that fix -- the decode had ALSO exhausted every quality threshold).
# Matching is via the existing lowercase-substring + dominance-ratio check
# below (case/punctuation-insensitive by construction: text is lowercased
# and only whitespace-normalized, so trailing "!"/"." on the transcript
# doesn't prevent the bare phrase from still dominating the ratio). No other
# hallucination staples added here -- stay scoped to this exact family.
_HALLUCINATION_STRING_BLACKLIST = (
    "thank you for watching",
    "thanks for watching",
    "untertitel der amara.org-community",
    "sous-titrage st' 501",
    "ご視聴ありがとうございました",
    "字幕由amara.org社区提供",
    "дякую за перегляд",
    "gracias por ver el video",
    "amara.org",
)


def _is_hallucinated_segments(seg_list, text):
    """True if the transcription shows Whisper's degenerate-repetition signature.

    BACKSTOP ONLY. The primary hallucination defenses are causal and run
    before/during transcription: faster-whisper's native no_speech_threshold/
    log_prob_threshold, the per-press condition_on_previous_text=False
    conversation-context reset, the contiguous-confidence VAD gate on short
    buffers, and the click-fade (see module-level _NO_SPEECH_THRESHOLD /
    _GATE_* / _FADE_MS constants and _buffer_has_contiguous_speech). This
    output-text heuristic is a cheap last-resort net for whatever slips
    through those, not the primary defense -- avoid extending the
    repetition-based signatures (A/B/D); the fixed-string blacklist
    (Signature E) is a deliberate, bounded exception since those exact
    strings are never legitimate dictation regardless of language.

    Uses telemetry Whisper already computed; no re-inference. Conservative:
    only fires on clear signatures so real speech is never dropped."""
    t = (text or "").strip()
    if not t:
        return False
    # Signature E: well-known multilingual Whisper hallucination strings
    # (subtitle-crowdsourcing credits that leak out on near-silent audio).
    # Language-independent -- checked regardless of the configured
    # dictation language.
    #
    # DOMINANCE, NOT PRESENCE: a legitimate utterance can simply MENTION one
    # of these strings ("I was reading about the amara.org community") --
    # discarding the whole transcription on bare substring presence would
    # eat real speech (the bare "amara.org" entry is the worst case for
    # this). Gate on the longest matching phrase covering >=80% of the
    # (whitespace-normalized) transcript instead -- that's the phrase
    # constituting substantially the whole output, not an incidental
    # mention. Below that ratio it's incidental and falls through to the
    # repetition signatures (A/B/D) below. Never scrub/mutate the text: a
    # mid-string removal would corrupt legitimate surrounding speech --
    # gate-or-pass is the only safe move, so this stays a pure boolean check.
    t_lower = t.lower()
    t_norm = re.sub(r'\s+', ' ', t_lower).strip()
    if t_norm:
        best_len = 0
        for phrase in _HALLUCINATION_STRING_BLACKLIST:
            phrase_norm = re.sub(r'\s+', ' ', phrase).strip()
            if phrase_norm and phrase_norm in t_norm:
                best_len = max(best_len, len(phrase_norm))
        if best_len and best_len / len(t_norm) >= 0.80:
            return True
    # Signature A: high compression ratio on any segment (repetition compresses hard).
    # Whisper's own reject threshold is 2.4; we use a slightly higher 3.0 to stay
    # conservative and avoid touching borderline-but-real speech.
    for s in seg_list:
        cr = getattr(s, "compression_ratio", None)
        if cr is not None and cr > 3.0:
            return True
    # Signature B: low lexical diversity repetition (e.g. "click click click click").
    # Strip surrounding punctuation before comparing words so "click," "click."
    # "click!" count as the same repeated word instead of inflating diversity.
    words = [w.strip(string.punctuation) for w in t.lower().split()]
    words = [w for w in words if w]
    if len(words) >= 4:
        uniq = len(set(words))
        if uniq <= max(2, len(words) // 4):
            return True
    # Signature D: the ENTIRE transcription is 2-3 identical tokens (e.g.
    # "click click", "beep beep beep"). Too short to trip Signature B's
    # >=4-word check. An embedded mention inside real speech ("I heard a
    # click click sound") is untouched -- this only fires when the repeat
    # IS the whole transcription, not part of a longer one.
    #
    # CORROBORATION REQUIRED: a bare 2-3 token whole-utterance repeat is
    # NOT on its own a reliable hallucination signal -- real emphatic
    # speech ("no no", "stop stop", "yes yes yes") looks identical at the
    # text level. This must only fire when acoustically corroborated: every
    # segment's no_speech_prob > 0.5 (near-silence). A user actually saying
    # "no no" into a live mic produces LOW no_speech_prob and now passes
    # through untouched; a phantom "click click" from a near-silent buffer
    # keeps HIGH no_speech_prob and is still caught. Empty seg_list or any
    # segment missing no_speech_prob telemetry means there's nothing to
    # corroborate with -- never fire in that case. Eating real speech is
    # worse than letting a rare hallucination through.
    if 2 <= len(words) <= 3 and len(set(words)) == 1 and seg_list:
        nsp_values = [getattr(s, "no_speech_prob", None) for s in seg_list]
        if all(v is not None and v > 0.5 for v in nsp_values):
            return True
    # Signature C: very high no_speech_prob across all segments AND short output
    # (near-silent buffer that still emitted a token or two).
    if seg_list:
        nsp = [getattr(s, "no_speech_prob", 0.0) or 0.0 for s in seg_list]
        if nsp and min(nsp) > 0.8 and len(words) <= 3:
            return True
    return False


# Trailing run of a repeated non-speech punctuation character (underscore,
# dash, period) -- 6 or more in a row. Confirmed in production
# (~/.samsara/logs/samsara.log, 2026-07-14/15) as Whisper's degenerate
# output on the near-silent tail of a toggle command-mode utterance, e.g.
# '"the __________"' and '"ready for ______...______"' (hundreds of chars).
# INVISIBLE to _is_hallucinated_segments' Signature B: that check strips
# surrounding punctuation before comparing words (string.punctuation
# includes '_'/'-'/'.'), so a run of underscores collapses to an empty
# "word" and is filtered out of the word list entirely rather than
# registering as repetition. Used only by _handle_command_mode_utterance
# (see there for why trim-not-reject is the right call for that path) --
# the hotkey path's _apply_segment_quality_gates is untouched.
_TRAILING_GARBAGE_RUN_RE = re.compile(r'([_\-.])\1{5,}\s*$')


def _trim_trailing_garbage_run(text):
    """Strip a trailing run of 6+ repeated underscore/dash/period characters
    from text. Real words consistently precede the run in every observed
    case, so this trims rather than discarding the whole string -- callers
    that need whole-utterance rejection (e.g. no real words at all) should
    check whether the result is empty."""
    match = _TRAILING_GARBAGE_RUN_RE.search(text)
    if not match:
        return text
    return text[:match.start()].rstrip()


def _drop_trailing_garbage_segments(seg_list):
    """Drop trailing segments whose own text is changed by
    _trim_trailing_garbage_run (fully consumed as pure garbage, or a real
    prefix with a garbage tail), stopping at the first trailing segment
    trimming leaves untouched.

    Whisper sets a segment's compression_ratio/no_speech_prob telemetry
    against its UNTRIMMED text -- a long repeated-character run compresses
    at 6-17x in testing (comfortably past _is_hallucinated_segments'
    Signature A threshold of 3.0), so leaving that segment's object in the
    list handed to _is_hallucinated_segments would reject the WHOLE
    utterance -- including real speech in segments before it -- off that
    one segment's inflated ratio, even after the delivered text itself has
    been trimmed clean. This only excludes the segment OBJECT (its
    telemetry) from that check; the real words it contained still reach
    the delivered text via the separate string-level
    _trim_trailing_garbage_run call on the joined text."""
    segs = list(seg_list)
    while segs:
        seg_text = (getattr(segs[-1], "text", "") or "").strip()
        if seg_text and _trim_trailing_garbage_run(seg_text) != seg_text:
            segs.pop()
            continue
        break
    return segs


# --- Queue 106: the context prompt poisons itself ---------------------------
#
# The toggle-session DICTATE lane feeds the tail of the pending buffer back to
# Whisper as initial_prompt (see _handle_command_mode_utterance). That earns
# its place: in perf_artifacts/nd_prompt_contamination.md, 37 recordings of a
# standalone "and" decode correctly 16/37 with NO prompt and 33/37 with a
# clean tail. The same table prices the downside exactly -- append one stray
# fragment token to that clean tail and it drops to 27/37, two and it is
# 3/37, three and it is 0/37: every single recording comes back "nd".
#
# The lane is a closed loop. Whatever Whisper emits is staged, and what is
# staged becomes the next prompt, so one fragment that gets in is fed back
# until the lane can produce nothing else. Two distinct failures drive it,
# and they need answers at opposite ends:
#
#   Steering -- the prompt's trailing tokens bias the next decode toward a
#       fragment ("...I don't know and nd nd nd" -> "nd", 37 times out of 37).
#       Answered on the INPUT side by _sanitise_context_tail.
#   Echo -- the decode reproduces a span of the prompt verbatim, as if it had
#       been spoken. 2026-09-15 21:28:53 staged "This changes a thing or two.",
#       21:28:56 staged "Fuck, no.", and at 21:29:01 a 2.7 s capture decoded as
#       "Changes a thing or two. Fuck, no." -- both earlier chunks, returned as
#       a new utterance and staged again. Answered on the OUTPUT side by
#       _is_context_echo.
#
# What is deliberately NOT done here: gating on no_speech_prob or avg_logprob.
# The owner's genuine "Fuck, no." at 21:28:56 carried no_speech_prob 0.646
# against the 0.600 threshold in _NO_SPEECH_THRESHOLD. Real short exclamations
# decode with low confidence, so a confidence gate would delete exactly the
# speech this lane exists to capture. Both checks below are about text and its
# relationship to the prompt, never about how sure Whisper was.

#: Characters stripped from each end of a word before comparing it. Curly
#: quotes and the ellipsis/dash characters Whisper emits are included;
#: apostrophes survive INSIDE a word, so "don't" stays one token.
_CONTEXT_WORD_STRIP = string.punctuation + '…—–“”‘’'


def _context_words(text):
    """Normalised word list for comparing a decode with its prompt: lower
    case, outer punctuation removed. '"Fuck, no."' and 'fuck no' compare
    equal, which is the point -- Whisper re-emits a regurgitated span with
    its own capitalisation and punctuation, never the original's."""
    words = []
    for word in str(text or '').split():
        word = word.strip(_CONTEXT_WORD_STRIP).lower()
        if word:
            words.append(word)
    return words


#: Fewest words an end-anchored match needs before it counts as an echo.
#: Chosen from the cost asymmetry, not a tuning sweep. Refusing a real
#: utterance costs the user one repeat and says so on screen; accepting an
#: echo corrupts the draft AND becomes the next prompt, which the queue 44
#: table prices at up to 30 of 37 recordings. Four is the smallest value that
#: takes the whole 2026-09-15 family -- the seven-word 21:29:01 echo and the
#: four-word "Oh shit, no way." pair at 21:27:17/21:27:23 -- while leaving the
#: one- and two-word fragments that table is made of ("nd", "nd nd") to the
#: prompt-side fix. Catching those here would need a character-level rule,
#: and a character-level rule refuses a genuine "and" because the tail ends
#: in "hand": deleting real speech, in a different costume.
_ECHO_MIN_WORDS = 4

#: What the listening indicator says when a decode is refused as an
#: echo. Names the cause (it came from the draft, not the mic) and the
#: consequence (nothing was added), because the alternative -- the way
#: this bug has behaved so far -- is text appearing that the user never
#: said, or nothing happening with no reason given.
_CONTEXT_ECHO_CHIP = 'echo of your draft - not staged'


def _is_context_echo(text, context_tail):
    """True when `text` is the prompt tail talking back, not the user.

    The test: the WHOLE decode, normalised, is the final run of words of the
    tail it was given.

    END-ANCHORED, and that is the discriminator the check rests on. Whisper
    regurgitates the most recent prompt tokens -- the ones it is conditioned
    to continue -- so an echo always lands flush against the end of the tail.
    A user genuinely repeating themselves after a gap has said other things
    in between, so their earlier instance sits in the MIDDLE of the tail and
    this returns False. Repetition alone is never the signal; repetition of
    the last thing in the prompt is.

    WHOLE decode only. A decode that is an echo followed by new material
    contains real speech, and refusing the utterance would eat it, so a
    partial match is not an echo.
    """
    if not context_tail:
        return False
    words = _context_words(text)
    if len(words) < _ECHO_MIN_WORDS:
        return False
    tail_words = _context_words(context_tail)
    return len(tail_words) >= len(words) and tail_words[-len(words):] == words


#: A trailing run of this many identical words in the tail is degenerate
#: repetition, not context.
#:
#: Three, the same floor _is_hallucinated_segments' Signature D uses for the
#: same-looking shape.
#:
#: TWO WAS TRIED AND REJECTED, and the reason is worth keeping. The argument
#: for two looked good: this trim only decides what the decoder is
#: CONDITIONED ON, not what the user gets, so a false positive costs two
#: words of context rather than two words of speech -- and it would break a
#: runaway one utterance sooner ("...and nd nd" recovers instead of decoding
#: "nd" again). What it actually did was cascade. The trim runs to a fixed
#: point, so a tail ending "Oh shit, no way. Baby, baby. What? What?" loses
#: "what what", then "baby baby", and stops having been cut back to
#: "Oh shit, no way." -- at which point a user genuinely saying that again
#: after a gap becomes an end-anchored match and _is_context_echo refuses
#: real speech. Trimming that reaches far enough back can MANUFACTURE the
#: echo it is supposed to prevent. Caught by
#: test_a_genuine_repeat_after_a_gap_is_still_staged.
#:
#: No arm in nd_prompt_contamination_106.md separates the two values -- all
#: five real tails sanitise identically under both, and tail_14:11:55's run
#: is three long so it is trimmed either way -- so the measured 0/37 -> 29/37
#: recovery is unaffected by this choice. The interaction above is.
_TAIL_REPEAT_RUN = 3
#: Characters of pending buffer used as decoder context. Unchanged from
#: SessionModeManager.dictate_context_tail's own default; the cap is not the
#: problem, what the slice contains is.
_CONTEXT_TAIL_CHARS = 200
#: Start of a sentence inside the window: terminal punctuation, optional
#: closing quote/bracket, whitespace. Whisper writes ".." and "..." freely,
#: hence the repeat.
_SENTENCE_START_RE = re.compile(r'[.!?…]+["\'’\)\]]*\s+')


def _sanitise_context_tail(raw, max_chars=_CONTEXT_TAIL_CHARS):
    """Clean the DICTATE context tail before Whisper is conditioned on it.

    The raw tail is dictate_context_tail()'s source[-max_chars:] -- an
    unaligned character slice of the pending buffer. Three things are wrong
    with feeding that back verbatim, and all three are visible in the five
    real tails recovered for queue 44
    (perf_artifacts/nd_prompt_contamination.json):

    1. EVERY one of them starts mid-word -- "de because...", "ecause...",
       "use you know...", "u know in dictate...", "t if..". The prompt's own
       first token is a word fragment. Conditioning a decoder whose failure
       mode is emitting fragments on a text that OPENS with one is the defect
       feeding itself its own example. The slice now begins at a sentence
       boundary when the window holds a usable one, and at a word boundary
       otherwise; never inside a word.

    2. `nd` is a decoder fragment, not an English word. It is removed wherever
       it occurs in the emitted tail, so a later real word cannot hide it from
       the old trailing-run rule ("...and nd submit").

    3. An immediate repeat of a short token is collapsed ("submit submit" ->
       "submit"). This is bounded to adjacent tokens and only changes decoder
       context, never delivered dictation.

    4. A trailing run of the same word ("...I don't know and nd nd nd") is
       degenerate repetition that got staged, and on the 37-recording table
       it is the whole distance between 0 and 27 correct decodes. Trimmed
       here -- the prompt-side twin of _trim_trailing_garbage_run's trailing
       punctuation trim. Same rule, finally applied to the input nobody had
       applied it to. The run is removed entirely rather than collapsed to
       one: leaving one "nd" behind lands on the tail_14:11:45 row (3/37),
       removing all three lands on tail_14:11:40 (27/37).

    5. A trailing punctuation-garbage run, for the same reason;
       _trim_trailing_garbage_run is reused directly rather than restated.

    Returns "" when nothing usable survives. No prompt at all is a worse
    prompt than a clean one (16/37 against 33/37) and a far better one than
    a poisoned one (0/37).

    Pure and side-effect free so the whole policy is testable without Qt, a
    model or a session.
    """
    raw = str(raw or '')
    # Was this slice cut out of a longer buffer? dictate_context_tail returns
    # source[-max_chars:], so hitting the cap exactly is the only evidence
    # that anything was cut off. Front alignment REPAIRS a cut; when there
    # was no cut the tail already starts wherever the user started, and
    # trimming its first word would throw away context for nothing.
    truncated = len(raw) >= max_chars
    text = ' '.join(raw.split())
    if not text or max_chars <= 0:
        return ''
    if len(text) > max_chars:
        text = text[-max_chars:]

    # 1. Front alignment. Prefer the earliest sentence boundary in the window
    #    (keeps the most context while still starting cleanly), but only when
    #    it leaves a prompt worth having -- otherwise a stray full stop near
    #    the end of the window would shrink the tail to a few words. Fall
    #    back to the first word boundary, which costs at most one fragment.
    if truncated:
        match = _SENTENCE_START_RE.search(text)
        if match is not None and len(text) - match.end() >= max_chars // 2:
            text = text[match.end():]
        elif ' ' in text:
            text = text[text.index(' ') + 1:]
        text = text.strip()

    # Remove the observed standalone decoder fragment everywhere, rather than
    # only at the end where a later spoken word can mask it.
    words = text.split()
    words = [word for word in words if word.strip(_CONTEXT_WORD_STRIP).lower() != 'nd']
    text = ' '.join(words)

    # Collapse immediate repeats of short words in the context. Long repeated
    # words are more likely intentional prose; short fragments are the decoder
    # failure shape and retaining both makes them a stronger next-prompt bias.
    collapsed = []
    for word in text.split():
        value = word.strip(_CONTEXT_WORD_STRIP).lower()
        if value and len(value) <= 8 and collapsed and value == collapsed[-1][1]:
            continue
        collapsed.append((word, value))
    text = ' '.join(word for word, _value in collapsed)

    # 4/5. Trailing trims, to a fixed point: removing a degenerate run can
    #      expose the run that seeded it ("a a a b b b").
    while text:
        trimmed = _trim_trailing_garbage_run(text)
        if trimmed != text:
            text = trimmed
            continue
        words = text.split()
        normalised = [w.strip(_CONTEXT_WORD_STRIP).lower() for w in words]
        run = 0
        for value in reversed(normalised):
            if not value or value != normalised[-1]:
                break
            run += 1
        if run >= _TAIL_REPEAT_RUN and normalised[-1]:
            text = ' '.join(words[:-run]).strip()
            continue
        break
    return text


def _is_quality_exhausted(seg_list, transcribe_params):
    """True if faster-whisper's OWN quality gate never actually passed for
    this decode -- its temperature fallback ladder exhausted every rung
    (0.0, 0.2, 0.4, ... up to 1.0 by default) still failing log_prob_
    threshold or the compression-ratio ceiling, and it returned the final
    failed attempt anyway rather than nothing. See module-level comment on
    _COMPRESSION_RATIO_THRESHOLD for the production incident this fixes.

    Distinct from _is_hallucinated_segments: that's a backstop against
    KNOWN hallucination TEXT signatures (fixed phrases, repetition) --
    this is a pure QUALITY-SIGNAL check, independent of what the text
    actually says. A transcription can look perfectly plausible and still
    be untrustworthy if Whisper itself never found a decode confident
    enough to stop early on.

    Reads log_prob_threshold from transcribe_params -- the SAME dict
    actually passed to model.transcribe() for this call -- so this can
    never drift from what was really configured. compression_ratio_
    threshold is never explicitly passed as a kwarg anywhere in this file
    (see get_transcription_params/_build_hotkey_transcribe_params), so
    there is no config value to read for it; _COMPRESSION_RATIO_THRESHOLD
    is faster-whisper's own real internal default for that check.

    Real speech does not produce these signals (that's the entire premise
    behind faster-whisper accepting a decode instead of escalating temp
    in the first place) -- a normal dictation's segments pass both checks,
    so this never fires on genuine speech, long or short. Empty seg_list
    means nothing to judge -- never fires."""
    if not seg_list:
        return False
    sig = diagnostics.segment_signals(seg_list)
    if sig['n_segments'] == 0:
        return False
    logprob_threshold = transcribe_params.get('log_prob_threshold')
    failing_logprob = (
        logprob_threshold is not None
        and sig['avg_logprob'] is not None
        and sig['avg_logprob'] < logprob_threshold
    )
    failing_compression = (
        sig['compression_ratio'] is not None
        and sig['compression_ratio'] > _COMPRESSION_RATIO_THRESHOLD
    )
    return failing_logprob or failing_compression


def _keep_low_confidence_long_chunk(seg_list, text, duration_s):
    """Allow a narrowly safe long-dictation fallback after quality exhaustion.

    Faster-whisper may exhaust its temperature ladder on a real, continuous
    sentence and still return a coherent final decode.  Silently throwing away
    that entire chunk is worse than preserving a possible transcription error.
    This is intentionally *not* a general quality bypass: callers must first
    reject known hallucination signatures, and this fallback additionally
    requires sustained content, low final compression, and audible speech.
    """
    if duration_s < 5.0:
        return False
    words = [word for word in re.findall(r"\b\w+\b", text or "") if word]
    if len(words) < 5:
        return False
    sig = diagnostics.segment_signals(seg_list)
    if sig["n_segments"] == 0:
        return False
    compression = sig["compression_ratio"]
    if compression is None or compression > _COMPRESSION_RATIO_THRESHOLD:
        return False
    # segment_signals returns the highest no-speech probability across the
    # chunk. A single near-silent segment is enough to keep the hard reject.
    no_speech = sig["no_speech_prob"]
    return no_speech is not None and no_speech <= 0.5


def _apply_segment_quality_gates(seg_list, transcribe_params, audio_duration):
    """Segment-level hallucination/quality gating for one full decode.

    Replaces the old per-chunk aggregate gating: on a single long decode,
    rejecting the AGGREGATE means total data loss for an accessibility tool
    whose user cannot retype what was lost. This evaluates hallucination and
    quality per segment instead, so one bad segment among many good ones
    only costs that segment, not the whole recording.

    Returns (text, low_confidence):
      text: the text to deliver (possibly "").
      low_confidence: True only when every segment that survived
        hallucination screening still failed the quality gate, and the
        never-silently-empty floor below delivered the raw text anyway.

    Order of operations:
    1. Whole-decode hallucination check, over every segment and the full
       joined text -- catches CROSS-segment patterns (the model echoing the
       same phrase across many segments), which is invisible to any
       single-segment check. If this fires the entire decode is discarded;
       a decode that IS entirely hallucination legitimately returns "" and
       the floor in step 3 does not apply to it.
    2. Otherwise, walk segments in order. A segment is dropped, and only
       that segment, when it is itself a hallucination in isolation
       (single-segment garbage -- e.g. one "click click click" segment
       embedded in an otherwise-real long recording, which whole-decode
       diversity/dominance checks could miss once diluted by the
       surrounding real speech) or when it individually fails
       _is_quality_exhausted. Surviving segments are joined in order.
    3. If every non-hallucinated segment was dropped for QUALITY and the
       unfiltered text is non-empty, never silently return empty on
       quality grounds alone: apply the SAME "plausible long dictation"
       judgment _keep_low_confidence_long_chunk already encodes (commit
       d5d6b2d's fix for a real 25s chunk), now over the whole decode
       instead of a 25s chunk. For a hands-free user, imperfect text is
       far easier to fix by voice than a lost thought is to re-dictate.
    """
    log = logging.getLogger("Samsara")
    raw_text = "".join(getattr(seg, "text", "") or "" for seg in seg_list).strip()

    if _is_hallucinated_segments(seg_list, raw_text):
        log.info(f"[GUARD] Suppressed hallucination: {raw_text!r}")
        return "", False

    kept = []
    dropped_for_quality = False
    for seg in seg_list:
        seg_text = (getattr(seg, "text", "") or "").strip()
        if not seg_text:
            continue
        if _is_hallucinated_segments([seg], seg_text):
            log.info(f"[GUARD] Suppressed hallucinated segment: {seg_text!r}")
            continue
        if _is_quality_exhausted([seg], transcribe_params):
            dropped_for_quality = True
            sig = diagnostics.segment_signals([seg])
            log.info(
                f"[QUALITY] dropped low-confidence segment (logprob "
                f"{sig['avg_logprob']}, compression {sig['compression_ratio']}, "
                f"no_speech {sig['no_speech_prob']}): {seg_text!r}"
            )
            continue
        kept.append(seg)

    text = "".join(getattr(seg, "text", "") or "" for seg in kept).strip()
    if text or not dropped_for_quality:
        return text, False

    # Every segment that survived hallucination screening still failed
    # quality. Never silently return empty on quality grounds alone.
    if raw_text and _keep_low_confidence_long_chunk(seg_list, raw_text, audio_duration):
        log.warning(
            f"[QUALITY] every segment failed quality gates -- delivering "
            f"low-confidence decode ({audio_duration:.1f}s): {raw_text!r}"
        )
        return raw_text, True

    log.info(
        f"[QUALITY] every segment failed quality gates and floor criteria "
        f"not met -- rejecting ({audio_duration:.1f}s): {raw_text!r}"
    )
    return "", False
