"""SQLite storage layer for nodes, edges, and manifest. No business logic."""

from __future__ import annotations

import json
from dataclasses import dataclass

from engram.db import EngramDB
from engram.indexer.extractor import NodeRecord
from engram.indexer.resolver import Edge


class GraphStore:
    """CRUD operations for the code graph."""

    def __init__(self, db: EngramDB):
        self.db = db
        self.conn = db.connect()

    # --- Nodes ---

    def upsert_node(self, node: NodeRecord):
        """INSERT OR REPLACE into nodes table."""
        self.conn.execute(
            """INSERT OR REPLACE INTO nodes
               (id, kind, name, file_path, line_start, line_end, language,
                signature, docstring, source_hash, is_exported, decorators,
                summary, full_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                node.id, node.kind, node.name, node.file_path,
                node.line_start, node.line_end, node.language,
                node.signature, node.docstring, node.source_hash,
                node.is_exported, json.dumps(node.decorators),
                node.summary, node.full_source,
            ),
        )

    def get_node(self, node_id: str) -> NodeRecord | None:
        """Fetch single node by ID."""
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def get_nodes_by_file(self, file_path: str) -> list[NodeRecord]:
        """All nodes in a file."""
        rows = self.conn.execute("SELECT * FROM nodes WHERE file_path = ?", (file_path,)).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_all_nodes(self) -> dict[str, NodeRecord]:
        """Return all nodes as {id: NodeRecord}."""
        rows = self.conn.execute("SELECT * FROM nodes").fetchall()
        return {r["id"]: self._row_to_node(r) for r in rows}

    def delete_nodes_by_file(self, file_path: str):
        """Remove all nodes from a file."""
        # First delete edges referencing these nodes
        node_ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM nodes WHERE file_path = ?", (file_path,)
        ).fetchall()]
        for nid in node_ids:
            self.delete_edges_by_node(nid)
        self.conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))

    def search_nodes_by_name(self, term: str) -> list[NodeRecord]:
        """Substring search on node name. Used by seed selection."""
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE name LIKE ? OR id LIKE ?",
            (f"%{term}%", f"%{term}%"),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # --- Edges ---

    def upsert_edge(self, edge: Edge):
        """INSERT OR REPLACE into edges table."""
        self.conn.execute(
            """INSERT OR REPLACE INTO edges (source_id, target_id, kind, metadata)
               VALUES (?, ?, ?, ?)""",
            (edge.source_id, edge.target_id, edge.kind, json.dumps(edge.metadata)),
        )

    def get_edges_from(self, node_id: str) -> list[Edge]:
        """All outgoing edges from a node."""
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE source_id = ?", (node_id,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_edges_to(self, node_id: str) -> list[Edge]:
        """All incoming edges to a node (reverse lookup)."""
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE target_id = ?", (node_id,)
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def delete_edges_by_node(self, node_id: str):
        """Remove all edges where node is source or target."""
        self.conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (node_id, node_id))

    def delete_edges_by_file(self, file_path: str):
        """Remove all edges involving nodes from this file."""
        self.conn.execute("""
            DELETE FROM edges WHERE source_id IN (SELECT id FROM nodes WHERE file_path = ?)
            OR target_id IN (SELECT id FROM nodes WHERE file_path = ?)
        """, (file_path, file_path))

    # --- Manifest ---

    def get_manifest_hash(self, file_path: str) -> str | None:
        """Get stored hash for a file, or None if not indexed."""
        row = self.conn.execute(
            "SELECT source_hash FROM manifest WHERE file_path = ?", (file_path,)
        ).fetchone()
        return row["source_hash"] if row else None

    def update_manifest(self, file_path: str, source_hash: str, node_count: int):
        """Update manifest entry for a file."""
        self.conn.execute(
            """INSERT OR REPLACE INTO manifest (file_path, source_hash, node_count)
               VALUES (?, ?, ?)""",
            (file_path, source_hash, node_count),
        )

    def delete_manifest(self, file_path: str):
        """Remove manifest entry."""
        self.conn.execute("DELETE FROM manifest WHERE file_path = ?", (file_path,))

    def get_stale_files(self, current_hashes: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
        """
        Compare current file hashes against manifest.
        Returns: (changed_files, new_files, deleted_files)
        """
        manifest = {}
        for row in self.conn.execute("SELECT file_path, source_hash FROM manifest").fetchall():
            manifest[row["file_path"]] = row["source_hash"]

        changed = []
        new = []
        deleted = []

        for path, current_hash in current_hashes.items():
            if path not in manifest:
                new.append(path)
            elif manifest[path] != current_hash:
                changed.append(path)

        for path in manifest:
            if path not in current_hashes:
                deleted.append(path)

        return changed, new, deleted

    # --- Stats ---

    def get_stats(self) -> dict:
        """Return project statistics."""
        node_count = self.conn.execute("SELECT COUNT(*) as c FROM nodes").fetchone()["c"]
        edge_count = self.conn.execute("SELECT COUNT(*) as c FROM edges").fetchone()["c"]
        file_count = self.conn.execute("SELECT COUNT(*) as c FROM nodes WHERE kind = 'FILE'").fetchone()["c"]
        func_count = self.conn.execute("SELECT COUNT(*) as c FROM nodes WHERE kind = 'FUNCTION'").fetchone()["c"]
        class_count = self.conn.execute("SELECT COUNT(*) as c FROM nodes WHERE kind = 'CLASS'").fetchone()["c"]
        languages = [r[0] for r in self.conn.execute("SELECT DISTINCT language FROM nodes").fetchall()]
        return {
            "node_count": node_count,
            "edge_count": edge_count,
            "file_count": file_count,
            "function_count": func_count,
            "class_count": class_count,
            "languages": languages,
        }

    def get_in_degree(self, node_id: str) -> int:
        """Count incoming edges to a node."""
        row = self.conn.execute(
            "SELECT COUNT(*) as c FROM edges WHERE target_id = ?", (node_id,)
        ).fetchone()
        return row["c"]

    def commit(self):
        """Commit pending changes."""
        self.conn.commit()

    # --- Internal ---

    def _row_to_node(self, row) -> NodeRecord:
        decorators = json.loads(row["decorators"]) if row["decorators"] else []
        return NodeRecord(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            file_path=row["file_path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            language=row["language"],
            signature=row["signature"],
            docstring=row["docstring"],
            source_hash=row["source_hash"],
            is_exported=bool(row["is_exported"]),
            decorators=decorators,
            full_source=row["full_source"] or "",
        )

    def _row_to_edge(self, row) -> Edge:
        return Edge(
            source_id=row["source_id"],
            target_id=row["target_id"],
            kind=row["kind"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )
