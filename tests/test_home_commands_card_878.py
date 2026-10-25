"""878: Home Commands no longer carries an example line."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from samsara.ui import home_qt, main_window_qt
from tests.test_home_qt import _FakeSettings, _app


def test_commands_card_has_no_suggestion_and_fits_240px(qapp, monkeypatch, qt_widget_scope):
    monkeypatch.setattr(home_qt, "QSettings", _FakeSettings)
    window = main_window_qt._MainWindow(_app())
    window._poll_timer.stop()
    window._activate("Home")
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    qapp.processEvents()
    with qt_widget_scope(window):
        home = window._stack.currentWidget()
        card = home._cards[0]
        card.setFixedSize(240, 220)
        card.show()
        qapp.processEvents()

        assert card.width() == 240
        assert card._value.text() == ""
        assert not card._value.isVisibleTo(card)
        assert card.layout().count() == 3  # title, description, packs button; no stretch/value slot
        assert "phrases, including windows, text and music." in card._description.text()
        assert all("Try:" not in label.text() for label in home.findChildren(QLabel))

        buttons = card.findChildren(QPushButton)
        assert [button.text() for button in buttons] == ["Command packs"]
        button = buttons[0]
        description_rect = card._description.geometry()
        assert card.rect().contains(description_rect)
        assert card.rect().contains(button.geometry())
        assert not description_rect.intersects(button.geometry())
        assert button.width() < card.width()
