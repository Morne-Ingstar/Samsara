"""Text-only repair of pause punctuation in an explicitly requested edit."""

import logging
import re
from functools import lru_cache

try:
    from lemminflect import getAllInflections, getAllLemmas
except ImportError:
    getAllInflections = getAllLemmas = None
    logging.getLogger(__name__).warning(
        "LemmInflect unavailable; offline cleanup is using its built-in verb list")


_WORD = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*", re.UNICODE)
_CLAUSE_WORD = re.compile(r"[^\W\d_]+(?:[-'\u2019][^\W\d_]+)*", re.UNICODE)
_PREPOSITIONS = frozenset(
    "about above across after against along among around as at before behind below beneath "
    "beside between beyond by despite down during except for from in inside into like near of "
    "off on onto out outside over past since through throughout till to toward under underneath "
    "until up upon via with within without".split())
_SUBORDINATORS = frozenset(
    "after although as because before if once since though unless until when whenever where "
    "wherever whether while".split())
_COORDINATORS = frozenset("and but or nor so yet".split())
_DETERMINERS = frozenset(
    "a an another the my your his her its our their this that these those each every whatever".split())
_SUBJECTS = frozenset("i you he she it we they there".split())
_OBJECT_PRONOUNS = frozenset("me you him her us them".split())
_AUXILIARIES = frozenset(
    "am is are was were be been being have has had do does did will would can could should "
    "shall may might must".split())
_FINITE_AUXILIARIES = _AUXILIARIES - {"be", "been", "being"}
_BARE_AUXILIARIES = frozenset({"be", "do", "have"})
_MODALS = frozenset("will would can could should shall may might must".split())
_ADVERBS = frozenset(
    "actually really still already always never only also finally just even often sometimes".split())
_HESITATIONS = frozenset({"uh", "um"})
_RELATIVES = frozenset("that which who whom whose".split())
# These prepositions can also finish a clause as adverbs or verb particles.
_ADVERBIAL_PREPOSITIONS = frozenset(
    "about above across after along around before behind below beneath beyond by down in "
    "inside near off on out outside over past through under underneath up".split())
_UNFINISHED_ENDINGS = (
    (_DETERMINERS - {"this", "that", "these", "those", "his", "her"})
    | (_PREPOSITIONS - _ADVERBIAL_PREPOSITIONS)
    | (_COORDINATORS - {"so", "yet"})
)
_REPLIES = frozenset("yes no okay ok thanks hello goodbye fine sure right stop cancel".split())
_SIGN_OFFS = frozenset({"over", "done", "end"})
_SENTENCE_OPENERS = frozenset({
    "after all", "anyway", "as a result", "by the way", "for example", "frankly",
    "honestly", "however", "if not", "if so", "in fact", "in other words",
    "meanwhile", "nevertheless", "of course", "on the other hand", "otherwise",
    "that said", "therefore",
})
_QUESTIONS = frozenset("what why how when where who whom whose which".split())
_INDEFINITE_SUBJECTS = frozenset(
    "anybody anyone anything everybody everyone everything nobody none nothing "
    "somebody someone something".split())
_FINITE_FORMS = frozenset(("VBD", "VBZ", "VBP"))
_CALENDAR = frozenset(
    "monday tuesday wednesday thursday friday saturday sunday "
    "january february march april may june july august september october november december".split())
# Grammatical contexts that license particular complements, independent of
# which inflected form of the verb occurs in the dictation.
_OBJECT_INFINITIVE = frozenset("help let make hear see watch feel".split())
_CLAUSAL_VERBS = frozenset("believe guess hope know say suppose think wonder".split())
_SPEECH_VERBS = frozenset("ask reply say speak tell whisper yell".split())
_NAME_CONTEXT = frozenset("called met named".split())
_INFINITIVE_CONTEXT = frozenset(
    "choose decide expect have hope learn like need plan promise seem start tend try want".split())
