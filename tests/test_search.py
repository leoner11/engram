"""Tests for FTS5 memory search."""

import pytest
import shutil
from pathlib import Path

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.memory.sessions import SessionManager
from engram.memory.observations import ObservationManager
from engram.memory.search import MemorySearch
from engram.cli import build_index


@pytest.fixture
def search_setup(tmp_path, simple_project_path):
    """Set up project with index, some observations, and search."""
    project = tmp_path / "test_project"
    shutil.copytree(simple_project_path, project)

    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    mem_search = MemorySearch(store)

    # Seed some observations
    obs_mgr.save(
        title="Stripe webhook signatures differ in test mode",
        content="Stripe sends different webhook signatures in test mode vs production. "
                "Must use the test signing secret locally.",
        type="bugfix",
        project=project.name,
        node_ids=["repository.py::save_order"],
    )
    obs_mgr.save(
        title="JWT refresh tokens use 24h lifetime",
        content="Changed from 7 days to 24 hours after security review. "
                "Refresh rotation enabled.",
        type="decision",
        project=project.name,
    )
    obs_mgr.save(
        title="Order validation needs rework",
        content="The validate_user_id function is too strict. "
                "Should accept email-based IDs too.",
        type="issue",
        project=project.name,
    )

    return mem_search, obs_mgr, store, project


def test_search_basic(search_setup):
    mem_search, _, _, _ = search_setup
    results = mem_search.search("webhook")
    assert len(results) >= 1
    assert "webhook" in results[0]["title"].lower()


def test_search_type_filter(search_setup):
    mem_search, _, _, _ = search_setup
    results = mem_search.search("webhook OR JWT OR order", type="bugfix")
    for r in results:
        assert r["type"] == "bugfix"


def test_search_progressive(search_setup):
    mem_search, _, _, _ = search_setup
    results = mem_search.search_progressive("webhook")
    assert len(results) >= 1
    assert "snippet" in results[0]
    assert len(results[0]["snippet"]) <= 103  # 100 + "..."


def test_search_node_boost(search_setup):
    mem_search, _, _, _ = search_setup
    # Search with affected nodes that overlap with webhook observation's linked nodes
    boosted = mem_search.search(
        "webhook OR JWT",
        affected_node_ids={"repository.py::save_order"},
    )
    # The webhook observation is linked to save_order, so it should rank higher
    assert len(boosted) >= 1
    # The boosted result should have node_boost > 0
    webhook_result = [r for r in boosted if "webhook" in r.get("title", "").lower()]
    if webhook_result:
        assert webhook_result[0]["node_boost"] > 0


def test_search_no_results(search_setup):
    mem_search, _, _, _ = search_setup
    results = mem_search.search("xyznonexistent")
    assert len(results) == 0


def test_search_limit(search_setup):
    mem_search, _, _, _ = search_setup
    results = mem_search.search("webhook OR JWT OR order", limit=1)
    assert len(results) <= 1
