"""One module per Samsara settings page (queue 21).

Each <page>_qt.py holds a mixin with that page's _build_*_tab and its
page-only helpers, moved verbatim from samsara/ui/settings_qt.py;
settings_qt._SettingsWindow inherits them, so the registry (_TAB_NAMES and
the _stack.addWidget(self._build_*_tab()) calls), the shared widgets, the
card/row helpers and _apply_and_close all stay in settings_qt.

The one textual change: moved code that built repo paths from
Path(__file__) (commands.json, sounds/, dictionaries/, the profiles app
dir) reads Path(_SETTINGS_QT_FILE) -- settings_qt.__file__ -- so every
path still resolves exactly where it did.

Import order: the page modules import shared names FROM settings_qt, and
settings_qt imports the page modules (after those names are defined) to
build _SettingsWindow. Importing settings_qt here first makes either entry
point work: `import samsara.ui.settings.modes_qt` runs this file, which
loads settings_qt completely (it imports modes_qt midway, by which time
everything modes_qt needs from it exists) before modes_qt itself runs.
"""

from samsara.ui import settings_qt as _settings_qt  # noqa: F401 -- import order, see above
