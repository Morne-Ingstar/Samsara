"""Shared language definitions for Whisper transcription and TTS voice matching.

LANGUAGES is the single source of truth for the "language" config key, used
by both the Settings General tab and the Voice Training language selector
(one key, not two -- see samsara/ui/settings_qt.py and
samsara/ui/voice_training_qt.py). "auto" is not a real Whisper language code;
it means "pass language=None to faster-whisper and let it auto-detect" --
see resolve_transcribe_language().
"""

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
