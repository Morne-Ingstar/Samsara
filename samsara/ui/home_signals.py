"""What Home and Voice help are allowed to say (queue 86).

ONE account of each capability's state, shared by Home's problem notice, the
Voice help page and the hint slot, so they cannot contradict each other
(queue 82, "the indicator, problem notice, diagnostic result and Ava label
must agree about what is known").

Pure functions, no Qt and no I/O beyond reading what the app already keeps in
memory (plus the correction queue file, which is small and already on disk).
Everything here is unit-testable against a plain namespace.

Vocabulary, deliberately distinct:
    READY        it works as far as we know
    PAUSED       the user turned it off for now -- NOT a fault
    OFF          not enabled in settings -- NOT a fault
    UNAVAILABLE  a known fault: the capability cannot do its job
    UNKNOWN      not checked, or the check is stale -- NOT health

Only UNAVAILABLE on an ENABLED capability produces a problem notice. Paused is
not failed; unknown is not healthy (it says so instead of claiming health).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from samsara import outcome_ring
from samsara.log import get_logger

logger = get_logger(__name__)

READY = "ready"
PAUSED = "paused"
OFF = "off"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"
WARMING = "warming"

DICTATION = "Dictation"
HANDS_FREE = "Hands-free"
AVA = "Ava"

#: Capability -> the consequence sentence a notice shows. The notice states
#: what the user loses, never "the app is broken" (queue 82): local dictation
#: working while cloud conversation is down is not a broken app.
CONSEQUENCES = {
    DICTATION: "Dictation cannot hear you.",
    HANDS_FREE: "The wake word cannot hear you.",
    AVA: "Voice questions will not get answers.",
}
#: Notice precedence: the capability whose loss costs the most comes first.
NOTICE_ORDER = (DICTATION, HANDS_FREE, AVA)

#: The one repair a notice can offer (queue 109). Home routes it to
#: home_qt.restart_hands_free; anything else keeps the default Voice help
#: route. A fault that cannot be repaired by a button never names one.
ACTION_RESTART_HANDS_FREE = "restart_hands_free"
RESTART_HANDS_FREE_LABEL = "Restart hands-free"

#: Ava's four presence states (queue 82). "awake" says only that Ava can
#: answer -- never that a microphone is open or that anything is being sent.
AVA_AWAKE = "awake"
AVA_DIM = "dim"
AVA_THINKING = "thinking"
AVA_ASLEEP = "asleep"
AVA_WARMING = "warming"
AVA_STATE_TEXT = {
    AVA_AWAKE: "Ava is ready to answer.",
    AVA_DIM: "Ava cannot answer right now.",
    AVA_THINKING: "Ava is working on your question.",
    AVA_ASLEEP: "Ava is off.",
    AVA_WARMING: "Ava is warming up.",
}
#: "Thinking" must resolve to an answer, a failure or a cancellation -- it can
#: never be an indefinite explanation for silence (queue 82). After this long
#: the app has lost track of the request and Ava stops claiming to be working.
AVA_THINKING_MAX_S = 60.0

# ---------------------------------------------------------------------------
# Hint tiers. A lower tier is only consulted when every higher one is empty.
# ---------------------------------------------------------------------------

TIER_DIAGNOSTIC = "diagnostic"
TIER_DISCOVERY = "discovery"
TIER_GENERIC = "generic"
TIER_ORDER = (TIER_DIAGNOSTIC, TIER_DISCOVERY, TIER_GENERIC)

#: A diagnostic must never fire on a single event -- a rule that nags after
#: one occurrence is noise (queue 86). Each threshold is stated with its rule.
CORRECTIONS_WAITING_MIN = 3        # entries in the review queue
NO_TEXT_WINDOW = 20                # most recent captures examined
NO_TEXT_MIN = 3                    # of them that produced no text
MISS_WINDOW = 8                    # most recent COMMAND outcomes examined
MISS_MIN = 3                       # of them that were "didn't catch"


@dataclass(frozen=True)
class CapabilityState:
    name: str
    status: str
    detail: str = ""               # why, in plain words
    evidence: str = ""             # what was observed, checkable
    #: The one thing that would fix THIS fault, when the app can offer one
    #: (queue 109). "" means the capability has no repair of its own and the
    #: notice keeps its default route into Voice help -- a missing microphone
    #: is not something a button can reconnect.
    action_label: str = ""
    action: str = ""               # ACTION_RESTART_HANDS_FREE, or ""

    @property
    def is_fault(self) -> bool:
        return self.status == UNAVAILABLE

    @property
    def consequence(self) -> str:
        return CONSEQUENCES.get(self.name, "")


@dataclass(frozen=True)
class Notice:
    capability: str
    headline: str                  # "Microphone unavailable."
    consequence: str               # "Dictation cannot hear you."
    evidence: str = ""
    action_label: str = ""         # "" -> the notice's default Voice help route
    action: str = ""

    @property
    def text(self) -> str:
        return f"{self.headline} {self.consequence}".strip()


@dataclass(frozen=True)
class Hint:
    id: str
    tier: str
    text: str
    action_label: str = ""         # the destination's own name
    action: str = ""               # "page" | "settings" | "cheatsheet" | "guides" | "voice_help"
    arg: Optional[str] = None
    evidence: str = ""             # diagnostics only: what it was derived from


# ---------------------------------------------------------------------------
# Reading the app -- every accessor tolerates a partially built app object
# ---------------------------------------------------------------------------

def _cfg(app) -> dict:
    cfg = getattr(app, "config", None)
    return cfg if isinstance(cfg, dict) else {}


def _cfg_get(cfg: dict, dotted: str, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def mic_available(app) -> Optional[bool]:
    """True/False, or None when the app has not enumerated microphones yet --
    which is UNKNOWN, not healthy."""
    mics = getattr(app, "available_mics", None)
    if mics is None:
        return None
    if not mics:
        return False
    chosen = _cfg(app).get("microphone")
    if chosen is None:
        return True
    return any(m.get("id") == chosen for m in mics if isinstance(m, dict))


def dictation_state(app) -> CapabilityState:
    if getattr(app, "snoozed", False):
        return CapabilityState(DICTATION, PAUSED, "You paused listening.",
                               "app.snoozed is set")
    mic = mic_available(app)
    if mic is None:
        return CapabilityState(DICTATION, UNKNOWN, "No microphone check has run yet.",
                               "the app has not listed microphones")
    if not mic:
        return CapabilityState(DICTATION, UNAVAILABLE, "Microphone unavailable.",
                               "the selected microphone is not in the device list")
    return CapabilityState(DICTATION, READY, "", "a microphone is selected and present")


def hands_free_fault(app):
    """The last fatal that stopped the wake listener, or None.

    ``DictationApp._hands_free_fault`` is an
    ``audio_engine.wake_consumer.HandsFreeFault``, read by duck typing so this
    module keeps its promise not to import the audio engine. It persists until
    a restart succeeds, which is the point: a user who was away from the
    machine when listening died must still be able to find out why.
    """
    fault = getattr(app, "_hands_free_fault", None)
    return fault if getattr(fault, "reason", None) else None


def wake_models_state(app) -> str:
    """'off' | 'loading' | 'ready' from the app's OWN readiness API
    (``DictationApp.wake_ready_state``, dictation.py). An app that does not
    have it -- a test double, a partially built app -- reports 'ready', which
    is what that API itself does for the same case: this function must not
    invent a loading state the app never claimed.
    """
    fn = getattr(app, "wake_ready_state", None)
    if not callable(fn):
        return "ready"
    try:
        return str(fn() or "ready")
    except Exception as exc:                            # never break Home
        logger.debug(f"[HOME] wake_ready_state unavailable: {exc}")
        return "ready"


def wake_consumer_running(app) -> Optional[bool]:
    """True/False from the wake consumer's own lifecycle flag, or None when
    there is no consumer to ask (no capture engine, or a test double).

    None is UNKNOWN, never health -- Home used to derive "ready" from config
    and a microphone, which stayed "ready" through a consumer that had died
    (queue 109, Astra F12).
    """
    consumer = getattr(app, "_wake_consumer", None)
    if consumer is None:
        return None
    running = getattr(consumer, "running", None)
    if running is None:
        running = getattr(consumer, "_running", None)
    return None if running is None else bool(running)


def hands_free_state(app) -> CapabilityState:
    """What hands-free is ACTUALLY doing, in this order:

    off -> stopped by a fault -> paused -> no microphone -> still starting ->
    not listening -> ready.

    The fault outranks everything but "switched off" because it is the thing
    that happened: a stopped listener that is also snoozed still stopped, and
    saying "you paused it" would blame the user for a crash. READY is reached
    only when the wake models are loaded, the consumer's poll thread is
    running AND wake detection is armed -- config alone can no longer claim it.
    """
    cfg = _cfg(app)
    if not cfg.get("wake_word_enabled", False):
        return CapabilityState(HANDS_FREE, OFF, "The wake word is switched off.",
                               "wake_word_enabled is false")
    fault = hands_free_fault(app)
    if fault is not None:
        detail = str(fault.reason)
        if getattr(fault, "retrying", False):
            detail = f"{detail} Trying again."
        return CapabilityState(
            HANDS_FREE, UNAVAILABLE, detail,
            getattr(fault, "evidence", "") or "the wake listener stopped with an error",
            action_label=RESTART_HANDS_FREE_LABEL, action=ACTION_RESTART_HANDS_FREE)
    if getattr(app, "snoozed", False):
        return CapabilityState(HANDS_FREE, PAUSED, "You paused listening.",
                               "app.snoozed is set")
    mic = mic_available(app)
    if mic is None:
        return CapabilityState(HANDS_FREE, UNKNOWN, "No microphone check has run yet.",
                               "the app has not listed microphones")
    if not mic:
        return CapabilityState(HANDS_FREE, UNAVAILABLE, "Microphone unavailable.",
                               "the selected microphone is not in the device list")

    models = wake_models_state(app)
    running = wake_consumer_running(app)
    armed = bool(getattr(app, "wake_word_active", False))
    starting = bool(getattr(app, "_wake_start_pending", False))
    if models == "loading" or starting:
        return CapabilityState(HANDS_FREE, UNKNOWN, "The wake word is still starting up.",
                               f"the wake models report {models!r} and listening is pending")
    if models == "off":
        # Requested in settings, never asked for at runtime: hands-free is
        # not up, and this is not evidence of a fault either.
        return CapabilityState(HANDS_FREE, UNKNOWN, "The wake word has not started yet.",
                               "the wake models have not been requested")
    if running is None:
        return CapabilityState(HANDS_FREE, UNKNOWN, "The wake listener has not been checked.",
                               "there is no wake consumer to ask")
    if not running or not armed:
        # Enabled, loaded, a microphone is present -- and nothing is
        # listening. This is the state Home used to call "ready".
        return CapabilityState(
            HANDS_FREE, UNAVAILABLE, "Hands-free is not listening.",
            f"the wake consumer is {'running' if running else 'stopped'} and "
            f"wake detection is {'armed' if armed else 'not armed'}",
            action_label=RESTART_HANDS_FREE_LABEL, action=ACTION_RESTART_HANDS_FREE)
    return CapabilityState(HANDS_FREE, READY, "",
                           "the wake models are loaded and the wake listener is running")


def ava_state(app) -> CapabilityState:
    cfg = _cfg(app)
    enabled = bool(_cfg_get(cfg, "ollama.enabled")) or bool(_cfg_get(cfg, "cloud_llm.enabled"))
    if not enabled:
        return CapabilityState(AVA, OFF, "Ava is switched off.",
                               "no Ava provider is enabled")
    try:
        from samsara import ava_readiness            # noqa: PLC0415
        readiness = ava_readiness.readiness_for(app)
    except Exception as exc:                          # never break Home
        logger.debug(f"[HOME] Ava readiness unavailable: {exc}")
        return CapabilityState(AVA, UNKNOWN, "Ava has not been checked yet.",
                               "the readiness check could not run")
    state = getattr(readiness, "state", None)
    if state == getattr(ava_readiness, "READY", "ready"):
        return CapabilityState(AVA, READY, "", "the provider answered its readiness check")
    if state == getattr(ava_readiness, "WARMING", "warming"):
        return CapabilityState(AVA, WARMING, readiness.sentence(),
                               "a background warm-up completion is in progress")
    if state == getattr(ava_readiness, "OFFLINE", "offline"):
        try:
            detail = readiness.sentence()
        except Exception:
            detail = "Ava is offline."
        return CapabilityState(AVA, UNAVAILABLE, detail, "the provider failed its readiness check")
    return CapabilityState(AVA, UNKNOWN, "Ava has not been checked yet.",
                           "no readiness result yet")


def capability_states(app) -> dict:
    """The one account every surface reads."""
    return {state.name: state for state in
            (dictation_state(app), hands_free_state(app), ava_state(app))}


def problem_notice(app) -> Optional[Notice]:
    """The Home notice, or None. Only a KNOWN fault on an ENABLED capability
    qualifies: paused is not failed, off is not failed, and unknown makes no
    claim either way."""
    states = capability_states(app)
    for name in NOTICE_ORDER:
        state = states.get(name)
        if state is not None and state.is_fault:
            return Notice(capability=name, headline=state.detail,
                          consequence=state.consequence, evidence=state.evidence,
                          action_label=state.action_label, action=state.action)
    return None


# ---------------------------------------------------------------------------
# Ava presence
# ---------------------------------------------------------------------------

def ava_presence(app, now: Optional[float] = None) -> tuple:
    """(state, sentence). `thinking` is read from a TIMESTAMP the app sets
    when it sends Ava a question; a stale one is ignored, so the avatar can
    never sit at "thinking" as an indefinite explanation for silence."""
    now = time.monotonic() if now is None else now
    started = getattr(app, "ava_thinking_since", None)
    state = ava_state(app)
    if isinstance(started, (int, float)) and 0 <= now - started <= AVA_THINKING_MAX_S:
        return AVA_THINKING, AVA_STATE_TEXT[AVA_THINKING]
    if state.status == OFF:
        return AVA_ASLEEP, AVA_STATE_TEXT[AVA_ASLEEP]
    if state.status == READY:
        return AVA_AWAKE, AVA_STATE_TEXT[AVA_AWAKE]
    if state.status == WARMING:
        return AVA_WARMING, state.detail or AVA_STATE_TEXT[AVA_WARMING]
    # UNAVAILABLE or UNKNOWN: dim, and the reason is always spelled out --
    # a dimmed avatar on its own cannot say why Ava is unavailable.
    return AVA_DIM, state.detail or AVA_STATE_TEXT[AVA_DIM]


# ---------------------------------------------------------------------------
# Tier 1: diagnostics, from this user's own recorded behaviour
# ---------------------------------------------------------------------------

def corrections_waiting(app) -> int:
    """Entries the user has captured but not reviewed. The one number queue
    82 endorsed for Home, because it leads to work worth doing."""
    try:
        from samsara.correction_queue import CorrectionQueue   # noqa: PLC0415
        return len(CorrectionQueue().pending)
    except Exception as exc:
        logger.debug(f"[HOME] correction queue unreadable: {exc}")
        return 0


def _diag_records(app, limit: int) -> list:
    try:
        from samsara import diagnostics                        # noqa: PLC0415
        return list(diagnostics.recent(limit))
    except Exception as exc:
        logger.debug(f"[HOME] diagnostics unavailable: {exc}")
        return []


def no_text_captures(app) -> tuple:
    """(how many of the recent captures produced no text, how many examined).
    outcome "empty"/"gated" is exactly what the recorder writes when a
    capture reached the model and produced nothing usable."""
    records = _diag_records(app, NO_TEXT_WINDOW)
    empty = sum(1 for r in records if getattr(r, "outcome", "ok") in ("empty", "gated"))
    return empty, len(records)


def recent_misses(app) -> tuple:
    """(misses, COMMAND outcomes examined) from the app's own outcome ring.

    Both halves come from ``samsara.outcome_ring``, the schema the writer
    (``DictationApp._show_outcome_chip``) uses -- the whole of Astra F11 was
    that this reader invented its own. The denominator is command outcomes
    only, so "3 of your last 4 commands were not recognised" counts four
    commands and not four assorted chips, three of which were dictations.
    """
    ring = getattr(app, "_outcome_ring", None)
    if not ring:
        return 0, 0
    sample = outcome_ring.command_sample(ring, MISS_WINDOW)
    misses = sum(1 for rec in sample if outcome_ring.is_command_miss(rec))
    return misses, len(sample)


def diagnostic_hints(app) -> list:
    """Hints derived from what this user's app actually recorded. Each cites
    its evidence, and none fires on a single occurrence."""
    hints = []

    waiting = corrections_waiting(app)
    if waiting >= CORRECTIONS_WAITING_MIN:
        word = "correction" if waiting == 1 else "corrections"
        hints.append(Hint(
            id="diag.corrections_waiting", tier=TIER_DIAGNOSTIC,
            text=f"{waiting} {word} are waiting for you to review.",
            action_label="Open Dictionary", action="page", arg="Dictionary",
            evidence=f"{waiting} pending entries in the correction queue "
                     f"(threshold {CORRECTIONS_WAITING_MIN})"))

    empty, examined = no_text_captures(app)
    if empty >= NO_TEXT_MIN and examined:
        hints.append(Hint(
            id="diag.no_text", tier=TIER_DIAGNOSTIC,
            text=f"{empty} of your last {examined} recordings produced no text.",
            action_label="Open Voice help", action="page", arg="Voice help",
            evidence=f"{empty}/{examined} recent captures recorded outcome empty or gated "
                     f"(threshold {NO_TEXT_MIN})"))

    misses, seen = recent_misses(app)
    if misses >= MISS_MIN and seen:
        hints.append(Hint(
            id="diag.misses", tier=TIER_DIAGNOSTIC,
            text=f"{misses} of your last {seen} commands were not recognised.",
            action_label="View window commands", action="cheatsheet", arg="",
            evidence=f"{misses}/{seen} recent outcomes were misses (threshold {MISS_MIN})"))

    return hints


# ---------------------------------------------------------------------------
# Tier 2: discovery, from what is actually not set up
# ---------------------------------------------------------------------------

def discovery_hints(app) -> list:
    """One entry per capability that is genuinely unexplored.

    "Not configured" is not the same as "not wanted" (queue 82): a capability
    the user switched OFF deliberately is not offered, and a capability whose
    prerequisite is missing is not offered as if it were independent.
    """
    cfg = _cfg(app)
    hints = []

    wake_enabled = cfg.get("wake_word_enabled", False)
    # "Configured once and then switched off" is a decision, not an
    # unexplored feature: the app records that in wake_word_configured.
    wake_seen = bool(cfg.get("wake_word_configured", False))
    if not wake_enabled and not wake_seen:
        hints.append(Hint(
            id="discover.wake_word", tier=TIER_DISCOVERY,
            text="Start listening without reaching for a key.",
            action_label="Set up wake word", action="settings", arg="Modes"))

    # Hands-free depends on the wake word: never promoted as if independent.
    if wake_enabled and cfg.get("command_mode", {}).get("mode") != "toggle":
        hints.append(Hint(
            id="discover.hands_free", tier=TIER_DISCOVERY,
            text="Keep talking after the wake word, without holding anything down.",
            action_label="Configure hands-free", action="settings", arg="Modes"))

    ava_enabled = bool(_cfg_get(cfg, "ollama.enabled")) or bool(_cfg_get(cfg, "cloud_llm.enabled"))
    if not ava_enabled and not cfg.get("ava_declined", False):
        hints.append(Hint(
            id="discover.ava", tier=TIER_DISCOVERY,
            text="Ask a question out loud and hear the answer.",
            action_label="Configure Ava", action="settings", arg="Ava / Cloud"))

    if not _user_words_exist():
        hints.append(Hint(
            id="discover.dictionary", tier=TIER_DISCOVERY,
            text="Teach it the names and words it keeps getting wrong.",
            action_label="Open Dictionary", action="page", arg="Dictionary"))

    return hints


def _user_words_exist() -> bool:
    try:
        from samsara.ui.home_qt import _user_word_count         # noqa: PLC0415
        return bool(_user_word_count())
    except Exception:
        return True      # unknown: do not nag


# ---------------------------------------------------------------------------
# Tier 3: generic tips, reusing queue 75's catalog generator
# ---------------------------------------------------------------------------

def generic_hints(app) -> list:
    """Filler, shown only when neither tier above has anything to say. The
    text comes from queue 75's generator, so there is one source of catalog
    tips rather than two."""
    try:
        from samsara.streaming import idle_hints_from_catalog   # noqa: PLC0415
        from samsara.ui.home_qt import catalog_records          # noqa: PLC0415
        lines = idle_hints_from_catalog(catalog_records(app) or [])
    except Exception as exc:
        logger.debug(f"[HOME] catalog tips unavailable: {exc}")
        return []
    # The destination is the command list itself, unfiltered: a tip about a
    # dictation phrase must not send the user to the window commands.
    return [Hint(id=f"tip.{i}", tier=TIER_GENERIC, text=line,
                 action_label="View the command list", action="cheatsheet", arg="")
            for i, line in enumerate(lines)]


# ---------------------------------------------------------------------------
# Choosing the one hint Home shows
# ---------------------------------------------------------------------------

def eligible_hints(app, dismissed=()) -> list:
    """Every hint the user has not dismissed, highest tier first. A lower
    tier is never mixed in while a higher one has something to say."""
    dismissed = set(dismissed or ())
    for producer in (diagnostic_hints, discovery_hints, generic_hints):
        try:
            tier = [h for h in producer(app) if h.id not in dismissed]
        except Exception as exc:
            logger.debug(f"[HOME] hint tier {producer.__name__} failed: {exc}")
            tier = []
        if tier:
            return tier
    return []


def choose_hint(app, dismissed=(), index: int = 0) -> Optional[Hint]:
    """The hint to show, or None -- and None means show nothing at all. An
    empty slot is honest; a manufactured suggestion is not."""
    hints = eligible_hints(app, dismissed)
    if not hints:
        return None
    return hints[index % len(hints)]