# Compact fallback for installations missing the inflection dictionary.
# Each row follows the common base, present singular, and past forms.
_BUILTIN_VERBS = (
    "arrive arrives arrived", "ask asks asked", "buy buys bought", "call calls called",
    "check checks checked", "choose chooses chose", "decide decides decided",
    "discuss discusses discussed", "feel feels felt", "finish finishes finished",
    "fix fixes fixed", "get gets got", "go goes went", "hear hears heard",
    "help helps helped", "hurt hurts hurt", "know knows knew", "leave leaves left",
    "let lets let", "like likes liked", "live lives lived", "make makes made", "meet meets met",
    "need needs needed", "see sees saw", "say says said", "stay stays stayed",
    "stop stops stopped", "tell tells told", "think thinks thought", "try tries tried",
    "use uses used", "want wants wanted", "watch watches watched", "work works worked",
)
_BUILTIN_FORMS = {}
for _row in _BUILTIN_VERBS:
    _base, _present, _past = _row.split()
    for _form, _tags in ((_base, {"VB", "VBP"}), (_present, {"VBZ"}), (_past, {"VBD"})):
        _BUILTIN_FORMS.setdefault(_form, (_base, set()))[1].update(_tags)

_CLOSED_CLASS = (_PREPOSITIONS | _SUBORDINATORS | _COORDINATORS | _DETERMINERS
                 | _SUBJECTS | _OBJECT_PRONOUNS | _AUXILIARIES | _RELATIVES
                 | frozenset("all any both either everything hers himself itself mine neither nobody "
                             "none nothing one oneself ourselves ours some somebody someone something "
                             "theirs themselves what yours yourself yourselves".split()))
_FUNCTION_WORDS = _CLOSED_CLASS | _ADVERBS | {"instead", "then"}
_BUILTIN_COMMON = _HESITATIONS | frozenset(
    "actually available being clinic done instead pausing pauses pause cleanup dictation "
    "ready reviewed someone store working".split())
_INLINE_CLEANUP_WORDS = frozenset({"cleanup", "dictation", "pause", "pauses"})
_LITERAL = re.compile(
    r"""https?://[^\s<>\"']*[^\s<>\"'.,!?;:)]|www\.[^\s<>\"']*[^\s<>\"'.,!?;:)]"""
    r"""|[\w.+-]+@[\w.-]+\.\w+"""
    r"""|(?:\b[A-Za-z]:[\\/]|\\\\|(?<!\w)/)[^\s<>\"']*[^\s<>\"'.,!?;:)]"""
    r"""|[\w.-]+(?:[\\/][\w.-]+)+(?<![.])"""
    r"""|\"[^\"\r\n]*\"|\u201c[^\u201d\r\n]*\u201d|[\x60][^\x60\r\n]*[\x60]"""
    r"""|(?<!\w)'[^']+'(?!\w)|\u2018[^\u2019\r\n]+\u2019"""
    r"""|\b\d+(?:\.\d+)+\b|\b(?:[A-Za-z]\.){2,}"""
    r"""|\b(?:Mr|Mrs|Ms|Dr|Prof|St)\."""
    r"""|\b(?:etc|vs)\.|\b[A-Z]\.(?=[ \t]+[A-Z])"""
    r"""|\b[\w-]+\.[A-Za-z]{1,8}\b""")
_LIST = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)]|[A-Za-z][.)])\s")
_COMMAND = re.compile(
    r"^(?:clean up|clear draft|show the change|bring it back|scratch that|"
    r"pause (?:VLC|Spotify)|play VLC)\b", re.I)
_BOUNDARY = re.compile(r"([ \t]*(?:\.{3,}|\u2026)[ \t]*|(?<!\.)\.(?!\.)[ \t]+)")
_HARD_BOUNDARY = re.compile(r"[.!?:;\u2026]")
_ELLIPSIS = re.compile(r"\.{3,}|\u2026")
_PERSON_CONTRACTION = re.compile(
    r"(?:i'm|\w+'(?:re|ve|ll|d)|(?:it|that|he|she|there)'s|\w+n't)")


def _words(text, pattern=_WORD):
    return [word.lower().replace("\u2019", "'") for word in pattern.findall(text)]


