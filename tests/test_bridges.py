"""Tests for cross-language bridge declarations (engram.yaml)."""

import pytest
import shutil
from pathlib import Path

from engram.bridges import (
    load_config,
    parse_bridges,
    resolve_bridge_edges,
    build_bridges,
    BridgeDeclaration,
)
from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.graph.traversal import GraphTraversal
from engram.graph.activation import ChangeType
from engram.cli import build_index


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def mixed_project(tmp_path):
    """Build index on mixed_project (Python + TypeScript) in a temp dir."""
    project = tmp_path / "mixed_project"
    shutil.copytree(FIXTURES_DIR / "mixed_project", project)
    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    return store, db, project


# --- Config loading ---

def test_load_config_found(tmp_path):
    config_path = tmp_path / "engram.yaml"
    config_path.write_text("bridges:\\n  - name: test\\n")
    config = load_config(tmp_path)
    assert config is not None
    assert "bridges" in config


def test_load_config_yml_extension(tmp_path):
    config_path = tmp_path / "engram.yml"
    config_path.write_text("bridges: []\\n")
    config = load_config(tmp_path)
    assert config is not None


def test_load_config_missing(tmp_path):
    config = load_config(tmp_path)
    assert config is None


def test_load_fixture_config():
    config = load_config(FIXTURES_DIR / "mixed_project")
    assert config is not None
    assert len(config["bridges"]) == 2
    assert config["bridges"][0]["name"] == "Event API"


# --- Bridge parsing ---

def test_parse_backend_frontend_bridge():
    config = {
        "bridges": [{
            "name": "Test API",
            "backend": {"node": "views.py::list_items"},
            "frontend": {"files": ["hooks/useItems.ts"]},
            "bidirectional": True,
        }]
    }
    bridges = parse_bridges(config)
    assert len(bridges) == 1
    assert bridges[0].name == "Test API"
    assert bridges[0].backend_node == "views.py::list_items"
    assert bridges[0].frontend_files == ["hooks/useItems.ts"]
    assert bridges[0].bidirectional is True


def test_parse_from_to_bridge():
    config = {
        "bridges": [{
            "name": "Auth",
            "from": {"node": "auth.ts::login"},
            "to": {"node": "api/auth.py::authenticate"},
            "edge_kind": "API_CALL",
            "bidirectional": False,
        }]
    }
    bridges = parse_bridges(config)
    assert len(bridges) == 1
    assert bridges[0].from_node == "auth.ts::login"
    assert bridges[0].to_node == "api/auth.py::authenticate"
    assert bridges[0].edge_kind == "API_CALL"
    assert bridges[0].bidirectional is False


def test_parse_empty_bridges():
    assert parse_bridges({}) == []
    assert parse_bridges({"bridges": []}) == []
    assert parse_bridges({"bridges": None}) == []


# --- Edge resolution ---

def test_resolve_bridge_creates_edges(mixed_project):
    store, _, _ = mixed_project
    bridge = BridgeDeclaration(
        name="Test",
        backend_node="backend/views.py::list_events",
        frontend_files=["frontend/useEvents.ts"],
        bidirectional=True,
    )
    edges = resolve_bridge_edges(bridge, store)
    # Should create edges in both directions (bidirectional)
    # backend node → each frontend node, and reverse
    assert len(edges) >= 2  # At least 1 pair for the backend node
    source_ids = {e.source_id for e in edges}
    target_ids = {e.target_id for e in edges}
    assert "backend/views.py::list_events" in source_ids or "backend/views.py::list_events" in target_ids


def test_resolve_bridge_unidirectional(mixed_project):
    store, _, _ = mixed_project
    bridge = BridgeDeclaration(
        name="OneWay",
        backend_node="backend/views.py::list_events",
        frontend_files=["frontend/useEvents.ts"],
        bidirectional=False,
    )
    edges = resolve_bridge_edges(bridge, store)
    # Only forward edges, not reverse
    for edge in edges:
        assert edge.source_id == "backend/views.py::list_events"


def test_resolve_bridge_nonexistent_node(mixed_project):
    store, _, _ = mixed_project
    bridge = BridgeDeclaration(
        name="Ghost",
        backend_node="nonexistent.py::ghost_function",
        frontend_files=["frontend/useEvents.ts"],
    )
    edges = resolve_bridge_edges(bridge, store)
    # No source resolved → no edges
    assert len(edges) == 0


