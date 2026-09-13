"""Ava Command Session -- D3: Left-Alt latched, COMMAND-FIRST voice session.

Ava Front Door spec v2 (post-tribunal, 2026-07-23), Door D3. Replaces
samsara/ai_command_mode.py (deleted in the same pass -- see
AVA_FRONT_DOOR_SPEC_v2.md's "Migration" section). This is a POLICY over
the one shared Ava backend (plugins.commands.ask_ollama's
handle_ask_ava/ACTION2 machinery), not a second, separate resolver:
"One BACKEND, multiple INTERACTION POLICIES" (spec "Frame"). D3 preserves
the deterministic command-UX contract Left-Alt represented -- it does not
become freeform chat: a genuine LLM-fallback miss (stage (c) below
returns a conversational, non-command reply) is spoken as a MISS, not
spoken as Ava's answer.

WATERFALL RESOLVER (spec-mandated, replaces v1 "retrieval"):
  (a) exact/alias fixed-phrase match against the FULL command registry --
      the same CommandExecutor.process_text() every other voice-command
      path in this app already uses (hands-free command mode, wake-word
      command dispatch). Builtin + plugin, remainder-tolerant (so
      registered prefix commands like "focus <x>"/"open <x>"/"close <x>"
      already resolve here), ~0ms, no truncation. Execution goes through
      the ONE choke point (samsara.execution_policy, route=exact): read/ui
      and write commands run immediately for a phrase the user spoke
      exactly; destructive ones stage a confirmation on every route.
  (b) ACTION2 grammar (focus/open/close), matched DETERMINISTICALLY here
      (no model call) via a small verb+argument regex over synonyms
      ("launch"/"quit"/"bring up"/leading "please"/"can you" fillers)
      that stage (a)'s registered prefix commands don't already cover.
      "close" (destructive in execution_policy._ACTION2_RISK) stages via the
      SAME ask_ollama._pending_action confirmation binding a model-derived
      ACTION2 close would use -- the argument still needs live
      window/app-index resolution, which carries real ambiguity risk
      unlike stage (a)'s zero-ambiguity literal phrase match.
  (c) ONLY on a miss from both (a) and (b): one LLM fallback pass, reusing
      ask_ollama.ask_ollama() for the model call (same backend, same
      memory) -- the D3-specific differences are (i) the system prompt's
      {COMMAND_LIST} is replaced with a small fuzzy SHORTLIST from the
      full registry (dependency-free string scorer; rapidfuzz was not
      available in this environment, see _fuzzy_score's docstring)
      instead of the generic first-100 alphabetical list, and (ii) a
      CLOSED-WORLD gate (_closed_world_selection_ok, added 2026-07-23
      after a G1 replay rerun found stage (c) fabricating novel commands
      from nonsense input): the model's response is only ever handed to
      ask_ollama.handle_response() -- which is what actually reaches
      execute_command()/do_open()/do_focus()/do_close() -- if it is
      EITHER (1) an ACTION whose command name matches a shortlist entry
      VERBATIM, or (2) an ACTION2 whose verb is a real ACTION2 verb.
      Anything else (conversation, schedule, or a fabricated/altered
      command name) is rejected in code and treated as a MISS -- spoken
      miss feedback, never spoken as free-form chat, and never executed.

This does NOT generalize the fixed-phrase matcher (separately
tribunal-gated): (a) and (b) stay deterministic; (c) is a bounded
fallback, not a new grammar.

Miss handling (PRESERVED from ai_command_mode, per spec's behavior
inventory): 1st consecutive miss speaks a notice; subsequent misses chime
only; miss_limit reached auto-exits with a spoken notice. Any hit resets
the counter. Session-generation staleness guard (also PRESERVED, 2026-07-19
incident mechanism) rechecked after every async gap.

Config (ava_command_session.* -- see dictation.py's one-time migration
from ai_command_mode.* for the exact key mapping and what was dropped):
  enabled, key (default "left_alt"), backend ("ollama"|"cloud"), model,
  queue_depth_cap, miss_limit, inactivity_timeout_s (spec-new, default 60),
  shortlist_size (default 12 -- replaces the old flat menu_limit, which
  no longer applies now that stage (c) sends a per-utterance shortlist,
  not the whole menu), keep_warm, ready_cue_enabled, ready_cue_dir.
"""
from __future__ import annotations

import difflib
import queue
import re
import threading
import time
from typing import Any, Optional