def _subject(word):
    return word.split("'", 1)[0] in _SUBJECTS


@lru_cache(maxsize=4096)
def _lemmas(word):
    if getAllLemmas is None:
        entry = _BUILTIN_FORMS.get(word)
        return {"VERB": (entry[0],)} if entry else {}
    return getAllLemmas(word)


@lru_cache(maxsize=4096)
def _verb_forms(word):
    if getAllInflections is None:
        entry = _BUILTIN_FORMS.get(word)
        return frozenset(entry[1]) if entry else frozenset()
    return frozenset(tag for lemma in _lemmas(word).get("VERB", ())
                     for tag, forms in getAllInflections(lemma, "VERB").items()
                     if word in forms)


def _verb(word):
    return bool(_lemmas(word).get("VERB")) or word in _AUXILIARIES


def _finite(word):
    return (word in _FINITE_AUXILIARIES
            or bool(_verb_forms(word) & _FINITE_FORMS)
            or bool(_PERSON_CONTRACTION.fullmatch(word)))


def _unambiguous_predicate(word):
    tags = _lemmas(word)
    return (_finite(word)
            and word not in _PREPOSITIONS
            and (word in _AUXILIARIES or "NOUN" not in tags
                 or any(lemma != word for lemma in tags.get("VERB", ()))
                 and "VBZ" in _verb_forms(word)))


def _clause(words):
    """Look for a subject and predicate, allowing nouns also used as verbs."""
    while words and words[0] in _ADVERBS | _HESITATIONS | {"now", "then"}:
        words = words[1:]
    if len(words) < 2:
        return False
    if _subject(words[0]) or words[0] in {"that", "this", "these", "those"}:
        return any(_finite(word) for word in words[1:]) or bool(
            _PERSON_CONTRACTION.fullmatch(words[0]))
    if (words[0] in _PREPOSITIONS | _SUBORDINATORS | _COORDINATORS | {"being", "been"}
            or _verb(words[0]) and "NOUN" not in _lemmas(words[0])):
        return False
    start = 2 if words[0] in _DETERMINERS else 1
    return any(_unambiguous_predicate(word) for word in words[start:])


def _noun_phrase(words):
    if not words or _subject(words[0]):
        return False
    if words[0] in _DETERMINERS:
        return len(words) > 1 and not _clause(words)
    return (not _clause(words) and any("NOUN" in _lemmas(word) for word in words)
            and not _unambiguous_predicate(words[0]))


def _subordinate(words):
    return (len(words) > 2 and words[0] in _SUBORDINATORS
            and words[1] not in _AUXILIARIES and _clause(words[1:]))


def _fronted_clause(text):
    dependent, comma, main = text.partition(",")
    if not comma:
        return False
    if text[0].isupper() and " ".join(_words(dependent)) in _SENTENCE_OPENERS:
        return True
    if not _subordinate(_words(dependent)):
        return False
    words = _words(main)
    if words[:1] == ["then"]:
        words = words[1:]
    return _clause(words) or bool(words and "VB" in _verb_forms(words[0]))


def _declarative_clause(words):
    if _clause(words):
        return True
    if not words or words[0] in _AUXILIARIES:
        return False
    # A dictionary can list the predicate as both a verb and a noun.
    # Look for a past-tense form or agreement with a plural subject head.
    start = 2 if words[0] in _DETERMINERS else 1
    for index in range(start, len(words)):
        head, predicate = words[index - 1:index + 1]
        nouns = _lemmas(head).get("NOUN", ())
        if head in _FUNCTION_WORDS and head not in _INDEFINITE_SUBJECTS:
            continue
        forms = _verb_forms(predicate)
        if "VBD" in forms and "VB" not in forms and (nouns or not _lemmas(head)):
            return True
        # A final ambiguous noun after a plural modifier is still a viable
        # compound noun phrase ("the settings page").
        if ("VBP" in forms and any(lemma != head for lemma in nouns)
                and (index == 1 or index < len(words) - 1 or "NOUN" not in _lemmas(predicate))):
            return True
        if predicate in _PREPOSITIONS | _RELATIVES:
            break
    return False


