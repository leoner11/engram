"""Project snapshot: concise architectural identity for session bootstrapping."""

from __future__ import annotations

from engram.graph.store import GraphStore
from engram.memory.observations import ObservationManager
from engram.patterns import PatternDetector


SNAPSHOT_TOPIC_KEY = "_project_snapshot"
MAX_SNAPSHOT_CHARS = 3200  # ~800 tokens


class SnapshotGenerator:
    """Generate and cache a project-level architectural snapshot.

    The snapshot is a concise summary of the project's stack, structure,
    conventions, and past decisions. It costs ~400-800 tokens and replaces
    the agent's "read 5 files to understand the project" ritual.

    Stored as an observation with topic_key="_project_snapshot" so
    regeneration auto-upserts via ObservationManager.
    """

    def __init__(
        self,
        store: GraphStore,
        obs_mgr: ObservationManager,
        project: str,
    ):
        self.store = store
        self.obs_mgr = obs_mgr
        self.project = project

    def get_or_generate(self, force_refresh: bool = False) -> str:
        """Return cached snapshot, or generate a fresh one.

        Returns the snapshot content string.
        """
        if not force_refresh:
            existing = self.obs_mgr.get_by_topic_key(SNAPSHOT_TOPIC_KEY, self.project)
            if existing:
                return existing["content"]

        content = self._generate()
        self.obs_mgr.save(
            title=f"Project Snapshot: {self.project}",
            content=content,
            type="architecture",
            project=self.project,
            topic_key=SNAPSHOT_TOPIC_KEY,
        )
        return content

    def _generate(self) -> str:
        """Build snapshot from graph data + patterns. No LLM needed."""
        parts = []

        # 1. Framework + languages + size
        detector = PatternDetector(self.store)
        framework = detector.detect_framework()
        stats = self.store.get_stats()

        stack_line = f"STACK: {framework or 'unknown framework'}"
        if stats["languages"]:
            stack_line += f" | Languages: {', '.join(stats['languages'])}"
        stack_line += f" | {stats['file_count']} files, {stats['node_count']} nodes"
        parts.append(stack_line)

        # 2. Directory structure (top 2 levels, grouped)
        dir_summary = self._summarize_directories()
        if dir_summary:
            parts.append(f"STRUCTURE:\n{dir_summary}")

        # 3. Key entry points
        entry_points = self._find_entry_points(limit=5)
        if entry_points:
            ep_lines = [f"  {ep}" for ep in entry_points]
            parts.append("ENTRY POINTS:\n" + "\n".join(ep_lines))

        # 4. Detected patterns
        patterns = detector.detect_patterns()
        if patterns:
            pat_lines = [f"  {p.name}" for p in patterns[:3]]
            parts.append("PATTERNS:\n" + "\n".join(pat_lines))

        # 5. Architecture observations from past sessions
        arch_obs = self._get_architecture_observations(limit=5)
        if arch_obs:
            obs_lines = [f"  {o['title']}" for o in arch_obs]
            parts.append("PAST DECISIONS:\n" + "\n".join(obs_lines))

        content = "\n".join(parts)
        if len(content) > MAX_SNAPSHOT_CHARS:
            content = content[:MAX_SNAPSHOT_CHARS]
        return content

    def _summarize_directories(self) -> str:
        """Group files by top-level directory, show purpose hints."""
        all_nodes = self.store.get_all_nodes()
        dir_counts: dict[str, int] = {}

        for node in all_nodes.values():
            if node.kind == "FILE":
                continue
            parts = node.file_path.split("/")
            if len(parts) > 2:
                top_dir = f"{parts[0]}/{parts[1]}"
            elif len(parts) > 1:
                top_dir = parts[0]
            else:
                top_dir = "(root)"
            dir_counts[top_dir] = dir_counts.get(top_dir, 0) + 1

        sorted_dirs = sorted(dir_counts.items(), key=lambda x: -x[1])
        lines = []
        for dir_path, count in sorted_dirs[:10]:
            lines.append(f"  {dir_path}/ ({count} declarations)")
        return "\n".join(lines)

    def _find_entry_points(self, limit: int = 5) -> list[str]:
        """Top N most-connected exported nodes."""
        all_nodes = self.store.get_all_nodes()
        scored = []
        for node_id, node in all_nodes.items():
            if node.kind == "FILE" or not node.is_exported:
                continue
            in_degree = self.store.get_in_degree(node_id)
            if in_degree > 0:
                sig = node.signature or node.name
                scored.append((sig, node.file_path, in_degree))

        scored.sort(key=lambda x: -x[2])
        return [f"{sig} ({path}, {deg} refs)" for sig, path, deg in scored[:limit]]

    def _get_architecture_observations(self, limit: int = 5) -> list[dict]:
        """Recent architecture + decision observations."""
        try:
            rows = self.store.conn.execute(
                """SELECT title, content FROM observations
                   WHERE project = ? AND type IN ('architecture', 'decision')
                   AND (topic_key IS NULL OR topic_key != ?)
                   ORDER BY updated_at DESC LIMIT ?""",
                (self.project, SNAPSHOT_TOPIC_KEY, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
