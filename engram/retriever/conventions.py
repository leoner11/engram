"""Convention discovery: find existing siblings of what the agent wants to build.

This is NOT a template library. It uses the project's own code to find examples.
"Add a new API endpoint" → find existing endpoints in this project → return one
at full detail so the agent can match the pattern.
"""

from __future__ import annotations

import re

from engram.graph.store import GraphStore
from engram.indexer.extractor import NodeRecord
from engram.retriever.seeds import SeedCandidate


# Prompt patterns that hint at what kind of thing the agent is building.
# Keys are category names, values define matching criteria.
CREATION_HINTS: dict[str, dict] = {
    "endpoint": {
        "decorators": ["@api_view", "@app.route", "@router.", "@action",
                       "@Get", "@Post", "@Put", "@Delete", "@app.get",
                       "@app.post", "@app.put", "@app.delete"],
        "name_patterns": ["view", "handler", "controller", "route", "endpoint"],
    },
    "model": {
        "decorators": [],
        "name_patterns": ["model", "schema", "entity"],
        "extends": ["Model", "Base", "BaseModel"],
    },
    "component": {
        "decorators": [],
        "name_patterns": ["component", "page", "screen", "widget"],
        "file_patterns": [r"\.tsx$", r"\.jsx$", r"\.vue$"],
    },
    "service": {
        "decorators": ["@service", "@injectable"],
        "name_patterns": ["service", "manager", "provider"],
    },
    "test": {
        "decorators": ["@pytest.mark", "@test"],
        "name_patterns": ["test_", "Test"],
        "file_patterns": [r"test_", r"_test\."],
    },
    "hook": {
        "decorators": [],
        "name_patterns": ["use"],
        "file_patterns": [r"hooks/", r"use[A-Z]"],
    },
    "middleware": {
        "decorators": [],
        "name_patterns": ["middleware", "interceptor", "guard"],
    },
    "serializer": {
        "decorators": [],
        "name_patterns": ["serializer", "schema", "dto"],
        "extends": ["Serializer", "Schema"],
    },
    "command": {
        "decorators": [],
        "name_patterns": ["command", "task", "job"],
        "extends": ["BaseCommand", "Command"],
    },
}