def _imperative(words):
    if words[:1] == ["please"]:
        words = words[1:]
    if words[:2] == ["do", "not"] or words[:1] == ["don't"]:
        return True
    return bool(words and (words[0] in _BARE_AUXILIARIES
                           or words[0] not in _CLOSED_CLASS and "VB" in _verb_forms(words[0])))


def _main_clause(text, words):
    if words and words[0] in _COORDINATORS:
        words = words[1:]
    if not words or words[0] == "like" or words[:2] == ["instead", "of"]:
        return False
    first, *rest = words
    if first in _RELATIVES and (first != "that" or not rest or _subject(rest[0])
                               or rest[0] in _DETERMINERS or _finite(rest[0])):
        return False
    # A participial supplement can contain a finite complement of its own.
    if "VBG" in _verb_forms(first) and rest and _subject(rest[0]):
        return False
    if first not in _SUBORDINATORS | _PREPOSITIONS:
        return _declarative_clause(words)
    # A lead-in can contain several comma-delimited modifiers before its
    # main clause. A finite verb inside the lead-in alone is insufficient.
    for part in text.split(",")[1:]:
        tail = _words(part, _CLAUSE_WORD)
        if not tail or tail[0] in _COORDINATORS | _RELATIVES | _PREPOSITIONS | _SUBORDINATORS:
            continue
        if _declarative_clause(tail) or _imperative(tail):
            return True
    return False


def _question_auxiliary(word):
    if word.endswith("n't"):
        word = word[:-3]
        word = {"ca": "can", "wo": "will", "sha": "shall", "ai": "am"}.get(word, word)
    return word if word in _FINITE_AUXILIARIES else None


def _question_start(words):
    if words and words[0] in _QUESTIONS:
        if words[0] in {"who", "what"} and len(words) > 1 and _finite(words[1]):
            return True
        # In a question such as "Which tray did she open", the entire
        # question phrase precedes the auxiliary.
        auxiliary = next((index for index, word in enumerate(words[1:], 1)
                          if _question_auxiliary(word)), len(words))
        words = words[auxiliary:]
    if len(words) < 2:
        return False
    opener = _question_auxiliary(words[0])
    if not opener:
        return False
    subject = words[1]
    if _subject(subject) or subject in _INDEFINITE_SUBJECTS:
        return True
    # A noun subject can be longer than one word. Skip its head before
    # looking for a predicate, so "Do the inventory" remains an imperative.
    if subject in _ADVERBS | (_FUNCTION_WORDS - _DETERMINERS):
        return False
    start = 3 if subject in _DETERMINERS else 2
    if opener in {"am", "is", "are", "was", "were"}:
        return len(words) > start
    forms = {"VBN"} if opener in {"have", "has", "had"} else {"VB"}
    for index in range(start, len(words)):
        head = words[index - 1]
        if opener in {"do", "have"} and not any(
                lemma != head for lemma in _lemmas(head).get("NOUN", ())):
            continue
        if _verb_forms(words[index]) & forms:
            return True
    return False


def _open_clause(previous):
    return (previous[-1] == "because"
            or previous[-1] in {"if", "whether"} and len(previous) > 1
            and _CLAUSAL_VERBS.intersection(_lemmas(previous[-2]).get("VERB", ())))


def _takes_bare_verb(previous):
    ending = previous[-1]
    if ending == "to" or len(previous) > 1 and previous[-2] == "to" and ending in _ADVERBS:
        return True
    return (len(previous) > 1 and ending in _OBJECT_PRONOUNS
            and _OBJECT_INFINITIVE.intersection(_lemmas(previous[-2]).get("VERB", ())))


