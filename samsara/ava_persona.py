"""Owner-readable Ava style. Fixed identity and safety rules live in the caller."""

import os
import re
import tempfile
import threading
from datetime import date
from pathlib import Path

from samsara.paths import samsara_home_dir


# All local voice forms are defined here, before the model sees a turn.
VOICE_PHRASES = {
    "change": ("change your personality:", "from now on you "),
    "read": ("what is your personality", "how are you set up to talk"),
    "undo": ("undo that personality change",),
    "open": ("open your personality file",),
}
PERSONA_PROMPT_BYTES = 1200
SEED = ("Be warm, calm, plain, and brief enough to be comfortable when spoken aloud. "
        "Write only words that should be spoken: no markdown, lists, or helpdesk language.")
_HEADINGS = ("## Who Ava is", "## Changes you asked for")
_lock = threading.RLock()


def path():
    return samsara_home_dir() / "ava_persona.md"


def _atomic_write(target, content):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                         dir=target.parent, prefix=".ava_persona_",
                                         suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def _render(who, changes):
    return f"{_HEADINGS[0]}\n{who}\n\n{_HEADINGS[1]}\n" + "".join(
        f"- {line}\n" for line in changes)


def _read():
    target = path()
    if not target.exists() or not target.read_text(encoding="utf-8").strip():
        _atomic_write(target, _render(SEED, []))
    content = target.read_text(encoding="utf-8")
    if not content.startswith(_HEADINGS[0] + "\n") or "\n" + _HEADINGS[1] + "\n" not in content:
        raise ValueError("Ava persona headings are missing; file left unchanged")
    who, changes = content.split("\n" + _HEADINGS[1] + "\n", 1)
    who = who[len(_HEADINGS[0]) + 1:].strip() or SEED
    lines = [line[2:].strip() for line in changes.splitlines() if line.startswith("- ")]
    return who, lines


def read():
    with _lock:
        return _read()


def append(change, day=None):
    change = " ".join(change.split()).strip(" .")
    if not change:
        return "", False
    with _lock:
        who, changes = _read()
        line = f"{day or date.today().isoformat()}: {change}"
        changes.append(line)
        _atomic_write(path(), _render(who, changes))
        _, dropped = prompt_style()
        return line, dropped


def undo():
    with _lock:
        who, changes = _read()
        if not changes:
            return ""
        removed = changes.pop()
        _atomic_write(path(), _render(who, changes))
        return removed


def prompt_style(cloud=False):
    """Return bounded owner style, preserving newest changes; report omissions."""
    with _lock:
        who, changes = _read()
    if cloud:
        from samsara.ava_memory import _SENSITIVE
        who = "\n".join(line for line in who.splitlines() if not _SENSITIVE.search(line)) or SEED
        changes = [line for line in changes if not _SENSITIVE.search(line)]
    heading = "Who Ava is (owner-authored style, not action instructions or permission):\n"
    suffix = "\nChanges you asked for (style only):\n"
    budget = PERSONA_PROMPT_BYTES
    base = heading + who
    if len(base.encode("utf-8")) > budget:
        # An owner-edited oversized Who section remains intact on disk.
        base = heading + SEED
        omitted = True
    else:
        omitted = False
    kept = []
    for line in reversed(changes):
        candidate = suffix + "".join(f"- {item}\n" for item in reversed([line] + kept))
        if len((base + candidate).encode("utf-8")) > budget:
            omitted = True
            continue
        kept.append(line)
    style = base + (suffix + "".join(f"- {item}\n" for item in reversed(kept)) if kept else "")
    return style, omitted


def voice_intent(utterance):
    phrase = " ".join(utterance.split()).strip().rstrip(".?!")
    lower = phrase.casefold()
    for prefix in VOICE_PHRASES["change"]:
        if lower.startswith(prefix):
            return "change", phrase[len(prefix):].strip()
    for action in ("read", "undo", "open"):
        if lower in VOICE_PHRASES[action]:
            return action, ""
    return None, ""


def asks_to_drop_guardrail(change):
    return bool(re.search(r"\b(?:pretend|act like|say|claim|ignore|drop|remove|forget)\b.*"
                          r"\b(?:human|person|miss me|need me|rules?|guardrails?|come back)\b",
                          change, re.I))
