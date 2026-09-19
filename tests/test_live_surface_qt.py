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
    assert (widget.width(), widget.height()) == ((44, 44) if form is VisibleForm.MARK else
        (120, 44) if form is VisibleForm.DRAFT_BADGE else
        (STATUS_SIZE[0], 68) if form is VisibleForm.STATUS else
        (LIVE_SIZE[0] if form is VisibleForm.LIVE else REVIEW_SIZE[0], widget.height()))
    if form not in (VisibleForm.MARK, VisibleForm.DRAFT_BADGE, VisibleForm.STATUS):
        assert 96 <= widget.height() <= REVIEW_SIZE[1]
    for child in widget.findChildren(type(widget._state)):
        if child.isVisible():
            assert widget.rect().contains(child.geometry())
    widget.close()


def test_150_percent_mark_has_36_pixel_source_image() -> None:
    image = render_device_mark("idle", "asleep", dpr=1.5)
    assert image.width() == 36
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
    assert not widget._provisional.font().italic()
    assert "dashed" not in widget._provisional.styleSheet()
    widget.close()


def test_draft_badge_uses_a_document_glyph_and_word_count(qapp):
    widget = LiveSurfaceWidget(_view(VisibleForm.DRAFT_BADGE, text="one two three",
                                     document=DocumentState.PARKED_DRAFT))
    assert "Ⅱ" not in widget._badge.text()
    assert "▤" in widget._badge.text()
    assert "Draft" in widget._badge.text()
    assert "3 words" in widget._badge.text()
    widget.close()


def test_draft_badge_keeps_its_12_dip_rounded_corner(qapp):
    widget = LiveSurfaceWidget(_view(VisibleForm.DRAFT_BADGE, text="one two",
                                     document=DocumentState.PARKED_DRAFT))
    widget.ensurePolished()
    qapp.processEvents()
    image = widget.grab().toImage()
    assert image.pixelColor(widget.width() - 1, 0).alpha() == 0
    assert image.pixelColor(widget.width() - 1, widget.height() // 2).alpha() > 0
    widget.close()


@pytest.mark.parametrize("form", (VisibleForm.LIVE, VisibleForm.REVIEW))
def test_compact_header_reserves_text_and_only_quiet_clear_chrome(qapp, form):
    widget = LiveSurfaceWidget(_view(form, text="Settled", partial="moving words",
                                     capture=CaptureState.RECORDING))
    widget.show()
    qapp.processEvents()
    assert widget._transcript.y() == widget.card_content_rect.y() + 44 + 8
    assert widget._transcript.height() >= 24
    assert widget._clear.isVisible() and widget._clear.height() >= MARK_SIZE
    assert not widget._commit.isVisible() and not widget._pause.isVisible() and not widget._correct.isVisible()
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
    assert widget._commit.focusPolicy() == Qt.FocusPolicy.StrongFocus
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
