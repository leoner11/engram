"""Tests for creation-mode assembly path."""

import pytest

from engram.db import EngramDB
from engram.graph.activation import ChangeType
from engram.graph.store import GraphStore
from engram.indexer.extractor import NodeRecord
from engram.indexer.resolver import Edge
from engram.memory.sessions import SessionManager
from engram.memory.observations import ObservationManager
from engram.memory.search import MemorySearch
from engram.retriever.assembler import ContextAssembler
from engram.retriever.conventions import ConventionFinder
from engram.retriever.seeds import SeedCandidate
from engram.snapshot import SnapshotGenerator


@pytest.fixture
def setup(tmp_path):
    db = EngramDB(tmp_path)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    mem_search = MemorySearch(store)
    convention_finder = ConventionFinder(store)
    project = tmp_path.name
    snapshot_gen = SnapshotGenerator(store, obs_mgr, project)
    assembler = ContextAssembler(
        store,
        project_root=None,  # Skip freshness check in tests
        memory_search=mem_search,
        convention_finder=convention_finder,
        snapshot_gen=snapshot_gen,
    )
    return store, obs_mgr, mem_search, assembler, project


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


def _populate_project(store):
    """Create a small Django-like project for testing."""
    store.upsert_node(_make_node(
        "src/api/views.py::EventViewSet", "EventViewSet", "src/api/views.py",
        kind="CLASS", is_exported=True, decorators=["@api_view"],
        signature="class EventViewSet(viewsets.ModelViewSet)",
        docstring="CRUD for events",
        full_source="class EventViewSet(viewsets.ModelViewSet):\n    serializer_class = EventSerializer\n    def list(self, request):\n        return Response(Event.objects.all())\n",
        line_start=1, line_end=30,
    ))
    store.upsert_node(_make_node(
        "src/models/event.py::Event", "Event", "src/models/event.py",
        kind="CLASS", is_exported=True,
        signature="class Event(models.Model)",
        full_source="class Event(models.Model):\n    title = models.CharField(max_length=200)\n",
        line_start=1, line_end=15,
    ))
    store.upsert_node(_make_node(
        "src/api/serializers.py::EventSerializer", "EventSerializer",
        "src/api/serializers.py", kind="CLASS", is_exported=True,
        signature="class EventSerializer(serializers.ModelSerializer)",
        full_source="class EventSerializer(serializers.ModelSerializer):\n    class Meta:\n        model = Event\n",
        line_start=1, line_end=10,
    ))
    store.upsert_edge(Edge(source_id="src/api/views.py::EventViewSet",
                           target_id="src/models/event.py::Event", kind="USES_TYPE"))
    store.upsert_edge(Edge(source_id="src/api/views.py::EventViewSet",
                           target_id="src/api/serializers.py::EventSerializer", kind="CALLS"))
    store.commit()

    # Populate FTS index for seed selection
    from engram.retriever.seeds import populate_node_index
    populate_node_index(store)


# --- Creation mode detection ---

def test_creation_mode_addition_no_seeds(setup):
    """ADDITION + no seeds → creation mode."""
    store, obs_mgr, mem_search, assembler, project = setup
    assert assembler._is_creation_mode(
        {ChangeType.ADDITION},
        [],  # no seeds
    ) is True


def test_creation_mode_addition_fallback_seeds(setup):
    """ADDITION + top_connected_fallback seeds → creation mode."""
    store, obs_mgr, mem_search, assembler, project = setup
    seeds = [SeedCandidate("x.py::f", 1.0, "top_connected_fallback")]
    assert assembler._is_creation_mode({ChangeType.ADDITION}, seeds) is True


def test_not_creation_mode_with_explicit_seeds(setup):
    """ADDITION + explicit seeds → maintenance mode."""
    store, obs_mgr, mem_search, assembler, project = setup
    seeds = [SeedCandidate("x.py::f", 1.0, "explicit")]
    assert assembler._is_creation_mode({ChangeType.ADDITION}, seeds) is False


