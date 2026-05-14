"""Tests for samsara.history (HistoryManager / SQLite persistence).

All tests use a temp file for the DB -- no display required, no mocking
of the UI layer.
"""

import os
import tempfile
import uuid

import pytest

from samsara.history import HistoryManager


@pytest.fixture
def tmp_db(tmp_path):
    """Yield a fresh DB path in a temp directory."""
    return str(tmp_path / "history.db")


# ---------------------------------------------------------------------------
# Schema / first-run
# ---------------------------------------------------------------------------

def test_history_db_creates_on_first_run(tmp_db):
    assert not os.path.exists(tmp_db)
    hm = HistoryManager(db_path=tmp_db)
    assert os.path.exists(tmp_db)

    # table and all expected columns must exist
    with hm._lock:
        cols = {row[1] for row in hm._conn.execute(
            "PRAGMA table_info(history)").fetchall()}
    for col in ("id", "timestamp", "raw_text", "display_text",
                "status", "session_id", "entry_type", "log_prob",
                "matched_command"):
        assert col in cols, f"column '{col}' missing from schema"

    hm.close()


def test_migration_adds_new_columns(tmp_db):
    """Opening a DB that lacks the new columns should add them via _migrate."""
    import sqlite3
    # Create an old-style DB without new columns
    conn = sqlite3.connect(tmp_db)
    conn.execute("""
        CREATE TABLE history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            display_text TEXT NOT NULL,
            status TEXT DEFAULT 'success'
        )
    """)
    conn.commit()
    conn.close()

    # Opening via HistoryManager should add the missing columns
    hm = HistoryManager(db_path=tmp_db)
    with hm._lock:
        cols = {row[1] for row in hm._conn.execute(
            "PRAGMA table_info(history)").fetchall()}
    for col in ("session_id", "entry_type", "log_prob", "matched_command"):
        assert col in cols
    hm.close()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_history_entry_persists(tmp_db):
    hm = HistoryManager(db_path=tmp_db)
    row_id = hm.add(raw_text="hello world", status="success",
                    entry_type="dictation")
    hm.close()

    # Re-open and verify
    hm2 = HistoryManager(db_path=tmp_db)
    rows = hm2.recent(limit=10)
    assert any(r['id'] == row_id and r['raw_text'] == "hello world"
               for r in rows)
    hm2.close()


def test_session_id_changes_on_restart(tmp_db):
    hm1 = HistoryManager(db_path=tmp_db)
    sid1 = hm1.session_id
    hm1.close()

    hm2 = HistoryManager(db_path=tmp_db)
    sid2 = hm2.session_id
    hm2.close()

    assert sid1 != sid2
    # Both must be valid UUID strings
    uuid.UUID(sid1)
    uuid.UUID(sid2)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def test_filter_successes(tmp_db):
    hm = HistoryManager(db_path=tmp_db)
    hm.add("good dictation", status="success", entry_type="dictation")
    hm.add("another good", status="success", entry_type="command")
    hm.add("bad one",       status="failed",  entry_type="failed")
    hm.add("silent",        status="empty",   entry_type="failed")

    rows = hm.recent_filtered("success", limit=100)
    assert len(rows) == 2
    assert all(r['status'] == 'success' for r in rows)
    hm.close()


def test_filter_failed(tmp_db):
    hm = HistoryManager(db_path=tmp_db)
    hm.add("ok",   status="success", entry_type="dictation")
    hm.add("fail", status="failed",  entry_type="failed")

    rows = hm.recent_filtered("failed", limit=100)
    assert len(rows) == 1
    assert rows[0]['status'] == 'failed'
    hm.close()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_filters_by_substring(tmp_db):
    hm = HistoryManager(db_path=tmp_db)
    hm.add("open Chrome browser", status="success")
    hm.add("lights red",          status="success")
    hm.add("going dark mode",     status="success")

    results = hm.search("chrome")
    assert len(results) == 1
    assert "chrome" in results[0]['raw_text'].lower()

    results = hm.search("LIGHTS")   # case-insensitive via LIKE
    assert len(results) == 1
    hm.close()


