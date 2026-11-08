"""Keep diagnostic structure, mask private values before preview or transport."""
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
    error = re.match(r"^([A-Za-z_][A-Za-z_0-9]{0,60}(?:Error|Exception)):", first)
    if error:
        return error.group(1) + ": <private error message redacted>"
    return "Details omitted for privacy (unrecognized text)."


_METRIC = re.compile(r"\b([a-zA-Z_][a-zA-Z_0-9]{0,40})\s*[=:]\s*(-?\d{1,9}(?:\.\d{1,6})?)(?:\s*(ms|s|Hz|dB|bytes|chars|words))?(?=\s|[,;)\]}]|$)")
_METRIC_KEYS = frozenset("elapsed_ms duration_ms audio_s transcribe_ms total_ms latency_ms rms peak frames samples channels rate sample_rate errno winerror count chars words segments n_segments t_total_ms t_transcribe_ms t_smart_ms t_corrections_ms avg_logprob compression_ratio no_speech_prob temperature total thread pid".split())
_ERROR_TYPE = re.compile(r"\b(?:[A-Za-z_][A-Za-z_0-9]{0,60}(?:Error|Exception)|SystemExit|KeyboardInterrupt)\b")
_TAG = re.compile(r"\[([A-Z][A-Z0-9_-]{0,40})\]")


def _structure(message):
    # Raw prose, including unknown continuations, is never assumed safe just
    # because it has a familiar tag. Retain structure and measurements instead.
    frames = redact_fault([message])
    if frames:
        return frames[0]
    if message.strip() == "Traceback (most recent call last):":
        return message.strip()
    if any(shape.fullmatch(message) for shape in _SHAPES):
        return message
    from .code_index import structured_message
    structured = structured_message(message)
    if structured is not None:
        return structured
    # Private fields can contain numbers/error names, so remove them first.
    message = re.sub(r"(?i)\b(?:heard|transcript|text|dictation|clipboard|title|window_title|device_name|microphone|prompt|answer|token|password|api_key|secret)\s*[:=].*", "<private value>", message)
    message = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", message)
    message = re.sub(r"(?:[A-Za-z]:[\\/]|~/|/home/|/Users/|\\\\)[^\r\n]*", "<path>", message)
    message = re.sub(r"(['\"]).*?\1", "<text>", message)
    tags = _TAG.findall(message)
    errors = list(dict.fromkeys(_ERROR_TYPE.findall(message)))
    metrics = [f"{key}={number}{unit or ''}" for key, number, unit in _METRIC.findall(message)
               if key in _METRIC_KEYS]
    timings = re.findall(r"(?<![\w.])-?\d{1,8}(?:\.\d{1,6})?\s*(?:ms|Hz|dB)\b", message)
    return " ".join([*(f"[{tag}]" for tag in tags), *errors, *metrics, *timings,
                     "<private text redacted>"])


def redact_lines(lines, limit=200):
    """Keep the last 200 entries, including their levels, events and timings."""
    result = []
    for line in list(lines)[-limit:]:
        line = str(line).rstrip("\r\n")
        if len(line) > 4000 or "\n" in line:
            result.append("<oversize or multiline text redacted>")
            continue
        envelope = _ENVELOPE.fullmatch(line)
        if envelope is None:
            continue
        message = envelope.group(1)
        prefix = line[:len(line) - len(message)] if message else ""
        result.append(prefix + _structure(message))
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
        match = re.fullmatch(r'File "(?:[^"\r\n]*[\\/])?(samsara|plugins)[\\/]([a-z_0-9]+(?:[\\/][a-z_0-9]+)*\.py)", line (\d{1,6}),? in ([a-z_][a-z_0-9]{0,63}|<module>)', text)
        if match:
            from .code_index import index
            package, relative, number, function = match.groups()
            relative = package + "/" + relative.replace("\\", "/")
            if function in index().get("frames", {}).get(relative, ()):
                result.append(f'File "<user>/{relative}", line {number} in {function}')
    return tuple(result)


def activity_text(text):
    """Only the user's optional description enters here; never read the clipboard.

    Mask credentials, addresses and paths even when pasted into this field.
    Automatic diagnostics use structural scrubbing above.
    """
    text = str(text).replace("\n", " ").replace("\r", " ")[:2000]
    text = re.sub(r"(?i)\b(?:bearer\s+\S+|(?:token|api[_ -]?key|password|secret)\s*[:=]\s*\S+|sk-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+)", "<redacted>", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", text)
    return re.sub(r"(?:[A-Za-z]:[\\/]|~/|/home/|/Users/)[^\s]*", "<path>", text)
