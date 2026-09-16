"""Shared language definitions for Whisper transcription and TTS voice matching.

LANGUAGES is the single source of truth for the "language" config key, used
by both the Settings General tab and the Voice Training language selector
(one key, not two -- see samsara/ui/settings_qt.py and
samsara/ui/voice_training_qt.py). "auto" is not a real Whisper language code;
it means "pass language=None to faster-whisper and let it auto-detect" --
see resolve_transcribe_language().
"""

import re
import math
import threading
import unicodedata
from collections import deque

# Measured 2026-09-10: floor((0.9833984375 + 0.82470703125) / 2 * 100) / 100.
# See perf_artifacts/hf_lang_confidence.{md,json}.
LANGUAGE_CONFIDENCE_FLOOR = 0.90

#: Queue 119. The floor above used to be reachable ONLY when the detected
#: language was NOT one we expected. That made it useless against the failure
#: it was best placed to catch: English TV audio, detected as `en`, which is
#: always in `expected`, so the probability was never consulted at all.
#:
#: Measured on the hands-free corpus + 59 of the owner's own wake captures
#: (perf_artifacts/hf_bench_119_after.md, reports/119):
#:     media_only (TV, 30 s)     p = 0.8403 / 0.8413 / 0.8862   -> 362-516 chars
#:     owner speech, corpus      p = 0.9819 .. 0.9946           ->  18-72  chars
#:     owner commands >= 2 s     p = 0.9648 .. 0.9961           ->   5-39  chars
#:     owner commands <  2 s     p = 0.7979 .. 0.9902
#: The signal separates cleanly (owner min 0.9819 vs media max 0.8862) for
#: anything long enough to measure -- but NOT for very short audio, where
#: Whisper's language_probability is unreliable. Both sub-floor owner clips
#: were 1.2 s, and one of those was a Whisper hallucination ("Thank you for
#: watching." over near-silence) that this gate SHOULD reject.
#:
#: So the floor is applied to same-language audio only when the utterance is
#: big enough for the probability to mean something. Duration is the honest
#: measure; when the caller does not supply it we fall back to transcript
#: length, which is what actually distinguishes the failure: a LONG transcript
#: from audio the model is not confident about is confabulation over
#: background speech, which is exactly what the TV clips produce.

#: Seconds of audio below which language_probability is not trusted for a
#: same-language rejection. 2.0: every owner clip under the floor was 1.2 s,
#: and every owner clip at or above 2 s scored >= 0.9648 (45 samples).
LANGUAGE_CONFIDENCE_MIN_DURATION_S = 2.0

#: Transcript-length fallback when duration is unavailable. 150 characters
#: sits between the longest owner utterance measured (72) and the shortest
#: media confabulation (362) -- a 2x margin on the side that matters, since
#: over-rejecting the owner is worse than under-rejecting the TV.
LANGUAGE_CONFIDENCE_MIN_CHARS = 150

# Unicode script blocks, filtered to letters below (punctuation/digits/emoji
# and inherited combining accents are neutral). Block reference:
# https://www.unicode.org/Public/17.0.0/ucd/Scripts.txt
SCRIPT_RANGES = {
    "latin": ((0x0041, 0x024F), (0x0250, 0x02AF), (0x1D00, 0x1DBF),
              (0x1E00, 0x1EFF), (0x2C60, 0x2C7F), (0xA720, 0xA7FF),
              (0xAB30, 0xAB6F), (0xFB00, 0xFB06), (0xFF21, 0xFF5A)),
    "cyrillic": ((0x0400, 0x052F), (0x1C80, 0x1C8F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F)),
    "arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x0870, 0x08FF),
               (0xFB50, 0xFDFF), (0xFE70, 0xFEFF), (0x1EE00, 0x1EEFF)),
    "han": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x323AF)),
    "kana": ((0x3040, 0x30FF), (0x31F0, 0x31FF), (0xFF66, 0xFF9F), (0x1B000, 0x1B16F)),
    "bopomofo": ((0x3100, 0x312F), (0x31A0, 0x31BF)),
    "hangul": ((0x1100, 0x11FF), (0x3130, 0x318F), (0xA960, 0xA97F),
               (0xAC00, 0xD7FF), (0xFFA0, 0xFFDC)),
    "greek": ((0x0370, 0x03FF), (0x1F00, 0x1FFF)),
    "hebrew": ((0x0590, 0x05FF), (0xFB1D, 0xFB4F)),
    "devanagari": ((0x0900, 0x097F), (0xA8E0, 0xA8FF)),
    "bengali": ((0x0980, 0x09FF),), "gurmukhi": ((0x0A00, 0x0A7F),),
    "gujarati": ((0x0A80, 0x0AFF),), "tamil": ((0x0B80, 0x0BFF),),
    "telugu": ((0x0C00, 0x0C7F),), "kannada": ((0x0C80, 0x0CFF),),
    "malayalam": ((0x0D00, 0x0D7F),), "sinhala": ((0x0D80, 0x0DFF),),
    "thai": ((0x0E00, 0x0E7F),), "lao": ((0x0E80, 0x0EFF),),
    "tibetan": ((0x0F00, 0x0FFF),),
    "myanmar": ((0x1000, 0x109F), (0xA9E0, 0xA9FF), (0xAA60, 0xAA7F)),
    "georgian": ((0x10A0, 0x10FF), (0x1C90, 0x1CBF), (0x2D00, 0x2D2F)),
    "armenian": ((0x0530, 0x058F), (0xFB13, 0xFB17)),
    "ethiopic": ((0x1200, 0x139F), (0x2D80, 0x2DDF), (0xAB00, 0xAB2F)),
    "khmer": ((0x1780, 0x17FF),), "mongolian": ((0x1800, 0x18AF),),
}

