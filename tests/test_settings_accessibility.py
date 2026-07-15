import threading
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QSizePolicy

from samsara.ui import settings_qt
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

        restart_hint = window.findChild(QLabel, "uiScaleRestartHint")
        assert restart_hint is not None
        assert "restart" in restart_hint.text().lower()
        assert "text, menus, and controls" in " ".join(
            label.text() for label in window.findChildren(QLabel)
        )
    finally:
        window.deleteLater()


def test_shared_and_provider_secondary_copy_is_larger_and_higher_contrast(qapp):
    assert 'QLabel[class="description"]' in settings_qt.STYLESHEET
    description_rule = settings_qt.STYLESHEET.split(
        'QLabel[class="description"]', 1
    )[1].split("}", 1)[0]
    assert "color: #AEB4C0" in description_rule
    assert "font-size: 13px" in description_rule

    header_rule = settings_qt.STYLESHEET.split(
        "QHeaderView::section", 1
    )[1].split("}", 1)[0]
    assert "color: #AEB4C0" in header_rule
    assert "font-size: 13px" in header_rule

    window = settings_qt._SettingsWindow(_SettingsApp())
    try:
        provider_style = window._widgets["cloud_info_label"].styleSheet()
        assert "#AEB4C0" in provider_style
        assert "13px" in provider_style
    finally:
        window.deleteLater()


def test_scaled_settings_window_uses_work_area_friendly_minimum_and_refresh_fits(
    qapp,
):
    window = settings_qt._SettingsWindow(_SettingsApp(1.30))
    try:
        assert window.minimumWidth() == 720
        assert window.minimumHeight() == 480

        refresh = window.findChild(QPushButton, "microphoneRefreshButton")
        assert refresh is not None
        assert refresh.minimumWidth() >= refresh.sizeHint().width()
        assert refresh.maximumWidth() > refresh.minimumWidth()
    finally:
        window.deleteLater()


def test_commands_page_scrolls_vertically_without_horizontal_overflow(qapp):
    window = settings_qt._SettingsWindow(_SettingsApp(1.30))
    try:
        commands_page = window._stack.widget(
            settings_qt._TAB_NAMES.index("Commands")
        )
        assert isinstance(commands_page, QScrollArea)
        assert commands_page.objectName() == "commandsPageScroll"
        assert commands_page.horizontalScrollBarPolicy() == (
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        assert commands_page.verticalScrollBarPolicy() == (
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        pack_scroll = commands_page.findChild(QScrollArea, "commandPacksScroll")
        assert pack_scroll is not None
        assert pack_scroll.minimumHeight() == 210
        assert pack_scroll.maximumHeight() > pack_scroll.minimumHeight()

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

        window.resize(720, 480)
        window.show()
        window._stack.setCurrentIndex(settings_qt._TAB_NAMES.index("Commands"))
        qapp.processEvents()
        assert commands_page.verticalScrollBar().maximum() > 0
        assert commands_page.horizontalScrollBar().maximum() == 0
    finally:
        window.hide()
        window.deleteLater()