from samsara.log import get_logger
from samsara.runtime import thread_registry
from samsara import execution_policy
from samsara.execution_policy import Route

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "key": "left_alt",
    "backend": "ollama",
    "model": "llama3.2:3b",
    "queue_depth_cap": 3,
    "miss_limit": 3,
    "inactivity_timeout_s": 60,
    "shortlist_size": 12,
    "keep_warm": True,
    "ready_cue_enabled": True,
    "ready_cue_dir": "assets/sounds/ava_cues",
}

# ---------------------------------------------------------------------------
# Module-level state -- same shape as ai_command_mode.py's worker/queue
# (PRESERVED architecture: single daemon worker serializes utterances so
# concurrent Ollama calls never overlap; only the per-utterance RESOLUTION
# logic is new).
# ---------------------------------------------------------------------------

_task_queue: queue.Queue = queue.Queue()
_cancel = threading.Event()
_worker_lock = threading.Lock()
_worker_started: bool = False


def _cfg(app) -> dict:
    return {**_DEFAULTS, **app.config.get("ava_command_session", {})}


# ---------------------------------------------------------------------------
# Stage (b): deterministic ACTION2 grammar (verb + argument extraction).
# No model call. Covers phrasings stage (a)'s registered prefix commands
# ("focus <x>"/"open <x>"/"close <x>", and aliases like "switch to <x>")
# do NOT already cover: synonym verbs and leading filler words.
# ---------------------------------------------------------------------------

_ACTION2_VERB_SYNONYMS: dict[str, str] = {
    "focus": "focus", "focus on": "focus", "bring up": "focus", "go to": "focus",
    "open": "open", "launch": "open", "start": "open", "run": "open",
    "close": "close", "quit": "close", "exit": "close",
}
# Longest-first so "focus on" is tried before bare "focus" in the alternation
# (regex alternation is first-match; a shorter overlapping prefix earlier in
# the list would otherwise steal "on <x>" as part of the argument).
_ACTION2_VERB_ALTERNATION = "|".join(
    re.escape(v) for v in sorted(_ACTION2_VERB_SYNONYMS, key=len, reverse=True)
)
_ACTION2_PATTERN = re.compile(
    rf"^(?:please\s+|can you\s+|could you\s+|would you\s+)?"
    rf"({_ACTION2_VERB_ALTERNATION})\s+(.+)$",
    re.IGNORECASE,
)


def _match_action2_grammar(utterance: str) -> Optional[tuple[str, str]]:
    """Returns (verb, argument) with verb in ("focus","open","close"), or
    None. Deterministic, no model call -- see module docstring stage (b)."""
    text = (utterance or "").strip()
    if not text:
        return None
    m = _ACTION2_PATTERN.match(text)
    if not m:
        return None
    verb_raw = m.group(1).lower()
    argument = m.group(2).strip().rstrip(".!?")
    if not argument:
        return None
    verb = _ACTION2_VERB_SYNONYMS.get(verb_raw)
    if verb is None:
        return None
    return verb, argument


def _dispatch_action2(app, verb: str, argument: str, generation: int, cfg: dict) -> None:
    """Execute (or stage for confirmation) a deterministically-matched
    ACTION2 verb through ask_ollama._execute_action2 -- the ONE ACTION2
    choke point (samsara.execution_policy). "close" is destructive there and
    stages via the same pending slot a model-derived close would use, so
    "yes"/"ava cancel" resolve it identically. The captured generation rides
    along: a resolution that lands after an exit is Denied(stale)."""
    from plugins.commands import ask_ollama  # noqa: PLC0415

    ask_ollama._execute_action2(app, verb, argument, route=Route.GRAMMAR, generation=generation,
                                source_text=argument,
                                speak_fn=lambda text: _speak(app, text, generation))


# ---------------------------------------------------------------------------
# Stage (c): fuzzy shortlist over the full registry (dependency-free).
# ---------------------------------------------------------------------------

def _fuzzy_score(query: str, candidate: str) -> float:
    """Dependency-free string similarity -- rapidfuzz is not installed in
    this environment (checked at build time), so this blends a
    normalized-token-overlap score with difflib.SequenceMatcher's ratio
    (stdlib, no new dependency). Not a rapidfuzz-quality scorer, but
    sufficient to rank a shortlist of a few hundred short phrases."""
    q = query.lower().strip()
    c = candidate.lower().strip()
    if not q or not c:
        return 0.0
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    overlap = len(q_tokens & c_tokens) / max(len(q_tokens), len(c_tokens), 1)
    seq_ratio = difflib.SequenceMatcher(None, q, c).ratio()
    return 0.5 * overlap + 0.5 * seq_ratio


