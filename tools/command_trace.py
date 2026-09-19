"""Safely trace common spoken phrases through the registry, policy and executor.

This is a diagnostic, not a command runner: the production matcher, catalog,
risk classifier and CommandExecutor are used, but every effect is replaced by
FakeInjector. It never imports dictation, starts plugin services, injects input
or launches applications. Pass an output path outside the repository.

    python tools/command_trace.py --output C:/.../reports/262/artifacts/command_trace.md
"""
from __future__ import annotations

import argparse
import contextlib
import io
import logging
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
PHRASES = (
    "pause music",
    "play music",
    "pause this",
    "play this",
    "volume up",
    "volume down",
    "set volume 40",
    "open firefox",
    "open notepad",
    "switch window",
    "close window",
    "scroll down",
    "scroll up",
    "copy",
    "paste",
    "undo",
    "show numbers",
    "read that back",
    "scratch that",
    "what can I say",
)


class FakeInjector:
    """Record attempted effects; deliberately has no OS or app integration."""

    def __init__(self):
        self.actions: list[str] = []

    def plugin_call(self, entry, remainder: str) -> bool:
        self.actions.append(
            f"fake plugin call: {entry.phrase} (remainder={remainder!r})")
        return True

    def builtin_call(self, entry):
        injector = self

        class _Handler:
            def execute(self, _command, _context):
                injector.actions.append(f"fake builtin call: {entry.phrase}")
                return True

        return _Handler()


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _outside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return True
    return False


def _load_app_runtime():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # samsara.log's standalone fallback otherwise writes to the user's profile.
    # A root NullHandler tells it this diagnostic process already owns logging.
    if not logging.getLogger().handlers:
        logging.getLogger().addHandler(logging.NullHandler())
    from samsara import command_catalog, commands as commands_module, execution_policy
    from samsara.command_packs import default_pack_config, get_enabled_packs
    from samsara.commands import CommandExecutor

    return (command_catalog, commands_module, execution_policy,
            default_pack_config, get_enabled_packs, CommandExecutor)


def _pause_failure_probe(media_keys) -> str:
    """Prove whether a failed SMTC result can still look successful, no I/O."""
    original_run = media_keys._run_async

    def fail_without_running(coro):
        coro.close()
        return False, "simulated SMTC failure (no session)"

    media_keys._run_async = fail_without_running
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            handler_return = media_keys.handle_pause_this(None, "")
    finally:
        media_keys._run_async = original_run
    executor_state = "completed" if handler_return else "miss"
    return (f"Fake SMTC failure returned `False`, but `handle_pause_this` returned "
            f"`{handler_return}`; the executor adapter therefore treats it as "
            f"`{executor_state}`. This is a false-success path, not proof of the "
            "owner's specific runtime session failure.")


