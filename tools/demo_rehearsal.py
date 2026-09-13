"""Rehearse the five-minute take (SAMSARA_VISION.md section 7) against
Samsara's own dispatch path -- hands off, no side effects by default.

The take is a fixed script (TAKE below). For every line the rehearsal asks
the REAL matching layers what would happen: the session-mode control words
(samsara.session_modes: abort phrases, "scratch that", switch words, Ava
invocations, the hands-free reserved-command probe), the command registry
(samsara.command_registry.CommandMatcher built from the live registry or a
catalog dump), and the argument grammar each resolved command actually
implements. Nothing is executed. The result is a table --
perf_artifacts/demo_rehearsal.md -- that re-runs after every change, so the
gap between today and the demo is a table, not an argument.

    WORKS    the line resolves to a command whose arguments the plugin can
             honour, in the lane the session would actually be in
    PARTIAL  it resolves, but needs a mode switch / extra utterance / a
             fallback path that is not quite the intent
    MISSING  no command, grammar or capability owns it -- the owner named

Usage:
    python tools/demo_rehearsal.py                     # dry run, live registry
    python tools/demo_rehearsal.py --catalog dump.json # dry run, a catalog dump
                                                       # (tools/dump_command_metadata.py output)
    python tools/demo_rehearsal.py --live              # execute the resolvable
                                                       # steps, 3 s apart (owner only)

This module never imports dictation.py: Samsara is usually live while it
runs. Everything it needs from the app is either a pure module
(session_modes, command_registry, wake_word_matcher, phonetic_wash) or a
copied constant marked with its origin.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from samsara.command_registry import CommandMatcher  # noqa: E402
from samsara.phonetic_wash import apply_phonetic_wash  # noqa: E402
from samsara.session_modes import (  # noqa: E402
    GLOBAL_SESSION_EXIT_PHRASES, SessionMode, _normalize_exact_phrase, is_scratch_that,
    match_ava_invocation, match_literal_payload, match_switch_word, normalize_utterance,
    resolve_ava_invocations,
)
from samsara.wake_word_matcher import match_wake_phrase  # noqa: E402

WORKS, PARTIAL, MISSING = "WORKS", "PARTIAL", "MISSING"
STEP_PAUSE_S = 3.0

# Copied from dictation.py (_HANDS_FREE_COMMIT_PREFIXES) -- the only part of
# the hands-free reserved-command probe that lets a command keep a remainder.
# tests/test_demo_rehearsal.py checks this copy against the original.
HANDS_FREE_COMMIT_PREFIXES = ("focus ", "switch to ", "window switch ", "go to window ", "click ", "tap ")

# Wake-word defaults (samsara.constants) -- read from config when present.
DEFAULT_WAKE_PHRASE = "jarvis"
DEFAULT_WAKE_PHRASE_OPTIONS = ["jarvis", "hey jarvis", "computer", "hey computer", "samsa", "hey samsa"]

# The 40-word paragraph for step 5 (tests assert the count).
PARAGRAPH = (
    "The mode switch fix landed this afternoon and the hands free session now "
    "enters in dictate, so the next thing to rehearse is the five minute "
    "take: the music, the two monitors, the Claude message, this note, one "
    "correction, sleep."
)


@dataclass(frozen=True)
class Step:
    number: int
    utterance: str
    kind: str          # wake | command | dictation | correction | scratch | sleep
    intent: str


TAKE = (
    Step(1, "wake up samsara", "wake", "hands-free opens"),
    Step(2, "play something from my alternative rock playlist", "command", "media starts"),
    Step(3, "put warp on the left screen and claude on the right", "command",
         "two windows placed on two monitors"),
    Step(4, "tell claude the mode switch fix landed and ask what's next", "command",
         "text dictated into the Claude app and sent"),
    Step(5, PARAGRAPH, "dictation", "a 40-word paragraph appears verbatim in Obsidian"),
    Step(6, "correct that", "correction", "one word fixed by voice"),
    Step(7, "scratch that", "scratch", "last action undone"),
    Step(8, "go to sleep", "sleep", "hands-free closes"),
)


@dataclass
class Resolution:
    step: Step
    resolves_to: str
    mode_required: str
    status: str
    missing: str = ""
    owner: str = ""
    live_text: Optional[str] = None      # the exact text --live would dispatch, if any
    dispatch_expected: bool = False       # in the lane the session is actually in

    def as_row(self) -> dict:
        return {
            "step": self.step.number, "utterance": self.step.utterance,
            "resolves_to": self.resolves_to, "mode_required": self.mode_required,
            "status": self.status, "missing": self.missing, "owner": self.owner,
        }


# ---------------------------------------------------------------------------
# Catalog -> matcher (the registry, minus handlers)
# ---------------------------------------------------------------------------

def _no_handler(app, remainder):  # pragma: no cover - never called in a dry run
    return False


def build_matcher(rows: list, enabled_packs=None) -> CommandMatcher:
    """A real CommandMatcher from catalog rows (tools/dump_command_metadata.py
    shape: phrase/source/type/pack/aliases/description/metadata)."""
    builtins, plugins = {}, {}
    for row in rows:
        meta = {k: v for k, v in (row.get("metadata") or {}).items() if v != "unknown"}
        if row.get("source") == "builtin":
            builtins[row["phrase"]] = {
                "type": row.get("type", "unknown"), "pack": row.get("pack", "core"),
                "description": row.get("description", ""), **meta,
            }
        else:
            entry = {
                "func": _no_handler, "phrase": row["phrase"], "aliases": list(row.get("aliases") or []),
                "source": row.get("source", "plugin"), "pack": row.get("pack", "core"),
                "metadata": dict(row.get("metadata") or {}), **meta,
            }
            plugins[row["phrase"]] = entry
            for alias in entry["aliases"]:
                plugins.setdefault(alias, entry)
    matcher = CommandMatcher()
    matcher.load_builtins(builtins)
    with contextlib.redirect_stdout(sys.stderr):
        matcher.load_plugins(plugins)
        matcher.freeze()
    if enabled_packs is not None:
        matcher.set_enabled_packs(set(enabled_packs))
    return matcher


def catalog_from_live_registry() -> list:
    """The live registry as catalog rows (loads every plugin module; no app,
    so plugin services never start). Construction chatter goes to stderr."""
    from tools.dump_command_metadata import build_dump  # noqa: PLC0415
    return build_dump()["commands"]


def catalog_from_file(path) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["commands"] if isinstance(data, dict) else data


def enabled_packs_from_config(config: dict):
    packs = (config or {}).get("command_packs")
    if not isinstance(packs, dict) or not packs:
        return None
    return {name for name, on in packs.items() if on}


def load_config(path=None) -> dict:
    if path is None:
        from samsara.paths import samsara_config_path  # noqa: PLC0415
        path = samsara_config_path()
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Argument grammars: what each resolved command can do with its remainder.
# Each mirrors the plugin's own parser (named in the comment) and returns
# (status, detail, missing, owner).
# ---------------------------------------------------------------------------

def _args_send(remainder: str, config: dict):
    # plugins/commands/windows.py: _parse_send_remainder + _parse_destination
    r = remainder.lower().strip()
    if r.startswith("the "):
        r = r[4:]
    app_name, dest = None, r
    for sep in (" to the ", " to ", " on the ", " on "):
        if sep in r:
            app_part, dest = r.split(sep, 1)
            app_name = None if app_part.strip() in ("", "this") else app_part.strip()
            break
    dest = dest.strip()
    known = "tv" in dest or "here" in dest or any(w.isdigit() for w in dest.split())
    if known:
        return WORKS, f"window '{app_name or 'foreground'}' -> monitor '{dest}'", "", ""
    return (MISSING, f"window '{app_name}' -> destination '{dest}' (unparseable)",
            "destination grammar knows only 'tv', 'here' and 'monitor N' -- not 'left/right screen'; "
            "a compound 'X on the left and Y on the right' is not split into two moves",
            "plugins/commands/windows.py (_parse_destination / handle_send)")


def _args_play_music(remainder: str, config: dict):
    # plugins/commands/music.py: handle_play
    q = remainder.strip().lower()
    if not q or q in ("music", "some music", "something"):
        return PARTIAL, "Spotify Liked Songs (shuffle)", "no playlist named", "plugins/commands/music.py"
    try:
        from plugins.commands.music import SONGS  # noqa: PLC0415
        names = list(SONGS)
    except Exception:
        names = []
    names += list((config.get("music_library") or {}).keys())
    hit = next((n for n in names if n.lower() in q), None)
    if hit:
        return WORKS, f"configured track '{hit}'", "", ""
    return (PARTIAL, f"opens a Spotify web search for '{q}' (nothing plays)",
            "no playlist-by-name path: music.py has fixed SONGS, config music_library, else a browser "
            "search; playing a named playlist needs the Spotify Web API (music.py's own REVISIT note)",
            "plugins/commands/music.py (handle_play)")


def _args_play_resume(remainder: str, config: dict):
    # plugins/commands/music.py: handle_media_play -- SMTC play, remainder ignored
    if remainder.strip():
        return (PARTIAL, "SMTC 'play' to whatever media session is current; the words after 'play' are ignored",
                "'play <anything>' resumes the current media session instead of choosing music; "
                "a playlist name is dropped on the floor",
                "plugins/commands/music.py (handle_play / handle_media_play)")
    return WORKS, "SMTC play on the current media session", "", ""


def _args_focus(remainder: str, config: dict):
    # plugins/commands/app_verbs.py: do_focus resolves against live windows
    if remainder.strip():
        return WORKS, f"focus window/app '{remainder.strip()}' (resolved live)", "", ""
    return MISSING, "focus needs a target", "no target", "plugins/commands/app_verbs.py"


ARGUMENT_GRAMMARS = {
    "send": _args_send,
    "play music": _args_play_music,
    "play": _args_play_resume,
    "focus": _args_focus,
}


# ---------------------------------------------------------------------------
# One utterance through the session-mode layers (no execution)
# ---------------------------------------------------------------------------

@dataclass
class LaneResult:
    kind: str                 # abort | scratch | switch | command | dictation | agent | miss
    detail: str = ""
    phrase: Optional[str] = None
    remainder: str = ""
    exact: bool = False
    new_mode: Optional[SessionMode] = None


def _abort_phrases(config: dict) -> set:
    configured = (config.get("command_mode") or {}).get("abort_phrases", [])
    if isinstance(configured, str):
        configured = [configured]
    return {normalize_utterance(p) for p in (*configured, *GLOBAL_SESSION_EXIT_PHRASES)}


def resolve_in_session(text: str, mode: SessionMode, matcher: CommandMatcher, config: dict) -> LaneResult:
    """Mirror samsara.session_modes.SessionModeManager.dispatch_utterance for
    a latched (toggle) hands-free session, minus the anti-hallucination
    gates and the side effects."""
    if normalize_utterance(text) in _abort_phrases(config):
        return LaneResult("abort", "session exit phrase")
    if is_scratch_that(text):
        return LaneResult("scratch", "unit-of-work stack pop (session_modes._do_scratch_that)")
    switch = match_switch_word(text, current_mode=mode)
    invocations = {_normalize_exact_phrase(p) for p in resolve_ava_invocations(config)}
    if switch is None and match_ava_invocation(text, invocations):
        return LaneResult("switch", "Ava invocation", new_mode=SessionMode.AVA)
    if switch is not None:
        detail = f"switch to {switch.target_mode.value}"
        if switch.payload:
            detail += f" with payload {switch.payload!r}"
        return LaneResult("switch", detail, new_mode=switch.target_mode, remainder=switch.payload or "")

    with contextlib.redirect_stdout(sys.stderr):   # the wash logs its rewrite to stdout
        washed = apply_phonetic_wash(text)
    match = matcher.match_detail(washed)
    if mode is SessionMode.DICTATE:
        # Buffered hands-free lane: only a reserved command that consumes the
        # whole utterance (or a curated prefix command) dispatches; everything
        # else is dictation staged until "end".
        if match_literal_payload(text) is not None:
            return LaneResult("dictation", "literal payload")
        normalized = normalize_utterance(washed)
        prefixed = any(normalized.startswith(p) for p in HANDS_FREE_COMMIT_PREFIXES)
        if match is not None and (not match.remainder or prefixed):
            return LaneResult("command", "hands-free reserved command (commits pending text first)",
                              phrase=match.entry.phrase, remainder=match.remainder, exact=not match.remainder)
        return LaneResult("dictation", "staged as dictation until 'end' (typed into the focused app)")
    if mode is SessionMode.AVA:
        return LaneResult("agent", "sent to Ava as natural language")
    if match is None:
        return LaneResult("miss", "no registered command")
    return LaneResult("command", "command lane", phrase=match.entry.phrase,
                      remainder=match.remainder, exact=not match.remainder)


# ---------------------------------------------------------------------------
# The take, step by step, with a simulated session mode
# ---------------------------------------------------------------------------

def _wake_options(config: dict) -> list:
    ww = config.get("wake_word_config") or {}
    options = [ww.get("phrase", DEFAULT_WAKE_PHRASE), *ww.get("phrase_options", DEFAULT_WAKE_PHRASE_OPTIONS)]
    return list(dict.fromkeys(p for p in options if p))


def _resolve_wake(step: Step, config: dict) -> Resolution:
    options = _wake_options(config)
    hits = [p for p in options if match_wake_phrase(step.utterance, p)[0]]
    cm_mode = (config.get("command_mode") or {}).get("mode", "hold")
    if hits:
        return Resolution(step, f"wake word '{hits[0]}' -> a one-command wake window ({cm_mode} session untouched)",
                          "asleep", PARTIAL,
                          "the wake word opens a short single-command window, not the latched hands-free "
                          "session (that opens on the command-mode toggle tap)",
                          "dictation.py (_process_wake_command / enter_command_mode)")
    return Resolution(step, f"no wake phrase matches (configured: {', '.join(options)})", "asleep", MISSING,
                      "'wake up samsara' is not a wake phrase (needs an openWakeWord model + wake_word_config."
                      "phrase), and even a matching wake word opens a one-command window, not the latched "
                      "hands-free session -- a 'wake word opens the toggle session' bridge does not exist",
                      "dictation.py (_process_wake_command / enter_command_mode) + wake_word_config")


def _resolve_sleep(step: Step, lane: LaneResult, mode: SessionMode, config: dict) -> Resolution:
    if lane.kind == "abort":
        return Resolution(step, "session exit phrase -> exit_command_mode()", "any latched mode", WORKS,
                          live_text=None)
    where = {"dictation": "typed into the focused app as text", "miss": "command miss",
             "command": f"command '{lane.phrase}'", "agent": "sent to Ava"}[lane.kind]
    return Resolution(step, f"in {mode.value}: {where}", "any latched mode", MISSING,
                      "'go to sleep' is not a session exit phrase (those are: "
                      + ", ".join(GLOBAL_SESSION_EXIT_PHRASES)
                      + "); add it to command_mode.abort_phrases in config, or to GLOBAL_SESSION_EXIT_PHRASES",
                      "samsara/session_modes.py (GLOBAL_SESSION_EXIT_PHRASES) / config command_mode.abort_phrases")


def _resolve_correction(step: Step, lane: LaneResult, mode: SessionMode) -> Resolution:
    where = {"dictation": "typed into the focused app as text", "miss": "command miss",
             "command": f"command '{lane.phrase}'", "agent": "Ava teaching intent 'correct that to <word>'"}[lane.kind]
    return Resolution(step, f"in {mode.value}: {where}", "ava (only grammar that knows 'correct that')", MISSING,
                      "no command edits the word just typed: the only 'correct that to Y' grammar is Ava's "
                      "vocabulary teaching (teach_patterns.parse_correction_add), which persists a future "
                      "transcription fix and does not touch the text on screen",
                      "plugins/commands/text_marker.py (select/retype the last word) with the grammar from "
                      "samsara/teach_patterns.py")


def _resolve_command_step(step: Step, lane: LaneResult, mode: SessionMode, matcher: CommandMatcher,
                          config: dict) -> Resolution:
    command_lane = resolve_in_session(step.utterance, SessionMode.COMMAND, matcher, config)
    if command_lane.kind != "command":
        return Resolution(step, "no registered command", "command", MISSING,
                          _missing_for(step), _owner_for(step))
    grammar = ARGUMENT_GRAMMARS.get(command_lane.phrase)
    if grammar is not None:
        status, detail, missing, owner = grammar(command_lane.remainder, config)
    else:
        status, detail, missing, owner = WORKS, f"remainder {command_lane.remainder!r}", "", ""
    resolves = f"'{command_lane.phrase}' ({detail})"
    in_lane = lane.kind == "command" and lane.phrase == command_lane.phrase
    if not in_lane and status != MISSING:
        status = PARTIAL
        missing = (f"in the {mode.value} lane this is {lane.detail}; say 'command mode' first"
                   + (f"; also: {missing}" if missing else ""))
    return Resolution(step, resolves, "command", status, missing, owner,
                      live_text=step.utterance if status != MISSING else None, dispatch_expected=in_lane)


def _missing_for(step: Step) -> str:
    if step.number == 4:
        return ("no 'tell <app> <text>' capability: needs focus-the-app, dictate the text and submit as one "
                "command (today: 'focus claude', 'dictate <text>', 'end', 'submit' -- four utterances)")
    return "no command matches"


def _owner_for(step: Step) -> str:
    if step.number == 4:
        return "plugins/commands/app_verbs.py (focus) + samsara/session_modes.py (dictate lane) -- a new 'tell' verb"
    return "commands.json / plugins"


def _resolve_dictation(step: Step, lane: LaneResult, mode: SessionMode) -> Resolution:
    words = len(step.utterance.split())
    if mode is SessionMode.DICTATE and lane.kind == "dictation":
        return Resolution(step, f"{words} words staged in the dictate lane, committed on 'end'", "dictate", PARTIAL,
                          "needs Obsidian focused ('focus obsidian' works in the lane) and the commit word 'end'; "
                          "'verbatim' is not guaranteed: the commit re-decodes the joined audio and applies "
                          "formatting tokens (verbatim profile exists only for terminals/URL bars)",
                          "samsara/session_modes.py (_commit_dictate_buffer) / docs/VERBATIM_PROFILE.md")
    return Resolution(step, f"in {mode.value}: {lane.detail}", "dictate", PARTIAL,
                      "say 'dictate' first (or prefix the paragraph with 'dictate'), focus Obsidian, "
                      "and finish with 'end'",
                      "samsara/session_modes.py (switch words)")


def _resolve_scratch(step: Step, lane: LaneResult, mode: SessionMode) -> Resolution:
    if lane.kind == "scratch":
        return Resolution(step, "session_modes 'scratch that' -> pops the last unit of work "
                          "(staged action, committed dictation chunk or command)", "any latched mode", WORKS,
                          "", "", dispatch_expected=True)
    return Resolution(step, lane.detail, "any latched mode", MISSING, "scratch that not recognised",
                      "samsara/session_modes.py")


def rehearse(matcher: CommandMatcher, config: dict, entry_mode: SessionMode = SessionMode.DICTATE) -> list:
    """Dry-run the whole take. The latched hands-free session enters in
    DICTATE (dictation.py enter_command_mode -> reset(initial_mode=DICTATE));
    the simulated mode only moves when the script itself contains a switch
    word -- it does not, which is most of the story."""
    mode = entry_mode
    out = []
    for step in TAKE:
        if step.kind == "wake":
            out.append(_resolve_wake(step, config))
            continue
        lane = resolve_in_session(step.utterance, mode, matcher, config)
        if step.kind == "command":
            res = _resolve_command_step(step, lane, mode, matcher, config)
        elif step.kind == "dictation":
            res = _resolve_dictation(step, lane, mode)
        elif step.kind == "correction":
            res = _resolve_correction(step, lane, mode)
        elif step.kind == "scratch":
            res = _resolve_scratch(step, lane, mode)
        elif step.kind == "sleep":
            res = _resolve_sleep(step, lane, mode, config)
        else:  # pragma: no cover
            raise ValueError(step.kind)
        out.append(res)
        if lane.kind == "switch" and lane.new_mode is not None:
            mode = lane.new_mode
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(results: list, catalog_rows: list, entry_mode: SessionMode) -> str:
    counts = {s: sum(1 for r in results if r.status == s) for s in (WORKS, PARTIAL, MISSING)}
    builtin = sum(1 for r in catalog_rows if r.get("source") == "builtin")
    lines = [
        "# Demo rehearsal -- the five-minute take (SAMSARA_VISION.md section 7)",
        "",
        "Dry run through the real dispatch path (session_modes control words, the command registry, "
        "each command's argument grammar). Nothing was executed. Generated by tools/demo_rehearsal.py; "
        "re-run it after every change.",
        "",
        f"Catalog: {len(catalog_rows)} commands ({builtin} builtin, {len(catalog_rows) - builtin} plugin). "
        f"Hands-free session entry lane: {entry_mode.value}.",
        "",
        f"Result: {counts[WORKS]} WORKS, {counts[PARTIAL]} PARTIAL, {counts[MISSING]} MISSING of {len(results)} steps.",
        "",
        "| step | utterance | resolves to | mode required | status | what is missing (owner) |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        utt = r.step.utterance if len(r.step.utterance) <= 60 else r.step.utterance[:57] + "..."
        missing = r.missing + (f" -- **{r.owner}**" if r.owner else "")
        lines.append(f"| {r.step.number} | {_cell(utt)} | {_cell(r.resolves_to)} | {_cell(r.mode_required)} | "
                     f"{r.status} | {_cell(missing)} |")
    lines += [
        "",
        "## Reading the table",
        "",
        "* The latched hands-free session (command-mode toggle tap) enters in the DICTATE lane. In that lane an "
        "utterance only dispatches as a command when it is a reserved exact command (or a curated prefix such as "
        "'focus <x>'); everything else is staged as dictation and typed into the focused app on 'end'. The take "
        "as written contains no switch word, so steps 2-4 and 6 would be typed, not performed.",
        "* 'mode required' is the lane in which the line resolves; 'resolves to' is what the session would "
        "actually do in the lane it is in.",
        "* MISSING names the plugin or module that would own the capability.",
        "",
        "Owners of every MISSING step:",
        "",
    ]
    for r in results:
        if r.status == MISSING:
            lines.append(f"* step {r.step.number}: {r.owner}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --live: the owner runs the resolvable steps for real
# ---------------------------------------------------------------------------

def samsara_is_running() -> bool:
    """A Samsara process other than this one: source (dictation.py) or frozen (Samsara.exe)."""
    cmd = ["powershell", "-NoProfile", "-Command",
           "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne %d -and "
           "($_.CommandLine -match 'dictation\\.py' -or $_.Name -match '^Samsara') } | "
           "Select-Object -ExpandProperty ProcessId" % os.getpid()]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.strip().isdigit() for line in out.splitlines())


def live_preconditions(is_interactive=None, running=None) -> Optional[str]:
    """None when --live may proceed, else the reason it must refuse."""
    interactive = sys.stdin.isatty() if is_interactive is None else is_interactive
    if not interactive:
        return "console is not interactive (stdin is not a TTY) -- --live is for the owner at a keyboard"
    if not (samsara_is_running() if running is None else running):
        return "Samsara is not running -- start it, then rehearse"
    return None


def run_live(results: list, config: dict) -> int:
    """Execute each resolvable step through the real CommandExecutor + execution
    policy, STEP_PAUSE_S apart, and print the chip the app would show. Steps
    that belong to the live app's ears (wake, dictation, correction, sleep)
    are announced for the owner to speak. Imports are local: this path is
    never taken by tests or the dry run."""
    from samsara.commands import CommandExecutor  # noqa: PLC0415
    from samsara.session_modes import outcome_chip  # noqa: PLC0415
    import types  # noqa: PLC0415

    app = types.SimpleNamespace(config=config, play_sound=lambda *_a, **_k: None,
                                command_matching_enabled=True, command_mode_active=True)
    with contextlib.redirect_stdout(sys.stderr):
        executor = CommandExecutor(ROOT / "commands.json", app=app)
    failures = 0
    for r in results:
        print(f"\n[{r.step.number}] {r.step.utterance!r} -> {r.status}: {r.resolves_to}")
        if r.live_text is None:
            print("    (speak this one to the running Samsara now)")
            time.sleep(STEP_PAUSE_S)
            continue
        input("    Enter to execute (Ctrl+C aborts): ")
        result = executor.process_text(r.live_text, app, force_commands=True)
        state = getattr(result, "state", None)
        kind = "command_executed" if result[1] and (state is None or state.value in ("completed", "queued")) \
            else ("command_failed" if result[1] else "command_miss")
        chip = outcome_chip(kind, {"phrase": result[0], "state": getattr(state, "value", "")})
        ok = kind == "command_executed"
        failures += 0 if ok else 1
        print(f"    chip: {chip}  {'PASS' if ok else 'FAIL'}")
        time.sleep(STEP_PAUSE_S)
    print(f"\nlive rehearsal: {len(results) - failures} ok, {failures} failed")
    return 1 if failures else 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", help="catalog JSON (tools/dump_command_metadata.py output) instead of the live registry")
    parser.add_argument("--config", help="config.json to read (default: the app's own)")
    parser.add_argument("--out", default=str(ROOT / "perf_artifacts" / "demo_rehearsal.md"))
    parser.add_argument("--live", action="store_true", help="execute the resolvable steps (owner only)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    rows = catalog_from_file(args.catalog) if args.catalog else catalog_from_live_registry()
    matcher = build_matcher(rows, enabled_packs_from_config(config))
    results = rehearse(matcher, config)
    report = render_markdown(results, rows, SessionMode.DICTATE)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(report, encoding="utf-8")
    sys.stdout.write(report)

    if args.live:
        reason = live_preconditions()
        if reason:
            print(f"\n--live refused: {reason}", file=sys.stderr)
            return 2
        return run_live(results, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
