"""Core verification: compare touched nodes vs expected affected set."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from engram.graph.activation import ChangeType
from engram.graph.store import GraphStore
from engram.graph.traversal import AffectedNode, GraphTraversal
from engram.retriever.anticipation import anticipate_change_types
from engram.verification.diff_parser import DiffParser, FileDiff
from engram.verification.mapper import DiffMapper, TouchedNode


class Verdict(str, Enum):
    STRUCTURALLY_COMPLETE = "STRUCTURALLY_COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    OVERCOMPLETE = "OVERCOMPLETE"


@dataclass
class MissingNode:
    node_id: str
    file_path: str
    line_start: int
    line_end: int
    reason: str
    edge_kind: str
    depth: int
    change_type: str
    confidence: str  # "high" | "medium" | "low"


@dataclass
class VerificationResult:
    verdict: Verdict
    seeds: list[str]
    change_types: list[str]
    touched_nodes: list[TouchedNode]
    expected_nodes: list[AffectedNode]
    missing_nodes: list[MissingNode]
    extra_nodes: list[TouchedNode]
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "seeds": self.seeds,
            "change_types": self.change_types,
            "touched_count": self.stats.get("touched_count", 0),
            "expected_count": self.stats.get("expected_count", 0),
            "missing_count": self.stats.get("missing_count", 0),
            "missing_nodes": [
                {"node_id": m.node_id, "reason": m.reason,
                 "edge_kind": m.edge_kind, "confidence": m.confidence,
                 "file_path": m.file_path, "line_start": m.line_start, "line_end": m.line_end}
                for m in self.missing_nodes
            ],
        }


class Verifier:
    """Structural completeness verification of patches."""

    def __init__(self, store: GraphStore, pattern_matcher=None):
        self.store = store
        self.traversal = GraphTraversal(store)
        self.diff_parser = DiffParser()
        self.mapper = DiffMapper(store)
        self.pattern_matcher = pattern_matcher

    def verify(
        self,
        diff_text: str,
        seeds: list[str] | None = None,
        change_types: set[str] | None = None,
        prompt: str | None = None,
    ) -> VerificationResult:
        """
        Full verification pipeline:
        1. Parse diff
        2. Map to touched nodes
        3. Infer seeds if not provided
        4. Infer change types if not provided
        5. Compute expected affected set
        6. Compare touched vs expected
        7. Assign confidence to missing nodes
        8. Determine verdict
        """
        # 1. Parse diff
        file_diffs = self.diff_parser.parse(diff_text)
        if not file_diffs:
            return VerificationResult(
                verdict=Verdict.STRUCTURALLY_COMPLETE,
                seeds=[], change_types=[], touched_nodes=[], expected_nodes=[],
                missing_nodes=[], extra_nodes=[],
                stats={"touched_count": 0, "expected_count": 0, "missing_count": 0,
                       "note": "Empty diff"},
            )

        # 2. Map diff to touched nodes
        touched = self.mapper.map_diff_to_nodes(file_diffs)
        touched_ids = {t.node_id for t in touched}

        # 3. Infer seeds
        if seeds:
            seed_ids = seeds
        else:
            seed_ids = self._infer_seeds(touched)

        if not seed_ids:
            return VerificationResult(
                verdict=Verdict.STRUCTURALLY_COMPLETE,
                seeds=[], change_types=[], touched_nodes=touched,
                expected_nodes=[], missing_nodes=[], extra_nodes=[],
                stats={"touched_count": len(touched), "expected_count": 0,
                       "missing_count": 0, "note": "No seeds identified"},
            )

        # 4. Infer change types
        if change_types:
            ct_set = {ChangeType(ct) for ct in change_types}
        else:
            ct_set = self._infer_change_types(touched, seed_ids, prompt)

        # 5. Compute expected affected set (including pattern-implied edges)
        extra_edges = []
        if self.pattern_matcher:
            try:
                extra_edges = self.pattern_matcher.get_implicit_edges()
            except Exception:
                pass  # Pattern matching is best-effort
        expected = self.traversal.traverse(seed_ids, ct_set, max_depth=2, extra_edges=extra_edges or None)
        expected_ids = {e.node_id for e in expected if e.depth > 0}  # Exclude seeds themselves

        # 6. Compare
        missing_ids = expected_ids - touched_ids
        extra_ids = touched_ids - {e.node_id for e in expected}

        # 7. Build missing node details with confidence
        missing_nodes = []
        for affected in expected:
            if affected.node_id in missing_ids:
                node = self.store.get_node(affected.node_id)
                if node is None:
                    continue

                confidence = self._assign_confidence(affected, ct_set)
                reason = self._generate_reason(affected, seed_ids)

                missing_nodes.append(MissingNode(
                    node_id=affected.node_id,
                    file_path=node.file_path,
                    line_start=node.line_start,
                    line_end=node.line_end,
                    reason=reason,
                    edge_kind=affected.reached_via,
                    depth=affected.depth,
                    change_type=", ".join(affected.change_types),
                    confidence=confidence,
                ))

        # Filter to HIGH and MEDIUM for verdict
        significant_missing = [m for m in missing_nodes if m.confidence in ("high", "medium")]

        extra_touched = [t for t in touched if t.node_id in extra_ids]

        # 8. Verdict
        if not significant_missing:
            verdict = Verdict.STRUCTURALLY_COMPLETE
        else:
            verdict = Verdict.INCOMPLETE

        return VerificationResult(
            verdict=verdict,
            seeds=seed_ids,
            change_types=[ct.value for ct in ct_set],
            touched_nodes=touched,
            expected_nodes=expected,
            missing_nodes=missing_nodes,
            extra_nodes=extra_touched,
            stats={
                "touched_count": len(touched),
                "expected_count": len(expected_ids),
                "missing_count": len(missing_nodes),
                "missing_high": len([m for m in missing_nodes if m.confidence == "high"]),
                "missing_medium": len([m for m in missing_nodes if m.confidence == "medium"]),
                "missing_low": len([m for m in missing_nodes if m.confidence == "low"]),
            },
        )

    def _infer_seeds(self, touched: list[TouchedNode]) -> list[str]:
        """Infer seed nodes from touched nodes — most-modified functions."""
        # Prefer nodes with the most lines touched
        scored = []
        for t in touched:
            if t.touch_type == "deleted":
                continue
            node = self.store.get_node(t.node_id)
            if node and node.kind in ("FUNCTION", "CLASS"):
                scored.append((t.node_id, len(t.lines_touched)))

        scored.sort(key=lambda x: -x[1])
        return [s[0] for s in scored[:3]]

    def _infer_change_types(
        self, touched: list[TouchedNode], seed_ids: list[str], prompt: str | None
    ) -> set[ChangeType]:
        """Infer change types from the diff content."""
        if prompt:
            anticipated = anticipate_change_types(prompt)
            if anticipated:
                return anticipated

        # Heuristic: check if seeds had signature changes
        change_types = set()
        for t in touched:
            if t.node_id in seed_ids:
                if t.touch_type == "added":
                    change_types.add(ChangeType.ADDITION)
                elif t.touch_type == "deleted":
                    change_types.add(ChangeType.DELETION)
                else:
                    # Default to BODY_MODIFICATION + SIGNATURE_MODIFICATION
                    change_types.add(ChangeType.BODY_MODIFICATION)
                    change_types.add(ChangeType.SIGNATURE_MODIFICATION)

        return change_types or {ChangeType.BODY_MODIFICATION, ChangeType.SIGNATURE_MODIFICATION}

    def _assign_confidence(self, affected: AffectedNode, change_types: set[ChangeType]) -> str:
        """Assign confidence level to a missing node."""
        high_change_types = {ChangeType.SIGNATURE_MODIFICATION, ChangeType.RENAME, ChangeType.DELETION}

        if affected.depth == 1:
            if affected.reached_via in ("CALLS", "EXTENDS"):
                if any(ct in high_change_types for ct in change_types):
                    return "high"
                return "medium"
            elif affected.reached_via == "USES_TYPE":
                return "medium"
            elif affected.reached_via == "IMPORTS":
                if ChangeType.RENAME in change_types or ChangeType.DELETION in change_types:
                    return "high"
                return "low"
        return "low"

    def _generate_reason(self, affected: AffectedNode, seed_ids: list[str]) -> str:
        """Generate human-readable reason for why a node needs updating."""
        seed_names = []
        for sid in seed_ids:
            node = self.store.get_node(sid)
            if node:
                seed_names.append(node.name)

        seeds_str = ", ".join(seed_names[:2]) or "seed"
        edge = affected.reached_via
        ct = ", ".join(affected.change_types)

        if edge == "CALLS":
            return f"Calls {seeds_str} which had a {ct}. Call site may need updating."
        elif edge == "EXTENDS":
            return f"Extends {seeds_str}. {ct} on parent may affect inherited behavior."
        elif edge == "USES_TYPE":
            return f"Uses type {seeds_str}. Field changes may require updates."
        elif edge == "IMPORTS":
            return f"Imports {seeds_str} which was modified. Import may need updating."
        return f"Related to {seeds_str} via {edge} edge ({ct})."
