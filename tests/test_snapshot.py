"""Tests for project snapshot generation and caching."""

import pytest

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.indexer.extractor import NodeRecord
from engram.memory.sessions import SessionManager
from engram.memory.observations import ObservationManager
from engram.snapshot.generator import SnapshotGenerator, SNAPSHOT_TOPIC_KEY


@pytest.fixture
def setup(tmp_path):
    db = EngramDB(tmp_path)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    project = tmp_path.name
    gen = SnapshotGenerator(store, obs_mgr, project)
    return store, obs_mgr, gen, project


def _make_node(node_id, name, file_path, kind="FUNCTION", language="python",
               is_exported=False, decorators=None, signature=None,
               docstring=None, full_source="", line_start=1, line_end=10):
    return NodeRecord(
        id=node_id, kind=kind, name=name, file_path=file_path,
        line_start=line_start, line_end=line_end, language=language,
        signature=signature, docstring=docstring, source_hash="abc",
        is_exported=is_exported, decorators=decorators or [],
        full_source=full_source,
    )


def test_generate_snapshot_empty_project(setup):
    """Snapshot produces valid output even with no nodes."""
    store, obs_mgr, gen, project = setup
    content = gen.get_or_generate()
    assert "STACK:" in content
    assert "unknown framework" in content


def test_generate_snapshot_with_nodes(setup):
    """Snapshot includes node count and structure."""
    store, obs_mgr, gen, project = setup
    store.upsert_node(_make_node("src/api/views.py::list_events", "list_events",
                                 "src/api/views.py", is_exported=True))
    store.upsert_node(_make_node("src/models/event.py::Event", "Event",
                                 "src/models/event.py", kind="CLASS", is_exported=True))
    store.commit()

    content = gen.get_or_generate()
    assert "2 nodes" in content
    assert "src/api" in content or "src/models" in content


def test_snapshot_detects_django(setup):
    """Snapshot detects Django framework from imports."""
    store, obs_mgr, gen, project = setup
    store.upsert_node(_make_node("src/views.py", "views.py", "src/views.py",
                                 kind="FILE", full_source="from django.http import HttpResponse"))
    store.commit()

    content = gen.get_or_generate()
    assert "django" in content.lower()


def test_snapshot_caching(setup):
    """Second call returns cached snapshot, not regenerated."""
    store, obs_mgr, gen, project = setup
    content1 = gen.get_or_generate()
    # Add a node — should NOT appear in cached version
    store.upsert_node(_make_node("new.py::new_func", "new_func", "new.py"))
    store.commit()
    content2 = gen.get_or_generate()
    assert content1 == content2


def test_snapshot_refresh(setup):
    """force_refresh=True regenerates snapshot."""
    store, obs_mgr, gen, project = setup
    content1 = gen.get_or_generate()
    store.upsert_node(_make_node("new.py::new_func", "new_func", "new.py", is_exported=True))
    store.commit()
    content2 = gen.get_or_generate(force_refresh=True)
    assert content1 != content2
    assert "1 nodes" in content2


def test_snapshot_token_budget(setup):
    """Content stays under MAX_SNAPSHOT_CHARS."""
    store, obs_mgr, gen, project = setup
    # Add many nodes to generate a long snapshot
    for i in range(100):
        store.upsert_node(_make_node(
            f"src/mod{i}/file{i}.py::func_{i}", f"func_{i}",
            f"src/mod{i}/file{i}.py", is_exported=True,
        ))
    store.commit()

    content = gen.get_or_generate()
    assert len(content) <= 3200


def test_snapshot_includes_entry_points(setup):
    """Snapshot shows most-connected exported nodes."""
    store, obs_mgr, gen, project = setup
    from engram.indexer.resolver import Edge

    store.upsert_node(_make_node("a.py::core_func", "core_func", "a.py",
                                 is_exported=True, signature="def core_func(x, y)"))
    store.upsert_node(_make_node("b.py::caller1", "caller1", "b.py"))
    store.upsert_node(_make_node("c.py::caller2", "caller2", "c.py"))
    store.upsert_edge(Edge(source_id="b.py::caller1", target_id="a.py::core_func",
                           kind="CALLS"))
    store.upsert_edge(Edge(source_id="c.py::caller2", target_id="a.py::core_func",
                           kind="CALLS"))
    store.commit()

    content = gen.get_or_generate()
    assert "ENTRY POINTS:" in content
    assert "core_func" in content


def test_snapshot_includes_architecture_observations(setup):
    """Past architecture decisions appear in snapshot."""
    store, obs_mgr, gen, project = setup
    obs_mgr.save(
        title="Switched to Meta Cloud API",
        content="Migrated WhatsApp from Baileys to official API",
        type="architecture",
        project=project,
    )
    content = gen.get_or_generate()
    assert "PAST DECISIONS:" in content
    assert "Switched to Meta Cloud API" in content


def test_snapshot_excludes_self_from_observations(setup):
    """Snapshot doesn't list itself in PAST DECISIONS."""
    store, obs_mgr, gen, project = setup
    # Generate snapshot first (creates the _project_snapshot observation)
    gen.get_or_generate()
    # Now regenerate
    content = gen.get_or_generate(force_refresh=True)
    # Should NOT show "Project Snapshot:" in PAST DECISIONS
    if "PAST DECISIONS:" in content:
        assert "Project Snapshot:" not in content.split("PAST DECISIONS:")[1]


def test_snapshot_upsert(setup):
    """Regeneration overwrites old snapshot via topic_key upsert."""
    store, obs_mgr, gen, project = setup
    gen.get_or_generate()
    gen.get_or_generate(force_refresh=True)

    # Should only be one snapshot observation
    rows = store.conn.execute(
        "SELECT COUNT(*) as c FROM observations WHERE topic_key = ?",
        (SNAPSHOT_TOPIC_KEY,),
    ).fetchone()
    assert rows["c"] == 1


def test_snapshot_directory_summary(setup):
    """Snapshot groups nodes by directory."""
    store, obs_mgr, gen, project = setup
    store.upsert_node(_make_node("src/api/v.py::view1", "view1", "src/api/v.py"))
    store.upsert_node(_make_node("src/api/v.py::view2", "view2", "src/api/v.py"))
    store.upsert_node(_make_node("src/models/m.py::Model1", "Model1", "src/models/m.py"))
    store.commit()

    content = gen.get_or_generate()
    assert "STRUCTURE:" in content
    assert "src/api" in content
