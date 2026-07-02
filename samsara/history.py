"""Persistent dictation history backed by SQLite.

Stores every transcription attempt -- including failures and empty results --
so the user can recover text, audit mistakes, and verify the app is working.
The database lives in ~/.samsara/history.db (user data, not project data).
"""

import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from samsara.log import get_logger

logger = get_logger(__name__)

DB_PATH = Path.home() / ".samsara" / "history.db"


class HistoryManager:
    """Thread-safe SQLite store for dictation history."""

    def __init__(self, db_path=None):
        path = Path(db_path) if db_path is not None else DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.session_id = str(uuid.uuid4())
        self._create_tables()
        self._migrate()

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

    def _migrate(self):
        """Add new columns when upgrading from an older schema. Safe to run every start."""
        new_cols = [
            ("session_id",       "TEXT DEFAULT ''"),
            ("entry_type",       "TEXT DEFAULT 'dictation'"),
            ("log_prob",         "REAL DEFAULT NULL"),
            ("matched_command",  "TEXT DEFAULT NULL"),
        ]
        with self._lock:
            for col, definition in new_cols:
                try:
                    self._conn.execute(
                        f"ALTER TABLE history ADD COLUMN {col} {definition}")
                    self._conn.commit()
                except sqlite3.OperationalError as e:
                    logger.debug(f"_migrate: {e}")

    def add(self, raw_text, display_text="", app_context="",
            duration_ms=0, mode="hold", status="success",
            audio_path=None, entry_type="dictation",
            log_prob=None, matched_command=None):
        """Add a transcription to history. Returns the row id."""
        if not display_text:
            display_text = raw_text
        timestamp = datetime.now().isoformat()
        with self._lock:
            cursor = self._conn.execute("""
                INSERT INTO history
                    (timestamp, app_context, raw_text, display_text,
                     duration_ms, mode, status, audio_path,
                     session_id, entry_type, log_prob, matched_command)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, app_context, raw_text, display_text,
                  duration_ms, mode, status, audio_path,
                  self.session_id, entry_type, log_prob, matched_command))
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

    def recent(self, limit=50, offset=0):
        """Get most recent entries. Optional offset for scroll-driven pagination."""
        with self._lock:
            return self._conn.execute("""
                SELECT * FROM history
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()

    def recent_filtered(self, status, limit=500, offset=0):
        """Get recent entries filtered by status. Pushes filter to SQL."""
        with self._lock:
            return self._conn.execute("""
                SELECT * FROM history WHERE status = ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, (status, limit, offset)).fetchall()

    def get_sessions(self, limit=20):
        """Return distinct sessions ordered newest first, with start time and entry count."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT
                    session_id,
                    MIN(timestamp) AS session_start,
                    COUNT(*)       AS entry_count
                FROM history
                GROUP BY session_id
                ORDER BY session_start DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_session_stats(self, session_id):
        """Return dict with stats for a session: total, successes, failures, session_start."""
        with self._lock:
            row = self._conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS successes,
                    COALESCE(SUM(CASE WHEN status = 'failed'  THEN 1 ELSE 0 END), 0) AS failures,
                    MIN(timestamp) AS session_start
                FROM history WHERE session_id = ?
            """, (session_id,)).fetchone()
        if row is None:
            return {'total': 0, 'successes': 0, 'failures': 0, 'session_start': None}
        return dict(row)

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
