"""Export memories to chunked JSONL for git-based team sharing."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from engram.graph.store import GraphStore


class MemoryExporter:
    """Export observations and sessions to JSONL files."""

    def __init__(self, store: GraphStore, project: str):
        self.store = store
        self.project = project

    def export_to_jsonl(self, output_dir: Path):
        """Export all observations + sessions as JSONL + manifest."""
        output_dir.mkdir(parents=True, exist_ok=True)

        # Export observations
        obs_path = output_dir / "observations.jsonl"
        obs_lines = []
        rows = self.store.conn.execute(
            "SELECT * FROM observations WHERE project = ? ORDER BY created_at",
            (self.project,),
        ).fetchall()

        for row in rows:
            obs = dict(row)
            # Get linked nodes
            links = self.store.conn.execute(
                "SELECT node_id, source FROM observation_nodes WHERE observation_id = ?",
                (obs["id"],),
            ).fetchall()
            obs["node_ids"] = [l["node_id"] for l in links]
            obs["node_sources"] = [l["source"] for l in links]
            line = json.dumps(obs, default=str)
            obs_lines.append(line)

        obs_path.write_text("\n".join(obs_lines) + "\n" if obs_lines else "")

        # Export sessions
        sess_path = output_dir / "sessions.jsonl"
        sess_lines = []
        sess_rows = self.store.conn.execute(
            "SELECT * FROM sessions WHERE project = ? ORDER BY started_at",
            (self.project,),
        ).fetchall()

        for row in sess_rows:
            sess_lines.append(json.dumps(dict(row), default=str))

        sess_path.write_text("\n".join(sess_lines) + "\n" if sess_lines else "")

        # Write manifest
        obs_content = obs_path.read_bytes()
        manifest = {
            "project": self.project,
            "exported_at": datetime.now().isoformat(),
            "observation_count": len(obs_lines),
            "session_count": len(sess_lines),
            "hash": hashlib.sha256(obs_content).hexdigest(),
        }
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
