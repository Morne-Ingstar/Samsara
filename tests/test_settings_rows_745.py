"""Shared Settings rows keep controls anchored to their title line."""
from PySide6.QtWidgets import QComboBox, QPushButton, QWidget, QVBoxLayout

from samsara.ui import theme
from samsara.ui.settings.presentation import SettingRow
from samsara.ui.settings.calm_layout import RowLayout
from tests.test_settings_calm_layout_459 import settings, settle


def test_controls_align_to_title_and_columns_match(qapp):
    host = QWidget()
    host.resize(1000, 600)
    column = QVBoxLayout(host)
    rows = [
        SettingRow("With detail", "A description below the title.", [QPushButton("Change")]),
        SettingRow("", "Description promoted to title.", [QPushButton("Open")]),
        SettingRow("Title only", "", [QPushButton("Choose")]),
    ]
    layouts = [RowLayout(row) for row in rows]
    for row in rows:
        row.setFixedWidth(940)
        column.addWidget(row)
    host.show()
    qapp.processEvents()
    for row in rows:
        title_center = row.heading.mapTo(row, row.heading.rect().center()).y()
        control_center = row.value.mapTo(row, row.value.rect().center()).y()
        assert abs(control_center - title_center) <= 1
    columns = [layout.row.grid.cellRect(0, 2).right() for layout in layouts]
    assert max(columns) - min(columns) <= 1
    host.close()


def test_description_only_row_uses_description_as_title(qapp):
    row = SettingRow("", "Promoted description", [QPushButton("Open")])
    assert row.heading.text() == "Promoted description"
    assert row.detail.text() == ""


def test_audio_has_no_duplicate_consecutive_group_headings(settings, qapp):
    window = settings
    page = window._pages["Audio"]
    settle(qapp)
    headings = [page.base.itemAt(index).widget().text()
                for index in range(page.base.count())
                if page.base.itemAt(index).widget().property("role") == "settingsGroup"]
    assert all(left != right for left, right in zip(headings, headings[1:]))


def test_every_settings_page_has_a_hairline_after_its_header(settings):
    pages = (*settings._pages.values(), *settings._tool_pages.values())
    for page in pages:
        divider = page.findChild(QWidget, "settingsPageDivider")
        assert divider is not None
        assert divider.findChild(QWidget, "settingsPageHairline").height() == 1
        assert page.column.indexOf(divider) < page.column.indexOf(page.base)


def test_row_buttons_match_dropdown_height_on_audio_and_general(settings, qapp):
    for page_name in ("Audio", "General"):
        page = settings._pages[page_name]
        settings._stack.setCurrentWidget(page)
        settle(qapp)
        dropdown = page.findChild(QComboBox)
        assert dropdown is not None
        buttons = (settings._widgets["audio_mic_setup_guide_button"],) if page_name == "Audio" else tuple(
            button for row in page.findChildren(SettingRow)
            for button in row.value.findChildren(QPushButton)
            if button.isVisibleTo(page)
        )
        assert buttons
        face_color = theme.qcolor(theme.BG2)

        def painted_face_height(control):
            # Grab the widget itself so scrolling, page clipping, and DPR
            # coordinate conversion cannot move the sampled face out of view.
            image = control.grab().toImage()
            left = round(image.width() * 0.2)
            right = round(image.width() * 0.8)
            rows = [
                y for y in range(image.height())
                if sum(image.pixelColor(x, y) == face_color for x in range(left, right))
                >= (right - left) * 0.45
            ]
            label = control.text() if isinstance(control, QPushButton) else control.currentText()
            assert rows, (page_name, label, control.geometry(), image.size())
            return max(rows) - min(rows) + 1

        dropdown_face = painted_face_height(dropdown)
        button_faces = [(button.text(), painted_face_height(button)) for button in buttons]
        assert dropdown.height() == theme.HIT_TARGET_MIN
        assert all(abs(face - dropdown_face) <= 2 for _, face in button_faces), (
            page_name, button_faces, dropdown_face
        )