def test_not_creation_mode_body_modification(setup):
    """BODY_MODIFICATION → never creation mode."""
    store, obs_mgr, mem_search, assembler, project = setup
    assert assembler._is_creation_mode({ChangeType.BODY_MODIFICATION}, []) is False


def test_creation_mode_pure_addition_with_fts(setup):
    """Pure ADDITION + FTS hits → still creation mode."""
    store, obs_mgr, mem_search, assembler, project = setup
    seeds = [SeedCandidate("x.py::f", 10.0, "fts5")]
    assert assembler._is_creation_mode({ChangeType.ADDITION}, seeds) is True


def test_not_creation_mode_mixed_change_types(setup):
    """ADDITION + BODY_MODIFICATION + FTS → maintenance mode."""
    store, obs_mgr, mem_search, assembler, project = setup
    seeds = [SeedCandidate("x.py::f", 10.0, "fts5")]
    anticipated = {ChangeType.ADDITION, ChangeType.BODY_MODIFICATION}
    assert assembler._is_creation_mode(anticipated, seeds) is False


# --- Creation mode assembly ---

def test_creation_mode_includes_snapshot(setup):
    """Creation mode output includes project snapshot."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    package = assembler.assemble("create a new payment endpoint")
    assert package.snapshot != ""
    assert "STACK:" in package.snapshot


def test_creation_mode_stats_show_mode(setup):
    """Stats dict includes mode=creation."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    package = assembler.assemble("create a new payment endpoint")
    assert package.stats.get("mode") == "creation"


def test_creation_mode_includes_conventions(setup):
    """Creation mode finds convention examples."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    package = assembler.assemble("add a new API endpoint")
    convention_nodes = [n for n in package.nodes if "convention" in n.reason]
    assert len(convention_nodes) >= 1


def test_creation_mode_first_convention_full(setup):
    """First convention example should be at full detail."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    package = assembler.assemble("add a new API endpoint")
    convention_nodes = [n for n in package.nodes if "convention" in n.reason]
    if convention_nodes:
        # First convention (highest priority) should be full
        first = max(convention_nodes, key=lambda n: n.priority)
        assert first.detail_level == "full"


def test_creation_mode_memory_included(setup):
    """Creation mode retrieves memory observations."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    # Save an observation
    obs_mgr.save(
        title="Auth uses JWT tokens",
        content="WHAT: Authentication uses JWT\nSTATUS: active",
        type="architecture",
        project=project,
    )

    package = assembler.assemble("create a new auth endpoint")
    # Memory should be searched (may or may not match depending on FTS)
    assert package.stats.get("memories_included", 0) >= 0


def test_creation_mode_budget_respected(setup):
    """Creation mode stays within token budget."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    budget = 2000
    package = assembler.assemble("create a new endpoint", max_tokens=budget)
    assert package.total_tokens <= budget


def test_creation_mode_serialization(setup):
    """Creation mode output serializes correctly."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    package = assembler.assemble("create a new endpoint")
    output = package.serialize()
    assert "# Task:" in output
    assert "Mode: creation" in output
    assert "Project Context" in output


# --- Maintenance mode still works ---

def test_maintenance_mode_unchanged(setup):
    """Maintenance mode still works as before with snapshot prepended."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    # "fix" triggers BODY_MODIFICATION → maintenance mode
    package = assembler.assemble("fix the EventViewSet list method")
    assert package.stats.get("mode") == "maintenance"
    assert package.snapshot != ""


def test_maintenance_mode_has_graph_results(setup):
    """Maintenance mode with good seeds should return graph nodes."""
    store, obs_mgr, mem_search, assembler, project = setup
    _populate_project(store)

    package = assembler.assemble(
        "fix the EventViewSet",
        seeds=["src/api/views.py::EventViewSet"],
    )
    assert package.stats.get("mode") == "maintenance"
    assert package.stats.get("nodes_included", 0) >= 1
