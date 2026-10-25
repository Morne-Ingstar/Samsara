"""843 / 861: Home's measured layout and its real host geometry."""
import itertools
from datetime import datetime

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QWidget

from samsara import recent_privacy
from samsara.ui import home_qt, home_signals, theme
from tests.test_home_qt import _FakeSettings, _app, _hub, HINT


def settle(qapp):
    for _ in range(60):
        qapp.processEvents()


@pytest.fixture(autouse=True)
def isolate(monkeypatch, qapp):
    old_font = QFont(qapp.font())
    old_theme = theme.active_theme()
    qapp.setFont(QFont("Segoe UI", 9))
    _FakeSettings.values = {home_qt.WHATS_NEW_SEEN_KEY: "999.0.0"}
    monkeypatch.setattr(home_qt, "QSettings", _FakeSettings)
    monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: None)
    monkeypatch.setattr(home_signals, "corrections_waiting", lambda app: 0)
    monkeypatch.setattr(home_signals, "_user_words_exist", lambda: True)
    monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
    monkeypatch.setattr(recent_privacy, "sharing_detected", lambda: False)
    yield
    qapp.setFont(old_font)
    theme.set_theme(old_theme, refresh=False)


def host(qapp, width=900, height=650, rows=0):
    app = _app()
    # Outcome-only synthetic rows; never put dictated text in proof artifacts.
    app.history_store.query = lambda **kw: [dict(id=i, timestamp=datetime.now().isoformat(),
        raw_text="", display_text="", status="success", entry_type="command") for i in range(rows)]
    window = _hub(qapp, app=app, width=width, height=height)
    # Exercise the existing screen clamp's hard upper bound, including below 700 DIP.
    window.setFixedSize(width, height)
    settle(qapp)
    return window, window._panel_cache["Home"]


def test_version_is_in_sidebar_above_settings(qapp):
    window, home = host(qapp)
    try:
        assert home.findChild(QLabel, "runningVersion") is None
        version = window.findChild(QLabel, "runningVersion")
        sidebar = window.findChild(QWidget, "hubSidebar")
        assert version.text().startswith("v")
        assert sidebar.layout().indexOf(version) < sidebar.layout().indexOf(window._nav_btns["Settings"])
    finally:
        window.close()


CASES = list(itertools.product(("dark", "light"), (1.0, 1.5),
                              ((900, 650), (1280, 800), (1920, 1080), (700, 500), (600, 360))))


