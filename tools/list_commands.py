import json
from pathlib import Path

# Load built-in commands
with open(r'C:\Users\Morne\Projects\Samsara-dev\commands.json') as f:
    cmds = json.load(f)['commands']

# Load plugin commands
import importlib.util, sys
sys.path.insert(0, r'C:\Users\Morne\Projects\Samsara-dev')

plugin_dir = Path(r'C:\Users\Morne\Projects\Samsara-dev\plugins\commands')
plugin_phrases = []
for py in plugin_dir.glob('*.py'):
    if py.name.startswith('_'):
        continue
    with open(py) as f:
        content = f.read()
    # Extract @command phrases
    import re
    for m in re.finditer(r'@command\("([^"]+)"(?:,\s*aliases=\[([^\]]*)\])?', content):
        phrase = m.group(1)
        aliases = []
        if m.group(2):
            aliases = [a.strip().strip('"\'') for a in m.group(2).split(',')]
        plugin_phrases.append((phrase, aliases, py.stem))

print("=== BUILT-IN COMMANDS ===")
for name in sorted(cmds.keys()):
    print(f"  {name}")

print(f"\nTotal built-in: {len(cmds)}")

print("\n=== PLUGIN COMMANDS ===")
for phrase, aliases, source in sorted(plugin_phrases):
    alias_str = f" (aliases: {', '.join(aliases)})" if aliases else ""
    print(f"  [{source}] {phrase}{alias_str}")

print(f"\nTotal plugin phrases: {len(plugin_phrases)}")
