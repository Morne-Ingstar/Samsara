"""Generate commands_catalog.json and docs/COMMAND_CATALOG.md from the live
command registry (samsara/command_catalog.py). Deterministic: re-running on
an unchanged tree is byte-identical.

Usage:
    F:\\envs\\sami\\python.exe tools\\gen_command_catalog.py                # write both files
    F:\\envs\\sami\\python.exe tools\\gen_command_catalog.py --check        # exit 1 if files are stale
    F:\\envs\\sami\\python.exe tools\\gen_command_catalog.py --write-collisions
        # (re)freeze tests/command_catalog_known_collisions.txt -- only after
        # you have looked at the new collision and accepted it
    F:\\envs\\sami\\python.exe tools\\gen_command_catalog.py --report       # counts, collisions, near-collisions
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from samsara import command_catalog as cc  # noqa: E402

JSON_PATH = ROOT / "commands_catalog.json"
MD_PATH = ROOT / "docs" / "COMMAND_CATALOG.md"
COLLISIONS_PATH = ROOT / "tests" / "command_catalog_known_collisions.txt"


def generate():
    from tools.dump_command_metadata import build_executor  # noqa: PLC0415
    executor = build_executor()
    specs = cc.build_catalog(executor)
    claims = cc.raw_phrase_claims(executor)
    return specs, claims


def report(specs, claims) -> str:
    by_risk = {}
    for s in specs:
        by_risk[s.risk] = by_risk.get(s.risk, 0) + 1
    colls = cc.collisions(claims)
    orphaned = cc.orphans(claims, specs)
    near = cc.near_collisions(specs)
    most = sorted(specs, key=lambda s: (-len(s.aliases), s.canonical_id))[:10]
    implied = [s for s in specs if s.kind == "plugin" and s.args and not _declared(s)]
    out = [
        f"commands: {len(specs)} (builtin {sum(1 for s in specs if s.kind == 'builtin')}, "
        f"plugin {sum(1 for s in specs if s.kind == 'plugin')})",
        f"aliases (phrases): {sum(len(s.aliases) for s in specs)}",
        "per risk: " + ", ".join(f"{k}={by_risk.get(k, 0)}" for k in cc.RISKS),
        f"collisions: {len(colls)}",
        *[f"  {p}\t{' '.join(ids)}" for p, ids in colls],
        f"orphaned phrases (dropped by the registry because their command's canonical phrase collided): {len(orphaned)}",
        *[f"  {p}\t{' '.join(ids)}" for p, ids in orphaned],
        f"near-collisions (Whisper confusions): {len(near)}",
        *[f"  {a!r} ~ {b!r}: {x} vs {y}" for a, b, x, y in near],
        f"aliases containing a Whisper-confusable token: {len(cc.confusable_aliases(specs))}",
        *[f"  {a!r}: {tok} ~ {'/'.join(alts)}" for a, tok, alts in cc.confusable_aliases(specs)],
        "most aliases:",
        *[f"  {len(s.aliases):3d}  {s.canonical_id}" for s in most],
        f"phrase implies an argument the handler does not declare: {len(implied)}",
        *[f"  {s.canonical_id}: {', '.join(a.name + ':' + a.type for a in s.args)}" for s in implied],
    ]
    return "\n".join(out)


def _declared(spec) -> bool:
    # inferred args carry the hint/remainder names; a declared param_schema
    # is recorded on the registry entry, which build_catalog consumed. The
    # catalog itself does not keep that bit, so recover it from the source.
    from samsara import plugin_commands  # noqa: PLC0415
    for entries in plugin_commands._MODULE_ENTRIES.values():
        for phrase, entry in entries.items():
            if f"{entry['source'].rsplit('.', 1)[-1]}.{cc.slug(phrase)}" == spec.canonical_id:
                return bool(entry.get("param_schema"))
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-collisions", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)

    specs, claims = generate()
    json_text = cc.dumps(cc.to_document(specs))
    md_text = cc.render_markdown(specs)
    problems = cc.validate_catalog(cc.to_document(specs))
    if problems:
        print("catalog invalid:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2

    if args.report:
        print(report(specs, claims))
        return 0
    if args.check:
        stale = []
        if not JSON_PATH.exists() or JSON_PATH.read_text(encoding="utf-8") != json_text:
            stale.append(str(JSON_PATH))
        if not MD_PATH.exists() or MD_PATH.read_text(encoding="utf-8") != md_text:
            stale.append(str(MD_PATH))
        print("stale: " + ", ".join(stale) if stale else "catalog up to date")
        return 1 if stale else 0

    JSON_PATH.write_text(json_text, encoding="utf-8", newline="\n")
    MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    MD_PATH.write_text(md_text, encoding="utf-8", newline="\n")
    print(f"wrote {JSON_PATH.name} ({len(specs)} commands) and {MD_PATH.relative_to(ROOT).as_posix()}")
    if args.write_collisions:
        colls, orphaned = cc.collisions(claims), cc.orphans(claims, specs)
        COLLISIONS_PATH.write_text(cc.format_collisions(colls, orphaned), encoding="utf-8", newline="\n")
        print(f"froze {len(colls)} collisions + {len(orphaned)} orphaned phrases in "
              f"{COLLISIONS_PATH.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
