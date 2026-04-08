"""Observation CRUD: save, upsert, dedup, and node linking."""

from __future__ import annotations

import hashlib
from engram.graph.store import GraphStore
from engram.memory.sessions import SessionManager


VALID_TYPES = {"decision", "bugfix", "architecture", "discovery", "preference", "issue"}


class ObservationManager:
    """Manages observations with topic-key upsert and node linking."""

    def __init__(self, store: GraphStore, session_mgr: SessionManager):
        self.store = store
        self.session_mgr = session_mgr

    def save(
        self,
        title: str,
        content: str,
        type: str,
        project: str,
        topic_key: str | None = None,
        node_ids: list[str] | None = None,
    ) -> int:
        """
        Store an observation. Returns observation ID.

        Steps:
        1. Ensure session active
        2. Compute source_hash for dedup
        3. topic_key upsert or insert
        4. Sync FTS5
        5. Link to code nodes
        """
        if type not in VALID_TYPES:
            raise ValueError(f"Invalid type '{type}'. Must be one of: {VALID_TYPES}")

        session_id = self.session_mgr.ensure_session(project)
        source_hash = self._compute_hash(title, content)

        # Dedup: skip if exact same content already exists
        existing_dup = self.store.conn.execute(
            "SELECT id FROM observations WHERE source_hash = ? AND project = ?",
            (source_hash, project),
        ).fetchone()
        if existing_dup:
            return existing_dup["id"]

        # Topic-key upsert
        if topic_key:
            existing = self.get_by_topic_key(topic_key, project)
            if existing:
                obs_id = existing["id"]
                # Delete old FTS entry
                self._delete_fts(obs_id, existing["title"], existing["content"],
                                 existing["type"], existing.get("project", ""))
                # Update
                self.store.conn.execute(
                    """UPDATE observations
                       SET title = ?, content = ?, type = ?, source_hash = ?,
                           updated_at = datetime('now'), session_id = ?
                       WHERE id = ?""",
                    (title, content, type, source_hash, session_id, obs_id),
                )
                self.store.conn.commit()
                # Sync FTS
                self._insert_fts(obs_id, title, content, type, project)
                # Re-link nodes
                self._clear_node_links(obs_id)
                self._link_nodes(obs_id, title + " " + content, node_ids)
                return obs_id

        # Fresh insert
        cursor = self.store.conn.execute(
            """INSERT INTO observations (session_id, type, title, content, project, topic_key, source_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, type, title, content, project, topic_key, source_hash),
        )
        self.store.conn.commit()
        obs_id = cursor.lastrowid

        # Sync FTS
        self._insert_fts(obs_id, title, content, type, project)

        # Link nodes
        self._link_nodes(obs_id, title + " " + content, node_ids)

        self.store.conn.commit()
        return obs_id

    def get(self, observation_id: int) -> dict | None:
        """Get full observation by ID with linked nodes."""
        row = self.store.conn.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        if row is None:
            return None

        result = dict(row)
        # Get linked nodes
        links = self.store.conn.execute(
            "SELECT node_id, source FROM observation_nodes WHERE observation_id = ?",
            (observation_id,),
        ).fetchall()
        result["linked_nodes"] = [{"node_id": l["node_id"], "source": l["source"]} for l in links]
        return result

    def get_by_topic_key(self, topic_key: str, project: str) -> dict | None:
        """Find observation by topic key + project."""
        row = self.store.conn.execute(
            "SELECT * FROM observations WHERE topic_key = ? AND project = ?",
            (topic_key, project),
        ).fetchone()
        return dict(row) if row else None

    def get_by_node(self, node_id: str) -> list[dict]:
        """All observations linked to a specific code node."""
        rows = self.store.conn.execute(
            """SELECT o.* FROM observations o
               JOIN observation_nodes on_ ON on_.observation_id = o.id
               WHERE on_.node_id = ?
               ORDER BY o.created_at DESC""",
            (node_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_by_session(self, session_id: str) -> list[dict]:
        """All observations in a session, chronological order."""
        rows = self.store.conn.execute(
            "SELECT * FROM observations WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, observation_id: int):
        """Delete observation, its node links, and FTS entry."""
        obs = self.get(observation_id)
        if obs:
            self._delete_fts(observation_id, obs["title"], obs["content"],
                             obs["type"], obs.get("project", ""))
            self._clear_node_links(observation_id)
            self.store.conn.execute("DELETE FROM observations WHERE id = ?", (observation_id,))
            self.store.conn.commit()

    def _link_nodes(self, observation_id: int, text: str, node_ids: list[str] | None):
        """
        Link observation to code graph nodes.
        Agent-provided IDs get source='agent', auto-detected get source='auto'.
        """
        linked = set()

        # Tier 1: Agent-provided
        if node_ids:
            for nid in node_ids:
                node = self.store.get_node(nid)
                if node and nid not in linked:
                    self.store.conn.execute(
                        "INSERT OR IGNORE INTO observation_nodes (observation_id, node_id, source) VALUES (?, ?, 'agent')",
                        (observation_id, nid),
                    )
                    linked.add(nid)

        # Tier 2: Auto-detect by substring matching node names in text
        all_nodes = self.store.get_all_nodes()
        for node_id, node in all_nodes.items():
            if node.kind == "FILE":
                continue
            if node_id in linked:
                continue
            # Check if qualified name or short name appears in text
            if node.name in text or node_id in text:
                self.store.conn.execute(
                    "INSERT OR IGNORE INTO observation_nodes (observation_id, node_id, source) VALUES (?, ?, 'auto')",
                    (observation_id, node_id),
                )
                linked.add(node_id)

    def _clear_node_links(self, observation_id: int):
        """Remove all node links for an observation."""
        self.store.conn.execute(
            "DELETE FROM observation_nodes WHERE observation_id = ?", (observation_id,)
        )

    def _compute_hash(self, title: str, content: str) -> str:
        return hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()

    def _insert_fts(self, obs_id: int, title: str, content: str, type: str, project: str):
        self.store.conn.execute(
            "INSERT INTO observations_fts(rowid, title, content, type, project) VALUES (?, ?, ?, ?, ?)",
            (obs_id, title, content, type, project),
        )

    def _delete_fts(self, obs_id: int, title: str, content: str, type: str, project: str):
        try:
            self.store.conn.execute(
                "INSERT INTO observations_fts(observations_fts, rowid, title, content, type, project) "
                "VALUES('delete', ?, ?, ?, ?, ?)",
                (obs_id, title, content, type, project),
            )
        except Exception:
            pass  # FTS entry might not exist
