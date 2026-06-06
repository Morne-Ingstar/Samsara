"""Case-formatting transforms for prefix-word dictation.

When dictated text starts with a recognized format keyword, the keyword is
stripped and the remaining words are transformed before typing.

Examples:
  "camel my variable name"  -> myVariableName
  "pascal my class name"    -> MyClassName
  "snake my variable name"  -> my_variable_name
  "constant max retries"    -> MAX_RETRIES
  "kebab my class name"     -> my-class-name
  "dotted my path name"     -> my.path.name
  "title the great gatsby"  -> The Great Gatsby
  "say hello world"         -> hello world

The formatter is first-token-only: a keyword in the middle of a dictation
phrase does not trigger transformation.

Enable via config key: enable_case_formatters: true  (default: false)
"""

from typing import Optional


def _words(text: str) -> list:
    """Split text into lowercase words, stripping leading/trailing punctuation."""
    result = []
    for w in text.split():
        cleaned = w.strip(".,!?;:\"'()[]{}").lower()
        if cleaned:
            result.append(cleaned)
    return result


def _fmt_camel(words: list) -> str:
    if not words:
        return ""
    return words[0] + "".join(w.capitalize() for w in words[1:])


def _fmt_pascal(words: list) -> str:
    return "".join(w.capitalize() for w in words)


def _fmt_snake(words: list) -> str:
    return "_".join(words)


def _fmt_constant(words: list) -> str:
    return "_".join(w.upper() for w in words)


def _fmt_kebab(words: list) -> str:
    return "-".join(words)


def _fmt_dotted(words: list) -> str:
    return ".".join(words)


def _fmt_title(words: list) -> str:
    return " ".join(w.capitalize() for w in words)


def _fmt_say(words: list) -> str:
    return " ".join(words)


# Table-driven: keyword -> transform function.
# Adding a new formatter is one line here.
FORMATTERS = {
    "camel":    _fmt_camel,
    "pascal":   _fmt_pascal,
    "snake":    _fmt_snake,
    "constant": _fmt_constant,
    "kebab":    _fmt_kebab,
    "dotted":   _fmt_dotted,
    "title":    _fmt_title,
    "say":      _fmt_say,
}


def apply_case_formatter(text: str) -> Optional[str]:
    """Apply a case formatter if the first word is a known format keyword.

    Returns the transformed string on a match, or None if no keyword matched
    (caller should use the original text unchanged).

    The keyword is matched case-insensitively and strips trailing punctuation
    that the transcription engine may attach (e.g. "Camel," matches "camel").
    """
    if not text:
        return None

    # Split into keyword + remainder on the first whitespace only
    parts = text.split(None, 1)
    keyword = parts[0].lower().rstrip(".,!?;:")

    if keyword not in FORMATTERS:
        return None

    # Nothing after the keyword -- user said the keyword alone, pass through
    if len(parts) < 2 or not parts[1].strip():
        return None

    words = _words(parts[1])
    if not words:
        return None

    return FORMATTERS[keyword](words)
