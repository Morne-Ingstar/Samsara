"""388: real pixel/motion/accessibility checks and measured Home geometry."""
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QStyle, QStyleOptionButton, QWidget

from samsara import config_schema
from samsara.ui import ava_captions_qt, home_qt, home_signals, splash_qt, theme
from samsara.ui.widgets.spin_word_label import SpinWordLabel
from tests.test_home_qt import _app, _hub, _isolated, _native_home_font, page


@pytest.fixture
def labels(qapp, monkeypatch):
    monkeypatch.setattr(splash_qt, "_system_reduced_motion", lambda: False)
    widgets = []

    def make(width=500, text=home_qt.SUPPORT_LINE, **kwargs):
        spin = SpinWordLabel(text, **kwargs)
        plain = home_qt._label(text, role="note", color=theme.TEXT_DISABLED, wrap=True)
        for w in (spin, plain):
            w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            w.setStyleSheet(home_qt._label_css("note", theme.TEXT_DISABLED, 400))
            w.setWordWrap(True)
            w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            w.resize(width, 80)
            w.show()
            widgets.append(w)
        qapp.processEvents()
        return spin, plain

    yield make
    for w in widgets:
        w.hide()
        w.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("width", [500, 140])
def test_rest_equals_plain_label_including_wrapping(labels, width):
    spin, plain = labels(width)
    assert spin.grab().toImage() == plain.grab().toImage()
    assert spin.sizeHint() == plain.sizeHint()
    assert spin.minimumSizeHint() == plain.minimumSizeHint()
    assert spin.heightForWidth(width) == plain.heightForWidth(width)


@pytest.mark.parametrize("width", [500, 140])
def test_one_word_changes_and_lands_exactly(labels, qapp, width):
    spin, _ = labels(width, duration_ms=40)
    rest = spin.grab().toImage()
    spin.spin_once()
    spin._animation.pause()
    spin._animation.setCurrentTime(20)
    qapp.processEvents()
    mid = spin.grab().toImage()
    word = spin._word_rect.toRect()
    assert mid.copy(word) != rest.copy(word)
    # Adjacent words stay pixel-identical; allow the rotating ink its empty spaces.
    if word.left() > 6:
        assert mid.copy(0, 0, word.left()-6, mid.height()) == rest.copy(0, 0, word.left()-6, rest.height())
    right = word.right()+7
    if right < mid.width():
        assert mid.copy(right, 0, mid.width()-right, mid.height()) == rest.copy(right, 0, rest.width()-right, rest.height())
    assert spin.accessibleName() == home_qt.SUPPORT_LINE
    finished = QSignalSpy(spin._animation.finished)
    spin._animation.resume()
    # A loaded worker may deliver the animation tick after a fixed 70 ms nap.
    assert finished.wait(1000), 'spin animation did not finish'
    assert spin.grab().toImage() == rest


def test_ten_intervals_hide_and_fresh_show(labels, monkeypatch):
    import samsara.ui.widgets.spin_word_label as module
    selected = []
    def choose(low, high):
        delay = low if len(selected) % 2 == 0 else high
        selected.append(delay)
        return delay
    monkeypatch.setattr(module.random, "randint", choose)
    spin, _ = labels(interval_bounds=(20, 40), duration_ms=1)
    armed, starts = [], []
    original_arm, original_spin = spin._arm, spin.spin_once

    def arm():
        original_arm()
        if spin._timer.isActive():
            armed.append((time.monotonic(), spin._timer.interval()))

    def once():
        starts.append(time.monotonic())
        original_spin()

    monkeypatch.setattr(spin, "_arm", arm)
    monkeypatch.setattr(spin, "spin_once", once)
    spin.hide()
    QTest.qWait(60)
    assert not spin._timer.isActive() and not starts
    spin.show()
    QTest.qWait(10)
    assert not starts
    deadline = time.monotonic()+2
    while len(starts) < 10 and time.monotonic() < deadline:
        QTest.qWait(2)
    spin.hide()
    assert len(starts) == 10
    assert len(armed) in (10, 11)
    assert all(start-at >= delay/1000-.001 for start, (at, delay) in zip(starts, armed))
    assert {delay for _, delay in armed} == {20, 40}
    assert all(.018 <= b-a < .15 for a, b in zip(starts, starts[1:]))
    QTest.qWait(80)
    assert len(starts) == 10 and not spin._timer.isActive()
    spin.show()
    QTest.qWait(10)
    assert len(starts) == 10
    assert spin._timer.isActive()


