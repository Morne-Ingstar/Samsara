"""Open the History entry belonging to a dictation result."""
from PySide6.QtCore import QSignalBlocker


class HistoryEntryNavigation:
    def open_entry(self, entry_id):
        """Open the exact linked result, independent of filters or newer rows."""
        from samsara.ui.history_view import FILTER_ALL, _SCOPE_ALL
        for control, change in ((self._search, lambda: self._search.clear()),
                                (self._filter, lambda: self._filter.setCurrentText(FILTER_ALL)),
                                (self._scope, lambda: self._scope.setCurrentText(_SCOPE_ALL))):
            with QSignalBlocker(control):
                change()
        self._scope.setEnabled(True)
        self._request_generation += 1
        rows = self._store.query(limit=1, before_id=entry_id + 1) if self._store and entry_id else []
        rows = [dict(row) for row in rows if row['id'] == entry_id]
        self._has_more = False
        self._render_rows(rows, append=False)
        for index in range(self._list.count()):
            item = self._list.item(index)
            data = self._row_data_for_item(item)
            if data and data.get('id') == entry_id:
                self._list.setCurrentItem(item)
                return True
        self._show_toast('That History entry is no longer available.')
        return False