# Other supported Whisper languages use Latin. Include contemporary alternate
# writing systems where a language is commonly written in more than one script.
_LANGUAGE_SCRIPT_GROUPS = {
    "ru uk bg mk be tg tt ba": {"cyrillic"},
    "sr bs": {"cyrillic", "latin"},
    "kk az uz": {"cyrillic", "latin", "arabic"},
    "mn": {"cyrillic", "mongolian"},
    "ar ur fa ps sd": {"arabic"}, "ha": {"latin", "arabic"},
    "zh yue": {"han", "bopomofo"}, "ja": {"han", "kana"},
    "ko": {"hangul", "han"}, "el": {"greek"}, "he yi": {"hebrew"},
    "hi mr ne sa": {"devanagari"}, "bn as": {"bengali"},
    "pa": {"gurmukhi", "arabic"}, "gu": {"gujarati"}, "ta": {"tamil"},
    "te": {"telugu"}, "kn": {"kannada"}, "ml": {"malayalam"},
    "si": {"sinhala"}, "th": {"thai"}, "lo": {"lao"}, "bo": {"tibetan"},
    "my": {"myanmar"}, "ka": {"georgian"}, "hy": {"armenian"},
    "am": {"ethiopic"}, "km": {"khmer"},
}
LANGUAGE_SCRIPTS = {lang: scripts for codes, scripts in _LANGUAGE_SCRIPT_GROUPS.items()
                    for lang in codes.split()}


def script_mismatch_ratio(text, expected_languages):
    """Share of letters outside expected scripts; accents/emoji stay neutral."""
    scripts = set().union(*(LANGUAGE_SCRIPTS.get(lang, {"latin"})
                            for lang in expected_languages))
    ranges = [span for script in scripts for span in SCRIPT_RANGES[script]]
    # NFKD lets full-width/styled Latin and decomposed accents behave like
    # ordinary letters. Counting letters avoids spaces diluting a wrong script.
    letters = [char for char in unicodedata.normalize("NFKD", text) if char.isalpha()]
    if not letters:
        return 0.0
    outside = sum(not any(lo <= ord(char) <= hi for lo, hi in ranges) for char in letters)
    return outside / len(letters)


class LanguageConfidenceGate:
    """Per-app rolling expectations, never serialized or written to config."""
    def __init__(self):
        self.accepted = deque(maxlen=20)
        self.lock = threading.Lock()

    def evaluate(self, text, language, probability, configured_language, floor,
                 *, remember=True, duration_s=None):
        try:
            floor = float(floor)
            if not math.isfinite(floor) or not 0 <= floor <= 1:
                raise ValueError("invalid floor")
        except (TypeError, ValueError):
            floor = LANGUAGE_CONFIDENCE_FLOOR
        with self.lock:
            expected = {configured_language} if configured_language else {"en", *self.accepted}
            if not text.strip():
                return None, expected
            # Queue 119. Two ways to fail the floor:
            #   - the language is not one we expect (the original rule), or
            #   - it IS expected, but this is a substantial utterance the
            #     model is unsure about -- the TV-audio signature.
            # The second is duration-gated (or length-gated when the caller
            # cannot supply duration) so a 1.2 s "done." is never judged on a
            # probability that short audio cannot support.
            if probability is not None and probability < floor:
                if duration_s is None:
                    substantial = len(text.strip()) >= LANGUAGE_CONFIDENCE_MIN_CHARS
                else:
                    try:
                        substantial = float(duration_s) >= LANGUAGE_CONFIDENCE_MIN_DURATION_S
                    except (TypeError, ValueError):
                        substantial = len(text.strip()) >= LANGUAGE_CONFIDENCE_MIN_CHARS
                if language not in expected or substantial:
                    return "low_confidence", expected
            if script_mismatch_ratio(text, expected) > 0.30:
                return "script_mismatch", expected
            if remember and language:
                self.accepted.append(language)
            return None, expected

