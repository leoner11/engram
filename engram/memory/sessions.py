"""Session lifecycle: start, end, auto-close, auto-summary."""

from __future__ import annotations

import uuid

from engram.graph.store import GraphStore


class SessionManager:
    """Manages session lifecycle for observation grouping."""

    def __init__(self, store: GraphStore):
        self.store = store
        self.active_session: str | None = None

    def start(self, project: str) -> str:
        """
        Start a new session. Returns session ID.
        Auto-closes any dangling previous session.
        """
        if self.active_session:
            self.end()

        session_id = uuid.uuid4().hex[:8]
        self.store.conn.execute(
            "INSERT INTO sessions (id, project) VALUES (?, ?)",
            (session_id, project),
        )
        self.store.conn.commit()
        self.active_session = session_id
        return session_id

    def end(self, summary: str | None = None) -> str | None:
        """
        End the active session.
        Auto-generates summary from observation titles if not provided.
        """
        if not self.active_session:
            return None

        if summary is None:
            summary = self._auto_summary(self.active_session)

        self.store.conn.execute(
            "UPDATE sessions SET ended_at = datetime('now'), summary = ? WHERE id = ?",
            (summary, self.active_session),
        )
        self.store.conn.commit()
        ended = self.active_session
        self.active_session = None
        return ended

    def ensure_session(self, project: str) -> str:
        """Return active session ID, auto-creating one if needed."""
        if self.active_session is None:
            return self.start(project)
        return self.active_session

    def get_active(self) -> str | None:
        """Return active session ID, or None."""
        return self.active_session

    def get_recent(self, project: str, limit: int = 5) -> list[dict]:
        """Return recent sessions for a project, newest first."""
        rows = self.store.conn.execute(
            """SELECT s.*, COUNT(o.id) as observation_count
               FROM sessions s
               LEFT JOIN observations o ON o.session_id = s.id
               WHERE s.project = ?
               GROUP BY s.id
               ORDER BY s.rowid DESC
               LIMIT ?""",
            (project, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def _auto_summary(self, session_id: str) -> str:
        """Generate summary from observation titles."""
        rows = self.store.conn.execute(
            "SELECT title FROM observations WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        titles = [r["title"] for r in rows]
        summary = "; ".join(titles) if titles else "Empty session"
        return summary[:200] if len(summary) > 200 else summary
