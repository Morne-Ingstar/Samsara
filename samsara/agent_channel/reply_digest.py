"""Transient CLI reply memory and deadline helpers for explicit spoken digests."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from .claude_reply import spoken_form

MAX_REPLY_CHARS = 1_000_000
_lock = threading.RLock()
_replies = {}
_generations = dict.fromkeys(("gpt", "claude", "hermes"), 0)
_latest = None


def canonical(name):
    return "gpt" if name == "codex" else name


@dataclass(frozen=True)
class StoredReply:
    source: str
    reply: object
    generation: int


def generation(name):
    with _lock:
        return _generations[canonical(name)]


def clear_reply(name):
    global _latest
    name = canonical(name)
    with _lock:
        _generations[name] += 1
        _replies.pop(name, None)
        if _latest is not None and _latest.source == name:
            _latest = None


def remember_reply(name, reply, expected_generation):
    """Keep full replies for three known agents; never truncate or persist them."""
    global _latest
    name = canonical(name)
    with _lock:
        if _generations[name] != expected_generation:
            return
        if not reply.text.strip() or len(reply.text) > MAX_REPLY_CHARS:
            clear_reply(name)  # Do not digest an older reply in its place.
            return
        _latest = _replies[name] = StoredReply(name, reply, expected_generation)


def last_reply(name=None):
    with _lock:
        return _latest if name is None else _replies.get(canonical(name))


def current(stored):
    with _lock:
        return _latest is stored and _replies.get(stored.source) is stored


def sentences(text, count):
    """Use the existing speech sanitizer before taking complete sentence chunks."""
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", text)
    text, _ = spoken_form(text, max(1, len(text.split())))
    text = re.sub(r"[*_`~]", "", text)
    chunks = re.findall(r'''.+?(?:[.!?]+["'”’\)\]]*(?=\s|$)|$)''', text)
    return " ".join(chunk.strip() for chunk in chunks[:count]).strip()


def ask_before(agent, question, config, deadline, *, home=None, progress=None):
    """Include the CLI turn lock in a digest's deadline, without queuing a late turn."""
    from . import cli_agents as cli
    lock = cli._turn_locks[agent.name]
    if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise TimeoutError()
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cli._closing.is_set():
            raise TimeoutError()
        options = dict(config or {})
        options["timeout_s"] = remaining
        return agent._ask_unlocked(question, options, home=home, progress=progress, deadline=deadline)
    finally:
        lock.release()
