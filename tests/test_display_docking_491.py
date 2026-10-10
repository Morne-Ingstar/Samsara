"""Display-edge docking, work-area recovery and current-size release regressions."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.model import (CaptureState, DocumentState, Notice, NoticeKind,
    PresentationState, SurfaceView, VisibleForm)
from samsara.live_surface.placement import (Edge, INSET_DIP, Placement, Rect, Screen,
    edge_mark_rect, free_placement_for_mark, placement_for_drop, placement_record)
from samsara.ui.live_surface_qt import LiveSurfaceWidget


def view(form, text="", notice=NoticeKind.NONE):
    return SurfaceView(form, CaptureState.RECORDING if form is VisibleForm.STATUS else CaptureState.OFF,
        None, DocumentState.EDITABLE_DRAFT if text else DocumentState.EMPTY, PresentationState.COMPACT,
        Notice(notice, "Synthetic message" if notice is not NoticeKind.NONE else "", None),
        "synthetic", 1, text, "", True)


def controller(qapp, screen, placement=Placement()):
    app = SimpleNamespace(config={"ui": {"live_surface": {"enabled": True, "placement": {
        "preferred_monitor": screen.id, "monitors": {screen.id: placement_record(placement)}}}}})
    c = LiveSurfaceController(app)
    c._screens = lambda: [screen]
    c.widget = LiveSurfaceWidget(c, reduced_motion=True)
    c._connect_intents()
    return c


@pytest.mark.parametrize("origin,area", [((0, 0), (1920, 1040)), ((-1920, -120), (1280, 680)),
    ((60, 0), (1860, 1080)), ((0, 40), (1920, 1040))])
@pytest.mark.parametrize("edge,t", [(e,t) for e in (Edge.TOP, Edge.BOTTOM, Edge.LEFT, Edge.RIGHT)
    for t in (0., .5, 1.)])
def test_docked_edge_and_corners_survive_every_form(qapp, origin, area, edge, t):
    screen = Screen("synthetic", Rect(*origin, *area), True)
    c = controller(qapp, screen, Placement(edge, t))
    try:
        for form, text, notice in [(VisibleForm.MARK,"",NoticeKind.NONE),
            (VisibleForm.STATUS,"",NoticeKind.NONE), (VisibleForm.LIVE,"Synthetic draft. "*9,NoticeKind.NONE),
            (VisibleForm.REVIEW,"Synthetic draft. "*50,NoticeKind.NONE),
            (VisibleForm.STATUS,"",NoticeKind.ERROR), (VisibleForm.DRAFT_BADGE,"Draft",NoticeKind.NONE)]:
            c.widget.refresh(view(form,text,notice), animate=False)
            c._place_widget()
            w = c.widget; a = screen.work_area
            b = Rect(w.x(), w.y(), w.width(), w.height())
            assert a.contains(b)
            if edge is Edge.TOP: assert b.y == a.y + INSET_DIP
            if edge is Edge.BOTTOM: assert b.bottom == a.bottom - INSET_DIP
            if edge is Edge.LEFT: assert b.x == a.x + INSET_DIP
            if edge is Edge.RIGHT: assert b.right == a.right - INSET_DIP
            if t in (0.,1.):
                if edge in (Edge.TOP,Edge.BOTTOM):
                    assert (b.x if t == 0 else b.right) == (a.x+INSET_DIP if t == 0 else a.right-INSET_DIP)
                else:
                    assert (b.y if t == 0 else b.bottom) == (a.y+INSET_DIP if t == 0 else a.bottom-INSET_DIP)
    finally:
        c.widget.hide(); c.widget.deleteLater()


@pytest.mark.parametrize("x,y,expected", [(700,10,Edge.TOP),(700,954,Edge.BOTTOM),
    (10,400,Edge.LEFT),(1550,400,Edge.RIGHT),(10,10,Edge.TOP),(1550,10,Edge.TOP),
    (10,954,Edge.BOTTOM),(1550,954,Edge.BOTTOM),(700,400,Edge.FREE)])
def test_four_edges_four_corners_and_free_release(qapp,x,y,expected):
    screen=Screen("synthetic",Rect(0,0,1920,1040),True)
    c=controller(qapp,screen)
    try:
        c.widget.refresh(view(VisibleForm.STATUS),animate=False)
        b=Rect(x,y,c.widget.width(),c.widget.height()); center=(x+30,y+30)
        preview=c.preview_move(b)
        c._move_requested({"center":center,"bounds":b})
        record=c.app.config["ui"]["live_surface"]["placement"]["monitors"][screen.id]
        assert record["edge"] == expected.value
        assert (c.widget.x(),c.widget.y()) == (round(preview.bounds.x),round(preview.bounds.y))
        if expected is Edge.FREE:
            mark=c.widget.mark_rect
            assert (c.widget.x()+mark.x()+22,c.widget.y()+mark.y()+22) == center
    finally:c.widget.hide(); c.widget.deleteLater()


@pytest.mark.parametrize('saved', [None, Placement(Edge.BOTTOM, .5),
    Placement(Edge.FREE, free_cx=.5, free_cy=.8)])
def test_caret_avoidance_only_for_never_placed_display(qapp, saved):
    from unittest.mock import Mock
    from samsara.live_surface.caret import CaretBounds
    from samsara.live_surface.placement import _overlaps, _expand, CARET_PADDING_DIP
    screen = Screen('synthetic', Rect(0, 0, 1920, 1040), True)
    c = controller(qapp, screen, saved or Placement())
    if saved is None:
        c.app.config = {}
    caret = CaretBounds(Rect(960, 800, 2, 20))
    c._caret_locator = Mock(request=Mock(return_value=caret))
    before = deepcopy(c.app.config)
    try:
        c.widget.refresh(view(VisibleForm.STATUS), animate=False)
        c._place_widget()
        if saved is None:
            c._caret_locator.request.assert_called_once()
            w = c.widget
            assert not _overlaps(Rect(w.x(), w.y(), w.width(), w.height()),
                                 _expand(caret.rect, CARET_PADDING_DIP))
            position = w.pos()
            c._place_widget()
            assert w.pos() == position
            c._caret_locator.request.assert_called_once()
        else:
            c._caret_locator.request.assert_not_called()
        assert c.app.config == before
    finally:c.widget.hide(); c.widget.deleteLater()


def compact_screen(area):
    from PySide6.QtCore import QRect
    return SimpleNamespace(name=lambda:'synthetic', availableGeometry=lambda:QRect(*area))


@pytest.mark.parametrize('area', [(0,0,1920,1040), (0,0,1280,693),
    (-1920,-120,1920,1040), (60,0,1860,1080), (0,40,1920,1040)])
@pytest.mark.parametrize('edge,t', [(e,t) for e in (Edge.TOP,Edge.BOTTOM,Edge.LEFT,Edge.RIGHT)
    for t in (0.,.5,1.)])
def test_compact_docked_edge_survives_size_and_chip_only_changes(qapp, area, edge, t):
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    from samsara.ui.listening_indicator import ListeningIndicator
    fake = compact_screen(area)
    with patch.object(QApplication,'primaryScreen',return_value=fake), patch.object(QApplication,'screens',return_value=[fake]):
        w = ListeningIndicator()
        try:
            w.set_custom_position('synthetic', .5, .5, edge=edge.value, t=t)
            before = deepcopy(w._custom_position)
            w.show()
            for state in ('idle','listening','draft','message','chip_only','idle'):
                w.clear_outcome()
                w.set_listening(state in ('listening','draft'))
                if state in ('draft','message','chip_only'):
                    w.show_outcome('Synthetic message with extra width', 'pending', None)
                w._chip_only = state == 'chip_only'
                w._reposition()
                b = Rect(w.x(),w.y(),w.width(),w.height()); a = Rect(*area)
                assert a.contains(b)
                if edge is Edge.TOP: assert b.y == a.y + 8
                if edge is Edge.BOTTOM: assert b.bottom == a.bottom - 8
                if edge is Edge.LEFT: assert b.x == a.x + 8
                if edge is Edge.RIGHT: assert b.right == a.right - 8
                if t in (0.,1.):
                    if edge in (Edge.TOP,Edge.BOTTOM):
                        assert (b.x if t == 0 else b.right) == (a.x+8 if t == 0 else a.right-8)
                    else:
                        assert (b.y if t == 0 else b.bottom) == (a.y+8 if t == 0 else a.bottom-8)
            assert w._custom_position == before
        finally:w.destroy(); w.deleteLater()


@pytest.mark.parametrize('x,y,edge,preset', [(10,10,'top','top-left'),
    (1700,10,'top','top-right'), (10,1000,'bottom','bottom-left'),
    (1700,1000,'bottom','bottom-right'), (900,10,'top','top-center'),
    (900,1000,'bottom','bottom-center'), (10,400,'left',None),
    (1780,400,'right',None), (700,400,'free',None)])
def test_compact_drop_saves_preset_or_reproducible_custom(qapp,x,y,edge,preset):
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    from samsara.ui.listening_indicator import ListeningIndicator
    fake = compact_screen((0,0,1920,1040))
    with patch.object(QApplication,'primaryScreen',return_value=fake), \
         patch.object(QApplication,'screens',return_value=[fake]), \
         patch.object(QApplication,'screenAt',return_value=fake):
        w = ListeningIndicator(); restored = ListeningIndicator()
        try:
            w.show(); w.enter_move_mode()
            if preset and preset.endswith('right'):
                x = fake.availableGeometry().width() - w.width() - 10
            if preset and preset.startswith('bottom'):
                y = fake.availableGeometry().height() - w.height() - 10
            w.move(x,y)
            if preset and preset.endswith('center'):
                x = (1920 - w.width()) // 2
                w.move(x,y)
            payloads=[]; w.placement_committed.connect(payloads.append)
            dock,preview = w._snap_drag(x,y,fake)
            assert dock.edge.value == edge
            w._commit_drag_position()
            payload = payloads[0]
            if preset:
                assert payload == {'type':'preset','position':preset}
                restored.set_position(preset)
            else:
                assert payload['type'] == 'custom'
                if edge != 'free': assert payload['edge'] == edge
                restored.set_custom_position(payload['screen'],payload['cx'],payload['cy'],
                    edge=payload.get('edge'),t=payload.get('t',.5))
            restored.show(); restored._reposition()
            assert restored.geometry() == w.geometry()
            if edge == 'free': assert (w.x(),w.y()) == (x,y)
        finally:
            w.destroy(); restored.destroy(); w.deleteLater(); restored.deleteLater()


def test_compact_drag_sticks_and_comes_free_before_release(qapp):
    from unittest.mock import patch
    from PySide6.QtCore import QPointF, QEvent, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from samsara.ui.listening_indicator import ListeningIndicator
    fake = compact_screen((0,0,1920,1040))
    with patch.object(QApplication,'primaryScreen',return_value=fake), patch.object(QApplication,'screenAt',return_value=fake):
        w = ListeningIndicator()
        try:
            w.show(); w.enter_move_mode()
            local = QPointF(10,10); start = QPointF(w.x()+10,w.y()+10)
            w.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress,local,start,
                Qt.MouseButton.LeftButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier))
            before = deepcopy(w._custom_position)
            for x,y in ((10,1000),(700,400)):
                target = QPointF(x+10,y+10)
                w.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove,local,target,
                    Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier))
                if x == 10: assert w.x() == 8 and w.y()+w.height() == 1032
                else: assert (w.x(),w.y()) == (x,y)
                assert w._custom_position == before
        finally:w.destroy(); w.deleteLater()


@pytest.mark.parametrize('x,y', [(700,400),(700,880)])
def test_compact_free_drop_with_chip_preserves_released_bounds_and_legacy_record(qapp,x,y):
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    from samsara.ui.listening_indicator import ListeningIndicator
    fake = compact_screen((0,0,1920,1040))
    with patch.object(QApplication,'primaryScreen',return_value=fake), \
         patch.object(QApplication,'screens',return_value=[fake]), \
         patch.object(QApplication,'screenAt',return_value=fake):
        w = ListeningIndicator()
        try:
            w.show(); w.show_outcome('Synthetic wide draft message','pending',None)
            w.enter_move_mode(); w.move(x,y); w._commit_drag_position()
            assert (w.x(),w.y()) == (x,y)
            saved = deepcopy(w._custom_position)
            assert set(saved) == {'screen','cx','cy'}
            w.clear_outcome(); w._reposition()
            w.show_outcome('Synthetic new message','pending',None); w._reposition()
            assert w._custom_position == saved
        finally:w.destroy(); w.deleteLater()


def test_p2_idle_drop_no_reserved_review_envelope(qapp):
    screen=Screen("synthetic",Rect(0,0,1920,1040),True)
    c=controller(qapp,screen)
    try:
        c.widget.refresh(view(VisibleForm.STATUS),animate=False)
        c._move_requested({"center":(30,1010)})
        assert c.widget.y()+c.widget.height() == 1032
        assert c.widget.x() == 8
        # 492: idle has no empty 56-DIP band; header rises only for a notice.
        assert (c.widget.x()+30,c.widget.y()+30) == (38,986)
    finally:c.widget.hide(); c.widget.deleteLater()


def test_saved_free_record_and_missing_monitor_remain_unchanged(qapp):
    screen=Screen("synthetic",Rect(-1920,-120,1920,1040),True)
    saved=free_placement_for_mark((-1000,350),screen.work_area)
    c=controller(qapp,screen,saved)
    records=c.app.config["ui"]["live_surface"]["placement"]["monitors"]
    records["unplugged"]={"edge":"left","t":.2}
    before=deepcopy(c.app.config)
    try:
        for form in VisibleForm:
            c.widget.refresh(view(form,"Synthetic draft. "*9),animate=False)
            c._place_widget()
        assert c.app.config == before
        assert edge_mark_rect(saved,screen.work_area).center == (-1000,350)
    finally:c.widget.hide(); c.widget.deleteLater()


@pytest.mark.parametrize('area', [(0, 0, 800, 800), (0, 0, 1920, 1040),
                                 (-1280, -120, 1280, 693)])
@pytest.mark.parametrize('x,y', [(-10000, 400), (10000, 400), (400, -10000),
    (400, 10000), (-10000, -10000), (10000, -10000),
    (-10000, 10000), (10000, 10000)])
@pytest.mark.parametrize('visible', [False, True])
def test_compact_offscreen_drop_recovers_even_when_move_started_hidden(qapp, area, x, y, visible):
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    from samsara.ui.listening_indicator import ListeningIndicator
    fake = compact_screen(area)
    with patch.object(QApplication, 'primaryScreen', return_value=fake), \
         patch.object(QApplication, 'screens', return_value=[fake]), \
         patch.object(QApplication, 'screenAt', return_value=None):
        w = ListeningIndicator()
        try:
            if visible:
                w.show()
            w.enter_move_mode()
            dock, preview = w._snap_drag(x, y, fake)
            a = Rect(*area)
            safe = Rect(a.x + 8, a.y + 8, a.width - 16, a.height - 16)
            assert safe.contains(preview.bounds)
            w.move(x, y)
            w._commit_drag_position()
            assert safe.contains(Rect(w.x(), w.y(), w.width(), w.height()))
            assert w.isVisible() == visible
        finally:
            w.destroy(); w.deleteLater()


@pytest.mark.parametrize('width', [800, 801, 1920, 1921])
@pytest.mark.parametrize('corner', ['top-center', 'bottom-center'])
def test_compact_center_pill_has_zero_pixel_drift_for_odd_and_even_widths(qapp, width, corner):
    from unittest.mock import patch
    from PySide6.QtWidgets import QApplication
    from samsara.ui.listening_indicator import ListeningIndicator
    fake = compact_screen((0, 0, width, 1040))
    with patch.object(QApplication, 'primaryScreen', return_value=fake):
        w = ListeningIndicator()
        try:
            w.set_position(corner); w.show()
            def pill_bounds():
                pill = w._layout()[2]
                return (w.x() + int(pill.x()), w.y() + int(pill.y()),
                        int(pill.width()), int(pill.height()))
            before = pill_bounds()
            for message in ('refused: focus lock', 'Synthetic wider message', 'Synthetic odd width'):
                w.show_outcome(message, 'pending', None)
                assert pill_bounds() == before
                w.clear_outcome()
                assert pill_bounds() == before
        finally:
            w.destroy(); w.deleteLater()