def test_resolve_bridge_file_to_all_nodes(mixed_project):
    store, _, _ = mixed_project
    bridge = BridgeDeclaration(
        name="FileLevel",
        backend_node="backend/views.py::list_events",
        frontend_files=["frontend/EventList.tsx"],
        bidirectional=False,
    )
    edges = resolve_bridge_edges(bridge, store)
    # Should connect to all functions in EventList.tsx
    target_ids = {e.target_id for e in edges}
    assert len(target_ids) >= 1


# --- build_bridges integration ---

def test_build_bridges_from_yaml(mixed_project):
    store, _, project = mixed_project
    count = build_bridges(project, store)
    assert count > 0

    # Verify edges are in the DB
    all_bridge_edges = store.conn.execute(
        "SELECT * FROM edges WHERE kind = 'API_BRIDGE'"
    ).fetchall()
    assert len(all_bridge_edges) > 0


def test_build_bridges_idempotent(mixed_project):
    store, _, project = mixed_project
    count1 = build_bridges(project, store)
    count2 = build_bridges(project, store)
    assert count1 == count2

    # Should not double up edges
    all_bridge_edges = store.conn.execute(
        "SELECT * FROM edges WHERE kind = 'API_BRIDGE'"
    ).fetchall()
    assert len(all_bridge_edges) == count1


def test_build_bridges_no_yaml(tmp_path):
    """No engram.yaml → 0 edges, no crash."""
    project = tmp_path / "empty"
    project.mkdir()
    db = EngramDB(project)
    store = GraphStore(db)
    count = build_bridges(project, store)
    assert count == 0


# --- Traversal crosses bridges ---

def test_traversal_crosses_bridge(mixed_project):
    """The whole point: changing a backend function should surface frontend nodes."""
    store, _, project = mixed_project

    # Build bridges
    build_bridges(project, store)

    # Traverse from backend views.py::list_events with SIGNATURE_MODIFICATION
    traversal = GraphTraversal(store)
    affected = traversal.traverse(
        seeds=["backend/views.py::list_events"],
        change_types={ChangeType.SIGNATURE_MODIFICATION},
        max_depth=2,
    )

    affected_ids = {a.node_id for a in affected}

    # Frontend nodes should appear in affected set via API_BRIDGE edges
    frontend_nodes = {nid for nid in affected_ids if "frontend/" in nid}
    assert len(frontend_nodes) > 0, (
        f"Expected frontend nodes in affected set, got: {affected_ids}"
    )


def test_traversal_crosses_bridge_body_mod(mixed_project):
    """BODY_MODIFICATION should also cross bridges."""
    store, _, project = mixed_project
    build_bridges(project, store)

    traversal = GraphTraversal(store)
    affected = traversal.traverse(
        seeds=["backend/views.py::create_event"],
        change_types={ChangeType.BODY_MODIFICATION},
        max_depth=2,
    )

    affected_ids = {a.node_id for a in affected}
    frontend_nodes = {nid for nid in affected_ids if "frontend/" in nid}
    assert len(frontend_nodes) > 0


def test_verification_catches_missing_frontend(mixed_project):
    """Verifier should flag frontend nodes as missing when backend changes."""
    store, _, project = mixed_project
    build_bridges(project, store)

    from engram.verification.verifier import Verifier, Verdict

    # Simulate: only backend/views.py was touched, frontend was not
    diff_text = """diff --git a/backend/views.py b/backend/views.py
--- a/backend/views.py
+++ b/backend/views.py
@@ -6,7 +6,7 @@
 def list_events(request):
-    events = Event.objects.all()
+    events = Event.objects.filter(active=True)
     serializer = EventSerializer(events, many=True)
     return serializer.data
"""
    verifier = Verifier(store)
    result = verifier.verify(
        diff_text=diff_text,
        seeds=["backend/views.py::list_events"],
        change_types={"BODY_MODIFICATION"},
    )

    # With API_BRIDGE in BODY_MODIFICATION rules, frontend nodes should be in expected set
    expected_ids = {e.node_id for e in result.expected_nodes if e.depth > 0}
    frontend_expected = {nid for nid in expected_ids if "frontend/" in nid}
    assert len(frontend_expected) > 0, (
        f"Expected frontend nodes in expected set, got: {expected_ids}"
    )


# --- CLI build_index includes bridges ---

def test_build_index_reports_bridge_count(mixed_project):
    """build_index stats should include bridge_edges count."""
    _, db, project = mixed_project
    stats = build_index(project, db, force=True)
    assert "bridge_edges" in stats
    assert stats["bridge_edges"] > 0
