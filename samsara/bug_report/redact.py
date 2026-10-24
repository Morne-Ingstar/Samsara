"""Fail-closed allow-list of diagnostic line shapes, never general log scrubbing.

Unknown lines (including continuations, masked prose and exception values) are
discarded. Only bounded numeric fields and literal program vocabulary survive.
"""
import re

_ENVELOPE = re.compile(r"^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d{3} - (?:DEBUG|INFO|WARNING|ERROR|CRITICAL) - )?(.*)$")
_EXCEPTIONS = r"(?:RuntimeError|ValueError|TypeError|OSError|AttributeError|KeyError|ImportError|TimeoutError)"
_COUNT = r"\d{1,8} chars, \d{1,8} words"
_SHAPES = tuple(re.compile(p) for p in (
    r"\[INIT\] Enumerating audio devices\.\.\.",
    r"\[EXIT\] Goodbye!",
    r"\[TTS\] feedback level=(?:off|minimal|normal|verbose|all|essential)",
    rf'\[CMD\] No command matched: "{_COUNT}"',
    rf'\[CMD\] "{_COUNT}" is a command in the disabled pack \'(?:core|hermes|ava|browser|editing)\'',
    rf"{_EXCEPTIONS}: \d{{1,8}} chars(?: errno=(?:None|\d{{1,6}}) winerror=(?:None|\d{{1,6}}))?",
))
_FIXED_ERRORS = frozenset((
    "Recording failed", "Mic unavailable.", "Microphone unavailable.",
    "Hermes is switched off. Enable it in Settings > Commands.",
    "Hermes couldn't start its background turn.", "Ask Hermes what?",
    "refused: focus lock", "refused: nothing staged", "refused: unclear",
    "refused: say it again", "nothing to correct", "nothing to take back",
    "sent, unconfirmed", "word not found",
))


def error_first_line(text):
    first = str(text or "").splitlines()
    first = first[0] if first else ""
    if first in _FIXED_ERRORS or _SHAPES[-1].fullmatch(first):
        return first
    return "Details omitted for privacy (unrecognized text)."


def redact_lines(lines, limit=50):
    """Consider the last 50 input lines; omit every unknown line in full."""
    result = []
    for line in list(lines)[-limit:]:
        line = str(line).rstrip("\r\n")
        if len(line) > 2000:
            continue
        envelope = _ENVELOPE.fullmatch(line)
        if envelope is None:
            continue
        message = envelope.group(1)
        if any(shape.fullmatch(message) for shape in _SHAPES):
            result.append(message)
    return tuple(result)


def redact_fault(lines):
    """Keep fault type and package-relative code frames, with no user paths.

    Source/extension paths outside the shipped package are omitted entirely.
    User profile components preceding samsara/ are replaced by <user>.
    """
    result = []
    for line in list(lines)[-100:]:
        text = str(line).strip()
        if text in ("Windows fatal exception: access violation", "Fatal Python error: Aborted",
                    "Windows fatal exception: code 0xc0000409"):
            result.append(text)
            continue
        match = re.fullmatch(r'File "[^"\r\n]*[\\/]samsara[\\/]([a-z_]+(?:[\\/][a-z_]+)*\.py)", line (\d{1,6}) in ([a-z_][a-z_0-9]{0,63})', text)
        if match:
            # Validate against shipped code; no arbitrary filenames or functions.
            from pathlib import Path
            import ast
            relative, number, function = match.groups()
            path = Path(__file__).resolve().parents[1] / relative.replace("\\", "/")
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                functions = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                if function in functions:
                    result.append(f'File "<user>/samsara/{relative.replace(chr(92), chr(47))}", line {number} in {function}')
            except (OSError, SyntaxError):
                pass
    return tuple(result)


def activity_text(text):
    """Only the user's optional description enters here; never read the clipboard.

    Mask credentials, addresses and paths even when pasted into this field.
    Automatic diagnostic text always uses the stricter allow-list above.
    """
    text = str(text).replace("\n", " ").replace("\r", " ")[:2000]
    text = re.sub(r"(?i)\b(?:bearer\s+\S+|(?:token|api[_ -]?key|password|secret)\s*[:=]\s*\S+|sk-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+)", "<redacted>", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", text)
    return re.sub(r"(?:[A-Za-z]:[\\/]|~/|/home/|/Users/)[^\s]*", "<path>", text)