def _all_ai_visible_phrases(app) -> list[tuple[str, str]]:
    """(phrase_or_alias, canonical_name) pairs for every AI-visible
    registered command -- builtin + plugin, canonical phrase + every
    alias. Corrects a known CommandMatcher.load_builtins() bug (built-in
    ai_visible is not threaded through, so matcher.list_commands() always
    reports True for builtins) by overriding from the raw commands.json
    dict for source=='builtin' entries, same workaround
    ai_command_mode.py's own _build_menu() used to need."""
    command_executor = getattr(app, "command_executor", None)
    matcher = getattr(command_executor, "_matcher", None)
    if matcher is None:
        return []
    raw_builtins = getattr(command_executor, "commands", {}) or {}
    out: list[tuple[str, str]] = []
    for entry in matcher.list_commands():
        ai_visible = entry.get("ai_visible", True)
        if entry.get("source") == "builtin":
            ai_visible = raw_builtins.get(entry["phrase"], {}).get("ai_visible", True)
        if not ai_visible:
            continue
        phrase = entry["phrase"]
        out.append((phrase, phrase))
        for alias in entry.get("aliases") or ():
            out.append((alias, phrase))
    return out


def _build_shortlist(app, utterance: str, cfg: dict) -> list[str]:
    """Top-N canonical command names (deduplicated, best-scoring alias or
    canonical phrase wins) for the stage (c) system prompt's
    {COMMAND_LIST} substitution."""
    n = int(cfg.get("shortlist_size", _DEFAULTS["shortlist_size"]))
    phrases = _all_ai_visible_phrases(app)
    scored = sorted(phrases, key=lambda p: -_fuzzy_score(utterance, p[0]))
    seen: set[str] = set()
    out: list[str] = []
    for _phrase_str, canonical in scored:
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
        if len(out) >= n:
            break
    return out


# Reinforces the CLOSED-WORLD constraint enforced in CODE below (see
# _closed_world_selection_ok) -- this text is defense-in-depth only, never
# the actual gate: models don't reliably follow instructions, so the code
# check is what stops a fabricated command from executing, not this
# paragraph. Added 2026-07-23 after a G1 replay rerun found stage (c)
# synthesizing novel commands from nonsense input (e.g. "purple thinking
# clouds today" -> a fabricated "set wallpaper to..." ACTION, "asdkfj
# random gibberish text" -> a fabricated "insert..." ACTION) -- zero
# misses on the 5-utterance nonsense corpus.
_CLOSED_WORLD_REMINDER = """

D3 COMMAND-SESSION CONSTRAINT (this adds one rule on top of everything above):
For ACTION, the command name must be copied EXACTLY, character-for-character,
from the comma-separated list above -- never invent, combine, extend, or
paraphrase a command name, and never append extra words to it. If nothing in
that list matches, and this is not a focus/open/close of a specific app or
window (ACTION2), respond conversationally instead -- never guess or make
something up just because the user said something."""