def test_reduced_motion_is_the_single_source(labels, monkeypatch):
    monkeypatch.setattr(splash_qt, "_system_reduced_motion", lambda: True)
    spin, plain = labels(interval_bounds=(20, 40))
    assert not spin._timer.isActive()
    spin.spin_once()
    QTest.qWait(60)
    assert spin.grab().toImage() == plain.grab().toImage()
    assert spin._layer is None and not spin._timer.isActive()


def test_absent_word_is_static_and_accessible(labels):
    spin, plain = labels(text="Your support turns the wheel")
    spin.spin_once()
    assert not spin._timer.isActive()
    assert spin.grab().toImage() == plain.grab().toImage()
    assert spin.accessibleName() == spin.text()
    assert spin.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_defaults_no_startup_spin(labels):
    spin, _ = labels()
    assert spin.interval_bounds == (300_000, 600_000)
    assert spin._animation.duration() == 700
    assert 300_000 <= spin._timer.remainingTime() <= 600_000
    assert spin._layer is None and spin._angle == 0
    assert spin.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_feedback_and_caption_copy(qapp):
    assert home_qt.SUPPORT_LINE == "Your support spins the wheel"
    assert theme.CREED_SCALE == 1.35
    assert list(config_schema.FEEDBACK_LEVEL_CHOICES) == ["silent", "brief", "full"]
    assert config_schema.FEEDBACK_LEVEL_CHOICES["silent"][1] == "Visual feedback only."
    assert all(0 < len(text) <= 90 for _, text in config_schema.FEEDBACK_LEVEL_CHOICES.values())
    panel = ava_captions_qt._CaptionsWindow(_app())
    assert panel._hint.text() == "Click anywhere to dismiss " + chr(0xB7) + " Drag to move"
    assert panel._hint.accessibleName() == "Click anywhere on the caption to dismiss it. Drag it to move it."
    assert "Click to dismiss  |  drag to move" not in Path(ava_captions_qt.__file__).read_text()
    panel.hide()
    panel.deleteLater()


def test_shown_status_and_links_geometry(qapp, monkeypatch):
    win = _hub(qapp, monkeypatch, width=900, height=650)
    try:
        sb = win.statusBar()
        values = (win._lbl_mode, win._lbl_wake, win._lbl_mic)
        labs = [v.parentWidget().layout().itemAt(0).widget() for v in values]
        x = lambda w: w.mapTo(win, QPoint()).x()
        assert x(labs[0]) == 16
        assert all(x(v) - (x(l)+l.width()) == 8 for l, v in zip(labs, values))
        seps = [s for s in sb.findChildren(QFrame) if s.width() == 1 and s.height() == 16]
        assert len(seps) == 2
        for i, s in enumerate(seps):
            assert x(s) - (x(values[i])+values[i].width()) == 14
            assert x(labs[i+1]) - (x(s)+s.width()) == 14
            assert abs(s.mapTo(win, QPoint()).y()+8 - (labs[i].mapTo(win, QPoint()).y()+labs[i].height()/2)) <= 1
        control = win._example_strip._next_btn
        painted_right = x(control)+control.width()/2+3+2+.875
        assert abs(win.width()-painted_right-16) <= 2
        assert control.width() == control.height() == 44
        assert sb.height() == 49
        p = win._panel_cache["Home"]
        links = list(p._links.values())
        dots = p._links_row.findChildren(QLabel, "homeLinkSeparator")
        # 501 replaced inkPadding with native content insets to keep labels
        # whole. The eight-pixel contract now separates layout targets.
        for i, d in enumerate(dots):
            assert x(d) - (x(links[i]) + links[i].width()) == 8
            assert x(links[i+1]) - (x(d) + d.width()) == 8
        for button in links:
            option = QStyleOptionButton()
            button.initStyleOption(option)
            content = button.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, option, button)
            metrics = option.fontMetrics
            bounds = metrics.tightBoundingRect(button.text())
            advance = metrics.horizontalAdvance(button.text())
            assert content.width() >= advance
            left = content.x() + (content.width() - advance) // 2
            assert content.left() <= left + bounds.left()
            assert left + bounds.right() <= content.right()
        # 679 made the links flow from the cell's left edge so they can wrap.
        # The support line and links row still share the same full-width cell.
        assert x(links[0]) == x(p._links_row)
        assert abs(x(p._support_text)+p._support_text.width()-(x(p._links_row)+p._links_row.width())) <= 2
        assert all(p._links_row.rect().contains(button.geometry()) for button in links)
        assert all(b.height() >= 44 for b in links)
        assert isinstance(p._support_text, SpinWordLabel)
    finally:
        win.hide()


