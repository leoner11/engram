"""Retrieval feedback: record missed nodes, generate boost maps."""

from __future__ import annotations

import hashlib
import json

from engram.graph.store import GraphStore
from engram.verification.verifier import VerificationResult


class RetrievalFeedback:
    """Records missed nodes from verification to improve future retrieval."""

    def __init__(self, store: GraphStore):
        self.store = store
        self._ensure_tables()

    def _ensure_tables(self):
        """Create feedback tables if they don't exist."""
        self.store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS retrieval_feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash  TEXT NOT NULL,
                change_type TEXT NOT NULL,
                missed_node TEXT NOT NULL,
                edge_kind   TEXT NOT NULL,
                depth       INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_change_type ON retrieval_feedback(change_type);
            CREATE INDEX IF NOT EXISTS idx_feedback_missed_node ON retrieval_feedback(missed_node);

            CREATE TABLE IF NOT EXISTS verification_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                seeds           TEXT NOT NULL,
                change_types    TEXT NOT NULL,
                touched_count   INTEGER NOT NULL,
                expected_count  INTEGER NOT NULL,
                missing_count   INTEGER NOT NULL,
                verdict         TEXT NOT NULL,
                missing_nodes   TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        self.store.conn.commit()

    def record(self, result: VerificationResult):
        """Record feedback from a verification result."""
        # Record the verification result
        self.store.conn.execute(
            """INSERT INTO verification_results
               (seeds, change_types, touched_count, expected_count, missing_count, verdict, missing_nodes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                json.dumps(result.seeds),
                json.dumps(result.change_types),
                result.stats.get("touched_count", 0),
                result.stats.get("expected_count", 0),
                result.stats.get("missing_count", 0),
                result.verdict.value,
                json.dumps([m.node_id for m in result.missing_nodes]),
            ),
        )

        # Record individual missed nodes for boost map
        query_hash = self._compute_query_hash(result.seeds, result.change_types)

        for missing in result.missing_nodes:
            if missing.confidence in ("high", "medium"):
                for ct in missing.change_type.split(", "):
                    self.store.conn.execute(
                        """INSERT INTO retrieval_feedback
                           (query_hash, change_type, missed_node, edge_kind, depth)
                           VALUES (?, ?, ?, ?, ?)""",
                        (query_hash, ct, missing.node_id, missing.edge_kind, missing.depth),
                    )

        self.store.conn.commit()

    def get_boost_map(self, change_types: set[str]) -> dict[str, float]:
        """
        Get priority boost scores for nodes historically missed
        with these change types.

        Returns: {node_id: boost_score} (capped at 50)
        """
        if not change_types:
            return {}

        placeholders = ",".join("?" * len(change_types))
        rows = self.store.conn.execute(
            f"""SELECT missed_node, COUNT(*) as miss_count
                FROM retrieval_feedback
                WHERE change_type IN ({placeholders})
                GROUP BY missed_node""",
            list(change_types),
        ).fetchall()

        return {row["missed_node"]: min(row["miss_count"], 50) for row in rows}

    def prune_stale(self):
        """Remove feedback for nodes that no longer exist in the graph."""
        self.store.conn.execute("""
            DELETE FROM retrieval_feedback
            WHERE missed_node NOT IN (SELECT id FROM nodes)
        """)
        self.store.conn.commit()

    def _compute_query_hash(self, seeds: list[str], change_types: list[str]) -> str:
        key = json.dumps({"seeds": sorted(seeds), "change_types": sorted(change_types)})
        return hashlib.sha256(key.encode()).hexdigest()[:16]