def _normalize_command_name(name: "str | None") -> str:
    """Mirrors ask_ollama.handle_response's own 'action' normalization
    (duplicated rather than imported -- this file's edits stay isolated to
    the D3-specific stage-(c) gate, see module docstring) so the shortlist
    comparison below lines up with what would actually reach
    execute_command() if this response were allowed through."""
    text = (name or "").strip().strip('"\'')
    text = text.rstrip('.!?,;: ').strip()
    text = re.sub(r'\bopen the\b', 'open', text)
    text = re.sub(r'\bclose the\b', 'close', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _closed_world_selection_ok(parsed: dict, shortlist: list[str]) -> bool:
    """Spec RULE CHANGE (defect found in a G1 replay rerun, 2026-07-23):
    stage (c) may ONLY (1) select one shortlist candidate VERBATIM, or
    (2) emit a valid ACTION2 phrase whose verb is a real ACTION2 verb.
    Everything else -- conversation, schedule, or a fabricated/altered
    command name -- is rejected HERE, in code, before
    ask_ollama.handle_response ever sees the response, so a hallucinated
    "action" can never reach execute_command(). This is the actual gate;
    _CLOSED_WORLD_REMINDER above is prompt-level reinforcement only.

    Free-text argument slots are legal ONLY for ACTION2 (2), whose
    argument was already a deterministically-resolved app/window name in
    the user's own words, not typed/inserted text -- never for ACTION,
    which per the shared system prompt's own MODE 2 has no argument slot
    at all: a well-formed ACTION line IS the complete command name, so
    anything beyond an exact shortlist entry is by definition either a
    fabricated command or free text smuggled in as part of the "name"."""
    if parsed["type"] == "action":
        command_name = _normalize_command_name(parsed.get("command"))
        if not command_name:
            return False
        normalized_shortlist = {_normalize_command_name(c) for c in shortlist}
        return command_name in normalized_shortlist
    if parsed["type"] == "action2":
        from plugins.commands import ask_ollama  # noqa: PLC0415
        return parsed.get("verb") in ask_ollama.ACTION2_VERBS
    return False  # conversation, schedule, or any other type -> MISS


def _stage_c_llm_fallback(app, utterance: str, shortlist: list[str], generation: int, cfg: dict) -> bool:
    """One LLM fallback pass via the shared Ava backend. Returns True on a
    confident command proposal that passed the closed-world gate (staged
    or executed), False on anything else (conversational reply, schedule,
    or a rejected fabrication) -- the caller registers that as a miss.
    Never speaks the raw conversational reply itself (that would be
    "become freeform chat", explicitly out of scope for D3), and never
    executes a command the model invented outside the shortlist/ACTION2
    grammar it was given (see _closed_world_selection_ok)."""
    from plugins.commands import ask_ollama  # noqa: PLC0415

    system = ask_ollama.get_system_prompt(app)
    if "{COMMAND_LIST}" in system:
        system = system.replace("{COMMAND_LIST}", ", ".join(shortlist))
    system += _CLOSED_WORLD_REMINDER
    model = cfg.get("model", _DEFAULTS["model"]) if cfg.get("backend") != "cloud" else None
    response = ask_ollama.ask_ollama(utterance, app, model=model, system=system)

    if getattr(app, "_ava_cmd_generation", generation) != generation:
        logger.debug(
            f"[AVA-CMD] Stale generation after stage (c) resolution "
            f"({generation} != {app._ava_cmd_generation}) -- dropping"
        )
        return True  # not a miss -- just stale; do not nag a dead session

    if not isinstance(response, str) or response == "__OLLAMA_DOWN__":
        return False

    parsed = ask_ollama._parse_structured_response(response)
    if not _closed_world_selection_ok(parsed, shortlist):
        logger.debug(
            f"[AVA-CMD] Stage (c) rejected -- not a closed-world selection: {parsed!r}"
        )
        return False

    ask_ollama.handle_response(app, response, original_text=utterance, generation=generation)
    return True


# ---------------------------------------------------------------------------
# Speaker / chime helpers -- same generation-staleness-gated pattern as
# ai_command_mode.py (2026-07-19 incident mechanism, PRESERVED).
# ---------------------------------------------------------------------------

def _speak(app, text: str, generation: int) -> None:
    if getattr(app, "_ava_cmd_generation", generation) != generation:
        logger.debug(
            f"[AVA-CMD] Stale generation ({generation} != {app._ava_cmd_generation}) "
            f"-- dropping TTS: {text!r}"
        )
        return
    ac = getattr(app, "audio_coordinator", None)
    if ac is not None:
        ac.speak(text, category="ava_command_session")
    else:
        print(f"[AVA-CMD] speak: {text}")


def _chime(app) -> None:
    """Soft non-spoken miss earcon -- reuses 'scratch_refuse', same asset
    ai_command_mode.py reused (the app's existing "didn't go through" sound)."""
    play_sound = getattr(app, "play_sound", None)
    if play_sound is None:
        return
    try:
        play_sound("scratch_refuse")
    except Exception as exc:
        logger.debug(f"_chime: {exc}")


def _set_thinking_indicator(app, active: bool) -> None:
    """Latch feedback: thinking indicator during LLM fallback (spec's
    "prevents double-talk"). Reuses ask_ollama's own indicator helper --
    same visual state D1's Ava exchanges already use."""
    try:
        from plugins.commands.ask_ollama import _set_thinking  # noqa: PLC0415
        _set_thinking(app, active)
    except Exception as exc:
        logger.debug(f"_set_thinking_indicator: {exc}")


def _register_miss(app, cfg: dict, generation: int) -> None:
    """Bump the consecutive-miss counter; nag once, chime after, auto-exit
    at the configured limit. PRESERVED verbatim from ai_command_mode.py's
    graduated escalation (see this repo's 2026-07-19 nag-incident fix).

    Gated on `generation` FIRST: if the session already exited (and
    possibly re-entered) since this utterance started resolving, touching
    counters or force-exiting here would corrupt or kill a DIFFERENT,
    possibly freshly-re-entered session."""
    if getattr(app, "_ava_cmd_generation", generation) != generation:
        logger.debug(
            f"[AVA-CMD] Stale generation at miss-registration "
            f"({generation} != {app._ava_cmd_generation}) -- dropping"
        )
        return

    miss_count = getattr(app, "_ava_cmd_miss_count", 0) + 1
    app._ava_cmd_miss_count = miss_count
    miss_limit = int(cfg.get("miss_limit", _DEFAULTS["miss_limit"]))

    if miss_limit > 0 and miss_count >= miss_limit:
        logger.info(f"[AVA-CMD] Miss limit ({miss_limit}) reached -- exiting")
        _speak(app, "Ava command session off.", generation)
        exit_fn = getattr(app, "exit_ava_command_session", None)
        if exit_fn is not None:
            exit_fn()
        return

    if miss_count <= 1:
        _speak(app, "I didn't catch a command in that.", generation)
    else:
        _chime(app)


def _register_hit(app) -> None:
    app._ava_cmd_miss_count = 0


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _process_utterance(app, generation: int, utterance: str) -> None:  # noqa: C901
    """Waterfall (a) -> (b) -> (c). See module docstring for the full
    rationale of each stage and why (a)/(b) reuse existing dispatch
    machinery instead of a new resolver."""
    cfg = _cfg(app)

    # Stage (a): existing full matcher (builtin + plugin, remainder-tolerant).
    t0 = time.monotonic()
    dispatch = app.command_executor.process_text(
        utterance, app, force_commands=True, route=Route.EXACT, generation=generation)
    _result, was_command = dispatch
    stage_a_ms = (time.monotonic() - t0) * 1000
    logger.debug(f"[AVA-CMD] Stage (a) took {stage_a_ms:.1f}ms")
    # was_command is True for every claimed state -- queued (async handler
    # returned None), failed, rejected, cancelled -- so a recognised command
    # never falls through to (b)/(c) to be interpreted a second time.
    if was_command:
        state = getattr(getattr(dispatch, "state", None), "value", "completed")
        if state not in ("completed", "queued", "matched"):
            logger.info(f"[AVA-CMD] Stage (a) command {_result!r} {state}; not re-interpreting")
        if getattr(app, "_ava_cmd_generation", generation) != generation:
            logger.debug("[AVA-CMD] Stale generation after stage (a) -- dropping")
            return
        _register_hit(app)
        return

    # Stage (b): deterministic ACTION2 grammar.
    t1 = time.monotonic()
    action2 = _match_action2_grammar(utterance)
    stage_b_ms = (time.monotonic() - t1) * 1000
    logger.debug(f"[AVA-CMD] Stage (b) took {stage_b_ms:.1f}ms")
    if action2 is not None:
        if getattr(app, "_ava_cmd_generation", generation) != generation:
            logger.debug("[AVA-CMD] Stale generation after stage (b) -- dropping")
            return
        verb, argument = action2
        _dispatch_action2(app, verb, argument, generation, cfg)
        _register_hit(app)
        return

    # Stage (c): ONE LLM fallback pass, with the latch's thinking indicator.
    _set_thinking_indicator(app, True)
    try:
        shortlist = _build_shortlist(app, utterance, cfg)
        hit = _stage_c_llm_fallback(app, utterance, shortlist, generation, cfg)
    finally:
        _set_thinking_indicator(app, False)

    if getattr(app, "_ava_cmd_generation", generation) != generation:
        logger.debug("[AVA-CMD] Stale generation after stage (c) -- dropping")
        return
    if hit:
        _register_hit(app)
    else:
        _register_miss(app, cfg, generation)


def _worker_loop(app) -> None:
    while True:
        try:
            item = _task_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if _cancel.is_set():
            # Session exited (and stays SET until the next fresh entry's
            # reset_cancel() -- see dictation.py's enter/exit_ava_command_session)
            # -- drain silently.
            continue
        generation, utterance = item
        if getattr(app, "_ava_cmd_generation", generation) != generation:
            logger.debug("[AVA-CMD] Stale generation at dequeue -- dropping")
            continue
        try:
            _process_utterance(app, generation, utterance)
        except Exception as exc:
            print(f"[AVA-CMD] Worker error: {exc}")
            import traceback  # noqa: PLC0415
            traceback.print_exc()


def _ensure_worker(app) -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    thread_registry.spawn("ava-cmd-worker", _worker_loop, args=(app,), daemon=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enqueue_utterance(app, generation: int, utterance: str) -> None:
    """Enqueue a finalized utterance tagged with the session generation
    active when it was captured (see dictation.py's
    _handle_ava_command_utterance). Drops oldest item (with a spoken
    warning) if full."""
    cfg = _cfg(app)
    cap = int(cfg.get("queue_depth_cap", _DEFAULTS["queue_depth_cap"]))
    _ensure_worker(app)
    if _task_queue.qsize() >= cap:
        _speak(app, "I'm a bit behind -- hold on.", generation)
        try:
            _task_queue.get_nowait()
        except queue.Empty as e:
            logger.debug(f"enqueue_utterance: {e}")
    _task_queue.put_nowait((generation, utterance))


def drain_stale(app) -> int:
    """Drop queued utterances whose generation is no longer current WITHOUT
    setting the cancel flag (the session stays alive). Used by the stop path
    (execution_policy.stop_all) so "cancel" mid-session empties the backlog
    while a fresh utterance still works. Returns how many were dropped."""
    current = getattr(app, "_ava_cmd_generation", None)
    kept, dropped = [], 0
    while True:
        try:
            item = _task_queue.get_nowait()
        except queue.Empty:
            break
        if current is not None and item[0] != current:
            dropped += 1
        else:
            kept.append(item)
    for item in kept:
        _task_queue.put_nowait(item)
    return dropped


def cancel_queue() -> None:
    """Set cancel flag and drain the queue."""
    _cancel.set()
    while not _task_queue.empty():
        try:
            _task_queue.get_nowait()
        except queue.Empty:
            break


def reset_cancel() -> None:
    """Clear the cancel flag so the worker accepts new utterances."""
    _cancel.clear()


def _play_ready_cue(app) -> None:
    """PRESERVED verbatim from ai_command_mode.py: pick a random WAV from
    the ready-cue directory and play it synchronously so the caller can
    arm the mic immediately after returning. Degrades silently if the
    directory is missing or empty."""
    import random
    from pathlib import Path

    cfg = _cfg(app)
    if not cfg.get("ready_cue_enabled", _DEFAULTS["ready_cue_enabled"]):
        return

    cue_dir_str = cfg.get("ready_cue_dir", _DEFAULTS["ready_cue_dir"])
    cue_path = Path(cue_dir_str)
    if not cue_path.is_absolute():
        repo_root = Path(__file__).resolve().parent.parent
        cue_path = repo_root / cue_dir_str

    if not cue_path.is_dir():
        return

    wavs = sorted(cue_path.glob("*.wav"))
    if not wavs:
        return

    chosen = random.choice(wavs)
    print(f"[AVA-CMD] Ready cue: {chosen.name}")
    try:
        import winsound
        winsound.PlaySound(str(chosen), winsound.SND_FILENAME)
    except Exception as exc:
        print(f"[AVA-CMD] Ready cue playback failed: {exc}")


def warm_up(app, on_done=None) -> None:
    """PRESERVED verbatim from ai_command_mode.py: fire a throwaway
    resolve call so the model is warm in Ollama's memory before the first
    real utterance. When backend is 'cloud', skips straight to on_done."""
    cfg = _cfg(app)
    backend = cfg.get("backend", _DEFAULTS["backend"])

    if backend == "cloud":
        def _cloud_noop():
            print("[AVA-CMD] Cloud backend -- skipping Ollama warm-up.")
            if on_done is not None:
                try:
                    on_done()
                except Exception as exc:
                    print(f"[AVA-CMD] warm_up on_done error: {exc}")
        thread_registry.spawn("ava-cmd-warmup", _cloud_noop, daemon=True)
        return

    from plugins.commands import ask_ollama  # noqa: PLC0415
    model = cfg.get("model", _DEFAULTS["model"])

    def _do():
        print(f"[AVA-CMD] Warming up {model!r}...")
        try:
            ask_ollama.ask_ollama("test", app, model=model, system="Reply with OK.")
        except Exception as exc:
            print(f"[AVA-CMD] Warm-up call failed: {exc}")
        print("[AVA-CMD] Warm-up done.")
        if on_done is not None:
            try:
                on_done()
            except Exception as exc:
                print(f"[AVA-CMD] warm_up on_done error: {exc}")

    thread_registry.spawn("ava-cmd-warmup", _do, daemon=True)