def _ellipsis_continuation(previous, following):
    first, ending = following[0], previous[-1]
    new_clause = _clause(following) or first == "maybe" and _clause(following[1:])
    if ending in _PREPOSITIONS and following[:2] == ["i", "mean"]:
        return " — "
    if (ending in _PREPOSITIONS and first in _HESITATIONS and len(following) > 1
            and "VBG" in _verb_forms(following[1])):
        return " "
    if first in _HESITATIONS and _subject(ending):
        return " — "
    if (ending in {"am", "are", "was", "were"} | _HESITATIONS
            or ending in _DETERMINERS - _RELATIVES - {"this", "these", "those"}
            or ending in {"i'm", "you're", "he's", "she's", "it's", "we're", "they're"}):
        # Mark an abandoned lead-in when the speaker starts a new clause.
        return " — " if new_clause and first != ending else " "
    if (ending == "like" and len(previous) > 1
            and {"feel", "look", "seem", "sound"}.intersection(
                _lemmas(previous[-2]).get("VERB", ()))):
        return " "
    if previous == ["now", "see"] and new_clause:
        return ", "
    return None


def _subject_predicate(previous, following):
    if "that" in previous:
        previous = previous[len(previous) - previous[::-1].index("that"):]
    if not _noun_phrase(previous):
        return False
    predicate = following[1:] if following[0] in _ADVERBS else following
    if not predicate or len(previous) == len(following) == 1:
        return False
    verb = predicate[0]
    # A short noun fragment does not establish a speaker. These could be
    # separate utterances with an omitted "I" ("My mistake. Said it wrong.").
    if _SPEECH_VERBS.intersection(_lemmas(verb).get("VERB", ())):
        return False
    forms = _verb_forms(verb)
    if forms & {"VBD", "VBZ"} or verb in _AUXILIARIES - {"be", "been", "being"}:
        return True
    # A bare verb after a singular subject usually starts a real command.
    plural = any(lemma != previous[-1] for lemma in _lemmas(previous[-1]).get("NOUN", ()))
    return plural and "VBP" in forms


