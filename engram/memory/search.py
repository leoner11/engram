"""FTS5 search across observations with node-boost ranking."""

from __future__ import annotations

from engram.graph.store import GraphStore


class MemorySearch:
    """Full-text search across observations with task-aware ranking."""

    def __init__(self, store: GraphStore):
        self.store = store

    def search(
        self,
        query: str,
        project: str | None = None,
        type: str | None = None,
        limit: int = 10,
        affected_node_ids: set[str] | None = None,
    ) -> list[dict]:
        """
        Full-text search with optional node-boost ranking.

        affected_node_ids: boost observations linked to these nodes
        (used by assembler for task-aware memory retrieval).
        """
        sql, params = self._build_fts_query(query, project, type, limit)

        try:
            rows = self.store.conn.execute(sql, params).fetchall()
        except Exception:
            return []

        results = []
        for row in rows:
            result = dict(row)
            result["node_boost"] = 0

            # Apply node boost
            if affected_node_ids:
                links = self.store.conn.execute(
                    "SELECT node_id FROM observation_nodes WHERE observation_id = ?",
                    (row["id"],),
                ).fetchall()
                for link in links:
                    if link["node_id"] in affected_node_ids:
                        result["node_boost"] += 10

            result["final_rank"] = abs(result.get("rank", 0)) + result["node_boost"]
            results.append(result)

        # Re-sort by final_rank
        results.sort(key=lambda r: -r["final_rank"])
        return results

    def search_progressive(
        self,
        query: str,
        project: str | None = None,
        type: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """
        Progressive disclosure: return title + snippet only.
        Agent calls get() for full content on selected observations.
        """
        sql, params = self._build_fts_query(query, project, type, limit)

        try:
            rows = self.store.conn.execute(sql, params).fetchall()
        except Exception:
            return []

        results = []
        for row in rows:
            r = dict(row)
            content = r["content"] or ""
            snippet = content[:100] + "..." if len(content) > 100 else content

            # Count linked nodes
            link_count = self.store.conn.execute(
                "SELECT COUNT(*) as c FROM observation_nodes WHERE observation_id = ?",
                (r["id"],),
            ).fetchone()["c"]

            results.append({
                "id": r["id"],
                "title": r["title"],
                "snippet": snippet,
                "type": r["type"],
                "project": r["project"],
                "rank": r.get("rank", 0),
                "linked_nodes": link_count,
                "created_at": r["created_at"],
            })

        return results

    def _build_fts_query(
        self, query: str, project: str | None, type: str | None, limit: int
    ) -> tuple[str, list]:
        """Build SQL for FTS5 search."""
        sql = """
            SELECT o.*, f.rank
            FROM observations o
            JOIN observations_fts f ON o.id = f.rowid
            WHERE observations_fts MATCH ?
        """
        params: list = [query]

        if project:
            sql += " AND o.project = ?"
            params.append(project)
        if type:
            sql += " AND o.type = ?"
            params.append(type)

        sql += " ORDER BY f.rank LIMIT ?"
        params.append(limit)

        return sql, params
