"""Cross-project structural pattern detection, catalog, and matching."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from engram.graph.store import GraphStore
from engram.indexer.resolver import Edge


@dataclass
class StructuralPattern:
    id: str
    name: str
    framework: str | None
    description: str
    node_pattern: dict
    implicit_edges: list[dict]
    priority_hints: dict = field(default_factory=dict)
    source_project: str | None = None
    confidence: float = 0.5
    is_builtin: bool = False


# Built-in patterns shipped with Engram
BUILTIN_PATTERNS = [
    StructuralPattern(
        id="django/views-serializers-models",
        name="Django View → Serializer → Model chain",
        framework="django",
        description="Views use serializers which use models. Model field changes affect all three.",
        node_pattern={
            "anchor": {"kind": "FUNCTION", "decorators_contain": ["@api_view", "@action"]},
            "chain": [
                {"edge": "CALLS", "target": {"kind": "CLASS", "name_contains": "Serializer"}},
                {"edge": "USES_TYPE", "target": {"kind": "CLASS", "name_contains": "Model"}},
            ],
        },
        implicit_edges=[{
            "from_pattern": "anchor",
            "to_pattern": "chain[1].target",
            "edge_kind": "USES_TYPE",
            "usage_pattern": "partial",
            "reason": "View depends on model indirectly through serializer",
        }],
        priority_hints={"chain[0].target": {"boost": 20}},
        confidence=0.85,
        is_builtin=True,
    ),
    StructuralPattern(
        id="generic/repository-service",
        name="Service → Repository → Model",
        framework=None,
        description="Service layer calls repository which accesses models. Model changes propagate through both.",
        node_pattern={
            "anchor": {"kind": "FUNCTION", "name_contains": "service"},
            "chain": [
                {"edge": "CALLS", "target": {"kind": "FUNCTION", "name_contains": "repository"}},
            ],
        },
        implicit_edges=[],
        priority_hints={"chain[0].target": {"boost": 10}},
        confidence=0.7,
        is_builtin=True,
    ),
    StructuralPattern(
        id="fastapi/route-schema-model",
        name="FastAPI Route → Pydantic Schema → ORM Model",
        framework="fastapi",
        description="Routes use Pydantic schemas which map to ORM models.",
        node_pattern={
            "anchor": {"kind": "FUNCTION", "decorators_contain": ["@app.get", "@app.post", "@router"]},
            "chain": [
                {"edge": "USES_TYPE", "target": {"kind": "CLASS", "name_contains": "Schema"}},
            ],
        },
        implicit_edges=[],
        priority_hints={},
        confidence=0.8,
        is_builtin=True,
    ),
]


class PatternDetector:
    """Detect framework and structural patterns in a project."""

    def __init__(self, store: GraphStore):
        self.store = store

    def detect_framework(self) -> str | None:
        """Auto-detect project framework from source text."""
        all_nodes = self.store.get_all_nodes()
        source_text = ""
        for node in all_nodes.values():
            if node.kind == "FILE" and node.full_source:
                source_text += node.full_source[:500] + "\n"

        checks = [
            ("django", ["from django", "import django"]),
            ("fastapi", ["from fastapi", "import fastapi"]),
            ("flask", ["from flask", "import flask"]),
            ("express", ["require('express')", "from 'express'"]),
            ("react", ["from 'react'", "import React"]),
        ]
        for framework, keywords in checks:
            for kw in keywords:
                if kw in source_text:
                    return framework
        return None

    def detect_patterns(self) -> list[StructuralPattern]:
        """Find matching patterns in the current project graph."""
        framework = self.detect_framework()
        matched = []

        for pattern in BUILTIN_PATTERNS:
            if pattern.framework and pattern.framework != framework:
                continue
            if self._pattern_matches(pattern):
                matched.append(pattern)

        return sorted(matched, key=lambda p: -p.confidence)

    def _pattern_matches(self, pattern: StructuralPattern) -> bool:
        """Check if a pattern's node_pattern matches the graph."""
        anchor_spec = pattern.node_pattern.get("anchor", {})
        all_nodes = self.store.get_all_nodes()

        for node in all_nodes.values():
            if self._node_matches_spec(node, anchor_spec):
                return True
        return False

    def _node_matches_spec(self, node, spec: dict) -> bool:
        """Check if a node matches a pattern spec."""
        if "kind" in spec and node.kind != spec["kind"]:
            return False
        if "name_contains" in spec and spec["name_contains"].lower() not in node.name.lower():
            return False
        if "decorators_contain" in spec:
            node_decs = " ".join(node.decorators).lower()
            if not any(d.lower() in node_decs for d in spec["decorators_contain"]):
                return False
        return True


