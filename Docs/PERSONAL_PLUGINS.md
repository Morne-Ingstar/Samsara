# Your personal plugins

Your personal version is Daily with your own command files in its personal plugin
folder. You can keep using Daily and add local features without maintaining a fork.

Put Python files in `<Samsara home>/personal_plugins`, outside the source checkout
and installation. The app does not create this folder. The home is selected by
`SAMSARA_HOME_DIR` when set; otherwise it is `~/.samsara`. Use the home selected by
Daily's launcher, rather than assuming that Daily and development share one.

In that home's `config.json`, `plugins.personal_dir` can select another directory:

```json
{"plugins": {"personal_dir": "D:/MyCommands"}}
```

An empty value uses `personal_plugins` in the home. Relative values start at the
home; `~` expands to your user folder. Files load at the next normal app start.

```python
from samsara.plugin_commands import command

@command("example greeting", ai_visible=False)
def greeting(app, remainder):
    return True
```

Say **example greeting** in Command mode after loading this example. It completes
without a visible action; replace the function body with your local action.
A personal phrase or alias cannot replace a shipped command. A collision skips
that entire personal command and logs a collision message without its phrase.
A broken plugin is rolled back, logged and skipped; other files still load.

## Move existing local commands

The runner runs this against the checkout that actually holds your local files,
with the destination set to Daily's personal folder. This is a dry run:

```powershell
python tools/migrate_personal_commands.py --repo . --destination "$env:SAMSARA_HOME_DIR/personal_plugins"
```

Set `SAMSARA_HOME_DIR` to Daily's actual home first. The script requires an explicit
destination and refuses one inside the checkout. It selects `local_*.py` and
`demo_commands.py`, and recognizes retired personal JSON commands by one-way
fingerprints. Add `--plugin filename.py` for another personal file. It lists each
file and command; keep that console output private. After reviewing the plan,
the runner adds `--apply`. Existing destination files are never overwritten.
Original files and catalogs are copied into `migration_backup` under the external
destination before removal. Restore from there manually if an interrupted run
needs recovery. Retired JSON commands become Python wrappers around the existing
handlers; unknown risk defaults to requiring confirmation. New plugins can still
need changes if they depended on relative imports or files in the checkout.

The script cleans matching shipped JSON catalog entries and reference rows.
Regenerate the command catalog from clean source before any release. No migration
runs automatically, and updates do not move your personal files.

## What stays local

Shipped catalog generation excludes personal plugins, even if the current process
has loaded them. Release data manifests exclude `personal_plugins`, `backups`,
`local_*.py` and `demo_commands.py`, including accidentally tracked copies.
Support and incident bundles redact command-bearing diagnostic records and the
configured personal directory. Personal source files and local backup archives
are not included in the support bundle. User backups intentionally include the
default personal folder; they remain local and do not follow configured external
folders. Keep personal files and backups outside the repository.

Plugins are trusted Python running with your permissions. They can write their own
logs or make network calls; the built-in exclusions are not a sandbox for plugin
code. Do not put private text in plugin-authored diagnostic messages.

Give agents this rule: **features for Morne go in personal_plugins, never the repo**.
