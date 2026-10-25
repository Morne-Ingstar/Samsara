import threading
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QSizePolicy, QWidget

from samsara.ui import settings_qt, theme
from samsara.ui_scale import UI_SCALE_OPTIONS


class _SettingsApp:
    def __init__(self, scale=1.0):
        self.config = {"ui_scale": scale}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(
            commands={}, find_command=lambda phrase: None
        )
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *args, **kwargs):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


def test_general_interface_size_uses_supported_values_and_normal_apply(qapp):
    window = settings_qt._SettingsWindow(_SettingsApp(1.30))
    try:
        combo = window._widgets["ui_scale_combo"]
        assert [combo.itemText(i) for i in range(combo.count())] == list(UI_SCALE_OPTIONS)
        assert combo.currentText() == "Extra large (130%)"

        combo.setCurrentText("Large (115%)")
        general_updates = window._save_fns[0]({})
        assert general_updates["ui_scale"] == 1.15

        all_labels_text = " ".join(
            label.text() for label in window.findChildren(QLabel)
        )
        assert "text, menus, and controls" in all_labels_text
        assert "restart" in all_labels_text.lower()
    finally:
        window.deleteLater()


def test_shared_and_provider_secondary_copy_is_larger_and_higher_contrast(qapp):
    assert 'QLabel[class="description"]' in settings_qt.stylesheet()
    description_rule = settings_qt.stylesheet().split(
        'QLabel[class="description"]', 1
    )[1].split("}", 1)[0]
    # Queue 129: the literal became the token it was approximating.
    assert f"color: {theme.TEXT_SECONDARY}" in description_rule
    assert f"font-size: {theme.TYPE_BODY}px" in description_rule  # 83: body floor

    header_rule = settings_qt.stylesheet().split(
        "QHeaderView::section", 1
    )[1].split("}", 1)[0]
    assert f"color: {theme.TEXT_SECONDARY}" in header_rule
    assert f"font-size: {theme.TYPE_BODY}px" in header_rule  # 83: body floor

    window = settings_qt._SettingsWindow(_SettingsApp())
    try:
        provider_style = window._widgets["cloud_info_label"].styleSheet()
        assert theme.TEXT_SECONDARY in provider_style
        assert f"{theme.TYPE_BODY}px" in provider_style  # 83: body floor
    finally:
        window.deleteLater()


def test_scaled_settings_window_uses_work_area_friendly_minimum_and_refresh_fits(
    qapp,
):
    window = settings_qt._SettingsWindow(_SettingsApp(1.30))
    try:
        # 360: the sidebar grew 180 -> 240, so the minimum grew by the same 60 px.
        assert window.minimumWidth() == 780
        assert window.minimumHeight() == 480

        refresh = window.findChild(QPushButton, "microphoneRefreshButton")
        assert refresh is not None
        assert refresh.minimumWidth() >= refresh.sizeHint().width()
        assert refresh.maximumWidth() > refresh.minimumWidth()
    finally:
        window.deleteLater()


def test_commands_page_scrolls_vertically_without_horizontal_overflow(qapp):
    window = settings_qt._SettingsWindow(_SettingsApp(1.30), tool="Command library")
    try:
        commands_page = window._tool_pages["Command library"]
        assert isinstance(commands_page, QScrollArea)
        assert commands_page.objectName() == "commandsPageScroll"
        assert commands_page.horizontalScrollBarPolicy() == (
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        assert commands_page.verticalScrollBarPolicy() == (
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        # 887: landed 840 removes nested scrolling; the page owns it.
        pack_panel = commands_page.findChild(QWidget, "commandPacksContent")
        assert pack_panel is not None
        assert commands_page.findChild(QScrollArea, "commandPacksScroll") is None
        assert pack_panel.maximumWidth() == 640

        descriptions = [
            label for label in commands_page.findChildren(QLabel)
            if label.objectName().startswith("commandPackDescription_")
        ]
        assert descriptions
        assert all(label.wordWrap() for label in descriptions)
        assert all(
            label.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
            for label in descriptions
        )

        for object_name in (
            "addCommandButton",
            "editCommandButton",
            "deleteCommandButton",
            "testCommandButton",
            "reloadCommandsButton",
        ):
            button = commands_page.findChild(QPushButton, object_name)
            assert button is not None
            assert button.maximumWidth() > button.minimumWidth()

        # Long explanatory sentences must wrap. Unwrapped, a QLabel reports its
        # full single-line width as minimumSizeHint and drags the whole page
        # wider than the window -- which is what put a horizontal scrollbar on
        # this page under any font stack wider than Segoe UI. This is the
        # font-independent half of the overflow guard below.
        content = commands_page.widget()
        long_labels = [
            label for label in content.findChildren(QLabel)
            if len(label.text()) > 40 and label.parent() is content
        ]
        assert long_labels, "expected the page's explanatory labels"
        assert all(label.wordWrap() for label in long_labels), (
            "an explanatory label is unwrapped and will force horizontal overflow: "
            + repr([label.text() for label in long_labels if not label.wordWrap()])
        )

        # Was `window.resize(720, 480)` + `maximum() == 0`. The 720 literal
        # encoded Segoe UI metrics -- it asserted "the page fits inside the
        # app's minimum window width", which is true for Segoe UI and false for
        # a ~30% wider fallback, where the five action buttons alone need
        # ~602px. Derive the width from the page's own content instead, so the
        # invariant under test is "given the width it asks for, this page never
        # scrolls horizontally" rather than a pixel count from one font.
        window.resize(720, 480)
        window.show()
        # 827c: the setup disclosure now belongs to Integrations; Commands
        # must still scroll correctly without it.
        qapp.processEvents()
        # Chrome (sidebar + margins + scrollbar) is only final once shown.
        chrome = window.width() - commands_page.viewport().width()
        required = content.minimumSizeHint().width() + chrome
        window.resize(max(720, required), 480)
        qapp.processEvents()
        assert commands_page.verticalScrollBar().maximum() > 0
        assert commands_page.horizontalScrollBar().maximum() == 0

        window.show_tab('Integrations')
        integrations_page = window._tool_pages['Integrations']
        from samsara.ui.settings.agents_section import AgentsControls
        agents = integrations_page.findChild(AgentsControls)
        agents.help.reveal()
        qapp.processEvents()
        chrome = window.width() - integrations_page.viewport().width()
        required = integrations_page.widget().minimumSizeHint().width() + chrome
        window.resize(max(720, required), 480)
        qapp.processEvents()
        assert integrations_page.verticalScrollBar().maximum() > 0
        assert integrations_page.horizontalScrollBar().maximum() == 0
    finally:
        window.hide()
        window.deleteLater()
