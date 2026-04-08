"""Excerpt extraction: relevant slices of a node's source around call sites or keyword matches."""

from __future__ import annotations

from engram.indexer.extractor import NodeRecord
from engram.indexer.resolver import Edge


def extract_excerpt(
    node: NodeRecord,
    seed_name: str,
    edge: Edge,
    context_lines: int = 10,
) -> str | None:
    """
    Extract a relevant excerpt from node's full_source.

    Rules:
    - Affected CALLS seed: ±context_lines around call_sites (precise)
    - Seed CALLS affected: excerpt doesn't apply (return None)
    - USES_TYPE/EXTENDS: keyword search for seed name in body
    - No match: return None (caller should use full or signature)
    """
    if not node.full_source:
        return None

    source_lines = node.full_source.splitlines()
    if len(source_lines) <= context_lines * 2 + 5:
        # Too small for excerpt — just use full
        return None

    if edge.kind == "CALLS":
        # Check direction: is this node calling the seed, or is the seed calling this node?
        if edge.source_id == node.id:
            # This node (affected) calls the seed → call_sites are in this node
            call_sites = edge.metadata.get("call_sites", [])
            if call_sites:
                # Convert absolute line numbers to relative
                relative_sites = [line - node.line_start for line in call_sites]
                relative_sites = [r for r in relative_sites if 0 <= r < len(source_lines)]
                if relative_sites:
                    return _extract_windows(source_lines, relative_sites, context_lines, node.line_start)

        elif edge.target_id == node.id:
            # Seed calls this node → no hot spot in this node
            return None

    elif edge.kind in ("USES_TYPE", "EXTENDS"):
        # Keyword search for seed name in the node's body
        anchors = _find_keyword_anchors(source_lines, seed_name)
        if anchors:
            return _extract_windows(source_lines, anchors, context_lines, node.line_start)

    return None


def _find_keyword_anchors(source_lines: list[str], keyword: str) -> list[int]:
    """Find line indices where keyword appears in source."""
    keyword_lower = keyword.lower()
    # Also try the last part of a qualified name
    short_name = keyword.split(".")[-1].lower() if "." in keyword else keyword_lower

    anchors = []
    for i, line in enumerate(source_lines):
        line_lower = line.lower()
        if keyword_lower in line_lower or short_name in line_lower:
            anchors.append(i)
    return anchors


def _extract_windows(
    source_lines: list[str],
    anchor_lines: list[int],
    context: int,
    node_line_start: int,
) -> str:
    """
    Extract and merge windows around anchor lines.

    Each line is prefixed with its absolute line number.
    Overlapping windows are merged.
    """
    if not anchor_lines:
        return ""

    # Build windows
    windows: list[tuple[int, int]] = []
    for anchor in sorted(anchor_lines):
        start = max(0, anchor - context)
        end = min(len(source_lines) - 1, anchor + context)
        windows.append((start, end))

    # Merge overlapping windows
    merged: list[tuple[int, int]] = [windows[0]]
    for start, end in windows[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    # Extract lines with absolute line numbers
    parts = []
    for i, (start, end) in enumerate(merged):
        if i > 0:
            parts.append("    ...")
        for line_idx in range(start, end + 1):
            abs_line = node_line_start + line_idx
            parts.append(f"L{abs_line}: {source_lines[line_idx]}")

    return "\n".join(parts)
