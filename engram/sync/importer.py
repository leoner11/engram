"""Import memories from JSONL files into local SQLite."""

from __future__ import annotations

import json
from pathlib import Path

from engram.graph.store import GraphStore
from engram.memory.observations import ObservationManager


class MemoryImporter:
    """Import observations from JSONL into local database."""

    def __init__(self, store: GraphStore, obs_mgr: ObservationManager):
        self.store = store
        self.obs_mgr = obs_mgr

    def import_from_jsonl(self, input_dir: Path) -> dict:
        """
        Import observations from .engram/sync/ into local SQLite.

        Merge strategy:
        - topic_key: upsert (latest updated_at wins)
        - no topic_key: source_hash dedup
        """
        result = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}

        # Verify manifest
        manifest_path = input_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            project = manifest.get("project", "")
        else:
            project = ""

        # Import sessions first
        sess_path = input_dir / "sessions.jsonl"
        if sess_path.exists():
            for line in sess_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    sess = json.loads(line)
                    existing = self.store.conn.execute(
                        "SELECT id FROM sessions WHERE id = ?", (sess["id"],)
                    ).fetchone()
                    if not existing:
                        self.store.conn.execute(
                            "INSERT INTO sessions (id, project, started_at, ended_at, summary) VALUES (?, ?, ?, ?, ?)",
                            (sess["id"], sess.get("project", project),
                             sess.get("started_at", ""), sess.get("ended_at"),
                             sess.get("summary")),
                        )
                except Exception:
                    result["errors"] += 1

        self.store.conn.commit()

        # Import observations
        obs_path = input_dir / "observations.jsonl"
        if not obs_path.exists():
            return result

        for line in obs_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                obs = json.loads(line)
                node_ids = obs.get("node_ids", [])

                obs_id = self.obs_mgr.save(
                    title=obs.get("title", ""),
                    content=obs.get("content", ""),
                    type=obs.get("type", "discovery"),
                    project=obs.get("project", project),
                    topic_key=obs.get("topic_key"),
                    node_ids=node_ids if node_ids else None,
                )

                # Determine if it was imported fresh or updated
                if obs.get("topic_key"):
                    result["updated"] += 1
                else:
                    result["imported"] += 1

            except Exception:
                result["errors"] += 1

        return result
