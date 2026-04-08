"""Map diff line ranges to graph nodes."""

from __future__ import annotations

from dataclasses import dataclass, field

from engram.graph.store import GraphStore
from engram.indexer.extractor import NodeRecord
from engram.verification.diff_parser import FileDiff


@dataclass
class TouchedNode:
    node_id: str
    file_path: str
    touch_type: str  # "modified" | "added" | "deleted"
    lines_touched: list[int] = field(default_factory=list)


class DiffMapper:
    """Map diff changes to graph nodes."""

    def __init__(self, store: GraphStore):
        self.store = store

    def map_diff_to_nodes(self, file_diffs: list[FileDiff]) -> list[TouchedNode]:
        """Map all file diffs to touched graph nodes."""
        touched: list[TouchedNode] = []

        for fd in file_diffs:
            path = fd.path
            if not path:
                continue

            if fd.is_new:
                # New file — all nodes in it are "added"
                nodes = self.store.get_nodes_by_file(path)
                for node in nodes:
                    if node.kind != "FILE":
                        touched.append(TouchedNode(
                            node_id=node.id, file_path=path, touch_type="added",
                        ))
            elif fd.is_deleted:
                # Deleted file — use old_path if available
                old_path = fd.old_path or path
                nodes = self.store.get_nodes_by_file(old_path)
                for node in nodes:
                    if node.kind != "FILE":
                        touched.append(TouchedNode(
                            node_id=node.id, file_path=old_path, touch_type="deleted",
                        ))
            else:
                # Modified file — find overlapping nodes
                nodes = self.store.get_nodes_by_file(path)
                modified_lines = set(fd.all_modified_lines)

                for node in nodes:
                    if node.kind == "FILE":
                        continue
                    node_lines = set(range(node.line_start, node.line_end + 1))
                    overlap = modified_lines & node_lines
                    if overlap:
                        touched.append(TouchedNode(
                            node_id=node.id, file_path=path,
                            touch_type="modified",
                            lines_touched=sorted(overlap),
                        ))

        return touched
