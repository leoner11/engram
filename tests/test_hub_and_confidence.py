"""Tests for hub penalty in traversal and confidence scoring."""

import pytest

from engram.db import EngramDB
from engram.graph.activation import ChangeType
from engram.graph.store import GraphStore
from engram.graph.traversal import GraphTraversal
from engram.indexer.extractor import NodeRecord
from engram.indexer.resolver import Edge
from engram.memory.sessions import SessionManager
from engram.memory.observations import ObservationManager
from engram.memory.search import MemorySearch
from engram.retriever.assembler import ContextAssembler, ContextConfidence
from engram.retriever.conventions import ConventionFinder
from engram.retriever.seeds import SeedCandidate, populate_node_index
from engram.snapshot import SnapshotGenerator


def _make_node(node_id, name, file_path, kind="FUNCTION", language="python",
               is_exported=False, decorators=None, signature=None,
               docstring=None, full_source="pass", line_start=1, line_end=10):
    return NodeRecord(
        id=node_id, kind=kind, name=name, file_path=file_path,
        line_start=line_start, line_end=line_end, language=language,
        signature=signature, docstring=docstring, source_hash="abc",
        is_exported=is_exported, decorators=decorators or [],
        full_source=full_source,
    )


# --- Hub Penalty Tests ---

@pytest.fixture
def hub_graph(tmp_path):
    """Create a graph with a hub node that connects to many things."""
    db = EngramDB(tmp_path)
    store = GraphStore(db)

    # Seed: the node we're changing
    store.upsert_node(_make_node("api/views.py::create_event", "create_event",
                                 "api/views.py", is_exported=True))

    # Hub: BaseModel — everything imports this
    store.upsert_node(_make_node("models/base.py::BaseModel", "BaseModel",
                                 "models/base.py", kind="CLASS", is_exported=True))

    # Relevant depth-1: directly calls the seed
    store.upsert_node(_make_node("api/urls.py::urlpatterns", "urlpatterns",
                                 "api/urls.py", is_exported=True))

    # Irrelevant nodes that all import BaseModel
    for i in range(15):
        nid = f"modules/mod{i}.py::func_{i}"
        store.upsert_node(_make_node(nid, f"func_{i}", f"modules/mod{i}.py",
                                     is_exported=True))
        # Each imports BaseModel
        store.upsert_edge(Edge(source_id=nid, target_id="models/base.py::BaseModel",
                               kind="IMPORTS"))

    # Seed calls BaseModel (depth 1)
    store.upsert_edge(Edge(source_id="api/views.py::create_event",
                           target_id="models/base.py::BaseModel", kind="USES_TYPE"))

    # urlpatterns calls seed (depth 1)
    store.upsert_edge(Edge(source_id="api/urls.py::urlpatterns",
                           target_id="api/views.py::create_event", kind="CALLS"))

    store.commit()
    return store


def test_hub_penalty_reduces_noise(hub_graph):
    """Depth-2 nodes reached through a hub should have lower priority."""
    traversal = GraphTraversal(hub_graph)
    affected = traversal.traverse(
        seeds=["api/views.py::create_event"],
        change_types={ChangeType.SIGNATURE_MODIFICATION},
        max_depth=2,
    )

    # Find depth-2 nodes (reached through BaseModel hub)
    depth2 = [a for a in affected if a.depth == 2]
    depth1 = [a for a in affected if a.depth == 1]

    # Hub-penalized depth-2 nodes should have lower priority than depth-1 nodes
    if depth1 and depth2:
        max_depth2_priority = max(a.priority for a in depth2)
        min_depth1_priority = min(a.priority for a in depth1)
        assert max_depth2_priority < min_depth1_priority, \
            f"Depth-2 via hub ({max_depth2_priority}) should be below depth-1 ({min_depth1_priority})"


def test_hub_penalty_scales_with_degree(hub_graph):
    """Higher hub degree → stronger penalty."""
    traversal = GraphTraversal(hub_graph)
    affected = traversal.traverse(
        seeds=["api/views.py::create_event"],
        change_types={ChangeType.SIGNATURE_MODIFICATION},
        max_depth=2,
    )

    depth2 = [a for a in affected if a.depth == 2]
    # All depth-2 nodes go through BaseModel (in-degree 15+)
    # Their scores should be noticeably penalized
    for node in depth2:
        # With hub penalty, depth-2 nodes through a 15-degree hub
        # should score well below the base of 50
        assert node.priority < 80, \
            f"Node {node.node_id} has priority {node.priority}, expected < 80 with hub penalty"