def test_status_at_150_percent_is_not_clipped(qapp, monkeypatch):
    import re
    app = _app(available_mics=[{"id": 7, "name": "Analogue 1 + 2 (Focusrite USB Audio)"}])
    win = _hub(qapp, monkeypatch, width=900, height=650, app=app)
    try:
        win._refresh_status()
        for widget in win.statusBar().findChildren(QWidget):
            widget.setStyleSheet(re.sub(r"font-size: (\d+)px", lambda m: f"font-size: {round(int(m[1])*1.5)}px", widget.styleSheet()))
        for _ in range(60):
            qapp.processEvents()
        control = win._example_strip._next_btn
        assert control.mapTo(win, QPoint()).x()+control.width() <= win.width()
        for value in (win._lbl_mode, win._lbl_wake, win._lbl_mic):
            assert QFontMetrics(value.font()).horizontalAdvance(value.text()) <= value.width()
        assert win.statusBar().height() == 73
    finally:
        win.hide()


def test_release_action_opens_changelog_and_dismiss_stays_separate(page, monkeypatch):
    opened = []
    monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
    def note(seen, **kwargs):
        return None if seen == "9.9.9" else ("9.9.9", "New.")
    monkeypatch.setattr(home_qt, "whats_new_note", note)
    monkeypatch.setattr(home_qt, "open_url", opened.append)
    p = page()
    try:
        action = p._hint_action_btn
        assert action.isVisibleTo(p)
        assert action.text() == action.accessibleName() == home_qt.CHANGELOG_LINK
        assert action.accessibleDescription() == home_qt.CHANGELOG_URL
        assert action.height() == home_qt._px(30) and "underline" not in action.styleSheet()
        assert p.tab_chain.index(action) < p.tab_chain.index(p._hint_hide_btn)
        action.click()
        assert opened == [home_qt.CHANGELOG_URL]
        assert p._whats_new == "9.9.9" and p._hint_card.isVisibleTo(p)
        assert not p._settings.value(home_qt.WHATS_NEW_SEEN_KEY, "")
        assert p.destinations()[home_qt.CHANGELOG_LINK] == ("url", home_qt.CHANGELOG_URL)
        p._hint_hide_btn.click()
        assert p._settings.value(home_qt.WHATS_NEW_SEEN_KEY) == "9.9.9"
        assert not p._hint_card.isVisibleTo(p)
    finally:
        p.hide()


def test_diagnostic_action_keeps_its_route(page, monkeypatch):
    hint = home_signals.Hint("diag.misses", home_signals.TIER_DIAGNOSTIC, "Missed things.", "View commands", "cheatsheet", "", evidence="3/8 were misses")
    monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [hint])
    p = page()
    calls = []
    monkeypatch.setattr(p, "_route", lambda *args: calls.append(args))
    try:
        assert p._hint_action_btn.text() == home_qt.slot_action_label(hint.action_label, hint.action, hint.arg)
        p._hint_action_btn.click()
        assert calls == [("cheatsheet", "")]
        assert p._hint is hint
    finally:
        p.hide()


def test_release_version_is_distinct_and_diagnostic_style_returns(page, monkeypatch, qapp):
    monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [])
    monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: ("9.9.9", "New."))
    p = page()
    try:
        assert p._hint_text.text() == "Samsara 9.9.9"
        assert p._hint_text.font().italic()
        assert not p._hint_sub.font().italic()
        assert theme.TEXT_SECONDARY in p._hint_sub.styleSheet()
        assert p._hint_card.layout().spacing() == 0
        qapp.processEvents()
        gap = p._hint_sub.y()-p._hint_text.y()-p._hint_text.height()
        assert gap == 0
        hint = home_signals.Hint("diag.misses", home_signals.TIER_DIAGNOSTIC, "Missed things.", "View commands", "cheatsheet", "", evidence="3/8 were misses")
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda app: [hint])
        p._refresh_hint()
        assert p._hint is hint and p._whats_new is None
        assert not p._hint_text.font().italic()
        assert p._hint_text.styleSheet() == home_qt._label_css("card title", theme.TEXT_PRIMARY, 400)
        assert p._hint_card.layout().spacing() == home_qt._px(6)
    finally:
        p.hide()


