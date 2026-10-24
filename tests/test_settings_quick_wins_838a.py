import ast
from pathlib import Path

from PySide6.QtWidgets import QComboBox


REPO = Path(__file__).resolve().parents[1]


def test_settings_qt_labels_do_not_contain_double_ampersands():
    for path in (REPO / "samsara" / "ui" / "settings").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            if not (isinstance(call.func, ast.Name) and call.func.id == "QLabel"):
                continue
            for arg in call.args:
                for value in ast.walk(arg):
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        assert "&&" not in value.value, f"{path}: QLabel contains &&"


def test_selected_settings_segment_has_theme_fill_and_twelve_pixel_padding(qapp):
    from samsara.ui import theme
    from samsara.ui.settings.calm_controls import SegmentedChoice

    combo = QComboBox()
    combo.addItems(["Duck", "Pause", "Leave alone"])
    choice = SegmentedChoice(combo)
    selected = choice.group.checkedButton()

    assert selected is choice.buttons[0]
    assert selected.horizontal_padding >= 12
    color = theme.qcolor(selected.selected_fill)
    assert color.isValid() and color.alpha() > 0
    choice.deleteLater()


def test_health_hides_apply_and_all_footers_say_cancel(qapp, monkeypatch):
    from samsara import autostart
    from samsara.ui.settings import tools_page
    from samsara.ui.settings_qt import PAGE_MODULES, _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults

    monkeypatch.setattr(autostart, "sync_run_entry", lambda *args, **kwargs: None)
    window = _SettingsWindow(_StubApp(_schema_defaults()))
    try:
        for module in PAGE_MODULES:
            stack_index = PAGE_MODULES.index(module)
            sidebar_row = next(row for row, index in window._sidebar_row_to_stack_index.items()
                               if index == stack_index)
            window._sidebar.setCurrentRow(sidebar_row)
            assert window._close_button.text() == "Cancel"

        help_index = next(i for i, module in enumerate(PAGE_MODULES)
                          if module.TITLE == "Help")
        help_row = next(row for row, index in window._sidebar_row_to_stack_index.items()
                        if index == help_index)
        window._sidebar.setCurrentRow(help_row)
        # Help now contains editable update/diagnostics settings that require Apply.
        assert not window._apply_button.isHidden()

        for name in tools_page.TOOLS:
            window._select_tool(name)
            assert window._close_button.text() == "Cancel"
            if name == "Health":
                assert window._apply_button.isHidden()
            else:
                assert not window._apply_button.isHidden()
    finally:
        window.deleteLater()
