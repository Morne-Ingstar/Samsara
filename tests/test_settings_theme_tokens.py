"""settings_qt.py draws from theme.py tokens, not its own palette (16).

Two checks:
  * a source scan: the only ``#rrggbb`` literals left in the settings window
    source -- settings_qt.py plus its per-page modules under
    samsara/ui/settings/ (split out in 21) -- are the documented exceptions,
    at their documented counts;
  * an offscreen smoke: the window builds with the token-substituted
    stylesheet, every tab is shown, and no widget stylesheet still carries
    an old-palette colour. No pixel comparison.
"""

import re
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QWidget

from samsara.ui import settings_qt, theme

_SOURCE = Path(settings_qt.__file__)
# 21 moved each page into samsara/ui/settings/<page>_qt.py; the scan covers
# the window as a whole, so the counts below are unchanged by the split.
_SOURCES = (_SOURCE, *sorted((_SOURCE.parent / "settings").glob("*.py")))
_HEX_RE = re.compile(r"#([0-9a-fA-F]{6})\b")

# Reported exceptions (see reports/16_code.md), upper-cased, with counts.
#   #AEB4C0 / #D7D9DE: TEXT_SECONDARY is rgba, no hex form -- owner said leave.
#   #1E1E24: hotkey-button hover, one step above BG2; theme has no BG3.
#   #55555C: sidebar group header caption; TEXT_DISABLED vs ICON_IDLE is a
#            judgement call, so it was left for the owner.
#: Queue 129 moved the last four literals in the Settings sources onto
#: tokens (#AEB4C0 -> TEXT_SECONDARY, #55555C -> TEXT_SECONDARY,
#: #1E1E24 -> BG1, #D7D9DE -> TEXT_PRIMARY), so nothing is left. The
#: whole-app version of this check is tests/test_colour_tokens.py.
EXPECTED_LEFTOVERS: dict[str, int] = {}

# The retired private palette -- must not appear anywhere any more.
OLD_PALETTE = (
    "#8A8A92", "#E8E8EA", "#5EEAD4", "#4DD8C2", "#0A0A0B", "#16161A",
    "#111114", "#161B24", "#FF6666", "#FF8888", "#E0A030", "#E89020",
    "#E2A030", "#D9B86C", "#C0392B",
)


class _StubApp:
    def __init__(self):
        self.config = {}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(commands={}, find_command=lambda p: None)
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *a, **k):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


def _source_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _SOURCES)


def _leftovers() -> Counter:
    src = _source_text()
    return Counter(h.upper() for h in _HEX_RE.findall(src))


class TestSourceScan:
    def test_scan_covers_the_page_modules(self):
        names = {path.name for path in _SOURCES}
        assert "settings_qt.py" in names
        assert {"general_qt.py", "modes_qt.py", "advanced_qt.py", "help_qt.py"} <= names

    def test_only_the_reported_literals_remain(self):
        assert dict(_leftovers()) == EXPECTED_LEFTOVERS

    def test_old_palette_is_gone_from_source(self):
        src = _source_text().upper()
        present = [c for c in OLD_PALETTE if c in src]
        assert present == []

    def test_tokens_are_referenced_not_copied(self):
        """The swap must reference theme.*, not paste the token values in."""
        src = _source_text().lower()
        for value in (theme.BG0, theme.BG1, theme.BG2, theme.ACCENT, theme.ERROR,
                      theme.WARNING, theme.ICON_IDLE, theme.TEXT_PRIMARY):
            assert value.lower() not in src, value


class TestStylesheet:
    def test_stylesheet_is_substituted_from_tokens(self):
        qss = settings_qt.stylesheet()
        assert "${" not in qss                       # every Template placeholder resolved
        for token in (theme.BG0, theme.BG1, theme.BG2, theme.TEXT_PRIMARY,
                      theme.ACCENT, theme.ACCENT_HOVER, theme.TEXT_ON_ACCENT):
            assert token in qss, token
        upper = qss.upper()
        assert not any(c in upper for c in OLD_PALETTE)

    def test_accent_is_the_theme_cyan_not_the_old_teal(self):
        """The visible change: settings used to be teal (#5EEAD4)."""
        assert theme.ACCENT.lower() != "#5eead4"
        assert f"color: {theme.ACCENT};" in settings_qt.stylesheet()
        assert f"background-color: {theme.ACCENT};" in settings_qt.stylesheet()

    def test_hotkey_button_styles_use_tokens(self):
        assert theme.BG2 in settings_qt._HotkeyButton._idle_qss()
        assert theme.TEXT_PRIMARY in settings_qt._HotkeyButton._idle_qss()
        assert theme.ACCENT in settings_qt._HotkeyButton._capturing_qss()


class TestOffscreenSmoke:
    def test_every_tab_renders_with_tokens(self, qapp):
        window = settings_qt._SettingsWindow(_StubApp())
        try:
            assert theme.ACCENT in window.styleSheet()
            for i, name in enumerate(settings_qt._TAB_NAMES):
                window._stack.setCurrentIndex(i)      # show_tab() would show() a window
                qapp.processEvents()
                assert window._stack.currentIndex() == i, name

            offenders = []
            for w in window.findChildren(QWidget):
                qss = w.styleSheet()
                if not qss:
                    continue
                up = qss.upper()
                hits = [c for c in OLD_PALETTE if c in up]
                if hits:
                    offenders.append((w.objectName() or type(w).__name__, hits))
            assert offenders == []

            # the swap reached the per-widget stylesheets, not only STYLESHEET
            tokens = (theme.ICON_IDLE, theme.TEXT_PRIMARY, theme.ACCENT)
            touched = [
                w for w in window.findChildren(QWidget)
                if any(t in w.styleSheet() for t in tokens)
            ]
            assert len(touched) > 20
        finally:
            window.deleteLater()
