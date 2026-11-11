"""Text-only pause repair for an explicitly requested, reviewable edit.

This is deliberately separate from automatic Clean hold delivery. No timing or
spoken-punctuation provenance reaches this seam. Unknown capitalized words stay
capitalized; shortness without a grammatical attachment is never enough.
"""
import re


_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)
_DEPENDENT = frozenset(
    'and but or so because which where when if with to of in on at for from '
    'into by as while although unless until'.split())
_DETERMINERS = frozenset('a an the my your our their this these whatever'.split())
_SUBJECTS = frozenset('i you he she it we they there'.split())
_FINITE = frozenset(
    'am is are was were have has had do does did will would can could should '
    'may might must want wants need needs go goes went work works worked know '
    'knows knew think thinks thought say says said tell tells told make makes '
    'made help helps helped live lives lived see sees saw arrive arrives arrived '
    'leave leaves left stay stays stayed finish finishes finished stop stops '
    'stopped like likes use uses used feel feels hear hears watch watches try '
    'tries tried check checks checked bought waved'.split())
_COMPLEMENTS = frozenset('try trying discuss discussed using saying'.split())
_ADVERBS = frozenset('actually really still already always never only also finally'.split())
_COMMON = frozenset(
    'dictation pauses pause cleanup saying sort sure gonna wanna actually really '
    'still already always never only also finally speaking trying checking '
    'everything every single word before after remain keep parcel small blue '
    'garden gate'.split())
_REPLIES = frozenset('yes no okay ok thanks hello goodbye fine sure right stop cancel'.split())
_QUESTIONS = frozenset(
    'is are was were do does did can could will would should what why how when '
    'where'.split())
_LITERAL = re.compile(
    r'https?://|www\.|\S+@\S+|[\\/]|[`:;"“”]|(?<!\w)[\'‘]|[\'’](?!\w)'
    r'|\b\d+\.\d+\b|\b(?:[A-Za-z]\.){2,}|\b[A-Z]\.(?=\s+[A-Z])'
    r'|\b(?:Mr|Mrs|Ms|Dr|Prof|etc|vs)\.'
    r'|\b[\w-]+\.[A-Za-z]{1,8}\b', re.IGNORECASE)
_LIST = re.compile(r'^\s*(?:[-*+•]|\d+[.)]|[A-Za-z][.)])\s')
_COMMAND = re.compile(
    r'^(?:clean up|clear draft|show the change|bring it back|scratch that|'
    r'pause (?:VLC|Spotify)|play VLC)\b', re.IGNORECASE)


def _words(text):
    return [match[0].casefold().replace('’', "'") for match in _WORD.finditer(text)]


def _finite(words):
    return any(word in _FINITE or re.fullmatch(r"(?:i'm|\w+'(?:re|ve|ll|d)|(?:it|that|he|she|there)'s|\w+n't)", word)
               for word in words)


def _independent(words):
    return (len(words) > 1 and _finite(words)
            and (words[0].split("'", 1)[0] in _SUBJECTS or words[0] in {'that', 'this'}
                 and words[1] in _FINITE))


def _join(left, right, legacy_join):
    previous, following = _words(left), _words(right)
    if not previous or not following or '?' in right or '!' in right:
        return False
    if previous[0] in _QUESTIONS:
        return False
    if _COMMAND.match(right) or following[0] in _REPLIES or _independent(following):
        return False
    if legacy_join(left, right):
        return True
    if following[0] in _DEPENDENT or following[:2] == ['instead', 'of']:
        return _finite(previous) or previous[-1] in _DEPENDENT
    if (previous[-1] in _COMPLEMENTS and len(following) <= 7
            and not _finite(following)):
        return True
    # "That" introducing a relative/complement clause is distinguishable from
    # "That was odd"; the latter is an independent demonstrative sentence.
    if following[0] == 'that' and not _independent(following):
        return _finite(previous)
    subject = previous
    if 'that' in previous:
        subject = previous[len(previous) - previous[::-1].index('that'):]
    noun = (bool(subject) and len(subject) <= 7 and not _finite(subject)
            and (subject[0] in _DETERMINERS or len(subject) == 1
                 and subject[0] in _COMMON - _REPLIES))
    predicate = following[1:] if following[0] in _ADVERBS else following
    return noun and bool(predicate) and predicate[0] in _FINITE


def repair(text, legacy_join, lowercase_words):
    """Keep hard boundaries; repair evidenced pauses to a stable fixed point.

    Literal-bearing lines are opaque: a conservative miss is preferable to
    editing a quote, list, abbreviation, number, path or filename as prose.
    Only a small ordinary-word vocabulary loses stray capitals, like 747/1209;
    unknown names, I/contractions, acronyms and mixed-case words survive.
    """
    common = lowercase_words | _COMMON | _FINITE | _DEPENDENT | _ADVERBS
    lines = re.split(r'(\r\n|\r|\n)', text or '')
    for index in range(0, len(lines), 2):
        line = lines[index]
        if _LITERAL.search(line) or _LIST.match(line):
            continue
        line = re.sub(r'(?<=\S)[ \t]{2,}(?=\S)', ' ', line)
        # A final ellipsis remains deliberate/unfinished. At this plain-text
        # seam an interior ellipsis has no explicit-command provenance.
        line = re.sub(r'(?<=\w)[ \t]*(?:\.{3,}|…)[ \t]*(?=\w)', ' ', line)
        while True:
            parts = re.split(r'((?<!\.)\.(?!\.)[ \t]+)', line)
            result = parts[0]
            for offset in range(1, len(parts), 2):
                separator, following = parts[offset:offset + 2]
                left = re.split(r'[.!?…]', result)[-1].strip()
                # Questions/exclamations are hard bounds, including the right
                # fragment's terminator, which the legacy predicate checks.
                probe = re.split(r'(?<=[.!?…])', following, maxsplit=1)[0]
                if _join(left, probe, legacy_join):
                    result = re.sub(r',[ \t]*$', '', result).rstrip(' \t')
                    separator = ' '
                result += separator + following
            if result == line:
                break
            line = result

        previous = ''

        def lower(match):
            nonlocal previous
            word = match[0]
            before, previous = previous, word.casefold().replace('’', "'")
            if (word.casefold() not in common or not word[:1].isupper()
                    or word.isupper() or any(c.isupper() for c in word[1:])):
                return word
            prefix = line[:match.start()].rstrip()
            # Ambiguous modal/name spellings and an explicit naming context
            # are positive name evidence, even for our ordinary vocabulary.
            named = (before in {'met', 'named', 'called', 'mr', 'mrs', 'ms', 'dr', 'prof'}
                     or word.casefold() in {'may', 'will'} and before not in _SUBJECTS)
            if not named and prefix and prefix[-1] not in '.!?…':
                return word.lower()
            return word

        lines[index] = _WORD.sub(lower, line)
    return ''.join(lines)