# ---------------------------------------------------------------------------
# Confidence band helper (logic only, no UI)
# ---------------------------------------------------------------------------

def test_confidence_band():
    """Verify the color-band logic used by HistoryFrame without importing Tk."""

    def confidence_color(log_prob):
        if log_prob is None:
            return 'gray'
        if log_prob > -0.5:
            return 'green'
        if log_prob > -1.0:
            return 'yellow'
        return 'red'

    assert confidence_color(0.0)  == 'green'
    assert confidence_color(-0.4) == 'green'
    assert confidence_color(-0.5) == 'yellow'   # boundary: > -0.5 is green, else yellow
    assert confidence_color(-0.9) == 'yellow'
    assert confidence_color(-1.0) == 'red'      # boundary: > -1.0 is yellow, else red
    assert confidence_color(-2.0) == 'red'
    assert confidence_color(None) == 'gray'


# ---------------------------------------------------------------------------
# Session stats
# ---------------------------------------------------------------------------

def test_session_stats(tmp_db):
    hm = HistoryManager(db_path=tmp_db)
    sid = hm.session_id
    hm.add("cmd1", status="success", entry_type="command")
    hm.add("cmd2", status="success", entry_type="command")
    hm.add("fail", status="failed",  entry_type="failed")

    stats = hm.get_session_stats(sid)
    assert stats['successes'] == 2
    assert stats['failures']  == 1
    assert stats['session_start'] is not None
    hm.close()


def test_session_stats_different_session(tmp_db):
    hm = HistoryManager(db_path=tmp_db)
    hm.add("mine",       status="success")
    stats = hm.get_session_stats("nonexistent-session-id")
    assert stats['successes'] == 0
    assert stats['failures']  == 0
    hm.close()


# ---------------------------------------------------------------------------
# Phase 2 additions
# ---------------------------------------------------------------------------

def test_load_recent_respects_limit(tmp_db):
    hm = HistoryManager(db_path=tmp_db)
    for i in range(20):
        hm.add(f"entry {i}", status="success")
    rows = hm.recent(limit=5)
    assert len(rows) == 5
    hm.close()


def test_load_recent_newest_first(tmp_db):
    import time as _time
    hm = HistoryManager(db_path=tmp_db)
    hm.add("first",  status="success")
    _time.sleep(0.01)
    hm.add("second", status="success")
    _time.sleep(0.01)
    hm.add("third",  status="success")
    rows = hm.recent(limit=10)
    texts = [r['raw_text'] for r in rows]
    assert texts[0] == "third"
    assert texts[-1] == "first"
    hm.close()


def test_search_no_match(tmp_db):
    hm = HistoryManager(db_path=tmp_db)
    hm.add("open Chrome",  status="success")
    hm.add("lights green", status="success")
    results = hm.search("xyzzy_nonexistent")
    assert len(results) == 0
    hm.close()


def test_get_sessions(tmp_db):
    import sqlite3, uuid as _uuid
    hm = HistoryManager(db_path=tmp_db)

    # Write entries under a second fake session_id
    fake_sid = _uuid.uuid4().hex[:12]
    with hm._lock:
        for text in ("alpha", "beta", "gamma"):
            hm._conn.execute("""
                INSERT INTO history
                    (timestamp, raw_text, display_text, status,
                     session_id, entry_type)
                VALUES (datetime('now'), ?, ?, 'success', ?, 'dictation')
            """, (text, text, fake_sid))
        hm._conn.commit()

    # Current session has no entries yet; add one
    hm.add("delta", status="success")

    sessions = hm.get_sessions(limit=20)
    session_ids = [s['session_id'] for s in sessions]
    assert hm.session_id in session_ids
    assert fake_sid in session_ids

    # Each session has an entry_count
    for s in sessions:
        assert s['entry_count'] >= 1
        assert s['session_start'] is not None

    # Ordered newest first
    assert sessions[0]['session_start'] >= sessions[-1]['session_start']
    hm.close()
