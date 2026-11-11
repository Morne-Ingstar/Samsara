"""Deterministic post-transcription cleanup.

Runs AFTER process_transcription (auto-cap + number formatting), BEFORE the
text is pasted. Filler/stutter removal and spacing/comma fixes -- no LLM
or model calls.

Conservative by design: every rule here is a potential corruption of free-form
dictation. We err toward keeping a word in than stripping it; "verbatim" mode
exists for code dictation and other content where every token must survive.

The raw Whisper output is preserved separately in history (raw_text column),
so any over-cleaning is recoverable.
"""

import re

# Remove hesitation sounds only. A decoder's comma does not establish that
# a discourse word is disposable; those words must survive in clean mode.
FILLERS = [
    r'\bum\b',
    r'\buh\b',
    r'\ber\b',
]
_CLEAN_FILLERS = [r'\berm\b', r'\bah\b', r'\buhm\b', r'\bhmm\b']

# A short prefix alone is not evidence of a false start if it is a word.
# Keep these even with punctuation/repetition: e.g. "in, in instead".
_PREFIX_WORDS = frozenset("""
a i an am as at be by do go he if in is it me my no of oh on or so to up us we
act add age air all and any app are arm art ask ate bad bag bar bat bed bee bet
big bit box boy bus but buy can cap car cat cow cry cup cut dad day did die dog
dry due ear eat egg end era eye far fat fax few fit fix fly for fox fun gas get
god got gun guy gym had ham has hat hay her hey hid him his hit hot how ice ill
ink inn its jam jar jaw jet job joy key kid kit lab lad lap law lay led leg let
lid lie lip lit log lot low mad man map mat may men met mix mom mud net new nod
nor not now odd off oil old one our out owe own pad pan par pat pay pea pen per
pet pie pig pin pit pop pot put ran rat raw ray red rid rip rob rod rot row rub
run sad sat saw say sea see set sew she shy sin sip sir sit six ski sky son sun
tap tar tax tea ten the tie tin tip toe ton too top toy try two use van via war
was wax way web wed wet who why win wit won wow yes yet you zoo
also back ball band bank base bear beat been bell bend best bill bird blow blue
boat body book born both bowl burn busy call calm came camp card care case cash
cast chat city club coal coat code cold come cook cool cope copy core cost cozy
crew crop dare dark data date dawn dead dear deep desk dial diet dirt dish does
done door down draw drop drug each earn east easy edge else even ever exam exit
face fact fail fair fall farm fast fate fear feed feel feet fell felt file fill
film find fine fire firm fish five flat flow food foot form four free from full
fund game gave gear gift girl give glad goal goes gold golf gone good grab grew
grow hair half hall hand hang hard harm hate have head hear heat held hell help
here hero hide high hike hill hold hole holy home hope host hour huge hung hurt
idea inch into iron item join joke jump just keen keep kept kick kill kind king
knew know lack lady lake land lane last late lead leaf lean left lend less life
lift like line link list live load loan lock long look lord lose loss lost love
luck made mail main make male many mark mass mate mean meat meet menu mere milk
mind mine miss mode mood moon more most move much must name near neck need news
next nice nine none nose note okay once only onto open over pack page paid pain
pair park part pass past path peak pick pink plan play plot plus poem poet pool
poor port pose post pray pull pure push quit race rain rank rare rate read real
rear rent rest rice rich ride ring rise risk road rock role roll roof room root
rope rose rule rush safe said sake sale same sand save seat seed seek seem seen
self sell send sent ship shop shot show shut sick side sign site size skin slow
snow soft soil sold some song soon sort soul soup spot star stay step stop such
suit sure take tale talk tall tank tape task team tear tech tell tend term test
text than that them then they thin this thus till time tiny told tone took tool
tour town tree trip true turn twin type unit upon used user vast very view vote
wait wake walk wall want warm wash wave ways weak wear week well went were west
what when whom wide wife wild will wind wine wing wipe wire wise wish with wolf
wood word wore work yard yeah year your zero zone
re co pre pro anti non sub super
""".split())
_DOUBLE_STUTTERS = frozenset({'and', 'the', 'to', 'of', 'i'})
_KEEP_REPEATS = frozenset("""
that had is no bye very really so more less much many far quite rather well
hear there now never ever again please yes right okay fine sure go come stop
zero one two three four five six seven eight nine ten eleven twelve thirteen
fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty
sixty seventy eighty ninety hundred thousand million billion trillion first
second third fourth fifth sixth seventh eighth ninth tenth
""".split())
_STUTTER_WORD = re.compile(r"(?<![\w'’])[A-Za-z]+(?:['’][A-Za-z]+)*(?![\w'’])")
_FRAGMENT_GAP = re.compile(r'[ \t,.…-]+')
_REPEAT_GAP = re.compile(r'(?:[ \t]+|[ \t]*,[ \t]*)')


