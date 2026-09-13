"""Print the live command registry's metadata as JSON.

The registry (samsara.command_registry.CommandMatcher, built by the production
CommandExecutor from commands.json plus every discovered plugin) is the ONE
owner of command metadata. This tool exports it; there is no second catalog
file to keep in sync.

Every field in command_registry.METADATA_FIELDS is reported verbatim as the
command's author declared it, or as "unknown" when it was never declared --
never defaulted to safe. Nothing here is enforced.

Usage:
    python tools/dump_command_metadata.py            # full JSON to stdout
    python tools/dump_command_metadata.py --summary  # counts only
"""
import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@contextlib.contextmanager
def _stdout_to_stderr():
    """Redirect file descriptor 1 as well as sys.stdout: logging handlers
    created at import time hold their own reference to the console."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with contextlib.redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def build_dump():
    from samsara.command_registry import METADATA_FIELDS, UNKNOWN
    from samsara.commands import CommandExecutor

    # Construction chatter ([OK] Loaded ..., [REGISTRY] ..., and log handlers
    # bound to the real stdout) goes to stderr so stdout stays parseable JSON.
    # No app: plugin services are not started.
    with _stdout_to_stderr():
        executor = CommandExecutor(ROOT / "commands.json")
    commands = [
        {
            "phrase": row["phrase"],
            "source": row["source"],
            "type": row["type"],
            "pack": row["pack"],
            "aliases": row["aliases"],
            "description": row["description"],
            "metadata": row["metadata"],
        }
        for row in executor._matcher.list_commands()
    ]
    commands.sort(key=lambda c: (c["source"], c["phrase"]))

    unknown_by_field = {
        name: sum(1 for c in commands if c["metadata"].get(name) == UNKNOWN)
        for name in METADATA_FIELDS
    }
    total_values = len(commands) * len(METADATA_FIELDS)
    unknown_values = sum(unknown_by_field.values())
    summary = {
        "commands": len(commands),
        "builtin": sum(1 for c in commands if c["source"] == "builtin"),
        "plugin": sum(1 for c in commands if c["source"] == "plugin"),
        "metadata_fields": list(METADATA_FIELDS),
        "metadata_values": total_values,
        "declared_values": total_values - unknown_values,
        "unknown_values": unknown_values,
        "unknown_by_field": unknown_by_field,
    }
    return {"summary": summary, "commands": commands}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", action="store_true", help="print only the summary counts")
    args = parser.parse_args(argv)
    dump = build_dump()
    out = dump["summary"] if args.summary else dump
    json.dump(out, sys.stdout, indent=2, ensure_ascii=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
