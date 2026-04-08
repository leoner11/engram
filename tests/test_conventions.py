"""Tests for convention discovery — finding sibling code patterns."""

import pytest

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.indexer.extractor import NodeRecord
from engram.indexer.resolver import Edge
from engram.retriever.conventions import ConventionFinder
from engram.retriever.seeds import SeedCandidate


@pytest.fixture
def setup(tmp_path):
    db = EngramDB(tmp_path)
    store = GraphStore(db)
    finder = ConventionFinder(store)
    return store, finder


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


# --- Category detection ---

def test_detect_endpoint_from_prompt(setup):
    store, finder = setup
    hints = finder._detect_category("add a new API endpoint")
    assert hints is not None
    assert "api_view" in str(hints.get("decorators", [])).lower() or \
           "endpoint" in str(hints.get("name_patterns", []))


def test_detect_model_from_prompt(setup):
    store, finder = setup
    hints = finder._detect_category("create a User model")
    assert hints is not None
    assert "model" in str(hints.get("name_patterns", [])).lower()


def test_detect_component_from_prompt(setup):
    store, finder = setup
    hints = finder._detect_category("build a login component")
    assert hints is not None
    assert "component" in str(hints.get("name_patterns", [])).lower()


def test_detect_service_from_prompt(setup):
    store, finder = setup
    hints = finder._detect_category("implement a payment service")
    assert hints is not None
    assert "service" in str(hints.get("name_patterns", [])).lower()


def test_detect_test_from_prompt(setup):
    store, finder = setup
    hints = finder._detect_category("write tests for the auth module")
    assert hints is not None


def test_no_category_for_vague_prompt(setup):
    store, finder = setup
    hints = finder._detect_category("do something")
    assert hints is None


# --- Finding by hint ---

def test_find_by_decorator(setup):
    store, finder = setup
    store.upsert_node(_make_node("v.py::list_events", "list_events", "v.py",
                                 decorators=["@api_view(['GET'])"],
                                 is_exported=True))
    store.upsert_node(_make_node("v.py::create_event", "create_event", "v.py",
                                 decorators=["@api_view(['POST'])"],
                                 is_exported=True))
    store.upsert_node(_make_node("m.py::Event", "Event", "m.py", kind="CLASS"))
    store.commit()

    results = finder.find_siblings("add a new API endpoint")
    names = [n.name for n in results]
    assert "list_events" in names or "create_event" in names
    # The model should NOT be in results for endpoint query
    assert "Event" not in names


def test_find_by_name_pattern(setup):
    store, finder = setup
    store.upsert_node(_make_node("s.py::AuthService", "AuthService", "s.py",
                                 kind="CLASS", is_exported=True))
    store.upsert_node(_make_node("s.py::PaymentService", "PaymentService", "s.py",
                                 kind="CLASS", is_exported=True))
    store.upsert_node(_make_node("m.py::User", "User", "m.py", kind="CLASS"))
    store.commit()

    results = finder.find_siblings("create a notification service")
    names = [n.name for n in results]
    assert "AuthService" in names or "PaymentService" in names


def test_find_by_extends(setup):
    store, finder = setup
    store.upsert_node(_make_node("m.py::Event", "Event", "m.py",
                                 kind="CLASS", is_exported=True))
    store.upsert_node(_make_node("m.py::Model", "Model", "m.py", kind="CLASS"))
    store.upsert_edge(Edge(source_id="m.py::Event", target_id="m.py::Model",
                           kind="EXTENDS"))
    store.commit()

    results = finder.find_siblings("create a Payment model")
    names = [n.name for n in results]
    assert "Event" in names


def test_find_by_file_pattern(setup):
    store, finder = setup
    store.upsert_node(_make_node("src/hooks/useAuth.tsx::useAuth", "useAuth",
                                 "src/hooks/useAuth.tsx", language="typescript"))
    store.upsert_node(_make_node("src/views/Login.tsx::Login", "Login",
                                 "src/views/Login.tsx", language="typescript"))
    store.commit()

    results = finder.find_siblings("add a new hook")
    names = [n.name for n in results]
    assert "useAuth" in names


# --- Finding by seed similarity ---

def test_find_by_seed_similarity(setup):
    store, finder = setup
    store.upsert_node(_make_node("src/api/events.py::list_events", "list_events",
                                 "src/api/events.py", decorators=["@api_view"]))
    store.upsert_node(_make_node("src/api/users.py::list_users", "list_users",
                                 "src/api/users.py", decorators=["@api_view"]))
    store.upsert_node(_make_node("src/models/event.py::Event", "Event",
                                 "src/models/event.py", kind="CLASS"))
    store.commit()

    seeds = [SeedCandidate(node_id="src/api/events.py::list_events",
                           score=10, match_reason="fts5")]
    results = finder.find_siblings("add payments", seed_candidates=seeds)
    names = [n.name for n in results]
    # list_users is in same directory + same kind as seed
    assert "list_users" in names


# --- Ranking ---

def test_rank_prefers_medium_size(setup):
    store, finder = setup
    small = _make_node("a.py::tiny", "tiny", "a.py", is_exported=True,
                       line_start=1, line_end=5, docstring="tiny func",
                       full_source="def tiny(): pass")
    medium = _make_node("b.py::medium", "medium", "b.py", is_exported=True,
                        line_start=1, line_end=50, docstring="medium func",
                        full_source="def medium():\n" + "    x = 1\n" * 48)
    huge = _make_node("c.py::huge", "huge", "c.py", is_exported=True,
                      line_start=1, line_end=300, docstring="huge func",
                      full_source="def huge():\n" + "    x = 1\n" * 298)
    store.upsert_node(small)
    store.upsert_node(medium)
    store.upsert_node(huge)
    store.commit()

    results = finder._rank_conventions([small, medium, huge])
    # Medium should be first
    assert results[0].name == "medium"


def test_rank_prefers_documented(setup):
    store, finder = setup
    documented = _make_node("a.py::doc", "doc", "a.py", is_exported=True,
                            line_start=1, line_end=30, docstring="This does X",
                            full_source="pass")
    undocumented = _make_node("b.py::undoc", "undoc", "b.py", is_exported=True,
                             line_start=1, line_end=30, full_source="pass")
    store.upsert_node(documented)
    store.upsert_node(undocumented)
    store.commit()

    results = finder._rank_conventions([undocumented, documented])
    assert results[0].name == "doc"


def test_dedup_by_file(setup):
    store, finder = setup
    store.upsert_node(_make_node("v.py::view1", "view1", "v.py",
                                 decorators=["@api_view"], is_exported=True,
                                 line_start=1, line_end=30))
    store.upsert_node(_make_node("v.py::view2", "view2", "v.py",
                                 decorators=["@api_view"], is_exported=True,
                                 line_start=31, line_end=60))
    store.commit()

    results = finder.find_siblings("add endpoint")
    # Both are in v.py — should only get one
    files = [n.file_path for n in results]
    assert files.count("v.py") == 1


# --- Fallback ---

def test_find_representative_fallback(setup):
    store, finder = setup
    store.upsert_node(_make_node("a.py::exported", "exported", "a.py",
                                 is_exported=True, full_source="def exported(): pass"))
    store.upsert_node(_make_node("b.py::internal", "internal", "b.py",
                                 is_exported=False))
    store.commit()

    results = finder.find_siblings("do something completely unrelated xyz123")
    # Should still return something — the exported node
    assert len(results) >= 1
    assert results[0].name == "exported"


def test_empty_project_returns_empty(setup):
    store, finder = setup
    results = finder.find_siblings("add endpoint")
    assert results == []
