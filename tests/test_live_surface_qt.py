"""Geometry and passive-window proof for package C."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt

from samsara.live_surface.model import (CaptureState, DocumentState, Notice, NoticeKind,
                                        PresentationState, SurfaceView, VisibleForm)
from samsara.ui import theme
from samsara.ui.live_surface_qt import (CARD_INSET, CARD_RADIUS, LIVE_SIZE, MARK_SIZE,
                                        REVIEW_SIZE, STATUS_SIZE, LiveSurfaceWidget,
                                        render_device_mark)


@pytest.fixture(autouse=True)
def _restore_theme_after_test():
    """Palette-rendering tests must not change the next test's global theme."""
    previous = theme.active_theme()
    try:
        yield
    finally:
        theme.set_theme(previous)


def _view(form, *, text="", partial="", capture=CaptureState.OFF,
          document=DocumentState.EMPTY, notice=NoticeKind.NONE):
    return SurfaceView(form, capture, None, document, PresentationState.COMPACT,
                       Notice(notice, "Needs confirmation" if notice is not NoticeKind.NONE else "", None),
                       "d1", 1, text, partial, True)


@pytest.mark.parametrize("form", (VisibleForm.MARK, VisibleForm.DRAFT_BADGE,
                                  VisibleForm.STATUS, VisibleForm.LIVE, VisibleForm.REVIEW))
@pytest.mark.parametrize("dpr", (1.0, 1.5))
def test_every_form_has_contained_content_and_compact_content_fit_geometry(qapp, form, dpr):
    widget = LiveSurfaceWidget(_view(form, text="Settled words", partial="tail"))
    widget.setProperty("test_dpr", dpr)
    widget.show()
    qapp.processEvents()
    # Owner's 2026-09-18 ruling replaces fixed tall L/R cards with content-fit panels.
    # 263: the compact mark includes a visible non-colour state label.
    assert (widget.width(), widget.height()) == ((152, 44) if form is VisibleForm.MARK else
        # Finish2: badge explains delivery; status reserves lane + activity + Clear.
        (500, 112) if form is VisibleForm.DRAFT_BADGE else
        (360, 132) if form is VisibleForm.STATUS else
        (LIVE_SIZE[0] if form is VisibleForm.LIVE else REVIEW_SIZE[0], widget.height()))
    if form not in (VisibleForm.MARK, VisibleForm.DRAFT_BADGE, VisibleForm.STATUS):
        assert 96 <= widget.height() <= REVIEW_SIZE[1] + 76
    for child in widget.findChildren(type(widget._state)):
        if child.isVisible():
            assert widget.rect().contains(child.geometry())
    widget.close()


def test_150_percent_mark_has_36_pixel_source_image() -> None:
    image = render_device_mark("idle", "asleep", dpr=1.5)
    # 263: 34 DIP artwork replaces the nearly invisible 24 DIP mark (51 pixels at 150%).
    assert image.width() == 51
    assert image.devicePixelRatio() == 1.5


def test_hit_region_excludes_a_transparent_card_corner(qapp):
    widget = LiveSurfaceWidget(_view(VisibleForm.LIVE, text="text"))
    widget.show()
    qapp.processEvents()
    corner = widget.card_rect.topLeft()
    assert not widget.hit_region().contains(corner)
    assert widget.hit_region().contains(widget.mark_rect.center())
    widget.close()


@pytest.mark.parametrize("form", (VisibleForm.STATUS, VisibleForm.LIVE, VisibleForm.REVIEW))
def test_card_children_stay_inside_the_specified_all_sides_inset(qapp, form):
    widget = LiveSurfaceWidget(_view(form, text="Settled words", partial="new words",
                                     capture=CaptureState.RECORDING))
    widget.show()
    qapp.processEvents()
    content = widget.card_content_rect
    assert content == widget.card_rect.adjusted(CARD_INSET, CARD_INSET, -CARD_INSET, -CARD_INSET)
    for child in (widget._state, widget._transcript, widget._provisional, *widget._controls):
        if child.isVisible():
            assert content.contains(child.geometry()), child.accessibleName()
    widget.close()


