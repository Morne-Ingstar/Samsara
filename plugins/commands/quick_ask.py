"""Voice command to send a Quick Ask prompt to ARC.

IPC design: Samsara writes a single JSON payload to ~/.arc_inbox.json
atomically (tmp file + os.replace). ARC polls that path once per second
and dispatches. No sockets, no Flask, no ports.

Phrasings handled by one dispatcher:
    "ask why is my dict broken"          -> Claude (default)
    "ask claude why is my dict broken"   -> Claude (explicit)
    "ask gemini what's the best regex"   -> Gemini
    "ask gpt explain decorators"         -> GPT
    "ask chat gpt what is a closure"     -> GPT (two-word alias)
"""

import json
import os
from pathlib import Path

from samsara.plugin_commands import command

INBOX_PATH = Path.home() / ".arc_inbox.json"
INBOX_TMP = Path.home() / ".arc_inbox.tmp"

# Model aliases -- maps spoken names to what ARC's call_any expects.
MODEL_MAP = {
    "claude": "claude",
    "gpt": "gpt",
    "chat gpt": "gpt",
    "chatgpt": "gpt",
    "gemini": "gemini",
}


def _clean(text):
    """Strip Whisper punctuation from remainder."""
    return text.strip().strip(".,!?;:'\"")


def _send_to_arc(model, question):
    """Write a Quick Ask payload to the shared inbox file.

    Uses atomic write (tmp + os.replace) to prevent ARC from
    reading a half-written file.
    """
    if not question:
        print("[QUICK ASK] Ask what?")
        return False

    payload = {
        "type": "quick_ask",
        "model": model,
        "question": question.strip(),
    }

    try:
        with open(INBOX_TMP, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(str(INBOX_TMP), str(INBOX_PATH))
        print(f"[QUICK ASK] Sent to {model}: {question}")
        return True
    except Exception as e:
        print(f"[QUICK ASK] Failed: {e}")
        return False


@command("ask")
def ask_default(app, remainder):
    """Ask with default model (Claude). 'ask why is my dict broken'."""
    if not remainder:
        print("[QUICK ASK] Ask what?")
        return False

    clean = _clean(remainder)
    if not clean:
        print("[QUICK ASK] Ask what?")
        return False

    parts = clean.split()
    first_word = parts[0].lower()
    two_words = ' '.join(parts[:2]).lower() if len(parts) >= 2 else ""

    if two_words in MODEL_MAP:
        model = MODEL_MAP[two_words]
        question = clean[len(two_words):].strip()
        return _send_to_arc(model, question)
    if first_word in MODEL_MAP:
        model = MODEL_MAP[first_word]
        question = clean[len(first_word):].strip()
        return _send_to_arc(model, question)

    # No model specified -- default to Claude
    return _send_to_arc("claude", clean)
