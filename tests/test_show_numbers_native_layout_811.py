"""811: native target filtering, bounded placement, style and DPI regressions.

Fixtures reconstruct the reported toolbar shapes; they are explicitly NOT
live UIA captures. The tests run offline without native providers or apps.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect
from samsara.uia.service import Node, UIAService, overlay_target_nodes, _overlay_actionable
from samsara.ui import numbers_overlay_qt as overlay
from plugins.commands import show_numbers as sn

FIXTURES = Path(__file__).parent / 'fixtures' / '811'


def load_fixture(name, scale=1.0, origin=(0, 0)):
    data = json.loads((FIXTURES / (name+'.json')).read_text(encoding='utf-8'))
    nodes = []
    for i, row in enumerate(data['nodes']):
        rect = tuple(round(v*scale)+origin[j % 2] for j, v in enumerate(row['rect']))
        nodes.append(Node(1, 1, i, row['name'], row['type'], rect,
                          row.get('enabled', True), row.get('offscreen', False),
                          False, row['depth']))
    return data, nodes


def elements_for(nodes):
    return [dict(control=node.index, prect=node.rect, rect=node.rect, name=node.name,
                 type=node.control_type) for node in nodes]


def overlaps(a, b):
    return a[0] < b[0]+b[2] and b[0] < a[0]+a[2] and a[1] < b[1]+b[3] and b[1] < a[1]+a[3]


@pytest.mark.parametrize('name,count,old', [('firefox',26,68), ('notepad',14,36), ('explorer',17,44)])
@pytest.mark.parametrize('scale', [1.0, 1.5])
@pytest.mark.parametrize('origin', [(0,0), (3840,-600)])
def test_fixture_labels_stay_on_their_targets_without_overlaps(name, count, old, scale, origin):
    data, nodes = load_fixture(name, scale, origin)
    selected = overlay_target_nodes(nodes)
    assert len(selected) == data['expected_count'] == count
    assert data['input_count'] == old
    # Synthetic fixtures do not purport to measure the owner's app counts.
    assert data['source'].startswith('reconstructed')
    pl, pt = origin
    mappings = [((pl,pt,pl+round(1200*scale),pt+round(800*scale)),
                 (pl,pt,pl+1200,pt+800), scale)]
    plan = sn.plan_overlay(elements_for(selected), nodes[0].rect, mappings)
    assert len(plan['labels']) == count
    assert [e['control'] for e in plan['kept']] == [node.index for node in selected]
    for i, badge in enumerate(plan['labels']):
        x,y,w,h,text = badge
        l,t,r,b = plan['kept'][i]['rect']
        assert l <= x+w/2 < r and t <= y+h/2 < b
        assert badge.target == (l,t,r,b)
        assert text == str(i+1)
        assert not badge.collision
        assert not any(overlaps(badge, other) for other in plan['labels'][:i])


def node(index, rect, depth=1, typ='ButtonControl', **changes):
    args = dict(hwnd=1,generation=1,index=index,name=str(index),control_type=typ,
                rect=rect,enabled=True,offscreen=False,in_document=False,depth=depth)
    return Node(**dict(args, **changes))


def test_clipping_offscreen_disabled_and_noninteractive_controls_are_excluded():
    nodes = [node(0,(0,0,800,600),0,'WindowControl'),
             node(1,(10,10,100,80),1,'PaneControl'),
             node(2,(20,20,60,50),2),  # visible, clipped by neither ancestor
             node(3,(90,20,120,50),2), # partially clipped by parent
             node(4,(0,0,0,0)), node(5,(20,20,60,50),offscreen=True),
             node(6,(20,20,60,50),enabled=False),
             node(7,(20,20,60,50),typ='TextControl'),
             node(8,(20,20,60,50),typ='StatusBarControl')]
    assert [n.index for n in overlay_target_nodes(nodes)] == [2]


def test_ancestor_duplicate_is_dropped_but_tab_and_close_have_distinct_actions():
    nodes = [node(0,(0,0,800,600),0,'WindowControl'),
             node(1,(10,20,200,60),typ='TabItemControl'),
             node(2,(170,25,195,55),2),
             node(3,(220,20,260,60)), node(4,(220,20,260,60),2)]
    assert [n.index for n in overlay_target_nodes(nodes)] == [1,2,4]


def test_offscreen_ancestor_hides_descendants_even_with_valid_rectangles():
    nodes=[node(0,(0,0,800,600),0,'WindowControl'),
           node(1,(10,10,300,200),typ='PaneControl',offscreen=True),
           node(2,(20,20,60,50),2)]
    assert overlay_target_nodes(nodes) == []


@pytest.mark.parametrize('getter', ['GetInvokePattern','GetTogglePattern','GetSelectionItemPattern','GetExpandCollapsePattern'])
def test_custom_controls_can_be_actionable_by_pattern(getter):
    ctrl=SimpleNamespace(**{getter:lambda:object()})
    assert _overlay_actionable(ctrl,'CustomControl')
    assert not _overlay_actionable(ctrl,'TextControl')
    ctrl.IsKeyboardFocusable=True
    assert _overlay_actionable(ctrl,'TextControl')


def test_service_applies_filter_before_cap_and_returns_original_click_targets():
    class Control:
        IsEnabled, IsOffscreen, NativeWindowHandle = True, False, 0
        def __init__(self, typ, rect, children=()):
            from samsara.uia.backend import Rect
            self.ControlTypeName, self.Name = typ, typ
            self.BoundingRectangle, self.children = Rect(*rect), children
        def GetChildren(self): return self.children
    leaf=Control('ButtonControl',(10,10,50,40))
    parent=Control('ButtonControl',(10,10,50,40),[leaf])
    root=Control('WindowControl',(0,0,800,600),[parent])
    service=UIAService()
    try:
        found,stats=service.overlay_elements(loader=lambda:object(),foreground=lambda auto:root)
        assert [entry['control'] for entry in found] == [leaf]
        assert found[0]['prect'] == (10,10,50,40)
        assert stats['candidates'] == 1
    finally: service.close()


def test_later_badge_is_nudged_within_its_own_target():
    labels=overlay.layout_target_labels([{'rect':(10,10,50,50)}, {'rect':(10,10,80,60)}])
    assert labels[0][:2] == [10,10]
    assert labels[1][1] == 10
    assert not overlaps(*labels)
    assert labels[1][0]+labels[1][2] <= 80


def test_compact_size_resolves_dense_controls_and_centres_very_narrow_targets():
    labels=overlay.layout_target_labels([{'rect':(0,0,24,18)}, {'rect':(20,0,40,18)}])
    assert labels[1].compact
    assert not overlaps(*labels)
    narrow=overlay.layout_target_labels([{'rect':(100,100,108,108)}])[0]
    assert narrow[0]+narrow[2]/2 == 104 and narrow[1]+narrow[3]/2 == 104


def test_impossible_collisions_retain_numbers_and_both_target_outlines():
    labels=overlay.layout_target_labels([{'rect':(10,10,18,18)}]*2)
    assert [label[4] for label in labels] == ['1','2']
    assert all(label.collision for label in labels)
    assert all(label.compact for label in labels[1:])
    assert labels[0].target == labels[1].target == (10,10,18,18)


def test_exact_monitor_boundary_uses_one_transform_for_both_target_corners():
    maps=[((0,0,3840,2160),(0,0,2560,1440),1.5),
          ((3840,0,5760,1080),(3840,0,5760,1080),1.0)]
    assert overlay.map_physical_rect_to_qt((3780,150,3840,210), maps) == (2520,100,2560,140)
    plan=sn.plan_overlay([{'prect':(3780,150,3840,210),'control':1}], (0,0,3840,2160),maps)
    assert plan['labels'][0].target == (2520,100,2560,140)


def test_dom_embedding_is_generated_from_the_same_style_as_native():
    source=(Path(__file__).resolve().parents[1]/'browser_extension/content.js').read_text(encoding='utf-8')
    embedded=source.split('style.textContent = ',1)[1].split(';\n',1)[0]
    assert json.loads(embedded) == overlay._BADGE_CSS
    assert overlay._pill_bg().name() == '#1a1a1f'
    assert overlay._text_colour().name().lower() == '#5eead4'
    assert overlay._pill_border().alpha() == 153


def test_renderer_uses_final_coordinates_and_draws_collision_target_offscreen(qapp):
    labels=overlay.layout_target_labels([{'rect':(100,100,108,108)}]*2)
    win=overlay.NumbersOverlayWindow(labels,qapp.primaryScreen())
    try:
        win.highlight_number(2)
        img=win.grab().toImage()
        x,y,w,h,_=labels[1]
        assert img.pixelColor(round(x+w/2),round(y+2)).alpha() > 100
        # The teal target outline must remain visible ABOVE the opaque badge.
        edge=img.pixelColor(100,104)
        assert edge.green() > edge.red()+30 and edge.blue() > edge.red()+20
        assert img.pixelColor(80,80).alpha() == 0
        assert not win.isVisible()
        assert win.windowFlags() & overlay.Qt.WindowTransparentForInput
    finally: win.deleteLater()

