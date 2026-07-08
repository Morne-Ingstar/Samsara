"""Task-shaped façade over samsara.history.HistoryManager.

Dictation history is already fully SQLite-backed via HistoryManager
(~/.samsara/history.db, wired into DictationApp at boot as
`self.history_db`) -- this module does NOT open a second database or
duplicate that persistence layer. It exists to give the redesigned
history list view (samsara/ui/history_qt.py) a simpler, purpose-shaped
API -- append/query/delete/clear -- than HistoryManager's richer
session-tracking surface, while delegating every call straight through to
one HistoryManager instance.

Audit note (cp1252 UnicodeEncodeError): HistoryManager does no file I/O of
its own (pure sqlite3 -- SQLite TEXT columns are UTF-8 native, no encoding=
argument to get wrong). dictation.py's history.json load/save
(load_history/save_history) already pass encoding='utf-8' explicitly. No
history-related open() call in the codebase was found missing
encoding='utf-8' -- there is currently no history export/import feature at
all. Nothing to fix; documented here so this audit doesn't need repeating.
"""

from samsara.log import get_logger

logger = get_logger(__name__)


class HistoryStore:
    """Thin wrapper around a HistoryManager instance (or None)."""

    def __init__(self, manager):
        self._manager = manager

    def append(self, entry_type: str, text: str):
        """Add an entry. Returns the new row id, or None if history is
        unavailable (never raises -- history logging must not break
        dictation)."""
        if self._manager is None:
            return None
        try:
            return self._manager.add(raw_text=text, display_text=text, entry_type=entry_type)
        except Exception as exc:
            logger.debug(f"[HISTORY] append failed: {exc}")
            return None

    def query(self, search: "str | None" = None, type_filter: "str | None" = None,
              limit: int = 200, before_id: "int | None" = None):
        """Windowed read: optional substring search, optional entry_type
        filter, optional before_id for "load older" pagination. Returns a
        list of sqlite3.Row (indexable by column name), or [] if history
        is unavailable."""
        if self._manager is None:
            return []
        try:
            return list(self._manager.recent_windowed(
                search=search, entry_type=type_filter, limit=limit, before_id=before_id,
            ))
        except Exception as exc:
            logger.debug(f"[HISTORY] query failed: {exc}")
            return []

    def delete(self, ids):
        """Delete one or more entries by row id."""
        if self._manager is None:
            return
        for row_id in ids:
            try:
                self._manager.delete(row_id)
            except Exception as exc:
                logger.debug(f"[HISTORY] delete({row_id}) failed: {exc}")

    def clear(self):
        """Delete every entry."""
        if self._manager is None:
            return
        try:
            self._manager.prune(max_entries=0)
        except Exception as exc:
            logger.debug(f"[HISTORY] clear failed: {exc}")
