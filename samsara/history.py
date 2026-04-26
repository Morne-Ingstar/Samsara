"""Persistent dictation history backed by SQLite.

Stores every transcription attempt -- including failures and empty results --
so the user can recover text, audit mistakes, and verify the app is working.
The database lives in ~/.samsara/history.db (user data, not project data).
"""

import sqlite3
import threading
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / ".samsara" / "history.db"


class HistoryManager:
    """Thread-safe SQLite store for dictation history."""

    def __init__(self, db_path=None):
        path = Path(db_path) if db_path is not None else DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._create_tables()

    def _create_tables(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    app_context TEXT DEFAULT '',
                    raw_text TEXT NOT NULL,
                    display_text TEXT NOT NULL,
                    duration_ms INTEGER DEFAULT 0,
                    mode TEXT DEFAULT 'hold',
                    status TEXT DEFAULT 'success',
                    audio_path TEXT DEFAULT NULL,
                    correction_source TEXT DEFAULT NULL
                )
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON history(timestamp DESC)
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_status
                ON history(status)
            """)
            self._conn.commit()

    def add(self, raw_text, display_text="", app_context="",
            duration_ms=0, mode="hold", status="success",
            audio_path=None):
        """Add a transcription to history. Returns the row id."""
        if not display_text:
            display_text = raw_text
        timestamp = datetime.now().isoformat()
        with self._lock:
            cursor = self._conn.execute("""
                INSERT INTO history
                (timestamp, app_context, raw_text, display_text,
                 duration_ms, mode, status, audio_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, app_context, raw_text, display_text,
                  duration_ms, mode, status, audio_path))
            self._conn.commit()
            return cursor.lastrowid

    def update_status(self, row_id, status, display_text=None):
        """Update status (e.g., transcribing -> success or failed)."""
        with self._lock:
            if display_text:
                self._conn.execute(
                    "UPDATE history SET status=?, display_text=? WHERE id=?",
                    (status, display_text, row_id))
            else:
                self._conn.execute(
                    "UPDATE history SET status=? WHERE id=?",
                    (status, row_id))
            self._conn.commit()

    def search(self, query, limit=50):
        """Substring search across raw_text and display_text."""
        with self._lock:
            return self._conn.execute("""
                SELECT * FROM history
                WHERE raw_text LIKE ? OR display_text LIKE ?
                ORDER BY timestamp DESC LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit)).fetchall()

    def recent(self, limit=50):
        """Get most recent entries."""
        with self._lock:
            return self._conn.execute("""
                SELECT * FROM history
                ORDER BY timestamp DESC LIMIT ?
            """, (limit,)).fetchall()

    def delete(self, row_id):
        """Delete a single entry."""
        with self._lock:
            self._conn.execute("DELETE FROM history WHERE id=?", (row_id,))
            self._conn.commit()

    def prune(self, max_entries=10000):
        """Remove oldest entries beyond the limit."""
        with self._lock:
            self._conn.execute("""
                DELETE FROM history WHERE id NOT IN (
                    SELECT id FROM history ORDER BY timestamp DESC LIMIT ?
                )
            """, (max_entries,))
            self._conn.commit()

    def get_failed(self):
        """Get all failed transcriptions."""
        with self._lock:
            return self._conn.execute("""
                SELECT * FROM history WHERE status='failed'
                ORDER BY timestamp DESC
            """).fetchall()

    def close(self):
        with self._lock:
            self._conn.close()
