"""Change-type-aware BFS traversal from seed nodes."""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field

from engram.graph.activation import ACTIVATION_RULES, ActivationRule, ChangeType, EdgeKind
from engram.graph.store import GraphStore
from engram.indexer.resolver import Edge


@dataclass
class AffectedNode:
    node_id: str
    depth: int
    priority: float
    reached_via: str          # EdgeKind string
    change_types: set[str] = field(default_factory=set)
    call_sites: list[int] = field(default_factory=list)


class GraphTraversal:
    """BFS traversal following edges allowed by activation rules."""

    def __init__(self, store: GraphStore):
        self.store = store

    def traverse(
        self,
        seeds: list[str],
        change_types: set[ChangeType],
        max_depth: int = 2,
        extra_edges: list | None = None,
    ) -> list[AffectedNode]:
        """
        BFS from seeds, following edges allowed by activation rules.

        Traversal is primarily on INCOMING edges to the seed:
        "if I change, who depends on me and might break?"

        extra_edges: optional list of Edge objects (e.g. from pattern matching)
                     injected alongside real DB edges during traversal.
        """
        all_affected: dict[str, AffectedNode] = {}

        # Seeds at depth 0
        for seed_id in seeds:
            all_affected[seed_id] = AffectedNode(
                node_id=seed_id,
                depth=0,
                priority=1000.0,
                reached_via="SEED",
                change_types={ct.value for ct in change_types},
            )

        # Run traversal for each change type independently, then union
        for change_type in change_types:
            rules = ACTIVATION_RULES.get(change_type, [])
            if not rules:
                continue

            visited: set[str] = set(seeds)
            # Queue entries: (node_id, depth, intermediary_id_or_None)
            queue: deque[tuple[str, int, str | None]] = deque()

            for seed_id in seeds:
                queue.append((seed_id, 0, None))

            while queue:
                current_id, current_depth, intermediary_id = queue.popleft()

                if current_depth >= max_depth:
                    continue

                # Get incoming edges: "who depends on me?"
                incoming = list(self.store.get_edges_to(current_id))

                # Also include pattern-implied extra edges pointing to this node
                if extra_edges:
                    for ee in extra_edges:
                        if ee.target_id == current_id:
                            incoming.append(ee)

                for edge in incoming:
                    # Check if this edge kind is activated by this change type
                    if not self._edge_activated(edge, rules):
                        continue

                    neighbor_id = edge.source_id
                    if neighbor_id in visited:
                        # Already found — but update if this path gives a bonus
                        if neighbor_id in all_affected:
                            all_affected[neighbor_id].change_types.add(change_type.value)
                        continue

                    visited.add(neighbor_id)
                    next_depth = current_depth + 1

                    # For depth-2 nodes, the intermediary is the depth-1 node
                    # that connected us. Check if it's a hub.
                    next_intermediary = current_id if next_depth >= 2 else None
                    hub_in_degree = 0
                    if next_depth >= 2 and intermediary_id is None:
                        # current_id is the depth-1 intermediary
                        hub_in_degree = self.store.get_in_degree(current_id)
                    elif intermediary_id:
                        hub_in_degree = self.store.get_in_degree(intermediary_id)

                    # Score this node (with hub penalty for depth 2+)
                    priority = self._score_node(
                        neighbor_id, next_depth, edge.kind, change_type,
                        hub_in_degree=hub_in_degree,
                    )

                    # Get call sites from edge metadata if available
                    call_sites = edge.metadata.get("call_sites", [])

                    if neighbor_id in all_affected:
                        # Already found via different change type — boost
                        existing = all_affected[neighbor_id]
                        existing.change_types.add(change_type.value)
                        existing.priority += 25  # Multi-label bonus
                        if call_sites:
                            existing.call_sites = sorted(set(existing.call_sites + call_sites))
                    else:
                        all_affected[neighbor_id] = AffectedNode(
                            node_id=neighbor_id,
                            depth=next_depth,
                            priority=priority,
                            reached_via=edge.kind,
                            change_types={change_type.value},
                            call_sites=call_sites,
                        )

                    queue.append((neighbor_id, next_depth, next_intermediary))

        # Sort by priority descending
        result = sorted(all_affected.values(), key=lambda n: -n.priority)
        return result

    def _edge_activated(self, edge: Edge, rules: list[ActivationRule]) -> bool:
        """Check if an edge is activated by any of the given rules."""
        for rule in rules:
            if rule.edge_kind.value == edge.kind:
                if self._check_condition(rule, edge):
                    return True
        return False

    def _check_condition(self, rule: ActivationRule, edge: Edge) -> bool:
        """Evaluate edge-specific activation conditions."""
        if rule.condition is None:
            return True

        metadata = edge.metadata or {}

        if rule.condition == "usage_pattern != 'passthrough'":
            return metadata.get("usage_pattern", "partial") != "passthrough"

        if rule.condition == "usage_pattern == 'exhaustive'":
            return metadata.get("usage_pattern") == "exhaustive"

        if rule.condition.startswith("accessed_fields includes"):
            # Conservative: if any fields are accessed, include the node
            return len(metadata.get("accessed_fields", [])) > 0

        return True  # Unknown condition → include (conservative)

    def _score_node(
        self, node_id: str, depth: int, edge_kind: str, change_type: ChangeType,
        hub_in_degree: int = 0,
    ) -> float:
        """Score an affected node based on depth, edge kind, and connectivity.

        ADDITION change type uses softer scoring — these nodes are
        informational (neighborhood context), not at-risk.

        Hub penalty: if this node was reached through a high-connectivity
        intermediary (hub), discount its priority. A node like BaseModel
        that everything imports shouldn't propagate context to the entire
        graph. Penalty kicks in when the intermediary has in-degree > 10.
        """
        # ADDITION: lower base scores — informational, not warnings
        if change_type == ChangeType.ADDITION:
            if depth == 1:
                base_scores = {
                    "IMPORTS": 50,
                    "EXTENDS": 45,
                    "DEFINES": 40,
                    "API_BRIDGE": 40,
                }
                base = base_scores.get(edge_kind, 30)
            else:
                base = 20
        elif depth == 1:
            base_scores = {
                "CALLS": 100,
                "EXTENDS": 95,
                "USES_TYPE": 90,
                "IMPORTS": 80,
                "DEFINES": 70,
                "EXPORTS": 70,
            }
            base = base_scores.get(edge_kind, 50)
        else:
            base = 50

        # Connectivity bonus
        in_degree = self.store.get_in_degree(node_id)
        connectivity = math.log2(in_degree + 1)

        score = base * max(connectivity, 1.0)

        # Hub penalty: if reached through a high-connectivity intermediary,
        # this node is probably noise — it shares a common ancestor with
        # the seed, not a direct relationship.
        # Penalty ramps: in-degree 10 → 0.8x, 20 → 0.6x, 50+ → 0.3x
        if hub_in_degree > 10 and depth >= 2:
            penalty = max(0.3, 1.0 - math.log2(hub_in_degree / 10) * 0.3)
            score *= penalty

        return score