def _remove_stutters(text):
    """Remove only evidenced false starts; leave all surrounding bytes alone.

    Prefixes are at most four letters and strictly shorter than the repair.
    A capitalized fragment/name/initial needs two identical false starts.
    Common words never qualify. No spelling guesses for prolongations.
    """
    words = list(_STUTTER_WORD.finditer(text))
    removed = {}
    for index, repair in enumerate(words):
        start = index
        while start:
            fragment = words[start - 1]
            value = fragment[0].casefold()
            gap = text[fragment.end():words[start].start()]
            if (not _FRAGMENT_GAP.fullmatch(gap)
                    or not 1 <= len(value) <= 4
                    or value in _PREFIX_WORDS
                    or len(value) >= len(repair[0])
                    or not repair[0].casefold().startswith(value)):
                break
            start -= 1
        if start == index:
            continue
        fragments = words[start:index]
        # AC adapter / Ac accessibility / Jo John are plausible names/initials.
        if any(w[0][0].isupper() for w in fragments) and not (
                len(fragments) >= 2
                and len({w[0].casefold() for w in fragments}) == 1):
            continue
        removed[start] = index

    # Keep the final spelling/case of the repair; consume only its false starts.
    pieces = []
    cursor = 0
    index = 0
    while index < len(words):
        if index in removed:
            end = removed[index]
            pieces.append(text[cursor:words[index].start()])
            cursor = words[end].start()
            index = end
        index += 1
    pieces.append(text[cursor:])
    text = ''.join(pieces)

    words = list(_STUTTER_WORD.finditer(text))
    pieces = []
    cursor = 0
    index = 0
    while index < len(words):
        end = index + 1
        value = words[index][0].casefold()
        while (end < len(words) and words[end][0].casefold() == value
               and _REPEAT_GAP.fullmatch(text[words[end - 1].end():words[end].start()])):
            end += 1
        run = words[index:end]
        if (value not in _KEEP_REPEATS
                and (len(run) >= 3 or len(run) == 2 and value in _DOUBLE_STUTTERS)
                and (value in _DOUBLE_STUTTERS or not (
                    any(w[0][0].isupper() for w in run[1:])
                    or words[index][0].isupper()))):
            pieces.append(text[cursor:words[index].end()])
            cursor = words[end - 1].end()
        elif value == 'i' and len(run) >= 2:
            pieces.append(text[cursor:words[index].end()])
            cursor = words[end - 1].end()
        index = end
    pieces.append(text[cursor:])
    return ''.join(pieces)


def clean_text(text, mode="clean"):
    """Clean Whisper output.

    Modes:
      "verbatim" -- return text unchanged (for code dictation, etc.)
      "clean"    -- remove fillers/stutters, fix spacing, normalize commas, ensure
                    final punctuation, capitalize sentence starts and "I".

    Returns the input unchanged for empty/None or in verbatim mode.
    """
    if mode == "verbatim" or not text:
        return text

    result = text
    if mode == 'clean':
        result = _remove_stutters(result)

    # Ah/Hmm can be deliberate replies when they are the entire utterance.
    reply = re.fullmatch(r'\s*(?:ah|hmm)[.!?]*\s*', text, flags=re.IGNORECASE)
    patterns = FILLERS + (_CLEAN_FILLERS if mode == 'clean' and not reply else [])
    for pattern in patterns:
        # Consume the filler's following comma so sentence starts do not
        # become "Next. , Continue". Keep any preceding clause separator.
        suffix = r',?' if mode == 'clean' else ''
        result = re.sub(pattern + suffix, '', result, flags=re.IGNORECASE)
    if mode == 'clean':
        result = re.sub(r',\s*([.!?])', r'\1', result)

    # Preserve legacy cleanup for unrecognized modes; new rules are Clean-only.
    if mode != 'clean':
        result = re.sub(r'\b(I)(?:[ \t]+\1)+\b', r'\1', result, flags=re.IGNORECASE)

    # Collapse runs of whitespace left by filler removal
    result = re.sub(r'\s{2,}', ' ', result)

    # Orphaned commas: ", ," -> ","  (run twice in case of three-in-a-row)
    result = re.sub(r',\s*,', ',', result)
    result = re.sub(r',\s*,', ',', result)

    # Strip leading whitespace BEFORE the sentence-cap regex, otherwise the
    # `^` anchor never sees the first letter.
    result = result.lstrip()

    # Drop a leading comma if filler removal left one
    result = re.sub(r'^\s*,\s*', '', result)

    # Trailing comma -> period
    result = re.sub(r',\s*$', '.', result)

    # Capitalize first letter of each sentence
    result = re.sub(
        r'(^|[.!?]\s+)([a-z])',
        lambda m: m.group(1) + m.group(2).upper(),
        result,
    )

    # Capitalize standalone "I"
    result = re.sub(r'\bi\b', 'I', result)

    # Tighten space before punctuation, ensure space after
    result = re.sub(r'\s+([.,!?;:])', r'\1', result)
    result = re.sub(r'([.,!?;:])([A-Za-z])', r'\1 \2', result)

    # Final trim + ensure terminal punctuation
    result = result.strip()
    if result and result[-1] not in '.!?':
        result += '.'

    return result