class PatternCatalog:
    """Store and retrieve patterns."""

    def __init__(self, store: GraphStore):
        self.store = store
        self._ensure_table()

    def _ensure_table(self):
        self.store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS patterns (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                framework   TEXT,
                description TEXT NOT NULL,
                node_pattern TEXT NOT NULL,
                implicit_edges TEXT NOT NULL,
                priority_hints TEXT,
                source_project TEXT,
                confidence  REAL NOT NULL DEFAULT 0.5,
                is_builtin  BOOLEAN DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        self.store.conn.commit()

    def get_all(self, framework: str | None = None) -> list[StructuralPattern]:
        """Get builtins + learned patterns."""
        patterns = [p for p in BUILTIN_PATTERNS
                    if framework is None or p.framework is None or p.framework == framework]

        rows = self.store.conn.execute("SELECT * FROM patterns").fetchall()
        for row in rows:
            patterns.append(StructuralPattern(
                id=row["id"], name=row["name"],
                framework=row["framework"], description=row["description"],
                node_pattern=json.loads(row["node_pattern"]),
                implicit_edges=json.loads(row["implicit_edges"]),
                priority_hints=json.loads(row["priority_hints"] or "{}"),
                source_project=row["source_project"],
                confidence=row["confidence"],
                is_builtin=bool(row["is_builtin"]),
            ))
        return patterns

    def save_learned(self, pattern: StructuralPattern):
        self.store.conn.execute(
            """INSERT OR REPLACE INTO patterns
               (id, name, framework, description, node_pattern, implicit_edges,
                priority_hints, source_project, confidence, is_builtin)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (pattern.id, pattern.name, pattern.framework, pattern.description,
             json.dumps(pattern.node_pattern), json.dumps(pattern.implicit_edges),
             json.dumps(pattern.priority_hints), pattern.source_project, pattern.confidence),
        )
        self.store.conn.commit()

    def export_patterns(self, output_path: Path):
        patterns = self.get_all()
        data = [{"id": p.id, "name": p.name, "framework": p.framework,
                 "description": p.description, "node_pattern": p.node_pattern,
                 "implicit_edges": p.implicit_edges, "priority_hints": p.priority_hints,
                 "confidence": p.confidence} for p in patterns]
        output_path.write_text(json.dumps(data, indent=2))

    def import_patterns(self, input_path: Path) -> int:
        data = json.loads(input_path.read_text())
        imported = 0
        for item in data:
            existing = self.store.conn.execute(
                "SELECT id FROM patterns WHERE id = ?", (item["id"],)
            ).fetchone()
            if not existing:
                pattern = StructuralPattern(**item, is_builtin=False)
                self.save_learned(pattern)
                imported += 1
        return imported


class PatternMatcher:
    """Match patterns against the graph and generate implicit edges for traversal."""

    def __init__(self, store: GraphStore, catalog: PatternCatalog | None = None):
        self.store = store
        self.catalog = catalog

    def get_implicit_edges(self, framework: str | None = None) -> list[Edge]:
        """
        Match all active patterns against the current graph and return
        the implicit edges they generate.

        These edges are added to the traversal temporarily so that
        pattern-implied relationships (e.g. View → Model through Serializer)
        are traversed correctly.
        """
        if self.catalog is None:
            return []

        patterns = self.catalog.get_all(framework=framework)
        implicit_edges = []

        all_nodes = self.store.get_all_nodes()

        for pattern in patterns:
            if not pattern.implicit_edges:
                continue

            # Find all bindings for this pattern
            bindings = self._match_pattern(pattern, all_nodes)
            for binding in bindings:
                for ie_spec in pattern.implicit_edges:
                    from_id = binding.get(ie_spec.get("from_pattern", ""))
                    to_id = binding.get(ie_spec.get("to_pattern", ""))
                    if from_id and to_id:
                        edge = Edge(
                            source_id=from_id,
                            target_id=to_id,
                            kind=ie_spec.get("edge_kind", "USES_TYPE"),
                            metadata={
                                "usage_pattern": ie_spec.get("usage_pattern", "partial"),
                                "pattern_id": pattern.id,
                                "reason": ie_spec.get("reason", "pattern-implied"),
                            },
                        )
                        implicit_edges.append(edge)
        return implicit_edges

    def _match_pattern(self, pattern: StructuralPattern, all_nodes: dict) -> list[dict]:
        """Find all instances of a pattern in the graph, return bindings."""
        anchor_spec = pattern.node_pattern.get("anchor", {})
        chain = pattern.node_pattern.get("chain", [])
        bindings = []

        for node in all_nodes.values():
            if not self._node_matches_spec(node, anchor_spec):
                continue

            # Try to walk the chain from this anchor
            binding = {"anchor": node.id}
            current_id = node.id
            chain_complete = True

            for i, step in enumerate(chain):
                edge_kind = step.get("edge", "CALLS")
                target_spec = step.get("target", {})

                # Find outgoing edges of this kind from current node
                outgoing = self.store.get_edges_from(current_id)
                matched_target = None
                for edge in outgoing:
                    if edge.kind != edge_kind:
                        continue
                    target_node = all_nodes.get(edge.target_id)
                    if target_node and self._node_matches_spec(target_node, target_spec):
                        matched_target = edge.target_id
                        break

                if matched_target:
                    binding[f"chain[{i}].target"] = matched_target
                    current_id = matched_target
                else:
                    chain_complete = False
                    break

            if chain_complete:
                bindings.append(binding)

        return bindings

    def _node_matches_spec(self, node, spec: dict) -> bool:
        """Check if a node matches a pattern spec."""
        if "kind" in spec and node.kind != spec["kind"]:
            return False
        if "name_contains" in spec and spec["name_contains"].lower() not in node.name.lower():
            return False
        if "decorators_contain" in spec:
            node_decs = " ".join(node.decorators).lower() if node.decorators else ""
            if not any(d.lower() in node_decs for d in spec["decorators_contain"]):
                return False
        return True

