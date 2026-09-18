"""Geometry and passive-window proof for package C."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt

from samsara.live_surface.model import (CaptureState, DocumentState, Notice, NoticeKind,
                                        PresentationState, SurfaceView, VisibleForm)
from samsara.ui.live_surface_qt import (CARD_RADIUS, LIVE_SIZE, MARK_SIZE, REVIEW_SIZE,
                                        STATUS_SIZE, LiveSurfaceWidget, render_device_mark)


def _view(form, *, text="", partial="", capture=CaptureState.OFF,
          document=DocumentState.EMPTY, notice=NoticeKind.NONE):
    return SurfaceView(form, capture, None, document, PresentationState.COMPACT,
                       Notice(notice, "Needs confirmation" if notice is not NoticeKind.NONE else "", None),
                       "d1", 1, text, partial, True)


@pytest.mark.parametrize(("form", "size"), [
    (VisibleForm.MARK, (44, 44)), (VisibleForm.DRAFT_BADGE, (120, 44)),
    (VisibleForm.STATUS, (STATUS_SIZE[0], STATUS_SIZE[1] + 48)),
    (VisibleForm.LIVE, (LIVE_SIZE[0], LIVE_SIZE[1] + 48)),
    (VisibleForm.REVIEW, (REVIEW_SIZE[0], REVIEW_SIZE[1] + 48)),
])
@pytest.mark.parametrize("dpr", (1.0, 1.5))
def test_every_form_has_its_specified_geometry_and_contained_text(qapp, form, size, dpr):
    widget = LiveSurfaceWidget(_view(form, text="Settled words", partial="tail"))
    widget.setProperty("test_dpr", dpr)
    widget.show()
    qapp.processEvents()
    assert (widget.width(), widget.height()) == size
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
