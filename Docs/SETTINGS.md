# Settings and Tools

Settings has six pages. Names and new explanatory copy are provisional (prompt 285).

| Page | Everyday rows | Advanced details |
|---|---:|---|
| General | 5 | Windows startup, indicator, floating-window recovery, experimental live surface, fixed-key streaming |
| Dictation | 6 | Recognition, wake setup, timing, text formatting, continuous preset and session limits |
| Audio | 7 | Spoken notices, speech engine/voice, interruption policy, ducking and echo cancellation |
| Ava | 4 | Route-specific setup, local models, editing scope, personality and limits |
| Shortcuts | 11 | Command-only, wake and continuous bindings; direct report shortcut; mouse suppression |
| Help | 5 | Optional local benchmark recording |

Choose **Advanced** on the relevant page to reveal less common controls. Search accepts
labels, help text and configuration keys and opens matching advanced sections.
Each question-mark has a 44-pixel target. Hover, keyboard focus or activation reveals
the full explanation; Escape on the question-mark or **Close help** dismisses it.
Only one row explanation is open at a time. Controls also expose accessible names
and descriptions. Dropdowns retain Qt keyboard behavior with a drawn chevron.

**Tools** is available from Settings and the tray. It opens separate windows for
Command library, Alarms, Health, Music, Components and Integrations. Applying a tool
does not apply pending preference edits in the Settings window. Command/alarm record
actions retain their immediate-save behavior; Music and tool preferences use Apply.
Command packs are disclosed within Command library; always-on packs are badges.
Health's voice reference is generated from the loaded command catalog.

Todoist remains opt-in under Integrations. Its token/show/test controls appear only
when enabled; the existing server disclosure remains alongside the opt-in.
Cloud routing and Ava permissions retain their existing consent flow.
**Shortcuts → Corrections → Open** opens correction capture with a report action inside.
The direct report shortcut remains reachable under Shortcuts → Advanced.
Setup/practice includes tutorials, microphone/Ava guides, dictionary profiles.
Private configuration backups remain separate from shareable support diagnostics.

No configuration migration, default changes, new dependencies or engine changes are
part of this restructure. Startup keys remain `launch_at_login` and `start_hidden`.
The experimental live surface remains off by default; continuous capture remains
available as an advanced preset. The 220-J engine/surface freeze is unchanged.

## Implementation and verification

`settings_qt.py` owns the window chrome and six-page registry. Each `*_page.py`
declares semantic rows; `assembly.py` reparents the existing controls once and
retains original factory validation, consent and ordered save callbacks. Legacy
`*_qt.py` modules remain control factories, not additional visible pages.
`presentation.py` owns modern controls and disclosures; `shared_qt.py` and
`services.py` contain shared widgets and controller services.

`tests/test_settings_row_inventory_285.py` checks 151 distinct audited keys,
single destinations, 38 base rows, conditional routing and save isolation.
Record-manager fields normalize to their collection row; repeated preview/actions
are not additional preference editors. The corrected focused gate uses
`tests/test_settings_accessibility.py`, not the nonexistent layout_a11y filename.
Production screenshots are rendered in an isolated temporary Samsara home at
100% and 150%, in both themes. Automated Qt traversal is not a substitute for a
human Windows Narrator listening check.
