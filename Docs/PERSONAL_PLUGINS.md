# Personal command plugins

Put your own Python command files in `personal_plugins` inside your Samsara
home folder (normally `~/.samsara`; Daily uses its own home folder).
Keep this folder outside the source checkout and installation directory.
These files are not bundled with releases or replaced by app updates.
Samsara does not create the folder; an absent or empty folder changes nothing.

To use another location, set `plugins.personal_dir` in your home `config.json`:
`{"plugins": {"personal_dir": "D:/MyCommands"}}`.
An empty value uses the default; relative paths start at the Samsara home.

Create `example.py` in that folder, then restart Samsara:

```python
from samsara.plugin_commands import command

@command("example greeting")
def greeting(app, remainder):
    """A synthetic example command."""
    return True
```

Personal commands use the same decorator and registry as bundled plugins,
with `source='personal'`. A phrase or alias collision skips the entire personal
command; existing commands win. Import failures are logged and skipped.
Use only Python code you trust: plugins run with your user permissions.
Use Python modules available in your Samsara installation.
