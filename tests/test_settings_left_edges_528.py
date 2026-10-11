"""Settings titles align unless the row is a declared hands-free dependent."""
from PySide6.QtCore import QPoint
from tests.test_settings_calm_layout_459 import settings, settle, reveal


# Runtime ducking ignores these stored levels while hands-free audio is off.
# They retain the existing visual nesting below that switch.
DEPENDENTS = {
    'adv_hands_free_ducking_level': 'adv_hands_free_ducking_enabled',
    'adv_hands_free_idle_level': 'adv_hands_free_ducking_enabled',
}


def test_visible_settings_titles_share_the_page_left_edge(settings, qapp):
    for name in ('General', 'Dictation', 'Audio', 'Ava', 'Shortcuts', 'Help'):
        page = reveal(settings, name, qapp)
        settle(qapp)
        rows = [row for row in settings._destination_rows
                if row.page_name == name and row.isVisibleTo(page)]
        regular = [row for row in rows if row.widget_keys[0] not in DEPENDENTS]
        edges = {row.title: row.heading.mapTo(page.widget(), QPoint()).x()
                 for row in regular}
        assert edges, name
        assert max(edges.values()) - min(edges.values()) <= 1, (name, edges)
        for row in rows:
            parent_key = DEPENDENTS.get(row.widget_keys[0])
            if parent_key:
                parent = settings._rows_by_widget[parent_key]
                assert parent in rows
                assert row.heading.mapTo(page.widget(), QPoint()).x() > edges[parent.title]
