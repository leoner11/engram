"""Cross-language bridge declarations from engram.yaml.

Bridges connect nodes across language boundaries (e.g. React frontend ↔ Django backend)
that no AST parser can infer. Users declare them manually and Engram stores them as
real edges in the graph, so traversal crosses the boundary naturally.

Example engram.yaml:

    bridges:
      - name: "Event API"
        backend:
          node: "src/api/views.py::EventViewSet"
        frontend:
          files: ["src/hooks/useEvents.ts", "src/components/EventList.tsx"]
        bidirectional: true

      - name: "Auth Flow"
        from:
          node: "src/services/auth.ts::login"
        to:
          node: "src/api/auth.py::authenticate"
        edge_kind: "API_CALL"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from engram.graph.store import GraphStore
from engram.indexer.resolver import Edge


@dataclass
class BridgeDeclaration:
    name: str
    edge_kind: str = "API_BRIDGE"
    bidirectional: bool = True
    # backend/frontend shorthand
    backend_node: str | None = None
    backend_files: list[str] = field(default_factory=list)
    frontend_node: str | None = None
    frontend_files: list[str] = field(default_factory=list)
    # explicit from/to (more flexible)
    from_node: str | None = None
    from_files: list[str] = field(default_factory=list)
    to_node: str | None = None
    to_files: list[str] = field(default_factory=list)


def load_config(root: Path) -> dict | None:
    """Load engram.yaml from project root. Returns None if not found."""
    for name in ["engram.yaml", "engram.yml"]:
        path = root / name
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    return None


def parse_bridges(config: dict) -> list[BridgeDeclaration]:
    """Parse bridge declarations from config dict."""
    raw_bridges = config.get("bridges", [])
    if not raw_bridges:
        return []

    bridges = []
    for raw in raw_bridges:
        if not isinstance(raw, dict):
            continue

        bridge = BridgeDeclaration(
            name=raw.get("name", "unnamed"),
            edge_kind=raw.get("edge_kind", "API_BRIDGE"),
            bidirectional=raw.get("bidirectional", True),
        )

        # backend/frontend shorthand
        if "backend" in raw:
            be = raw["backend"]
            bridge.backend_node = be.get("node")
            bridge.backend_files = be.get("files", [])
        if "frontend" in raw:
            fe = raw["frontend"]
            bridge.frontend_node = fe.get("node")
            bridge.frontend_files = fe.get("files", [])

        # explicit from/to
        if "from" in raw:
            fr = raw["from"]
            bridge.from_node = fr.get("node")
            bridge.from_files = fr.get("files", [])
        if "to" in raw:
            to = raw["to"]
            bridge.to_node = to.get("node")
            bridge.to_files = to.get("files", [])

        bridges.append(bridge)

    return bridges


def resolve_bridge_edges(bridge: BridgeDeclaration, store: GraphStore) -> list[Edge]:
    """Convert a bridge declaration into concrete Edge objects.

    Resolves file references to all FUNCTION/CLASS nodes in those files,
    and node references to exact node IDs. Creates edges between the
    resolved source(s) and target(s).
    """
    source_ids = _resolve_side(
        bridge.backend_node or bridge.from_node,
        bridge.backend_files or bridge.from_files,
        store,
    )
    target_ids = _resolve_side(
        bridge.frontend_node or bridge.to_node,
        bridge.frontend_files or bridge.to_files,
        store,
    )

    if not source_ids or not target_ids:
        return []

    edges = []
    metadata = {
        "bridge_name": bridge.name,
        "usage_pattern": "exhaustive",
        "source": "bridge_config",
    }

    for src in source_ids:
        for tgt in target_ids:
            edges.append(Edge(
                source_id=src,
                target_id=tgt,
                kind=bridge.edge_kind,
                metadata=metadata,
            ))
            if bridge.bidirectional:
                edges.append(Edge(
                    source_id=tgt,
                    target_id=src,
                    kind=bridge.edge_kind,
                    metadata=metadata,
                ))

    return edges


def build_bridges(root: Path, store: GraphStore) -> int:
    """Load engram.yaml, resolve bridges, store edges. Returns edge count."""
    config = load_config(root)
    if config is None:
        return 0

    bridges = parse_bridges(config)
    if not bridges:
        return 0

    # Clear old bridge edges first (so rebuild is idempotent)
    store.conn.execute("DELETE FROM edges WHERE kind = 'API_BRIDGE'")

    total = 0
    for bridge in bridges:
        edges = resolve_bridge_edges(bridge, store)
        for edge in edges:
            store.upsert_edge(edge)
        total += len(edges)

    store.commit()
    return total


def _resolve_side(
    node_id: str | None,
    files: list[str],
    store: GraphStore,
) -> list[str]:
    """Resolve a bridge side (node + files) to a list of node IDs."""
    ids = []

    # Exact node reference
    if node_id:
        node = store.get_node(node_id)
        if node:
            ids.append(node_id)
        else:
            # Try fuzzy: maybe it's just a function name without file prefix
            # Search for nodes whose ID ends with the given string
            all_nodes = store.get_all_nodes()
            for nid in all_nodes:
                if nid.endswith(node_id) or nid.endswith(f"::{node_id}"):
                    ids.append(nid)

    # File references → prefer exported nodes, fall back to all non-FILE nodes
    for file_path in files:
        nodes = store.get_nodes_by_file(file_path)
        exported = [n for n in nodes if n.kind != "FILE" and n.is_exported and n.id not in ids]
        if exported:
            # Use only exported nodes — these are the public API surface
            for node in exported:
                ids.append(node.id)
        else:
            # No exports detected (common in JS without explicit export)
            # Fall back to top-level functions/classes only (skip inner/helper functions)
            for node in nodes:
                if node.kind != "FILE" and node.id not in ids:
                    # Heuristic: skip functions that look like private helpers
                    # (start with _, or are nested — contain a dot in the name portion)
                    name_part = node.id.split("::")[-1] if "::" in node.id else node.name
                    if not name_part.startswith("_"):
                        ids.append(node.id)

    return ids