class ConventionFinder:
    """Find existing code that resembles what the agent wants to build."""

    def __init__(self, store: GraphStore):
        self.store = store

    def find_siblings(
        self,
        prompt: str,
        seed_candidates: list[SeedCandidate] | None = None,
        limit: int = 5,
    ) -> list[NodeRecord]:
        """Find nodes that match the convention the agent is trying to follow.

        Strategy:
        1. Detect what KIND of thing from the prompt
        2. Find existing nodes of that kind (by decorator, name pattern, base class)
        3. Rank by quality (medium size, documented, exported)
        4. Return top N — caller decides detail levels

        Returns NodeRecord objects.
        """
        # Step 1: What kind of thing?
        hints = self._detect_category(prompt)

        # Step 2: Find matching nodes
        candidates: list[NodeRecord] = []
        if hints:
            candidates = self._find_by_hint(hints)

        # Step 3: If no category match, try structural similarity to seeds
        if not candidates and seed_candidates:
            candidates = self._find_by_seed_similarity(seed_candidates)

        # Step 4: If still nothing, find representative exported nodes
        if not candidates:
            candidates = self._find_representative(limit)

        # Step 5: Rank and return
        ranked = self._rank_conventions(candidates)
        return ranked[:limit]

    def _detect_category(self, prompt: str) -> dict | None:
        """Match prompt text against known creation hint categories."""
        prompt_lower = prompt.lower()

        best_match = None
        best_score = 0

        for category, hints in CREATION_HINTS.items():
            score = 0
            # Category name in prompt
            if category in prompt_lower:
                score += 10

            # Name patterns
            for pattern in hints.get("name_patterns", []):
                if pattern.lower() in prompt_lower:
                    score += 5

            if score > best_score:
                best_score = score
                best_match = hints

        return best_match if best_score >= 5 else None

    def _find_by_hint(self, hints: dict) -> list[NodeRecord]:
        """Find nodes matching a creation hint."""
        all_nodes = self.store.get_all_nodes()
        matched: list[NodeRecord] = []
        matched_ids: set[str] = set()

        for node_id, node in all_nodes.items():
            if node.kind == "FILE" or node_id in matched_ids:
                continue

            # Check decorators
            if hints.get("decorators") and node.decorators:
                node_decs = " ".join(node.decorators).lower()
                if any(d.lower() in node_decs for d in hints["decorators"]):
                    matched.append(node)
                    matched_ids.add(node_id)
                    continue

            # Check name patterns
            if hints.get("name_patterns"):
                name_lower = node.name.lower()
                if any(p.lower() in name_lower for p in hints["name_patterns"]):
                    matched.append(node)
                    matched_ids.add(node_id)
                    continue

            # Check base classes (from edges)
            if hints.get("extends"):
                extends_edges = self.store.get_edges_from(node_id)
                for edge in extends_edges:
                    if edge.kind == "EXTENDS":
                        target = self.store.get_node(edge.target_id)
                        if target and any(
                            e.lower() in target.name.lower()
                            for e in hints["extends"]
                        ):
                            matched.append(node)
                            matched_ids.add(node_id)
                            break

            # Check file patterns
            if hints.get("file_patterns") and node_id not in matched_ids:
                if any(re.search(fp, node.file_path) for fp in hints["file_patterns"]):
                    matched.append(node)
                    matched_ids.add(node_id)

        return matched

    def _find_by_seed_similarity(
        self, seed_candidates: list[SeedCandidate],
    ) -> list[NodeRecord]:
        """Find nodes structurally similar to the best seed candidates.

        "Similar" = same directory + same kind, or same decorators.
        """
        if not seed_candidates:
            return []

        all_nodes = self.store.get_all_nodes()
        candidates: list[NodeRecord] = []
        seen: set[str] = set()

        for seed in seed_candidates[:3]:
            seed_node = self.store.get_node(seed.node_id)
            if seed_node is None or seed_node.kind == "FILE":
                continue

            seed_dir = "/".join(seed_node.file_path.split("/")[:-1])
            seed_decs = set(
                d.lower() for d in (seed_node.decorators or [])
            )

            for node_id, node in all_nodes.items():
                if node.kind == "FILE" or node.id == seed.node_id or node_id in seen:
                    continue

                # Same directory + same kind = likely sibling
                node_dir = "/".join(node.file_path.split("/")[:-1])
                if node_dir == seed_dir and node.kind == seed_node.kind:
                    candidates.append(node)
                    seen.add(node_id)
                    continue

                # Same decorators = same pattern
                if seed_decs and node.decorators:
                    node_decs = set(d.lower() for d in node.decorators)
                    if seed_decs & node_decs:
                        candidates.append(node)
                        seen.add(node_id)

        return candidates

    def _find_representative(self, limit: int) -> list[NodeRecord]:
        """Fallback: find the most representative exported nodes."""
        all_nodes = self.store.get_all_nodes()
        scored: list[tuple[NodeRecord, float]] = []
        for node_id, node in all_nodes.items():
            if node.kind == "FILE" or not node.is_exported:
                continue
            in_degree = self.store.get_in_degree(node_id)
            source_len = len(node.full_source or "")
            # Prefer medium-sized nodes
            size_score = min(source_len, 2000) / 2000
            scored.append((node, in_degree + size_score * 5))

        scored.sort(key=lambda x: -x[1])
        return [node for node, _ in scored[:limit]]

    def _rank_conventions(self, candidates: list[NodeRecord]) -> list[NodeRecord]:
        """Rank convention candidates for best example quality.

        Best example = medium-sized (20-80 lines), documented, exported.
        First result will be shown at full detail by the assembler.
        """
        def score(node: NodeRecord) -> float:
            s = 0.0
            line_count = node.line_end - node.line_start

            # Medium size preferred
            if 20 <= line_count <= 80:
                s += 20
            elif 10 <= line_count <= 120:
                s += 10
            elif line_count > 200:
                s -= 10

            # Has docstring = self-documenting
            if node.docstring:
                s += 10

            # Exported = public API
            if node.is_exported:
                s += 5

            # In-degree = other code depends on this
            s += min(self.store.get_in_degree(node.id), 10)

            return s

        candidates.sort(key=lambda n: -score(n))

        # Deduplicate by file
        seen_files: set[str] = set()
        deduped: list[NodeRecord] = []
        for node in candidates:
            if node.file_path not in seen_files:
                deduped.append(node)
                seen_files.add(node.file_path)
            if len(deduped) >= 10:
                break

        return deduped
