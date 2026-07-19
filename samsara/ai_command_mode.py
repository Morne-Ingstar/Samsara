"""AI Command Mode -- LLM-backed voice-to-command resolver with queue worker.

Architecture:
  - enqueue_utterance() feeds a depth-capped queue.Queue.
  - A single daemon worker resolves each utterance via Ollama
    (format:"json", temperature:0) and executes the action list step-by-step
    with a configurable settle delay between steps.
  - Unsafe commands gate the whole plan behind a spoken confirmation;
    the next utterance is checked for a confirm or deny word.
  - _cancel lets toggle-key-off and stop-word paths halt cleanly mid-plan.

Config block (add to config.json -- all keys optional, defaults apply if absent):
  "ai_command_mode": {
    "enabled":            true,
    "key":                "right_ctrl",
    "wake_phrase":        "command mode",
    "model":              "llama3.2:3b",
    "queue_depth_cap":    3,
    "step_settle_seconds": 0.4,
    "show_plan_hud":      true,
    "keep_warm":          true,
    "miss_limit":         3,
    "menu_limit":         0
  }

Miss handling (unresolved utterances -- resolver returned no actions):
  - 1st consecutive miss:  spoken "I didn't catch a command in that."
  - subsequent misses:     soft chime only (reuses the 'scratch_refuse'
                            earcon -- the established "didn't go through"
                            sound elsewhere in the app)
  - miss_limit reached:    mode auto-exits with a single spoken
                            "AI command mode off"
  Any resolved utterance (actions found, or a pending-plan confirm/deny)
  resets the counter. See _process_utterance / _register_miss / _register_hit.

Menu cap: the command menu handed to the resolver is UNTRUNCATED by
default (menu_limit=0). Set ai_command_mode.menu_limit to a positive int
to cap it (e.g. for a small/slow local model's context window) -- any
truncation that then actually occurs is logged as a WARNING with exact
counts, never silent. See _capped_menu.

Session-generation staleness guard (2026-07-19 incident report items
7-8): dictation.py's app._ai_cmd_generation is an int bumped on every
enter_ai_command_mode() AND exit_ai_command_mode(). The generation active
when an utterance was captured is threaded through enqueue_utterance ->
_process_utterance -> _execute_plan/_speak/_resolve_via_cloud/
_register_miss, and rechecked after every async gap (ready-wait,
transcription, resolution, before each plan step, before every TTS call).
A mismatch means a legitimate exit (and possibly re-entry) happened while
this utterance's work was in flight -- it is dropped silently (debug log
only), never acted on or spoken for. cancel_queue()'s _cancel event is
the complementary, coarser guard: exit leaves it SET until the next
entry's reset_cancel(), so anything still sitting in the queue at dequeue
time is also caught even before generation is checked.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
from typing import Any, Optional

from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults -- applied when config key is absent or partially populated
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "key": "right_ctrl",
    "wake_phrase": "command mode",
    "backend": "ollama",
    "model": "llama3.2:3b",
    "queue_depth_cap": 3,
    "step_settle_seconds": 0.4,
    "show_plan_hud": True,
    "keep_warm": True,
    "ready_cue_enabled": True,
    "ready_cue_dir": "assets/sounds/ava_cues",
    "miss_limit": 3,
    "menu_limit": 0,
}

# Words that trigger immediate queue-cancel (checked inline before enqueue)
_STOP_WORDS: frozenset[str] = frozenset({
    "stop", "cancel", "cancel that", "scratch that",
})

# Words that confirm a pending unsafe plan
_CONFIRM_WORDS: frozenset[str] = frozenset({
    "yes", "confirm it", "do it", "go ahead", "yeah do it",
    "yeah", "yep", "yup", "sure",
})

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_task_queue: queue.Queue = queue.Queue()
_cancel = threading.Event()
_worker_lock = threading.Lock()
_worker_started: bool = False

# Pending unsafe plan awaiting a spoken "yes"
_pending_plan: Optional[list[str]] = None
_pending_plan_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Config/host helpers
# ---------------------------------------------------------------------------

def _cfg(app) -> dict:
    return {**_DEFAULTS, **app.config.get("ai_command_mode", {})}


def _host(app) -> str:
    return app.config.get("ollama", {}).get("host", "http://localhost:11434")


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a voice command translator. The user speaks a natural-language request.\n"
    "Translate it into an ordered list of exact commands from the menu below.\n\n"
    "Available commands: {COMMAND_LIST}\n\n"
    "Rules:\n"
    "1. Only use commands from the list -- do not invent names.\n"
    '2. Return ONLY valid JSON: {"actions":[{"command":"<exact name>"}, ...]}\n'
    '3. If nothing fits, return {"actions":[]}\n'
    "4. No explanation -- only the JSON object."
)


def resolve_utterance(
    utterance: str,
    menu: list[str],
    model: str,
    host: str,
) -> list[str]:
    """Call Ollama with format:json, return validated in-menu command names in order.

    `menu` is joined as-is -- callers that need it capped (see
    ai_command_mode.menu_limit) must pass an already-capped list via
    _capped_menu(); this function never truncates on its own."""
    cmd_list = ", ".join(menu)
    system = _SYSTEM_PROMPT.replace("{COMMAND_LIST}", cmd_list)
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": utterance},
        ],
        "format":     "json",
        "stream":     False,
        "options":    {"temperature": 0},
        "keep_alive": "5m",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read())
        content = raw.get("message", {}).get("content", "{}")
        parsed = json.loads(content)
        actions = parsed.get("actions", [])
        menu_set = set(menu)
        return [
            a["command"]
            for a in actions
            if isinstance(a, dict) and isinstance(a.get("command"), str)
            and a["command"] in menu_set
        ]
    except Exception as exc:
        print(f"[AI-CMD] Resolver error: {exc}")
        return []


def _build_menu(app) -> list[str]:
    """Return sorted list of ai_visible command names from the command executor."""
    if hasattr(app, "command_executor") and hasattr(app.command_executor, "commands"):
        return sorted(
            name
            for name, entry in app.command_executor.commands.items()
            if entry.get("ai_visible", True)
        )
    return []


def _capped_menu(menu: list[str], cfg: dict) -> list[str]:
    """Cap the command menu passed to the resolver, per ai_command_mode.menu_limit.

    Replaces the old unconditional `menu[:200]` slice, which silently
    dropped commands once the registry (288+ phrases plus runtime plugin
    registrations) grew past 200 -- no config, no warning, commands past
    the cut just silently stopped being resolvable.

    menu_limit=0 (default) means no cap: the full menu is returned as-is.
    Any truncation that DOES occur (menu_limit > 0 and menu longer than
    it) is logged as a WARNING with exact counts -- never silent."""
    limit = int(cfg.get("menu_limit", _DEFAULTS["menu_limit"]))
    if limit <= 0 or len(menu) <= limit:
        return menu
    dropped = len(menu) - limit
    logger.warning(
        f"[AI-CMD] Menu truncated: {len(menu)} commands -> {limit} "
        f"(ai_command_mode.menu_limit={limit}), {dropped} command(s) dropped "
        "from this resolve call"
    )
    return menu[:limit]


def _resolve_via_cloud(utterance: str, menu: list[str], app, generation: int) -> list[str]:
    """Resolve utterance via the configured cloud LLM provider.

    Reuses the existing cloud_llm plumbing (send_json) — no new HTTP client.
    Returns a validated in-menu command list, same contract as resolve_utterance().

    `menu` is joined as-is -- callers that need it capped (see
    ai_command_mode.menu_limit) must pass an already-capped list via
    _capped_menu(); this function never truncates on its own.

    `generation` is threaded into any _speak() calls -- see _speak's
    docstring for the staleness-guard mechanism."""
    from samsara import cloud_llm  # noqa: PLC0415
    cloud_cfg = app.config.get("cloud_llm", {})
    if not cloud_cfg.get("api_key", ""):
        _speak(app, "No cloud API key is configured. Add one in Settings under Ava Cloud.", generation)
        return []
    cmd_list = ", ".join(menu)
    system = _SYSTEM_PROMPT.replace("{COMMAND_LIST}", cmd_list)
    raw = cloud_llm.send_json(system, utterance, app)
    if raw.startswith("Error:"):
        print(f"[AI-CMD] Cloud resolver error: {raw}")
        _speak(app, "Cloud resolver failed. Check your API key and network, then try again.", generation)
        return []
    try:
        parsed = json.loads(raw)
        actions = parsed.get("actions", [])
        menu_set = set(menu)
        return [
            a["command"]
            for a in actions
            if isinstance(a, dict) and isinstance(a.get("command"), str)
            and a["command"] in menu_set
        ]
    except Exception as exc:
        print(f"[AI-CMD] Cloud resolve parse error: {exc} | raw: {raw!r}")
        return []


# ---------------------------------------------------------------------------
# Speaker and mic-duck helpers
# ---------------------------------------------------------------------------

def _speak(app, text: str, generation: int) -> None:
    """Speak `text`, unless the mode has moved on to a different generation
    (an exit, or exit + re-entry) since the caller captured `generation` --
    2026-07-19 incident report item 7: exit set the ready event but
    nothing rechecked ai_command_mode_active before speaking, so feedback
    for an utterance that started resolving before a legitimate exit could
    still land after it. Every caller in this module threads through the
    generation it captured at (or before) its own async work started."""
    if getattr(app, "_ai_cmd_generation", generation) != generation:
        logger.debug(
            f"[AI-CMD] Stale generation ({generation} != {app._ai_cmd_generation}) "
            f"-- dropping TTS: {text!r}"
        )
        return
    ac = getattr(app, "audio_coordinator", None)
    if ac is not None:
        ac.speak(text, category="ai_command")
    else:
        print(f"[AI-CMD] speak: {text}")


def _duck(app, duck: bool) -> None:
    """Duck/unduck the mic via audio_coordinator if supported."""
    ac = getattr(app, "audio_coordinator", None)
    if ac is None:
        return
    try:
        if duck:
            ac.duck_mic()
        else:
            ac.unduck_mic()
    except AttributeError as e:
        logger.debug(f"_duck: {e}")


def _chime(app) -> None:
    """Soft non-spoken miss earcon. Reuses 'scratch_refuse' -- the app's
    existing "this didn't go through" sound (see dictation.py's hands-free
    session dispatch) -- rather than adding a new asset."""
    play_sound = getattr(app, "play_sound", None)
    if play_sound is None:
        return
    try:
        play_sound("scratch_refuse")
    except Exception as exc:
        logger.debug(f"_chime: {exc}")


def _register_miss(app, cfg: dict, generation: int) -> None:
    """Bump the consecutive-miss counter; nag once, chime after, auto-exit
    at the configured limit. See module docstring for the exact sequence.

    Mirrors dictation.py's command_mode miss_limit auto-exit, but adds the
    graduated spoken-notice -> chime-only escalation this mode was missing
    (see the 2026-07-19 AI-command-mode nag incident).

    Gated on `generation` FIRST, before touching any state: if the mode
    already exited (and possibly re-entered) since this utterance started
    resolving, incrementing the counter or -- worse -- calling
    exit_ai_command_mode() here would corrupt or force-exit a DIFFERENT,
    possibly freshly-re-entered session (2026-07-19 incident report
    items 7-8)."""
    if getattr(app, "_ai_cmd_generation", generation) != generation:
        logger.debug(
            f"[AI-CMD] Stale generation at miss-registration "
            f"({generation} != {app._ai_cmd_generation}) -- dropping"
        )
        return

    miss_count = getattr(app, "_ai_cmd_miss_count", 0) + 1
    app._ai_cmd_miss_count = miss_count
    miss_limit = int(cfg.get("miss_limit", _DEFAULTS["miss_limit"]))

    if miss_limit > 0 and miss_count >= miss_limit:
        logger.info(f"[AI-CMD] Miss limit ({miss_limit}) reached -- exiting")
        _speak(app, "AI command mode off.", generation)
        exit_fn = getattr(app, "exit_ai_command_mode", None)
        if exit_fn is not None:
            exit_fn()
        return

    if miss_count <= 1:
        _speak(app, "I didn't catch a command in that.", generation)
    else:
        _chime(app)


def _register_hit(app) -> None:
    """Reset the consecutive-miss counter after any resolved utterance
    (a matched plan, or a pending-plan confirm/deny)."""
    app._ai_cmd_miss_count = 0


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

def _execute_step(app, command_name: str) -> None:
    """Execute one command via command_executor (follows _execute_safe pattern)."""
    if hasattr(app, "command_executor"):
        app.command_executor.execute_command(command_name)


def _execute_plan(app, generation: int, actions: list[str], cfg: dict) -> None:
    """Execute each step with settle delay; check cancel and generation
    between steps -- a stale generation (exit, or exit + re-entry, since
    this plan was resolved) drops the remaining steps silently, same as
    _cancel.is_set() (2026-07-19 incident report item 8)."""
    settle = float(cfg.get("step_settle_seconds", _DEFAULTS["step_settle_seconds"]))
    show_hud = bool(cfg.get("show_plan_hud", _DEFAULTS["show_plan_hud"]))

    if show_hud:
        _hud_show(app, actions)

    for i, cmd in enumerate(actions):
        if _cancel.is_set():
            print("[AI-CMD] Plan cancelled mid-execution")
            break
        if getattr(app, "_ai_cmd_generation", generation) != generation:
            logger.debug(
                f"[AI-CMD] Stale generation before step {i + 1}/{len(actions)} "
                f"({generation} != {app._ai_cmd_generation}) -- dropping remaining plan"
            )
            break
        print(f"[AI-CMD] Step {i + 1}/{len(actions)}: {cmd!r}")
        if show_hud:
            _hud_step(i, "running")
        try:
            _execute_step(app, cmd)
            if show_hud:
                _hud_step(i, "done")
        except Exception as exc:
            print(f"[AI-CMD] Step {i + 1} error: {exc}")
            if show_hud:
                _hud_step(i, "failed")
        if i < len(actions) - 1 and not _cancel.is_set():
            time.sleep(settle)

    if show_hud:
        time.sleep(1.5)
        _hud_hide()


# ---------------------------------------------------------------------------
# HUD interface (lazy-import to keep this module Qt-free at load time)
# ---------------------------------------------------------------------------

def _hud_show(app, actions: list[str]) -> None:
    try:
        from samsara.ui.ai_command_hud_qt import show_hud  # noqa: PLC0415
        show_hud(app, actions)
    except Exception as exc:
        print(f"[AI-CMD] HUD show: {exc}")


def _hud_step(step_index: int, status: str) -> None:
    try:
        from samsara.ui.ai_command_hud_qt import update_step  # noqa: PLC0415
        update_step(step_index, status)
    except Exception as exc:
        print(f"[AI-CMD] HUD update: {exc}")


def _hud_hide() -> None:
    try:
        from samsara.ui.ai_command_hud_qt import hide_hud  # noqa: PLC0415
        hide_hud()
    except Exception as exc:
        print(f"[AI-CMD] HUD hide: {exc}")


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _process_utterance(app, generation: int, utterance: str) -> None:  # noqa: C901
    """Resolve one utterance: confirm-gate check, then Ollama resolve, then execute.

    `generation` is the AI-command-mode generation active when this
    utterance was captured (see dictation.py's _handle_ai_command_utterance
    and enqueue_utterance below). Rechecked after resolution -- checkpoint
    (c) of the 2026-07-19 incident report's staleness-guard sequence --
    and threaded into every _speak()/_execute_plan() call so mid-flight
    work never acts, or gives feedback, for a session that has already
    ended."""
    global _pending_plan
    cfg = _cfg(app)
    text_lower = utterance.lower().strip()

    # --- Pending unsafe-plan confirmation check ------------------------------
    with _pending_plan_lock:
        pending = _pending_plan

    if pending is not None:
        _register_hit(app)  # confirm or deny -- either way the utterance was understood
        if text_lower in _CONFIRM_WORDS:
            with _pending_plan_lock:
                _pending_plan = None
            print(f"[AI-CMD] Plan confirmed: {pending}")
            _execute_plan(app, generation, pending, cfg)
        else:
            with _pending_plan_lock:
                _pending_plan = None
            _speak(app, "Plan cancelled.", generation)
        return

    # --- Resolve utterance --------------------------------------------------
    menu = _capped_menu(_build_menu(app), cfg)
    backend = cfg.get("backend", _DEFAULTS["backend"])

    print(f"[AI-CMD] Resolving via {backend!r}: {utterance!r}")
    _duck(app, True)
    try:
        if backend == "cloud":
            actions = _resolve_via_cloud(utterance, menu, app, generation)
        else:
            model = cfg.get("model", _DEFAULTS["model"])
            host = _host(app)
            actions = resolve_utterance(utterance, menu, model, host)
    finally:
        _duck(app, False)

    # Checkpoint (c): after resolution, before acting on it at all -- the
    # resolver call (network round-trip, up to 20s) is the single longest
    # async gap in this whole pipeline.
    if getattr(app, "_ai_cmd_generation", generation) != generation:
        logger.debug(
            f"[AI-CMD] Stale generation after resolution "
            f"({generation} != {app._ai_cmd_generation}) -- dropping"
        )
        return

    if not actions:
        _register_miss(app, cfg, generation)
        return

    _register_hit(app)
    print(f"[AI-CMD] Plan: {actions}")

    # --- Unsafe-command gate ------------------------------------------------
    try:
        from plugins.commands.ask_ollama import _UNSAFE_COMMANDS  # noqa: PLC0415
        unsafe = [a for a in actions if a in _UNSAFE_COMMANDS]
    except ImportError:
        unsafe = []

    if unsafe:
        unsafe_str = ", ".join(unsafe)
        with _pending_plan_lock:
            _pending_plan = actions
        _speak(
            app,
            f"This plan includes {unsafe_str}. Say yes to confirm or scratch that to cancel.",
            generation,
        )
        return

    _execute_plan(app, generation, actions, cfg)


def _worker_loop(app) -> None:
    while True:
        try:
            item = _task_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if _cancel.is_set():
            # Mode exited (and stays SET until the next fresh entry's
            # reset_cancel() -- see dictation.py's enter/exit_ai_command_mode)
            # -- drain silently.
            continue
        generation, utterance = item
        try:
            _process_utterance(app, generation, utterance)
        except Exception as exc:
            print(f"[AI-CMD] Worker error: {exc}")
            import traceback  # noqa: PLC0415
            traceback.print_exc()


def _ensure_worker(app) -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    t = thread_registry.spawn("ai-cmd-worker", _worker_loop, args=(app,), daemon=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enqueue_utterance(app, generation: int, utterance: str) -> None:
    """Enqueue a finalized utterance tagged with the AI-command-mode
    generation active when it was captured (see dictation.py's
    _handle_ai_command_utterance and the module docstring's
    session-generation section). Drops oldest item (with a spoken
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


def cancel_queue() -> None:
    """Set cancel flag and drain the queue. Clears any pending unsafe plan."""
    global _pending_plan
    _cancel.set()
    with _pending_plan_lock:
        _pending_plan = None
    while not _task_queue.empty():
        try:
            _task_queue.get_nowait()
        except queue.Empty:
            break


def reset_cancel() -> None:
    """Clear the cancel flag so the worker accepts new utterances."""
    _cancel.clear()


def _play_ready_cue(app) -> None:
    """Pick a random WAV from the ready-cue directory and play it synchronously.

    Blocks until playback finishes so the caller can arm the mic immediately
    after returning. Degrades silently if the directory is missing or empty.
    """
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
    print(f"[AI-CMD] Ready cue: {chosen.name}")
    try:
        import winsound
        winsound.PlaySound(str(chosen), winsound.SND_FILENAME)
    except Exception as exc:
        print(f"[AI-CMD] Ready cue playback failed: {exc}")


def warm_up(app, on_done=None) -> None:
    """Fire a throwaway resolve call to load the model into Ollama's memory.

    on_done: optional zero-argument callable invoked on the warm-up thread
    after the resolve call returns. Used to chain the ready-cue and mic-arm.

    When backend is 'cloud', skips the Ollama call and fires on_done directly
    (still async so the caller's flow is consistent).
    """
    cfg = _cfg(app)
    backend = cfg.get("backend", _DEFAULTS["backend"])

    if backend == "cloud":
        def _cloud_noop():
            print("[AI-CMD] Cloud backend -- skipping Ollama warm-up.")
            if on_done is not None:
                try:
                    on_done()
                except Exception as exc:
                    print(f"[AI-CMD] warm_up on_done error: {exc}")
        thread_registry.spawn("ai-cmd-warmup", _cloud_noop, daemon=True)
        return

    model = cfg.get("model", _DEFAULTS["model"])
    host = _host(app)

    def _do():
        print(f"[AI-CMD] Warming up {model!r}...")
        resolve_utterance("test", ["screenshot"], model, host)
        print("[AI-CMD] Warm-up done.")
        if on_done is not None:
            try:
                on_done()
            except Exception as exc:
                print(f"[AI-CMD] warm_up on_done error: {exc}")

    thread_registry.spawn("ai-cmd-warmup", _do, daemon=True)