def _join(left, right, *, ellipsis):
    previous, following = _words(left), _words(right)
    if not previous or not following or right.endswith(("?", "!")) or _COMMAND.match(right):
        return None
    first, ending = following[0], previous[-1]
    if first in _REPLIES or (first == "that" and len(following) > 1
                             and following[1] in _AUXILIARIES):
        return None
    if len(following) == 1 and first in _SIGN_OFFS:
        # A stranded copula still takes a one-word complement.
        if not (first in {"done", "over"}
                and ending in {"am", "is", "are", "was", "were", "be", "been", "being"}
                and previous[0] != "there"
                and not any(word in _QUESTIONS | _SUBORDINATORS for word in previous[:-1])):
            return None
    if _fronted_clause(right):
        return None
    open_clause = _open_clause(previous)
    # A stranded article, preposition, coordinator or auxiliary can make
    # the next fragment look like a sentence ("the. Store and ..."). Let
    # the existing pause rules evaluate those continuations. Lexical verbs
    # in infinitives and presentational "there it is" can finish a clause.
    unfinished = ending in _UNFINISHED_ENDINGS or (
        ending in _AUXILIARIES and previous[0] != "there" and previous[-2:-1] != ["to"])
    if not ellipsis and not unfinished:
        right_clause = _words(right, _CLAUSE_WORD)
        if _question_start(right_clause):
            return None
        left_clause = _words(left, _CLAUSE_WORD)
        if first not in _COORDINATORS and (_main_clause(left, left_clause)
                                          or _imperative(left_clause)):
            if _main_clause(right, right_clause) and not open_clause:
                return None
            # A lowercase bare verb can be a dictated continuation. Look
            # for a determiner after the verb or an explicit request marker.
            imperative_cue = (right[0].isupper() or first in {"please", "don't"}
                              or len(right_clause) > 1 and right_clause[1] in _DETERMINERS)
            if imperative_cue and _imperative(right_clause) and not _takes_bare_verb(previous):
                return None
    # A fronted dependent clause can attach to its main clause. Once it has
    # a main clause, only an open complement or a participial aside fits.
    if _subordinate(previous) and _clause(following):
        if (not _fronted_clause(left) or ending == "that"
                or "VBG" in _verb_forms(first)):
            return ", "
    if previous[0] in _QUESTIONS | _AUXILIARIES:
        return None
    complete = _clause(previous)
    if ellipsis and first == "then" and complete and following[1:2] == previous[:1]:
        return " "
    if ellipsis:
        continuation = _ellipsis_continuation(previous, following)
        if continuation:
            return continuation
    if (len(previous) > 1 and previous[-2] == "to" and ending in _ADVERBS
            and "VB" in _verb_forms(first)):
        return " "

    dependent = (first in _SUBORDINATORS | _RELATIVES | _PREPOSITIONS
                 or following[:2] == ["instead", "of"])
    if _subject(first) or _clause(following) and not (dependent or first in _COORDINATORS):
        if ellipsis and (_CLAUSAL_VERBS.intersection(_lemmas(ending).get("VERB", ()))
                         or ending == "that" and len(previous) > 2):
            return " "
        # A bare terminal 'while', 'once' etc. can finish a clause. Only
        # a complement-selecting verb can license a new subject here.
        if not open_clause or ending == "because" and not complete:
            return None
    # A repeated predicate after a comma is a continuation of the same list.
    last_item = left.rpartition(",")
    if (last_item[1] and len(following) > 2 and following[1] in _OBJECT_PRONOUNS
            and "VBZ" in _verb_forms(first) and _words(last_item[2])[:2] == following[:2]):
        return ", "
    if not ellipsis and right[0].islower():
        return " "

    if ending in _PREPOSITIONS | _COORDINATORS | _SUBORDINATORS | _DETERMINERS | _RELATIVES:
        if ending != "so" or previous == ["so"]:
            return " "
    if ending in _AUXILIARIES:
        if len(previous) > 1 and previous[0] != "there":
            return " "
    if (ending in _OBJECT_PRONOUNS and len(previous) > 1
            and _OBJECT_INFINITIVE.intersection(_lemmas(previous[-2]).get("VERB", ()))
            and (first == "to" or "VB" in _verb_forms(first))):
        return " "

    if dependent and complete:
        return " "
    if first in _COORDINATORS and complete:
        subject = next((word.split("'", 1)[0] for word in previous if _subject(word)), None)
        if (not _clause(following[1:]) or right[0].islower() or following[1:2] == ["maybe"]
                or subject is not None and following[1:2] == [subject]):
            return " "

    # A candidate object has no independent predicate. The verb immediately
    # before the break must actually be in a verb position in the left clause.
    if complete and _verb(ending) and following != ["the", "end"]:
        after_infinitive = (previous[-2] == "to"
                            or previous[-2] in _MODALS | {"gonna", "wanna"})
        if after_infinitive or "NOUN" not in _lemmas(ending):
            if first in _DETERMINERS and _noun_phrase(following):
                return " "
        if ellipsis and (_subject(previous[-2]) or ending in _NAME_CONTEXT):
            if not _verb(first) or right[0].isupper() and ending in _NAME_CONTEXT:
                return " "
    return " " if _subject_predicate(previous, following) else None


def _title_word(word):
    return len(word) > 1 and word.isalpha() and word.istitle()


def _governed_infinitive(word, before, governor):
    return (before == "to" and "VB" in _verb_forms(word)
            and _INFINITIVE_CONTEXT.intersection(_lemmas(governor).get("VERB", ())))


