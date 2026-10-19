"""Print plugin import tracebacks without preventing the subsequent CI tests."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
import traceback


ROOT = Path(__file__).resolve().parents[1]


def plugin_modules(root):
    """Use the loader's public *.py rule for plugins and its command folders."""
    plugins = Path(root) / 'plugins'
    directories = [plugins, *sorted(path for path in plugins.iterdir() if path.is_dir())]
    for directory in directories:
        if directory.name.startswith('_'):
            continue
        for path in sorted(directory.glob('*.py')):
            if not path.name.startswith('_'):
                yield '.'.join(path.relative_to(root).with_suffix('').parts)


def main(root=ROOT):
    sys.path.insert(0, str(root))
    try:
        from samsara import plugin_commands

        def command_count(name):
            # Count active canonical entries, not their alias keys or the
            # registry delta (another plugin may have imported this one).
            return len({id(entry) for entry in plugin_commands._REGISTRY.values()
                        if entry['source'] == name})

        for name in plugin_modules(root):
            try:
                importlib.import_module(name)
            except BaseException:
                print(f'FAIL {name} commands={command_count(name)}', flush=True)
                traceback.print_exc(file=sys.stdout)
            else:
                print(f'OK {name} commands={command_count(name)}', flush=True)
    except BaseException:
        print('FAIL plugin discovery', flush=True)
        traceback.print_exc(file=sys.stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