@pytest.mark.parametrize("scale", [1, 1.5])
def test_release_band_is_at_most_one_fifth_with_version_below_heading(qapp, monkeypatch, scale):
    monkeypatch.setattr(home_signals, "diagnostic_hints", lambda _: [])
    monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: ("9.9.9", "A beta for testers."))
    original_font = QFont(qapp.font())
    font = QFont(original_font)
    font.setPointSizeF(home_qt.BASE_POINT_SIZE * scale)
    qapp.setFont(font)
    app = _app(available_mics=[{"id": 7, "name": "Analogue 1 + 2 (Focusrite USB Audio)"}])
    win = _hub(qapp, monkeypatch, width=900, height=650, app=app)
    try:
        # Measure the requested default size, even if larger text asks for more width.
        win.setFixedSize(900, 650)
        win._refresh_status()
        if scale != 1:
            import re
            for widget in win.statusBar().findChildren(QWidget):
                widget.setStyleSheet(re.sub(r"font-size: (\d+)px", lambda m: f"font-size: {round(int(m[1])*scale)}px", widget.styleSheet()))
        for _ in range(60):
            qapp.processEvents()
        p = win._panel_cache["Home"]
        assert win.width() == 900 and win.height() == 650
        assert p._hint_card.height() * 5 <= p._scroll.viewport().height()
        heading, version, sentence = p._hint_heading, p._hint_text, p._hint_sub
        assert version.x() == heading.x() == sentence.x()
        assert version.y() >= heading.y() + heading.height()
        assert sentence.y() >= version.y() + version.height()
        assert heading.y() - 1 == p._hint_card.height() - 1 - sentence.y() - sentence.height()
        assert version.alignment() & Qt.AlignmentFlag.AlignLeft
        assert version.font().italic() and version.font().pixelSize() == home_qt._px(theme.TYPE_SECTION_LABEL)
        action, dismiss = p._hint_action_btn, p._hint_hide_btn
        for btn, label in ((action, home_qt.CHANGELOG_LINK), (dismiss, home_qt.DISMISS)):
            assert btn.text() == btn.accessibleName() == label
            assert btn.height() == home_qt._px(30)
            assert btn.font().pixelSize() == home_qt._px(theme.TYPE_MIN)
            assert not btn.property("primary")
            assert "text-decoration: underline" not in btn.styleSheet()
            point = btn.mapTo(p._hint_card, QPoint())
            assert point.x() >= sentence.x() + sentence.width()
            assert abs((point.y() + btn.height()/2) - p._hint_card.height()/2) <= 1
            assert point.y() - 1 >= home_qt._px(theme.SPACE_2)
            assert p._hint_card.height() - 1 - point.y() - btn.height() >= home_qt._px(theme.SPACE_2)
        assert action.mapTo(p._hint_card, QPoint()).x() < dismiss.mapTo(p._hint_card, QPoint()).x()
        assert p._cards_cols == 3
        assert home_qt.overflowing_widgets(p) == []
        if scale == 1:
            assert p._scroll.verticalScrollBar().maximum() == 0
        monkeypatch.setattr(home_signals, "diagnostic_hints", lambda _: [home_signals.Hint(
            "diag.misses", home_signals.TIER_DIAGNOSTIC, "Missed things.", "View commands", "cheatsheet", "")])
        p._refresh_hint()
        assert action.property("primary")
        assert action.styleSheet() == action.property("diagnosticStyle")
        assert dismiss.styleSheet() == dismiss.property("diagnosticStyle")
        assert action.minimumHeight() == dismiss.minimumHeight() == home_qt.MIN_TARGET
        assert action.maximumHeight() == dismiss.maximumHeight() == 16777215
        assert p._hint_card.layout().getItemPosition(p._hint_card.layout().indexOf(action.parentWidget())) == (4, 0, 1, 2)
    finally:
        win.hide()
        qapp.setFont(original_font)