def _original_capitals(line):
    """Collect names before joins erase the original sentence boundaries."""
    matches = list(_WORD.finditer(line))
    words = [match[0].lower().replace("\u2019", "'") for match in matches]
    protected = set()
    for index, match in enumerate(matches):
        word = match[0]
        folded = words[index]
        if len(word) < 2 or not word[0].isupper() or not word[1:].islower():
            continue
        previous = matches[index - 1] if index else None
        between = line[previous.end() if previous else 0:match.start()]
        if previous and between.isspace() and _title_word(previous[0]) and _title_word(word):
            head = words[index - 1]
            infinitive = index >= 3 and _governed_infinitive(head, words[index - 2], words[index - 3])
            if head not in _FUNCTION_WORDS and not infinitive:
                protected.add(head)
                if folded not in _CLOSED_CLASS:
                    protected.add(folded)
        prefix = between.rstrip() or (previous[0] if previous else "")
        if not prefix or _HARD_BOUNDARY.fullmatch(prefix[-1]):
            continue
        before = words[index - 1] if index else ""
        following = words[index + 1] if index + 1 < len(words) else ""
        # Keep the established lowercase repair for ordinary cleanup words,
        # relative markers and predicative verbs with an object pronoun.
        if (folded.partition("'")[0] in _CLOSED_CLASS or folded.endswith("n't")
                or folded in _INLINE_CLEANUP_WORDS
                or before in _DETERMINERS and folded in _BUILTIN_COMMON
                or following in _OBJECT_PRONOUNS and "VBZ" in _verb_forms(folded)
                or _subject(before) and _unambiguous_predicate(folded)):
            continue
        protected.add(folded)
    return protected


def _lowercase(line, protected, marker):
    prior = ""
    previous = ""

    def lower(match):
        nonlocal prior, previous
        word = match[0]
        before, previous = previous, word.lower().replace("\u2019", "'")
        before_link, prior = prior, before
        if (previous in protected or previous == "i" or previous.startswith("i'")
                or not word[0].isupper() or (word != "A" and word.isupper())
                or any(char.isupper() for char in word[1:])):
            return word
        prefix = line[:match.start()].rstrip()
        if (not prefix or prefix[-1] in ".!?:;\u2026" or prefix.endswith(marker)
                or before in _NAME_CONTEXT):
            return word
        # Adjacent title-case words can be a name. A capitalized function
        # word still lowercases when it introduces a continuation.
        tail = re.match(r"[ \t]+([^\W\d_]+)", line[match.end():])
        if ((previous not in _FUNCTION_WORDS or previous == "will" and not _subject(before)) and tail
                and _title_word(tail[1]) and tail[1].lower() not in _FUNCTION_WORDS
                and not _governed_infinitive(previous, before, before_link)):
            return word
        if previous in {"will", "rose"} and before in _COORDINATORS and before_link in protected:
            return word
        if (previous.partition("'")[0] in _FUNCTION_WORDS or _PERSON_CONTRACTION.fullmatch(previous)
                or previous in _BUILTIN_COMMON or _lemmas(previous)):
            return word.lower()
        return word

    return _WORD.sub(lower, line)


def repair(text, vocabulary=()):
    """Mask local literals, then repair evidenced pauses without changing words."""
    protected = {word for entry in vocabulary or () for word in _words(entry)}
    protected.update(_CALENDAR)
    lines = re.split(r"(\r\n|\r|\n)", text or "")
    for line in lines[::2]:
        protected.update(_original_capitals(line))
    for index in range(0, len(lines), 2):
        line = lines[index]
        if _LIST.match(line):
            continue
        marker = "\0"
        while marker in line:
            marker += "\0"
        literals = []

        def mask(match):
            literals.append(match[0])
            return marker

        line = _LITERAL.sub(mask, line)
        line = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", line)
        while True:
            parts = _BOUNDARY.split(line)
            result = parts[0]
            for offset in range(1, len(parts), 2):
                separator, following = parts[offset:offset + 2]
                left = _HARD_BOUNDARY.split(result)[-1].strip()
                # Keep a right-hand question or exclamation even when its
                # words could be attached to the preceding sentence.
                right = re.split(r"(?<=[.!?:;\u2026])", following, maxsplit=1)[0].strip()
                join = (None if right.startswith(marker) or left.endswith(marker) and not _words(left)
                        else _join(left, right, ellipsis=bool(_ELLIPSIS.search(separator))))
                if join:
                    result = result.rstrip(" \t").removesuffix(",")
                    separator = join
                result += separator + following
            if result == line:
                break
            line = result
        line = _lowercase(line, protected, marker)
        hidden = iter(literals)
        lines[index] = re.sub(re.escape(marker), lambda _: next(hidden), line)
    return "".join(lines)
