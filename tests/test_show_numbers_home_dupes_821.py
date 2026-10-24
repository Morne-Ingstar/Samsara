"""Home duplicate-button filtering for Show Numbers (821)."""
from samsara.uia.service import Node, overlay_target_nodes


def node(index, rect, depth=1, typ='ButtonControl'):
    return Node(1, 1, index, str(index), typ, rect, True, False, False, depth)


def test_nested_container_and_large_child_produce_one_number():
    parent = node(1, (10, 10, 210, 70))
    child = node(2, (5, 8, 215, 72), depth=2)
    assert overlay_target_nodes([parent, child]) == [parent]


def test_real_neighbouring_buttons_each_produce_a_number():
    left = node(1, (10, 10, 100, 70))
    right = node(2, (110, 10, 200, 70))
    assert overlay_target_nodes([left, right]) == [left, right]


def test_small_nested_close_button_remains_independently_numbered():
    tab = node(1, (10, 10, 210, 50), typ='TabItemControl')
    close = node(2, (180, 15, 200, 40), depth=2)
    assert overlay_target_nodes([tab, close]) == [tab, close]