@pytest.mark.parametrize("palette,text_scale,size", CASES)
def test_real_host_layout_geometry(qapp, monkeypatch, palette, text_scale, size):
    theme.set_theme(palette, refresh=False)
    font = qapp.font()
    font.setPointSizeF(9 * text_scale)
    qapp.setFont(font)
    window, home = host(qapp, *size, rows=3 if size[0] > 900 else 0)
    try:
        window.apply_theme()
        opened = size[0] in (700, 1280)
        home.show_guides(opened)
        settle(qapp)
        vp = home._scroll.viewport()
        origin = home._content.mapTo(home, QPoint())
        assert origin.x() == 28
        assert origin.y() == 24
        assert home._content.width() == min(760, vp.width() - 56)
        assert home._scroll.horizontalScrollBar().maximum() == 0
        assert home._content.width() + 56 <= vp.width()
        assert home_qt.overflowing_widgets(home) == []
        minimum = max(card._measured_minimum for card in home.cards)
        expected = max(1, min(3, (home._content.width() + 16) // (minimum + 16)))
        assert home._cards_cols == expected
        if text_scale == 1 and size[0] == 1920:
            assert home._cards_cols == 3
        if text_scale == 1.5 and size[0] == 700:
            assert home._cards_cols == 1
        for a, b in itertools.combinations(home.cards, 2):
            assert not a.geometry().intersects(b.geometry())
        for button in home._links.values():
            assert button.width() >= button.sizeHint().width()
            assert button.height() >= home_qt._px(44)
            assert button.font().capitalization() == QFont.Capitalization.MixedCase
            assert not button.font().underline()
            assert "font-weight: 400" in button.styleSheet()
        assert not home.findChildren(QLabel, "homeLinkSeparator")
        footer = home._footer
        assert footer.mapTo(home, QPoint()).x() == 28
        if home._footer_in_scroll:
            assert footer.parentWidget() is home._content
            assert home.height() - footer.height() - 12 < 160
        else:
            assert home.height() - footer.mapTo(home, QPoint()).y() - footer.height() == 12
            assert home._scroll.height() >= 160
        if home._recent.capacity:
            assert home._scroll.verticalScrollBar().maximum() == 0
        # Required stats and the final link remain reachable through the single scroller.
        bar = home._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        settle(qapp)
        last = footer if home._footer_in_scroll else home._identity
        bottom = last.mapTo(vp, last.rect().bottomLeft()).y()
        assert bottom < vp.height()
        assert len(home.findChildren(QScrollArea)) == 1
        if opened:
            tray = home._guides_tray
            assert all(b.parentWidget() is tray for b in home._guide_btns.values())
            guide = home.cards[1]
            tray_top = home._guides_slot.mapTo(guide.parentWidget(), QPoint()).y()
            assert tray_top - guide.geometry().bottom() - 1 == 8
            if home._cards_cols == 3:
                assert home._guides_slot.x() == guide.x()
            for button in home._guide_btns.values():
                assert button.height() >= home_qt._px(44)
                assert button.x() >= 12 and button.y() >= 12
                assert button.x() + button.width() <= tray.width() - 12
                assert button.y() + button.height() <= tray.height() - 12
    finally:
        window.close()
        window.deleteLater()
        settle(qapp)


def test_notices_reduce_optional_recent_and_stats_stay_at_top_when_height_grows(qapp, monkeypatch):
    window, home = host(qapp, 1280, 800, rows=3)
    try:
        capacity = home._recent.capacity
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [HINT])
        home.refresh()
        settle(qapp)
        assert home._recent.capacity <= capacity
        if home._scroll.verticalScrollBar().maximum():
            assert home._recent.capacity == 0
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
        home.refresh()
        window.setFixedSize(1280, 1000)
        settle(qapp)
        assert home._recent.capacity == 3
        y = home._identity.y()
        window.setFixedSize(1280, 1100)
        settle(qapp)
        assert home._identity.y() == y
    finally:
        window.close()


def test_footer_remeasures_font_and_wraps_complete_targets(qapp):
    window, home = host(qapp)
    try:
        footer = home._footer
        for width in (760, 664, 464, 280):
            footer.setFixedSize(width, footer.heightForWidth(width))
            footer.arrange()
            for button in home._links.values():
                assert button.width() >= button.sizeHint().width()
                assert button.x() + button.width() <= footer.links.width()
            links = list(home._links.values())
            assert all(not a.geometry().intersects(b.geometry()) for a, b in itertools.combinations(links, 2))
    finally:
        window.close()


def test_filled_surfaces_retheme_and_privacy_concealment(qapp, monkeypatch):
    window, home = host(qapp, 1920, 1080, rows=3)
    try:
        for palette in ("light", "dark"):
            theme.set_theme(palette, refresh=False)
            window.apply_theme()
            home._recent._sharing = True
            home._recent._paint_rows()
            settle(qapp)
            assert theme.DATA_SURFACE in home.cards[0].styleSheet()
            assert theme.DATA_SURFACE in home._words_card.styleSheet()
            assert theme.DATA_SURFACE in home._recent.styleSheet()
            for _, heard, _, button in home._recent._slots:
                assert heard.text() == "hidden"
                assert button.text() == "Open in History"
                assert not heard.toolTip()
        assert list(home._guide_btns) == [label for label, _, _ in home_qt.GUIDES]
        assert home.tab_chain[-1] is home._links[home_qt.KOFI_LINK]
        first = home.cards[0]
        qapp.setActiveWindow(window)
        first.setFocus(Qt.FocusReason.TabFocusReason)
        settle(qapp)
        assert first.hasFocus()
    finally:
        window.close()

