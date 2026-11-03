"""Move explicitly personal command files to an external folder; dry-run by default.

Parses Python without importing it. Run locally: its console plan contains private
names and must not be attached to a public issue or a release report.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
from pathlib import Path

# One-way signatures of commands removed in 881, never their private wording.
RETIRED = frozenset({
    '03603896f1aefc3ca30a6ab13adb8b03e379cbc052d9b57282154cda57a06aab',
    '055c8d859be435859407994c37fb1aa0496cb19533bc61036f36af9c75c3c32f',
    '14171ef6f2aa501c5867e80c98b03ccf515325eaa968a8f39142472c112f9913',
    '2ac02b7ec9a5e31539a46b97c25fd02666794dc5f8f9d1a1e048194f2bfd2a38',
    '430cff51878f0b6c28e740a956046022dd42bbcf4f10dcbd924ee98db4438119',
    '4bf56c06b192bf101d1aa0c7398a0a8c8bf314966eeec2f8a0e88d7f040ecd0c',
    '4c8d06b7fd86cb3f4ee58af5dabe0032b187c52c13b5fe541fdbfef16de0e018',
    '54f81f9669136a0c23c02fc68d85ac649a4cec21600856b80821a9efe0c0ff1a',
    '5d9312f1f3b77168a0689721fa2b5a39f5ea328573764162d63ebd38a90ef8bf',
    '6b53baca66d11d2ef621c64c62edf75cbf8501cabaaaa060d9f4441f6f0eda4d',
    '88148b5549f1d016aedfe3c6ec37e1157346772cd0468d603aa93b34ceaa1f53',
    'a301567691c986379139fae81d6fe8452537dd9dbe7808fd9003741116ba0b19',
    'a43eea4ca256f4f15a7008f28238e16c81c9bfc392d22c8ce5084c176339acff',
    'a4f1c653f9815f945efa9bef934e36fb8a41288402eb92b37c3e9e097f10caf7',
    'b46194ea272eaa5bac03752f677894fc6d103964fed18150ba727b00d2af51ea',
    'c77f64bc9928c9d945b68f490a035f1bfec88d53396782c9eaacb38e54579f33',
    'ce20b114480908fe7ae472fc02b47493109829336a4dbc46fcec15693bd9b5a2',
    'dc63c8663546ca3671536c7d44495c3df5fbbf9a753926b6addc1597fc5dd98e',
    'e1256d8dae4e33603841c89c256b2c497b7f6fb39388e2f29ef019ecb42ed75f',
    'fb91bfb1dfcd61a454942be24522a7dc85adf2006241f09ecb3810555d31141b'
})


def fingerprint(text):
    return hashlib.sha256(' '.join(re.findall(r'[a-z0-9]+', text.lower())).encode()).hexdigest()


def plugin_phrases(path):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    phrases = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call) and getattr(deco.func, 'id', getattr(deco.func, 'attr', '')) == 'command':
                if not deco.args:
                    raise ValueError('A selected plugin has a dynamic command declaration')
                phrase = ast.literal_eval(deco.args[0])
                if not isinstance(phrase, str):
                    raise ValueError('A selected plugin has a non-text command')
                phrases.append(phrase)
    return phrases


def migrate(repo, destination, *, apply=False, plugins=()):
    repo, destination = Path(repo).resolve(), Path(destination).expanduser().resolve()
    if destination == repo or repo in destination.parents:
        raise ValueError('Personal destination must be outside the repository')
    folder = repo / 'plugins' / 'commands'
    selected = set(folder.glob('local_*.py')) | set(folder.glob('demo_commands.py'))
    for name in plugins:
        if Path(name).name != name or not name.endswith('.py'):
            raise ValueError('--plugin must be a plain Python filename')
        selected.add(folder / name)
    selected = sorted(selected)
    for path in selected:
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(repo)):
            raise ValueError('Selected plugin must be an ordinary existing file')
        if (destination / path.name).exists():
            raise ValueError('Destination already exists; refusing to overwrite')
    phrases = [phrase for path in selected for phrase in plugin_phrases(path)]
    command_path = repo / 'commands.json'
    for target in (command_path, repo / 'commands_catalog.json', repo / 'Docs' / 'COMMAND_CATALOG.md'):
        if not target.resolve().is_relative_to(repo):
            raise ValueError('Catalog path escapes the repository')
    original = json.loads(command_path.read_text(encoding='utf-8-sig'))
    removed = {phrase: data for phrase, data in original.get('commands', {}).items()
               if any(fingerprint(p) in RETIRED for p in [phrase, *data.get('aliases', [])])}
    phrases += list(removed)
    if removed and (destination / 'migrated_commands.py').exists():
        raise ValueError('Migrated command destination already exists')
    updates = {}
    if removed:
        clean = dict(original)
        clean['commands'] = {p: d for p, d in original['commands'].items() if p not in removed}
        updates[command_path] = json.dumps(clean, ensure_ascii=False, indent=2) + '\n'
    catalog = repo / 'commands_catalog.json'
    removed_ids = set()
    if catalog.exists():
        data = json.loads(catalog.read_text(encoding='utf-8-sig'))
        records = data['commands']
        def private(row):
            source = row.get('source', '').replace('\\', '/')
            return (any(p in phrases for p in row.get('aliases', []))
                    or any(fingerprint(p) in RETIRED for p in row.get('aliases', []))
                    or any(path.relative_to(repo).as_posix() in source for path in selected))
        kept = [row for row in records if not private(row)]
        removed_ids = {row['canonical_id'] for row in records if private(row)}
        if len(kept) != len(records):
            data['commands'] = kept
            updates[catalog] = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    markdown = repo / 'Docs' / 'COMMAND_CATALOG.md'
    if removed_ids and markdown.exists():
        updates[markdown] = ''.join(line for line in markdown.read_text(encoding='utf-8').splitlines(keepends=True)
                                   if not any(cid in line for cid in removed_ids))
    for path in selected:
        print(f'MOVE {path.name}')
    for phrase in phrases:
        print(f'COMMAND {phrase}')
    for path in updates:
        print(f'CLEAN {path.relative_to(repo)}')
    if not apply or not (selected or updates):
        return len(phrases)
    destination.mkdir(parents=True, exist_ok=True)
    # Keep all original bytes outside the tree before any removal. A second
    # application refuses existing targets rather than overwriting local work.
    backup = destination / 'migration_backup'
    if backup.exists():
        raise ValueError('Migration backup already exists; inspect it before retrying')
    backup.mkdir()
    for path in [*selected, *updates]:
        target = backup / path.relative_to(repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for path in selected:
        with (destination / path.name).open('xb') as target:
            target.write(path.read_bytes())
    if removed:
        source = ('from samsara.plugin_commands import command\n'
                  'from samsara.handlers import get_handler\n\n'
                  'def register(phrase, data):\n'
                  '    @command(phrase, aliases=data.get("aliases", []), pack=data.get("pack", "core"), ai_visible=False, risk_class=data.get("risk_class", "destructive"))\n'
                  '    def run(app, remainder):\n'
                  '        handler = get_handler(data.get("type"))\n'
                  '        if handler is None or app is None:\n'
                  '            return False\n'
                  '        return handler.execute(data, app.command_executor._build_context(app))\n\n')
        source += '\n'.join(f'register({p!r}, {d!r})' for p, d in removed.items()) + '\n'
        with (destination / 'migrated_commands.py').open('x', encoding='utf-8') as target:
            target.write(source)
    for path, content in updates.items():
        path.write_text(content, encoding='utf-8')
    for path in selected:
        path.unlink()
    return len(phrases)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--plugin', action='append', default=[])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    migrate(args.repo, args.destination, apply=args.apply, plugins=args.plugin)
    print('Migration applied.' if args.apply else 'Dry run only; add --apply after reviewing the plan.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
