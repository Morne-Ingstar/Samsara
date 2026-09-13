"""VERBATIM dictation profile -- what a programmer dictating into a terminal
or a URL bar actually wants.

Pure functions, no app state, no imports from dictation.py. dictation.py
decides WHEN to apply this (target process / address bar / spoken toggle);
this module only decides WHAT the text becomes.

The three live failures it fixes (2026-09-11):

    spoken "echo hello"                -> Whisper: "Echo, hello?"
    spoken "git log --oneline -5"      -> Whisper: "Git log dash dash oneline dash five."
    spoken "github dot com"            -> Whisper: "Github. com."

Each is a different defect -- auto-capitalisation/punctuation, unmapped
spoken symbols, and whitespace around a dot -- and one profile closes all
three:

    apply("Echo, hello?")                            -> "echo hello"
    apply("Git log dash dash oneline dash five.")    -> "git log --oneline -5"
    apply("Github. com.")                            -> "github.com"

EXTENDING IT: everything is a table at the top of this file. Add a row to
SYMBOLS (one spoken word) or PHRASE_SYMBOLS (several words) and it works --
no code change. See docs/VERBATIM_PROFILE.md.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Tables -- edit these, not the code below
# ---------------------------------------------------------------------------

#: Multi-word spoken symbols. Matched BEFORE single words, longest first, so
#: "dash dash" wins over two separate "dash"es and "at sign" over "at".
PHRASE_SYMBOLS: dict[str, str] = {
    "dash dash": "--",
    "double dash": "--",
    "at sign": "@",
    "open paren": "(",
    "close paren": ")",
    "open parenthesis": "(",
    "close parenthesis": ")",
    "open bracket": "[",
    "close bracket": "]",
    "open brace": "{",
    "close brace": "}",
    "open angle": "<",
    "close angle": ">",
    "single quote": "'",
    "double quote": '"',
    "new line": "\n",
}

#: Single spoken words -> literal character.
SYMBOLS: dict[str, str] = {
    "dash": "-",
    "minus": "-",
    "hyphen": "-",
    "dot": ".",
    "period": ".",
    "point": ".",
    "slash": "/",
    "backslash": "\\",
    "underscore": "_",
    "colon": ":",
    "semicolon": ";",
    "pipe": "|",
    "tilde": "~",
    "hash": "#",
    "pound": "#",
    "dollar": "$",
    "percent": "%",
    "caret": "^",
    "ampersand": "&",
    "asterisk": "*",
    "star": "*",
    "equals": "=",
    "plus": "+",
    "space": " ",
    "tab": "\t",
    "newline": "\n",
    "enter": "\n",
}

#: Spelled digits. Converted to a digit ONLY next to a symbol or another
#: digit -- so "dash five" is "-5" but "one line" stays words.
DIGITS: dict[str, str] = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

#: No space on EITHER side of these (the whitespace rule).
NO_SPACE_AROUND: frozenset[str] = frozenset({".", "/", "\\", "_", ":", "-", "@"})

#: Openers and sigils take no space after them ("$HOME", "#define", "~/bin");
#: closers take no space before them.
NO_SPACE_AFTER: frozenset[str] = frozenset({"(", "[", "{", "<", "$", "#", "~"})
NO_SPACE_BEFORE: frozenset[str] = frozenset({")", "]", "}", ">", ";"})

#: Whisper's own sentence punctuation, removed unless a spoken symbol
#: produced it. "." is handled separately -- it is legitimate mid-token
#: ("github.com") and only stripped when it is sentence-final.
STRIP_PUNCTUATION: frozenset[str] = frozenset({",", "?", "!", ";", ":", '"', "'"})

#: The word that starts a spelling run, and the one that capitalises the
#: next letter: "spell g i t" -> "git", "spell cap g i t" -> "Git".
SPELL_WORD = "spell"
CAP_WORD = "cap"

#: Processes that always get the verbatim profile. Matched on process name,
#: case-insensitively, with or without ".exe".
DEFAULT_PROCESSES: tuple[str, ...] = (
    "warp", "windowsterminal", "cmd", "powershell", "pwsh",
    "conhost", "alacritty", "wezterm", "code",
)

#: Address bar detection. A focused EDIT control matching any of these is a
#: URL bar. AutomationIds are the stable ones; names are the visible label
#: and vary by locale, so they are matched case-insensitively as substrings.
ADDRESS_BAR_CONTROL_TYPES: frozenset[str] = frozenset({"EditControl", "ComboBoxControl"})
ADDRESS_BAR_AUTOMATION_IDS: frozenset[str] = frozenset({
    "view_1000",      # Chromium omnibox (Chrome / Edge / Brave / Vivaldi)
    "urlbar-input",   # Firefox
})
ADDRESS_BAR_NAME_MARKERS: tuple[str, ...] = (
    "address and search bar",          # Chrome
    "search or enter web address",     # Edge / Chromium
    "search with google or enter address",   # Firefox
    "enter address",
)

#: Spoken toggle. Forces the profile on/off regardless of target, until
#: turned off or the hands-free session ends.
TOGGLE_ON: frozenset[str] = frozenset({"literal on", "verbatim on"})
TOGGLE_OFF: frozenset[str] = frozenset({"literal off", "verbatim off"})


def match_toggle(text: str) -> "bool | None":
    """True/False for an on/off toggle phrase, None when it is not one.

    Whole utterance only, case- and punctuation-insensitive: "literal on"
    switches, "use the literal on-ramp" is ordinary dictation.
    """
    normalized = " ".join(
        re.sub(r"[^\w\s]", " ", (text or "")).lower().split()
    )
    if normalized in TOGGLE_ON:
        return True
    if normalized in TOGGLE_OFF:
        return False
    return None


_WORD = "word"
_SYMBOL = "symbol"
#: A word the user spelled out ("spell cap g i t"). Joins like a word but is
#: exempt from the sentence-capital undo -- the casing was asked for.
_LITERAL = "literal"


# ---------------------------------------------------------------------------
# Predicates -- pure, so dictation.py's WHEN decision stays testable
# ---------------------------------------------------------------------------

def normalize_process(name: str) -> str:
    """'Warp.exe' -> 'warp'. Tolerates a full path."""
    stem = (name or "").strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
    return stem[:-4] if stem.endswith(".exe") else stem


def matches_process(name: str, processes=None) -> bool:
    """True when `name` is in the verbatim target list."""
    if not name:
        return False
    targets = DEFAULT_PROCESSES if processes is None else processes
    return normalize_process(name) in {normalize_process(p) for p in targets}


def is_address_bar(control_type: str = "", automation_id: str = "",
                   name: str = "") -> bool:
    """True when the focused UIA element looks like a browser URL bar.

    Pure on purpose: dictation.py does the COM query and passes the three
    properties in, so this rule is unit-testable with no browser.
    """
    if control_type and control_type not in ADDRESS_BAR_CONTROL_TYPES:
        return False
    if (automation_id or "").strip() in ADDRESS_BAR_AUTOMATION_IDS:
        return True
    lowered = (name or "").strip().lower()
    return any(marker in lowered for marker in ADDRESS_BAR_NAME_MARKERS) if lowered else False


# ---------------------------------------------------------------------------
# The transform
# ---------------------------------------------------------------------------

def _strip_edge_punctuation(token: str, *, keep_trailing_dot: bool = False) -> str:
    """Drop Whisper's own sentence punctuation from a token's edges.

    keep_trailing_dot: a "." on a NON-final token is a separator the user
    spoke ("github dot com" often comes back as "Github. com."), not
    auto-punctuation -- the caller promotes it to a symbol atom instead.
    Only the utterance-final "." is sentence punctuation and dropped.
    """
    while token and token[0] in STRIP_PUNCTUATION:
        token = token[1:]
    while token and token[0] == ".":
        token = token[1:]
    while token and token[-1] in STRIP_PUNCTUATION:
        token = token[:-1]
    if not keep_trailing_dot:
        while token and token[-1] == ".":
            token = token[:-1]
    return token


def _decapitalize_first(word: str) -> str:
    """Undo Whisper's sentence capital without touching real casing.

    Only a token that looks sentence-cased (first letter upper, rest lower)
    is lowered -- "Echo" -> "echo", while "GitHub", "URL" and "McCoy" are
    left exactly as spoken.
    """
    if len(word) >= 2 and word[0].isupper() and word[1:].islower():
        return word.lower()
    if len(word) == 1 and word.isupper():
        return word.lower()
    return word


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", (text or "").strip()) if t]


def _apply_spelling(tokens: list[str]) -> list[str]:
    """'spell g i t' -> 'git'; 'spell cap g i t' -> 'Git'.

    A spelling run consumes following single-letter tokens (and the CAP_WORD
    marker) until a token that is not one.
    """
    out: list[tuple[str, bool]] = []
    i = 0
    while i < len(tokens):
        if _strip_edge_punctuation(tokens[i]).lower() != SPELL_WORD:
            out.append((tokens[i], False))
            i += 1
            continue
        i += 1
        letters: list[str] = []
        capitalise_next = False
        while i < len(tokens):
            candidate = _strip_edge_punctuation(tokens[i])
            low = candidate.lower()
            if low == CAP_WORD:
                capitalise_next = True
                i += 1
                continue
            if len(candidate) == 1 and candidate.isalpha():
                letters.append(candidate.upper() if capitalise_next else candidate.lower())
                capitalise_next = False
                i += 1
                continue
            break
        if letters:
            out.append(("".join(letters), True))
    return out


def _to_atoms(spelled: list[tuple[str, bool]]) -> list[tuple[str, str]]:
    """Map tokens to (kind, value) atoms, resolving phrase and word symbols."""
    atoms: list[tuple[str, str]] = []
    tokens = [tok for tok, _ in spelled]
    i = 0
    max_phrase = max((len(p.split()) for p in PHRASE_SYMBOLS), default=1)
    last_index = len(tokens) - 1
    while i < len(tokens):
        matched = False
        for span in range(min(max_phrase, len(tokens) - i), 1, -1):
            phrase = " ".join(
                _strip_edge_punctuation(t).lower() for t in tokens[i:i + span]
            )
            if phrase in PHRASE_SYMBOLS:
                atoms.append((_SYMBOL, PHRASE_SYMBOLS[phrase]))
                i += span
                matched = True
                break
        if matched:
            continue

        raw = tokens[i]
        is_spelled = spelled[i][1]
        # A trailing "." on a non-final token is a separator the user spoke,
        # not sentence punctuation -- keep it and emit it as its own symbol.
        keep_dot = (i != last_index)
        core = _strip_edge_punctuation(raw, keep_trailing_dot=keep_dot)
        trailing_dot = False
        if keep_dot and core.endswith(".") and len(core) > 1:
            core = core[:-1]
            trailing_dot = True
        low = core.lower()
        if is_spelled:
            atoms.append((_LITERAL, core))
        elif low in SYMBOLS:
            atoms.append((_SYMBOL, SYMBOLS[low]))
        elif core:
            atoms.append((_WORD, core))
        if trailing_dot:
            atoms.append((_SYMBOL, "."))
        i += 1
    return atoms


def _apply_digits(atoms: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Spelled digit -> numeral, but only next to a symbol or another digit."""
    def _is_digit_atom(atom):
        return atom is not None and atom[0] == _WORD and atom[1].isdigit()

    out = list(atoms)
    for idx, (kind, value) in enumerate(out):
        if kind is not _WORD and kind != _WORD:
            continue
        if value.lower() not in DIGITS:
            continue
        prev = out[idx - 1] if idx > 0 else None
        nxt = out[idx + 1] if idx + 1 < len(out) else None
        adjacent = (
            (prev is not None and prev[0] == _SYMBOL)
            or (nxt is not None and nxt[0] == _SYMBOL)
            or _is_digit_atom(prev) or _is_digit_atom(nxt)
        )
        if adjacent:
            out[idx] = (_WORD, DIGITS[value.lower()])
    return out