# (display name, ISO 639-1 code). Display names are native/endonym names in
# the form "Native (code)", e.g. "Deutsch (de)", except English and Auto.
# Full list of the ~99 languages faster-whisper/openai-whisper support.
LANGUAGES = [
    ("Auto",                    "auto"),
    ("English (en)",            "en"),
    ("中文 (zh)",                "zh"),
    ("Deutsch (de)",            "de"),
    ("Español (es)",            "es"),
    ("Русский (ru)",            "ru"),
    ("한국어 (ko)",               "ko"),
    ("Français (fr)",           "fr"),
    ("日本語 (ja)",               "ja"),
    ("Português (pt)",          "pt"),
    ("Türkçe (tr)",             "tr"),
    ("Polski (pl)",             "pl"),
    ("Català (ca)",             "ca"),
    ("Nederlands (nl)",         "nl"),
    ("العربية (ar)",             "ar"),
    ("Svenska (sv)",            "sv"),
    ("Italiano (it)",           "it"),
    ("Bahasa Indonesia (id)",   "id"),
    ("हिन्दी (hi)",              "hi"),
    ("Suomi (fi)",              "fi"),
    ("Tiếng Việt (vi)",         "vi"),
    ("עברית (he)",              "he"),
    ("Українська (uk)",         "uk"),
    ("Ελληνικά (el)",           "el"),
    ("Bahasa Melayu (ms)",      "ms"),
    ("Čeština (cs)",            "cs"),
    ("Română (ro)",             "ro"),
    ("Dansk (da)",              "da"),
    ("Magyar (hu)",             "hu"),
    ("தமிழ் (ta)",               "ta"),
    ("Norsk (no)",              "no"),
    ("ไทย (th)",                "th"),
    ("اردو (ur)",                "ur"),
    ("Hrvatski (hr)",           "hr"),
    ("Български (bg)",          "bg"),
    ("Lietuvių (lt)",           "lt"),
    ("Latina (la)",             "la"),
    ("Māori (mi)",              "mi"),
    ("മലയാളം (ml)",             "ml"),
    ("Cymraeg (cy)",            "cy"),
    ("Slovenčina (sk)",         "sk"),
    ("తెలుగు (te)",              "te"),
    ("فارسی (fa)",               "fa"),
    ("Latviešu (lv)",           "lv"),
    ("বাংলা (bn)",               "bn"),
    ("Српски (sr)",             "sr"),
    ("Azərbaycan (az)",         "az"),
    ("Slovenščina (sl)",        "sl"),
    ("ಕನ್ನಡ (kn)",              "kn"),
    ("Eesti (et)",              "et"),
    ("Македонски (mk)",         "mk"),
    ("Brezhoneg (br)",          "br"),
    ("Euskara (eu)",            "eu"),
    ("Íslenska (is)",           "is"),
    ("Հայերեն (hy)",            "hy"),
    ("नेपाली (ne)",              "ne"),
    ("Монгол (mn)",             "mn"),
    ("Bosanski (bs)",           "bs"),
    ("Қазақша (kk)",            "kk"),
    ("Shqip (sq)",              "sq"),
    ("Kiswahili (sw)",          "sw"),
    ("Galego (gl)",             "gl"),
    ("मराठी (mr)",               "mr"),
    ("ਪੰਜਾਬੀ (pa)",             "pa"),
    ("සිංහල (si)",              "si"),
    ("ខ្មែរ (km)",               "km"),
    ("chiShona (sn)",           "sn"),
    ("Yorùbá (yo)",             "yo"),
    ("Soomaali (so)",           "so"),
    ("Afrikaans (af)",         "af"),
    ("Occitan (oc)",           "oc"),
    ("ქართული (ka)",            "ka"),
    ("Беларуская (be)",         "be"),
    ("Тоҷикӣ (tg)",             "tg"),
    ("سنڌي (sd)",                "sd"),
    ("ગુજરાતી (gu)",             "gu"),
    ("አማርኛ (am)",               "am"),
    ("ייִדיש (yi)",              "yi"),
    ("ລາວ (lo)",                "lo"),
    ("Oʻzbekcha (uz)",         "uz"),
    ("Føroyskt (fo)",           "fo"),
    ("Kreyòl ayisyen (ht)",    "ht"),
    ("پښتو (ps)",                "ps"),
    ("Türkmençe (tk)",          "tk"),
    ("Nynorsk (nn)",            "nn"),
    ("Malti (mt)",              "mt"),
    ("संस्कृतम् (sa)",            "sa"),
    ("Lëtzebuergesch (lb)",    "lb"),
    ("မြန်မာ (my)",              "my"),
    ("བོད་སྐད་ (bo)",            "bo"),
    ("Tagalog (tl)",            "tl"),
    ("Malagasy (mg)",           "mg"),
    ("অসমীয়া (as)",             "as"),
    ("Татарча (tt)",            "tt"),
    ("ʻŌlelo Hawaiʻi (haw)",   "haw"),
    ("Lingála (ln)",            "ln"),
    ("Hausa (ha)",              "ha"),
    ("Башҡортса (ba)",          "ba"),
    ("Basa Jawa (jw)",          "jw"),
    ("Basa Sunda (su)",         "su"),
    ("粵語 (yue)",               "yue"),
]

