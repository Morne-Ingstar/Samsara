# Upgrading to beta.5

<!-- Stand-in wording for the owner's review. -->

## Portable beta.4: use the installer once

Use `SamsaraSetup-0.30.0-beta.5.exe`. Keep the portable folder and install into a different folder. A manual install does not import portable edits or create a migration snapshot. Installing over the old folder can overwrite edits. Do not use the old portable updater or a ZIP folder swap for this transition.

1. Finish or save unsent words, then quit the old copy before taking the backup. Do not edit commands while the upgrade is running.
2. Copy the entire old portable folder to a separate backup folder outside either installation. This retains custom plugins, profiles and any older flat-layout files. Keep the original too.
3. Back up the user-data folder separately: `%USERPROFILE%\.samsara`, or the directory specified by `SAMSARA_HOME_DIR` if you use that override. This contains settings, dictionary/training data and saved profiles; it is separate from the program folder. Keep this backup private because settings may contain credentials.
4. Run the beta.5 installer and select a folder different from the old portable folder. The usual per-user destination is `%LOCALAPPDATA%\Programs\Samsara`; an all-users installation normally uses `%ProgramFiles%\Samsara`. If that destination already contains a copy, back it up too before installing. The actual folder selected in the installer takes precedence.
5. Leave the installer’s launch checkbox off. Before the first start, make only the file-copy restores described below while Samsara is closed. Start the new copy, then use its Commands or Profiles Import screen for exported profiles. Check the version and your custom commands/profiles, and keep both backups and the old portable folder until that check succeeds.

## Exact backup locations

Here `OLD` is the folder containing the old `Samsara.exe`; `NEW` is the folder containing the installed beta.5 `Samsara.exe`. These paths apply to both installed and portable frozen copies.

| What | Current frozen layout | Older flat layout |
| --- | --- | --- |
| Edited commands | `OLD\_internal\commands.json` | `OLD\commands.json` |
| Command plugins and their data | `OLD\_internal\plugins\commands\` | `OLD\plugins\commands\` |
| Bundled or edited profiles | `OLD\_internal\profiles\` | `OLD\profiles\` |
| Legacy Settings profile-manager files, if present | `OLD\_internal\samsara\ui\profiles\` | `OLD\samsara\ui\profiles\` |

Back up both sets if both exist. `_internal\commands.json` is the runtime copy when present; a root copy is not a substitute. User-created command/dictionary profiles normally live in `%USERPROFILE%\.samsara\profiles\commands\` and `profiles\dictionaries\` (or under `SAMSARA_HOME_DIR`). Older profile screens can also have written to the legacy Settings path in the table. Copying the whole old program folder plus the whole user-data folder covers these cases without guessing which screen saved a profile.

## Bring customizations back

- **Before first start, with Samsara closed — file copies:** keep a copy of `NEW\_internal\commands.json` before changing it. Merge your added or edited entries from the backed-up runtime `commands.json` into that file, keeping beta.5's new entries. The file is a JSON object keyed by command phrase; preserve the complete entry for each phrase you customized. Do not replace it wholesale with the old file, which would discard new shipped entries. Copy your own extra plugin files and their supporting data into `NEW\_internal\plugins\commands\`, keeping their relative folders. If a filename already ships in beta.5, retain the new file and reapply only your own edits after comparing the backup. Do not copy the entire old plugins tree over the new one. Old plugin files in migration archives are backups, not automatically active plugins.
- **After first launch — in-app Imports:** if you have an exported command profile, open Settings > Commands and use the Profiles Import screen. For exported command or dictionary profiles, open the matching Profiles screen and choose **Import…**. These screens import profile data into the user-data home; they do not restore edits to packaged `commands.json` or plugin files.
- **Profiles stored in package files:** the same user-data home remains in use, so its profiles do not need moving when only the program folder changes. If you need to restore custom profiles by copying their files from the old package, close Samsara first and copy them into the corresponding new location from the table. Preserve beta.5 defaults when names collide. If changing `SAMSARA_HOME_DIR`, restore the backed-up home to that new directory before launch.
- **Check:** confirm beta.5 is running, your custom command works, your own plugin appears, and your saved command/dictionary profiles are listed. If anything is missing, keep the backups and compare paths before deleting or overwriting more files.

## Installed beta.4: normal update path

For a registered per-user installation, turn on **Include beta updates** in Updates, check for updates, and use the offered installer update when the verified installer and its matching checksum are available. Save unsent words and stop editing commands before beginning. This in-app path makes a customization snapshot and beta.5 restores command edits and archives old plugins before confirming success. Saved user-home profiles remain in the same home. Keep an independent backup of the locations above, especially edited packaged profiles, which are not part of the commands/plugins migration.

If Updates offers only an installer link, or the copy is installed for all users or its registration needs repair, this is a manual upgrade: back up the whole existing program folder and user-data home first, run the installer into the existing installed location, leave automatic launch off, and bring customizations back using the instructions above. A manual installer does not create the in-app snapshot. Do not replace an installed folder with a portable ZIP: it contains uninstall information that must be retained.

These are upgrade instructions, not evidence that a real installer trial has passed. The release still needs a disposable manual and in-app upgrade trial with edited commands, plugins and profiles before publication.
