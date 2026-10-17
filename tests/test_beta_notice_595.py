"""595: inline support links and content-height, visit-scoped dismissal."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout
from samsara.ui.widgets import beta_notice as notice


def test_beta_only_and_shared_session_hides_later_lazy_pages(qapp):
    session = notice._Session(qapp)
    widgets = [notice.BetaNotice(version='beta.5', session=session) for _ in range(2)]
    stable = notice.BetaNotice(version='1.0', session=session)
    assert all(not widget.isHidden() for widget in widgets) and stable.isHidden()
    widgets[0].close_button.click()
    assert all(widget.isHidden() for widget in widgets)
    later = notice.BetaNotice(version='beta.6', session=session)
    fresh_visit = notice.BetaNotice(version='beta.6', session=notice._Session(qapp))
    assert later.isHidden() and not fresh_visit.isHidden()
    for widget in [*widgets, stable, later, fresh_visit]:
        widget.hide()
        widget.deleteLater()


def test_real_contact_links_and_keyboard_exposure_without_underlines(qapp, monkeypatch):
    opened = []
    monkeypatch.setattr(notice.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    widget = notice.BetaNotice(version='beta')
    assert widget.message.accessibleName() == notice.NOTICE_TEXT
    assert 'title="Morne"' in widget.message.text()
    assert 'text-decoration: none' in widget.message.text()
    assert 'underline' not in widget.message.text() + widget.message.styleSheet()
    assert widget.message.focusPolicy() == Qt.FocusPolicy.StrongFocus
    for url in (notice.DISCORD_INVITE_URL, 'mailto:me@morneis.com'):
        widget.message.linkActivated.emit(url)
    assert opened == ['https://discord.gg/tPYvf369hQ', 'mailto:me@morneis.com']
    widget.hide()
    widget.deleteLater()


def test_notice_grows_only_when_text_wraps(qapp):
    host = QWidget()
    column = QVBoxLayout(host)
    column.setContentsMargins(0, 0, 0, 0)
    widget = notice.BetaNotice(version='beta')
    column.addWidget(widget)
    column.addStretch()
    host.resize(900, 300)
    host.show()
    qapp.processEvents()
    wide = widget.height()
    assert wide <= widget.message.fontMetrics().height() + 16
    host.resize(600, 300)
    qapp.processEvents()
    assert widget.height() > wide
    assert widget.height() <= widget.message.heightForWidth(widget.message.width()) + 16
    host.hide()
    host.deleteLater()
