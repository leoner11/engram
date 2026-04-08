"""Export project brain as static BRAIN.md or JSON snapshot."""

from __future__ import annotations

import json
from pathlib import Path

from engram.graph.store import GraphStore


def export_brain(store: GraphStore, format: str = "md") -> str:
    """Generate a static project snapshot."""
    if format == "json":
        return _export_json(store)
    return _export_markdown(store)


def _export_markdown(store: GraphStore) -> str:
    """Generate BRAIN.md — pasteable into any LLM conversation."""
    stats = store.get_stats()
    all_nodes = store.get_all_nodes()

    lines = []
    lines.append("# Project Brain (Engram Export)")
    lines.append("")

    # Overview
    lines.append("## Overview")
    lines.append(f"- Files: {stats['file_count']}")
    lines.append(f"- Functions: {stats['function_count']}")
    lines.append(f"- Classes: {stats['class_count']}")
    lines.append(f"- Edges: {stats['edge_count']}")
    lines.append(f"- Languages: {', '.join(stats['languages'])}")
    lines.append("")

    # File map
    lines.append("## File Map")
    file_nodes: dict[str, list] = {}
    for node in all_nodes.values():
        if node.kind == "FILE":
            continue
        file_nodes.setdefault(node.file_path, []).append(node)

    for file_path in sorted(file_nodes):
        nodes = file_nodes[file_path]
        lines.append(f"- `{file_path}` ({len(nodes)} declarations)")
        for node in sorted(nodes, key=lambda n: n.line_start):
            exported = " [exported]" if node.is_exported else ""
            lines.append(f"  - {node.kind.lower()} `{node.name}`{exported}")
    lines.append("")

    # Top-level API (exported functions/classes with signatures)
    lines.append("## Exported API")
    exported = [n for n in all_nodes.values() if n.is_exported and n.kind != "FILE"]
    exported.sort(key=lambda n: (n.file_path, n.name))

    for node in exported:
        sig = node.signature or node.name
        lines.append(f"- `{sig}`")
        if node.docstring:
            first_line = node.docstring.split("\n")[0].strip()
            if first_line:
                lines.append(f"  {first_line}")
    lines.append("")

    # Most connected nodes
    lines.append("## Key Nodes (by connectivity)")
    node_degrees = []
    for node_id, node in all_nodes.items():
        if node.kind == "FILE":
            continue
        degree = store.get_in_degree(node_id)
        if degree > 0:
            node_degrees.append((node, degree))

    node_degrees.sort(key=lambda x: -x[1])
    for node, degree in node_degrees[:10]:
        lines.append(f"- `{node.name}` — {degree} incoming edges ({node.file_path})")
    lines.append("")

    return "\n".join(lines)


def _export_json(store: GraphStore) -> str:
    """Export full graph as JSON."""
    all_nodes = store.get_all_nodes()
    stats = store.get_stats()

    nodes_data = []
    for node in all_nodes.values():
        nodes_data.append({
            "id": node.id,
            "kind": node.kind,
            "name": node.name,
            "file_path": node.file_path,
            "line_start": node.line_start,
            "line_end": node.line_end,
            "language": node.language,
            "signature": node.signature,
            "is_exported": node.is_exported,
            "summary": node.summary,
        })

    # Collect all edges
    edges_data = []
    seen_edges = set()
    for node_id in all_nodes:
        for edge in store.get_edges_from(node_id):
            key = (edge.source_id, edge.target_id, edge.kind)
            if key not in seen_edges:
                seen_edges.add(key)
                edges_data.append({
                    "source": edge.source_id,
                    "target": edge.target_id,
                    "kind": edge.kind,
                    "metadata": edge.metadata,
                })

    return json.dumps({
        "stats": stats,
        "nodes": nodes_data,
        "edges": edges_data,
    }, indent=2)
