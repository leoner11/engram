"""Tests for session lifecycle."""

import pytest
from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.memory.sessions import SessionManager


@pytest.fixture
def session_mgr(tmp_path):
    db = EngramDB(tmp_path)
    store = GraphStore(db)
    return SessionManager(store), store


def test_start_session(session_mgr):
    mgr, _ = session_mgr
    sid = mgr.start("test-project")
    assert sid is not None
    assert len(sid) == 8
    assert mgr.active_session == sid


def test_end_session(session_mgr):
    mgr, store = session_mgr
    sid = mgr.start("test-project")
    ended = mgr.end(summary="Did some work")
    assert ended == sid
    assert mgr.active_session is None

    # Verify in DB
    row = store.conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    assert row["summary"] == "Did some work"
    assert row["ended_at"] is not None


def test_auto_close_dangling(session_mgr):
    mgr, store = session_mgr
    sid1 = mgr.start("test-project")
    sid2 = mgr.start("test-project")

    # sid1 should be auto-closed
    row = store.conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (sid1,)).fetchone()
    assert row["ended_at"] is not None
    assert mgr.active_session == sid2


def test_auto_summary(session_mgr):
    mgr, store = session_mgr
    sid = mgr.start("test-project")

    # Add some observations manually
    store.conn.execute(
        "INSERT INTO observations (session_id, type, title, content, project) VALUES (?, ?, ?, ?, ?)",
        (sid, "bugfix", "Fixed the webhook", "Details here", "test-project"),
    )
    store.conn.execute(
        "INSERT INTO observations (session_id, type, title, content, project) VALUES (?, ?, ?, ?, ?)",
        (sid, "decision", "Switched to JWT", "More details", "test-project"),
    )
    store.conn.commit()

    mgr.end()

    row = store.conn.execute("SELECT summary FROM sessions WHERE id = ?", (sid,)).fetchone()
    assert "Fixed the webhook" in row["summary"]
    assert "Switched to JWT" in row["summary"]


def test_ensure_session_creates(session_mgr):
    mgr, _ = session_mgr
    assert mgr.active_session is None
    sid = mgr.ensure_session("test-project")
    assert sid is not None
    assert mgr.active_session == sid


def test_ensure_session_reuses(session_mgr):
    mgr, _ = session_mgr
    sid1 = mgr.start("test-project")
    sid2 = mgr.ensure_session("test-project")
    assert sid1 == sid2


def test_get_recent(session_mgr):
    mgr, _ = session_mgr
    mgr.start("proj")
    mgr.end("First session")
    mgr.start("proj")
    mgr.end("Second session")

    recent = mgr.get_recent("proj", limit=5)
    assert len(recent) == 2
    assert recent[0]["summary"] == "Second session"  # Newest first


def test_end_no_active(session_mgr):
    mgr, _ = session_mgr
    result = mgr.end()
    assert result is None