def run(output: Path) -> int:
    if not _outside_repo(output):
        raise ValueError("output must be outside the repository")
    (catalog, commands_module, execution_policy, default_pack_config,
     get_enabled_packs, CommandExecutor) = _load_app_runtime()
    if "dictation" in sys.modules:
        raise RuntimeError("safety check failed: dictation was imported")

    rows = catalog.load_catalog_json()
    if rows is None:
        raise RuntimeError("commands_catalog.json is missing or invalid")
    catalog_by_phrase = {}
    for row in rows:
        for phrase in row.get("aliases", []):
            catalog_by_phrase[catalog.normalize_phrase(phrase)] = row

    config = {"command_packs": default_pack_config()}
    app = SimpleNamespace(config=config, command_mode_active=False,
                          _ava_cmd_generation=0)
    # Construction uses app=None so no plugin services start. Controllers are
    # also replaced before construction: even an accidental real handler would
    # not have a keyboard or mouse controller available.
    commands_module.KeyboardController = lambda: object()
    commands_module.MouseController = lambda: object()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        executor = CommandExecutor(app=None)
    app.command_executor = executor
    executor._matcher.set_enabled_packs(get_enabled_packs(config))
    # No foreground-window probing: these sample phrases are global commands.
    executor._matcher.set_context_provider(lambda: None)

    original_get_handler = commands_module.get_handler
    original_stage = executor._stage_confirmation
    results = []
    injector = FakeInjector()
    try:
        for phrase in PHRASES:
            match = executor._matcher.match_detail(phrase)
            if match is None:
                results.append({
                    "phrase": phrase, "matched": "—", "catalog_risk": "—",
                    "tier": "—", "confirm": "no", "action": "not attempted",
                    "state": "miss",
                })
                continue

            entry = match.entry
            catalog_row = catalog_by_phrase.get(catalog.normalize_phrase(entry.phrase))
            catalog_risk = catalog_row.get("risk", "—") if catalog_row else "missing"
            risk, _reversible, _schema = execution_policy.classify(
                entry.phrase, executor=executor)
            staged = []
            executor._stage_confirmation = lambda _app, _inv, decision: staged.append(decision)
            action_count = len(injector.actions)
            previous_handler = entry.handler
            try:
                if entry.source == "plugin":
                    entry.handler = lambda _app, remainder, e=entry: injector.plugin_call(e, remainder)
                else:
                    commands_module.get_handler = lambda _kind, e=entry: injector.builtin_call(e)
                args = {"remainder": match.remainder} if entry.source == "plugin" else None
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    dispatch = executor.execute_canonical(
                        entry.phrase, app, args=args, source_text=phrase)
            finally:
                entry.handler = previous_handler
                commands_module.get_handler = original_get_handler
                executor._stage_confirmation = original_stage

            attempted = len(injector.actions) > action_count
            confirmation = "yes" if staged or dispatch.detail.get("awaiting_confirmation") else "no"
            action = injector.actions[-1] if attempted else "not attempted"
            results.append({
                "phrase": phrase,
                "matched": f"{entry.phrase} ({entry.source})",
                "catalog_risk": catalog_risk,
                "tier": risk,
                "confirm": confirmation,
                "action": action,
                "state": dispatch.state.value,
            })
    finally:
        commands_module.get_handler = original_get_handler
        executor._stage_confirmation = original_stage

    confirmed = sum(row["confirm"] == "yes" for row in results)
    attempted = sum(row["action"] != "not attempted" for row in results)
    lines = [
        "# Command trace (safe, simulated)",
        "",
        "Generated by `tools/command_trace.py` using the production registry matcher, "
        "checked-in catalog, execution-policy classifier and `CommandExecutor`. "
        "Only fake handlers ran; no keyboard/mouse injection, media request, app launch, "
        "or `dictation` import occurred.",
        "",
        "The 20 sample phrases are drawn from the families named in task 262. The "
        "checked-in registry has no daily-use frequency counts, so this is not a "
        "frequency-ranked top 20. `Would-confirm?` means the executor's yes/no risk "
        "policy; the separate dictation-lane `say no` cancel window is not counted as "
        "a risk confirmation.",
        "",
        "| Phrase | Matched command | Catalog risk | Policy tier | Would-confirm? | Action attempted | State |",
        "|---|---|---|---|---:|---|---|",
    ]
    for row in results:
        lines.append("| " + " | ".join(_cell(row[key]) for key in (
            "phrase", "matched", "catalog_risk", "tier", "confirm", "action", "state")) + " |")
    lines += [
        "",
        f"Summary: {len(results)} phrases; {sum(r['matched'] != '—' for r in results)} matched; "
        f"{confirmed} would require executor confirmation; {attempted} fake actions attempted.",
        "",
        "## Pause failure diagnostic",
        "",
        _pause_failure_probe(__import__("plugins.commands.media_keys", fromlist=["media_keys"])),
        "",
        "For pause, the registered alias resolves to `media_keys.pause_this` and the "
        "plugin declares/catalogs `ui`. Its handler targets the foreground process's "
        "SMTC session (`try_pause_async`), not a synthetic media key. The task report "
        "contains source-line evidence and distinguishes the dictation cancel window "
        "from the risk-policy confirmation path.",
        "",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(results)} rows; {attempted} fake actions; {confirmed} policy confirmations: {output}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="Markdown output path outside the repository")
    args = parser.parse_args(argv)
    try:
        return run(args.output)
    except Exception as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