# ISO codes considered to need multilingual (non-.en) models -- i.e. every
# entry except English and Auto. Used by the .en-model compatibility guard.
NON_ENGLISH_CODES = {code for _name, code in LANGUAGES if code not in ("en", "auto")}


def resolve_transcribe_language(app) -> "str | None":
    """Single source of truth for the Whisper `language` transcribe kwarg.

    "auto" means let faster-whisper auto-detect (language=None); any other
    configured value is passed through as-is (ISO 639-1 code). Falls back to
    "en" if the config key is entirely absent, matching the historical
    default.
    """
    lang = getattr(app, "config", {}).get("language", "en")
    return None if lang == "auto" else lang


def describe_diagnostics_language(configured: "str | None", detected: "str | None" = None) -> str:
    """Human-readable language value for a DiagRecord: `configured` (the
    code actually passed for THIS transcribe call, e.g. "en" for a
    command-mode utterance forced to English regardless of the general
    dictation language setting) or "auto->{detected}" when auto-detect ran
    and faster-whisper's `info` object exposed a detected language."""
    configured = configured or "en"
    if configured == "auto":
        return f"auto->{detected}" if detected else "auto"
    return configured


def is_english_only_model(model_name: str) -> bool:
    """True for the .en-suffixed faster-whisper model sizes (tiny.en,
    base.en, small.en, medium.en) -- these cannot transcribe non-English
    audio at all."""
    return bool(model_name) and model_name.endswith(".en")


# ---------------------------------------------------------------------------
# Script detection -- CJK/Thai/Hangul/etc have no whitespace word boundaries,
# unlike Latin-script languages. Shared by the voice-training corrections
# engine (word-boundary regex anchors don't apply) and Smart Corrections'
# translation guardrail (script-ratio check).
# ---------------------------------------------------------------------------

_BOUNDARYLESS_SCRIPT_RANGES = (
    (0x3040, 0x30FF),    # Hiragana + Katakana
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0xAC00, 0xD7A3),    # Hangul Syllables
    (0x0E00, 0x0E7F),    # Thai
)


def is_boundaryless_script_char(ch: str) -> bool:
    """True if `ch` belongs to a script with no whitespace word boundaries
    (CJK, Hangul, Thai, ...) -- regex \\b anchors are meaningless for text in
    these scripts since adjacent characters are also \\w with no separator."""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _BOUNDARYLESS_SCRIPT_RANGES)


def contains_boundaryless_script(s: str) -> bool:
    """True if any character in `s` belongs to a boundaryless script."""
    return any(is_boundaryless_script_char(ch) for ch in s)


def is_predominantly_boundaryless_script(s: str) -> bool:
    """True if the majority of LETTER characters in `s` belong to a
    boundaryless script (CJK/Thai/etc). Non-letter characters (digits,
    punctuation, whitespace) don't count toward the ratio. Returns False for
    text with no letters at all (nothing to judge)."""
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return False
    boundaryless = sum(1 for ch in letters if is_boundaryless_script_char(ch))
    return boundaryless / len(letters) > 0.5


_LATIN_SCRIPT_RANGES = (
    (0x0041, 0x005A),    # Basic Latin A-Z
    (0x0061, 0x007A),    # Basic Latin a-z
    (0x00C0, 0x024F),    # Latin-1 Supplement + Latin Extended A/B
    (0x1E00, 0x1EFF),    # Latin Extended Additional
)