@pytest.mark.parametrize("size", [14, 21])
def test_status_icons_preserve_accessible_text_and_scale(qapp, size):
    from PySide6.QtGui import QAccessible
    from samsara.ui import main_window_qt
    from samsara.ui.settings.sidebar import icon_family
    from samsara.ui.widgets.status_label import StatusLabel
    assert icon_family(), "Windows status proof requires the existing system icon font"
    for text in ("Mode", "Wake", "Mic"):
        segment, value = main_window_qt._status_segment(text, "Ready")
        label = segment.layout().itemAt(0).widget()
        plain = QLabel(text.upper())
        try:
            assert isinstance(label, StatusLabel)
            label.setStyleSheet(f"font-size: {size}px; color: {theme.TEXT_SECONDARY};")
            label.ensurePolished()
            qapp.processEvents()
            assert label.text() == plain.text() == text.upper()
            assert label.accessibleName() == plain.accessibleName()
            assert QAccessible.queryAccessibleInterface(label).text(QAccessible.Text.Name) == QAccessible.queryAccessibleInterface(plain).text(QAccessible.Text.Name)
            assert label.contentsMargins().left() == round(size*22/14)
            assert label.focusPolicy() == Qt.FocusPolicy.NoFocus
            assert value.text() == "Ready" and value.accessibleName() == ""
        finally:
            segment.deleteLater()
            plain.deleteLater()


@pytest.mark.parametrize("scale, expected", [(1, 18), (1.5, 27)])
def test_smaller_creed_uses_existing_token_and_restores_second_dot(page, qapp, scale, expected):
    old = qapp.font()
    font = QFont(old)
    font.setPointSizeF(home_qt.BASE_POINT_SIZE*scale)
    qapp.setFont(font)
    p = page()
    try:
        assert p._creed.font().pixelSize() == expected
        assert home_qt.CREED == "Free " + chr(0xB7) + " Open source " + chr(0xB7) + " Accessibility first"
        assert p._creed.text().count(chr(0xB7)) == 2
        assert p._support_text.text() == home_qt.SUPPORT_LINE
    finally:
        p.hide()
        qapp.setFont(old)


def test_status_reflows_without_losing_text_and_returns_to_one_line(qapp, monkeypatch):
    import re
    app = _app(available_mics=[{"id": 7, "name": "Analogue 1 + 2 (Focusrite USB Audio)"}])
    win = _hub(qapp, monkeypatch, width=900, height=650, app=app)
    try:
        win._refresh_status()
        values = (win._lbl_mode, win._lbl_wake, win._lbl_mic)
        texts = [w.text() for w in values]
        names = [w.accessibleName() for w in values]
        originals = [(w, w.styleSheet()) for w in win.statusBar().findChildren(QWidget)]
        for w, css in originals:
            w.setStyleSheet(re.sub(r"font-size: (\d+)px", lambda m: f"font-size: {round(int(m[1])*1.5)}px", css))
        for _ in range(60):
            qapp.processEvents()
        assert [w.text() for w in values] == texts
        assert [w.accessibleName() for w in values] == names
        for w in win.statusBar().findChildren(QWidget):
            if w.isVisibleTo(win):
                pos = w.mapTo(win, QPoint())
                assert 0 <= pos.x() and pos.x()+w.width() <= win.width()
                assert 0 <= pos.y() and pos.y()+w.height() <= win.height()
        for w, css in originals:
            w.setStyleSheet(css)
        for _ in range(60):
            qapp.processEvents()
        assert win.statusBar().height() == 49
        assert [w.text() for w in values] == texts
        assert all(QFontMetrics(w.font()).horizontalAdvance(w.text()) <= w.width() for w in values)
    finally:
        win.hide()


def test_release_buttons_leave_clear_space_below_the_card_border(page, monkeypatch, qapp):
    monkeypatch.setattr(home_signals, "diagnostic_hints", lambda _: [])
    monkeypatch.setattr(home_qt, "whats_new_note", lambda *a, **k: ("9.9.9", "A beta for testers."))
    p = page()
    try:
        qapp.processEvents()
        for button in (p._hint_action_btn, p._hint_hide_btn):
            top = button.mapTo(p._hint_card, QPoint()).y()
            clear_below = p._hint_card.height() - 1 - top - button.height()
            assert clear_below >= home_qt._px(theme.SPACE_2)
            assert abs((top - 1) - clear_below) <= 1
    finally:
        p.hide()