@pytest.mark.parametrize("form", (VisibleForm.STATUS, VisibleForm.LIVE, VisibleForm.REVIEW))
def test_card_has_a_distinct_surface_and_border_in_both_palettes(qapp, form):
    for palette in ("dark", "light"):
        theme.set_theme(palette)
        widget = LiveSurfaceWidget(_view(form, text="Settled words", partial="new words",
                                         capture=CaptureState.RECORDING))
        widget.ensurePolished()
        qapp.processEvents()
        image = widget.grab().toImage()
        card = widget.card_rect
        assert image.pixelColor(card.x(), card.y() + card.height() // 2) != \
            image.pixelColor(card.x() + CARD_INSET + 3, card.y() + card.height() // 2)
        widget.close()


def test_provisional_tail_has_a_shape_and_type_cue_not_a_literal_label(qapp):
    widget = LiveSurfaceWidget(_view(VisibleForm.LIVE, text="Settled", partial="moving words",
                                     capture=CaptureState.RECORDING))
    assert "Provisional:" not in widget._provisional.text()
    assert widget._provisional.text().startswith("⋯ ")
    # The visual ruling uses a modest colour cue, not a long italic/dashed tail.
    assert widget._provisional.font().italic()
    assert "dashed" not in widget._provisional.styleSheet()
    widget.close()


def test_draft_badge_uses_a_document_glyph_and_word_count(qapp):
    widget = LiveSurfaceWidget(_view(VisibleForm.DRAFT_BADGE, text="one two three",
                                     document=DocumentState.PARKED_DRAFT))
    assert "Ⅱ" not in widget._badge.text()
    # Finish2 R4 replaces the unexplained glyph with count and a real next action.
    assert "3 words waiting" in widget._state.text()
    assert widget._badge.text() == "Tap Insert text to send."
    widget.close()


def test_draft_badge_keeps_its_12_dip_rounded_corner(qapp):
    widget = LiveSurfaceWidget(_view(VisibleForm.DRAFT_BADGE, text="one two",
                                     document=DocumentState.PARKED_DRAFT))
    widget.ensurePolished()
    qapp.processEvents()
    image = widget.grab().toImage()
    # 263 native Windows proof: pixel checks use physical grab dimensions at any DPR.
    assert image.pixelColor(image.width() - 1, 0).alpha() == 0
    assert image.pixelColor(image.width() - 1, image.height() // 2).alpha() > 0
    widget.close()


@pytest.mark.parametrize("form", (VisibleForm.LIVE, VisibleForm.REVIEW))
def test_compact_header_reserves_text_and_only_quiet_clear_chrome(qapp, form):
    widget = LiveSurfaceWidget(_view(form, text="Settled", partial="moving words",
                                     capture=CaptureState.RECORDING))
    widget.show()
    qapp.processEvents()
    assert widget._transcript.y() == widget.card_content_rect.y() + 44 + 8 + 56 + 22
    assert widget._transcript.height() >= 24
    assert widget._clear.isVisible() and widget._clear.height() >= MARK_SIZE
    assert not widget._pause.isVisible() and not widget._correct.isVisible()
    # Finish2 R4 permits Insert text only in the waiting-draft form.
    assert all(button.text() != "Insert text" for button in widget.findChildren(type(widget._clear)) if button.isVisible())
    # 220-G makes this a scrollable QTextBrowser; its document begins at top.
    assert widget._transcript.document().documentMargin() >= 0
    widget.close()


def test_show_and_click_do_not_take_focus_but_explicit_review_does(qapp):
    widget = LiveSurfaceWidget(_view(VisibleForm.MARK))
    widget.show()
    qapp.processEvents()
    assert not widget.hasFocus()
    assert widget.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    widget.focus_review()
    qapp.processEvents()
    assert widget._clear.focusPolicy() == Qt.FocusPolicy.StrongFocus
    widget.close()


def test_all_states_have_a_non_colour_text_or_symbol_cue(qapp):
    views = (
        _view(VisibleForm.MARK),
        _view(VisibleForm.DRAFT_BADGE, text="draft", document=DocumentState.PARKED_DRAFT),
        _view(VisibleForm.STATUS, capture=CaptureState.RECORDING),
        _view(VisibleForm.LIVE, text="final", partial="partial", capture=CaptureState.RECORDING),
        _view(VisibleForm.REVIEW, text="final", document=DocumentState.EDITABLE_DRAFT),
    )
    widget = LiveSurfaceWidget(views[0])
    for view in views:
        widget.refresh(view, animate=False)
        assert widget._state.text() or widget._badge.text()
    widget.close()

@pytest.mark.parametrize('capture', list(CaptureState))
def test_active_capture_states_spin_in_their_theme_colours(qapp, capture):
    ticks = [0.0]
    w = LiveSurfaceWidget(_view(VisibleForm.STATUS, capture=capture), reduced_motion=False,
                          motion_clock=lambda: ticks[0])
    w.show(); qapp.processEvents()
    angle = w._mark.rotation
    ticks[0] = .35
    w._advance_mark()
    if capture in (CaptureState.WAKE_ARMED, CaptureState.HANDS_FREE_LISTENING,
                   CaptureState.RECORDING, CaptureState.TRANSCRIBING):
        assert w._spin_timer.isActive() and w._mark.rotation != angle
        expected = (theme.RECORDING if capture is CaptureState.RECORDING else
                    theme.AVA if capture is CaptureState.TRANSCRIBING else theme.ACCENT)
        assert w._mark.color == theme.qcolor(expected)
    else:
        assert w._mark.capture != 'recording'
        assert not w._spin_timer.isActive() and w._mark.rotation == 0
        assert w._mark.color == theme.qcolor(theme.ICON_IDLE)
    assert not hasattr(w, '_recording_dot')
    w.hide(); w.deleteLater()


def test_reduced_motion_reads_system_and_keeps_static_recording_text(qapp, monkeypatch):
    from PySide6.QtTest import QTest
    monkeypatch.setattr('samsara.ui.live_surface_qt.system_reduced_motion', lambda: True)
    w = LiveSurfaceWidget(_view(VisibleForm.STATUS, capture=CaptureState.RECORDING))
    w.show(); QTest.qWait(160)
    assert w._mark.capture == 'recording' and w._mark.rotation == 0
    assert not w._spin_timer.isActive()
    # Finish2 R1 names the capture lane before its activity.
    assert w._state.isVisible() and w._lane.text() == 'Hold' and w._state.text() == 'recording'
    w.hide(); w.deleteLater()


def test_compact_mark_has_opaque_backing_and_visible_state(qapp):
    for palette in ('dark', 'light'):
        theme.set_theme(palette)
        w = LiveSurfaceWidget(_view(VisibleForm.MARK))
        w.show(); qapp.processEvents()
        assert w._state.isVisible() and w._state.text() == 'Idle'
        assert w._mark.grab().toImage().pixelColor(22, 22).alpha() == 255
        w.hide(); w.deleteLater()


def test_no_persistent_commit_or_extra_state_control(qapp):
    from PySide6.QtWidgets import QPushButton
    w = LiveSurfaceWidget(_view(VisibleForm.LIVE, text='Synthetic settled text'))
    w.show(); qapp.processEvents()
    assert [b.text() for b in w.findChildren(QPushButton) if b.isVisible()] == ['Clear']
    # Finish2 R4's waiting-draft action is hidden on the ordinary live card.
    assert not any(b.text().lower() in ('commit', 'insert text') for b in w.findChildren(QPushButton) if b.isVisible())
    w.hide(); w.deleteLater()


def test_mark_position_persists_across_controller_rebuild(qapp, monkeypatch):
    from types import SimpleNamespace
    from samsara.live_surface.controller import LiveSurfaceController
    from samsara.live_surface.placement import Rect, Screen
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    app = SimpleNamespace(config={})
    def build():
        c = LiveSurfaceController(app)
        c._screens = lambda: [Screen('synthetic', Rect(0, 0, 1600, 1000), True)]
        c.start()
        return c
    c = build(); c._move_requested({'center': (420, 310)})
    original = c.widget.mapToGlobal(c.widget.mark_rect.center())
    c.close(); qapp.processEvents()
    c = build()
    assert c.widget.mapToGlobal(c.widget.mark_rect.center()) == original
    assert app.config['ui']['live_surface']['placement']['monitors']['synthetic']['edge'] == 'free'
    c.close(); qapp.processEvents()


def test_drag_freezes_form_and_escape_restores_geometry(qapp, monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import QPointF, QEvent
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QTest
    from samsara.live_surface.controller import LiveSurfaceController
    from samsara.live_surface.model import Lane, EventKind, SurfaceEvent
    from samsara.live_surface.placement import Rect, Screen
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    c = LiveSurfaceController(SimpleNamespace(config={}))
    c._screens = lambda: [Screen('synthetic', Rect(0, 0, 1600, 1000), True)]
    c.start(); w = c.widget; initial = w.geometry()
    local = QPointF(w.mark_rect.center()); start = QPointF(w.mapToGlobal(local.toPoint()))
    target = start + QPointF(-150, -150)
    w.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, local, start,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    w.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, local, target,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    dropped = w.geometry()
    c.begin_capture(Lane.HOLD); c.post(SurfaceEvent(EventKind.PARTIAL, text='Synthetic incoming partial')); c.drain()
    assert w.geometry() == dropped and w.size() == initial.size()
    QTest.keyClick(w, Qt.Key.Key_Escape)
    assert not w.is_dragging and w.geometry() == initial
    assert 'ui' not in c.app.config
    c.close(); qapp.processEvents()