def is_latin_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _LATIN_SCRIPT_RANGES)


def script_class(s: str) -> "str | None":
    """Crude script classification for translation-guardrail purposes:
    'latin' or 'non_latin' based on which is the majority among LETTER
    characters in `s`, or None if `s` has no letters at all (nothing to
    judge -- digits/punctuation/whitespace only)."""
    letters = [ch for ch in s if ch.isalpha()]
    if not letters:
        return None
    latin = sum(1 for ch in letters if is_latin_char(ch))
    return 'latin' if latin / len(letters) > 0.5 else 'non_latin'


# ---------------------------------------------------------------------------
# Same-script translation guard (Smart Corrections tribunal Fix 4) --
# script_class()/is_latin_char() above only catch a SCRIPT flip (e.g.
# Japanese -> English); they're blind to es/fr/de/pt/it/nl -> English,
# since all of those are Latin script too. This is a second, narrower
# heuristic for exactly that case: bounded to the handful of Latin-script
# languages where a same-script mistranslation is realistic, using each
# language's most common function words (articles/conjunctions/
# prepositions/pronouns) rather than any model call or new dependency.
#
# Each set deliberately avoids words that are also common English function
# words (cognates like Dutch "is" or Italian "in") -- a word that reads as
# both "still <language>" and "now English" can't discriminate a real
# translation from a legitimate correction, so it would either miss real
# translations (false negative) or flag legitimate same-language corrections
# (false positive). See looks_translated_to_english() for how these are used.
# ---------------------------------------------------------------------------

SAME_SCRIPT_FUNCTION_WORDS = {
    "es": {"el", "la", "los", "las", "de", "que", "y", "en", "un", "una",
           "es", "por", "con", "no", "se"},
    "fr": {"le", "la", "les", "de", "que", "et", "en", "un", "une", "est",
           "pour", "avec", "ne", "se", "du"},
    "de": {"der", "die", "das", "und", "ist", "ich", "nicht", "zu", "den",
           "mit", "ein", "eine", "auf", "für", "sich"},
    "pt": {"o", "os", "as", "de", "que", "e", "em", "um", "uma", "é",
           "para", "com", "não", "se", "isso"},
    "it": {"il", "la", "gli", "le", "di", "che", "e", "sono", "un", "una",
           "è", "per", "con", "non", "si"},
    "nl": {"de", "het", "een", "en", "van", "niet", "te", "dat", "met",
           "op", "voor", "zijn", "ik", "je", "wij"},
}

# Top ~15 English function words -- chosen to have zero overlap with any
# set above (see the false-negative/false-positive note).
ENGLISH_FUNCTION_WORDS = {
    "the", "and", "is", "to", "of", "a", "in", "that", "it", "for",
    "on", "with", "at", "this", "are",
}

_WORD_TOKEN_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _function_word_tokens(s: str) -> set:
    return set(_WORD_TOKEN_RE.sub(' ', s.lower()).split())


def looks_translated_to_english(lang_code: "str | None", original: str, text: str) -> bool:
    """Same-script translation guard: True if `text` looks like `original`
    got translated from `lang_code` into English instead of corrected.

    Pure function, no I/O. Bounded and conservative -- only evaluates
    codes in SAME_SCRIPT_FUNCTION_WORDS (skip entirely for en/auto/
    non-Latin/unlisted codes, since the heuristic isn't meaningful there).
    Fires only when `original` has >=2 hits from `lang_code`'s function-word
    set, `text` has ZERO of them, AND `text` picks up >=2 English function
    words it didn't already have (gained, not merely present -- a source
    sentence that already borrowed an English word or two must not itself
    count as evidence of translation).
    """
    word_set = SAME_SCRIPT_FUNCTION_WORDS.get(lang_code or "")
    if not word_set:
        return False

    orig_tokens = _function_word_tokens(original)
    if len(orig_tokens & word_set) < 2:
        return False

    text_tokens = _function_word_tokens(text)
    if text_tokens & word_set:
        return False

    gained_english = (text_tokens & ENGLISH_FUNCTION_WORDS) - (orig_tokens & ENGLISH_FUNCTION_WORDS)
    return len(gained_english) >= 2


DEFAULT_TTS_VOICES = {
    "en": "en-US-AvaNeural",
    "es": "es-MX-DaliaNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",
    "nl": "nl-NL-ColetteNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "ar": "ar-SA-ZariyahNeural",
    "hi": "hi-IN-SwaraNeural",
    "tr": "tr-TR-EmelNeural",
    "pl": "pl-PL-ZofiaNeural",
    "sv": "sv-SE-SofieNeural",
}