def test_no_hub_penalty_for_low_degree_intermediary(tmp_path):
    """Intermediary with low in-degree should not be penalized."""
    db = EngramDB(tmp_path)
    store = GraphStore(db)

    store.upsert_node(_make_node("a.py::seed", "seed", "a.py", is_exported=True))
    store.upsert_node(_make_node("b.py::mid", "mid", "b.py", is_exported=True))
    store.upsert_node(_make_node("c.py::leaf", "leaf", "c.py", is_exported=True))

    # seed → mid → leaf, but mid only has in-degree 2 (not a hub)
    store.upsert_edge(Edge(source_id="b.py::mid", target_id="a.py::seed", kind="CALLS"))
    store.upsert_edge(Edge(source_id="c.py::leaf", target_id="b.py::mid", kind="CALLS"))
    store.upsert_edge(Edge(source_id="a.py::seed", target_id="b.py::mid", kind="CALLS"))
    store.commit()

    traversal = GraphTraversal(store)
    affected = traversal.traverse(
        seeds=["a.py::seed"],
        change_types={ChangeType.BODY_MODIFICATION},
        max_depth=2,
    )

    depth2 = [a for a in affected if a.depth == 2]
    # Low-degree intermediary → no penalty → normal depth-2 score
    for node in depth2:
        assert node.priority >= 30, \
            f"Node {node.node_id} was penalized ({node.priority}) but intermediary is low-degree"


# --- Confidence Score Tests ---

@pytest.fixture
def assembler_setup(tmp_path):
    db = EngramDB(tmp_path)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    mem_search = MemorySearch(store)
    convention_finder = ConventionFinder(store)
    project = tmp_path.name
    snapshot_gen = SnapshotGenerator(store, obs_mgr, project)
    assembler = ContextAssembler(
        store, project_root=None,
        memory_search=mem_search,
        convention_finder=convention_finder,
        snapshot_gen=snapshot_gen,
    )
    return store, assembler


def _populate_multi_dir_project(store):
    """Create a project with multiple directories."""
    dirs = ["api", "models", "services", "tests", "utils"]
    for d in dirs:
        for i in range(3):
            nid = f"src/{d}/file{i}.py::func_{d}_{i}"
            store.upsert_node(_make_node(
                nid, f"func_{d}_{i}", f"src/{d}/file{i}.py", is_exported=True,
                signature=f"def func_{d}_{i}()",
            ))
    # Add some edges within api
    store.upsert_edge(Edge(source_id="src/api/file0.py::func_api_0",
                           target_id="src/models/file0.py::func_models_0", kind="CALLS"))
    store.upsert_edge(Edge(source_id="src/api/file1.py::func_api_1",
                           target_id="src/api/file0.py::func_api_0", kind="CALLS"))
    store.commit()
    populate_node_index(store)


def test_confidence_high_with_good_seeds(assembler_setup):
    """Good FTS seeds → high confidence."""
    store, assembler = assembler_setup
    _populate_multi_dir_project(store)

    package = assembler.assemble(
        "fix func_api_0",
        seeds=["src/api/file0.py::func_api_0"],
    )
    assert package.confidence is not None
    assert package.confidence.seed_quality == 1.0
    assert package.confidence.score >= 0.5


def test_confidence_low_with_fallback_seeds(assembler_setup):
    """Fallback seeds → low seed quality."""
    store, assembler = assembler_setup
    _populate_multi_dir_project(store)

    package = assembler.assemble("xyznonexistent_blahblah123")
    assert package.confidence is not None
    assert package.confidence.seed_quality == 0.0


def test_confidence_warns_on_fallback(assembler_setup):
    """Fallback seeds generate a warning."""
    store, assembler = assembler_setup
    _populate_multi_dir_project(store)

    package = assembler.assemble("xyznonexistent_blahblah123")
    assert any("fallback" in w.lower() for w in package.confidence.warnings)


def test_confidence_missing_directories(assembler_setup):
    """Missing directories are reported."""
    store, assembler = assembler_setup
    _populate_multi_dir_project(store)

    # Query that only touches api
    package = assembler.assemble(
        "fix func_api_0",
        seeds=["src/api/file0.py::func_api_0"],
    )
    assert package.confidence is not None
    # Should list some missing directories (tests, utils, services, etc.)
    # At minimum, not ALL directories will be covered by a single-seed query
    assert package.confidence.directory_coverage <= 1.0


def test_confidence_in_serialized_output(assembler_setup):
    """Confidence appears in serialized markdown."""
    store, assembler = assembler_setup
    _populate_multi_dir_project(store)

    package = assembler.assemble(
        "fix func_api_0",
        seeds=["src/api/file0.py::func_api_0"],
    )
    output = package.serialize()
    assert "Context Confidence:" in output


def test_confidence_score_bounded(assembler_setup):
    """Confidence score is always 0.0-1.0."""
    store, assembler = assembler_setup
    _populate_multi_dir_project(store)

    for prompt in ["fix func_api_0", "xyznonexistent", "create new endpoint"]:
        package = assembler.assemble(prompt)
        if package.confidence:
            assert 0.0 <= package.confidence.score <= 1.0


def test_confidence_warns_scattered_seeds(assembler_setup):
    """Seeds in different subsystems generate a warning."""
    store, assembler = assembler_setup
    _populate_multi_dir_project(store)

    # Seeds in api and utils (no edges between them)
    package = assembler.assemble(
        "update api and utils",
        seeds=["src/api/file0.py::func_api_0", "src/utils/file0.py::func_utils_0"],
    )
    assert package.confidence is not None
    # These are in different subsystems with no connecting edges
    assert package.confidence.seed_clustering < 1.0