def _join(atoms: list[tuple[str, str]]) -> str:
    """Join atoms applying the whitespace rule."""
    def _all_no_space(value: str) -> bool:
        """'--' counts the same as '-': every char is a no-space symbol."""
        return bool(value) and all(ch in NO_SPACE_AROUND for ch in value)

    def _is_dash_run(value: str) -> bool:
        return bool(value) and set(value) == {"-"}

    parts: list[str] = []
    for idx, (kind, value) in enumerate(atoms):
        if idx == 0:
            parts.append(value)
            continue
        prev_kind, prev_value = atoms[idx - 1]
        glue = " "
        if kind == _SYMBOL and _is_dash_run(value) and prev_kind != _SYMBOL:
            # A dash-run after a word starts a new flag: "oneline -5", not
            # "oneline-5". After another symbol it stays glued ("--" then
            # "-x"). TRADE-OFF: a spoken hyphenated word ("well dash known")
            # therefore comes out "well -known". This profile targets
            # terminals and URL bars, where flags vastly outnumber hyphenated
            # words; say "spell" or type the hyphen for the other case.
            glue = " "
        elif _all_no_space(value) or _all_no_space(prev_value):
            glue = ""
        elif prev_value in NO_SPACE_AFTER or value in NO_SPACE_BEFORE:
            glue = ""
        elif kind == _SYMBOL and value in ("\n", "\t", " "):
            glue = ""
        elif prev_kind == _SYMBOL and prev_value in ("\n", "\t", " "):
            glue = ""
        elif value.isdigit() and prev_value.isdigit():
            # "eight zero eight zero" -> "8080", not "8 0 8 0".
            glue = ""
        parts.append(glue + value)
    return "".join(parts)


def apply(text: str) -> str:
    """Run the verbatim profile over one finalized utterance.

    No capitalisation, no auto-punctuation, no trailing period, no smart
    quotes, no smart-corrections rewrites -- the caller skips those entirely.
    Idempotent enough to be safe if it ever runs twice: its output contains
    no spoken-symbol words to re-map.
    """
    tokens = _tokenize(text)
    if not tokens:
        return ""
    spelled = _apply_spelling(tokens)
    if not spelled:
        return ""
    atoms = _to_atoms(spelled)
    if not atoms:
        return ""
    if atoms[0][0] == _WORD:
        atoms[0] = (_WORD, _decapitalize_first(atoms[0][1]))
    atoms = _apply_digits(atoms)
    return _join(atoms)
