"""Tests for observation CRUD, topic-key upsert, dedup, and node linking."""

import pytest
from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.memory.sessions import SessionManager
from engram.memory.observations import ObservationManager
from engram.cli import build_index
from pathlib import Path
import shutil


@pytest.fixture
def obs_setup(tmp_path, simple_project_path):
    """Set up a project with index + observation manager."""
    project = tmp_path / "test_project"
    shutil.copytree(simple_project_path, project)

    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    return obs_mgr, store, session_mgr, project


def test_save_basic(obs_setup):
    obs_mgr, store, _, project = obs_setup
    obs_id = obs_mgr.save(
        title="Fixed the order bug",
        content="The save_order function was not committing properly.",
        type="bugfix",
        project=project.name,
    )
    assert obs_id > 0

    obs = obs_mgr.get(obs_id)
    assert obs is not None
    assert obs["title"] == "Fixed the order bug"
    assert obs["type"] == "bugfix"


def test_save_auto_creates_session(obs_setup):
    obs_mgr, _, session_mgr, project = obs_setup
    assert session_mgr.active_session is None

    obs_mgr.save(
        title="Test",
        content="Content",
        type="discovery",
        project=project.name,
    )

    assert session_mgr.active_session is not None


def test_topic_key_upsert(obs_setup):
    obs_mgr, store, _, project = obs_setup
    name = project.name

    id1 = obs_mgr.save(
        title="JWT config",
        content="Using RS256 with 15-min tokens",
        type="decision",
        project=name,
        topic_key="auth/jwt-config",
    )

    id2 = obs_mgr.save(
        title="JWT config",
        content="Switched to 30-min tokens with refresh rotation",
        type="decision",
        project=name,
        topic_key="auth/jwt-config",
    )

    # Should update, not duplicate
    assert id1 == id2

    obs = obs_mgr.get(id1)
    assert "30-min" in obs["content"]


def test_dedup_same_content(obs_setup):
    obs_mgr, _, _, project = obs_setup
    name = project.name

    id1 = obs_mgr.save(title="Same", content="Same content", type="bugfix", project=name)
    id2 = obs_mgr.save(title="Same", content="Same content", type="bugfix", project=name)

    assert id1 == id2  # Dedup by source_hash


def test_node_linking_agent(obs_setup):
    obs_mgr, store, _, project = obs_setup
    obs_id = obs_mgr.save(
        title="Fixed save_order",
        content="The function was broken",
        type="bugfix",
        project=project.name,
        node_ids=["repository.py::save_order"],
    )

    obs = obs_mgr.get(obs_id)
    linked = obs["linked_nodes"]
    agent_links = [l for l in linked if l["source"] == "agent"]
    assert len(agent_links) >= 1
    assert agent_links[0]["node_id"] == "repository.py::save_order"


def test_node_linking_auto(obs_setup):
    obs_mgr, store, _, project = obs_setup
    obs_id = obs_mgr.save(
        title="Issue with save_order",
        content="The save_order function in repository needs fixing",
        type="bugfix",
        project=project.name,
    )

    obs = obs_mgr.get(obs_id)
    linked_ids = {l["node_id"] for l in obs["linked_nodes"]}
    # Should auto-detect save_order from the text
    assert "repository.py::save_order" in linked_ids


def test_get_by_node(obs_setup):
    obs_mgr, _, _, project = obs_setup
    obs_mgr.save(
        title="About save_order",
        content="Details",
        type="bugfix",
        project=project.name,
        node_ids=["repository.py::save_order"],
    )

    results = obs_mgr.get_by_node("repository.py::save_order")
    assert len(results) >= 1
    assert results[0]["title"] == "About save_order"


def test_get_by_session(obs_setup):
    obs_mgr, _, session_mgr, project = obs_setup
    sid = session_mgr.start(project.name)

    obs_mgr.save(title="First", content="A", type="bugfix", project=project.name)
    obs_mgr.save(title="Second", content="B", type="decision", project=project.name)

    results = obs_mgr.get_by_session(sid)
    assert len(results) == 2
    assert results[0]["title"] == "First"


def test_delete(obs_setup):
    obs_mgr, _, _, project = obs_setup
    obs_id = obs_mgr.save(
        title="To delete",
        content="Temporary",
        type="discovery",
        project=project.name,
    )
    obs_mgr.delete(obs_id)
    assert obs_mgr.get(obs_id) is None


def test_invalid_type(obs_setup):
    obs_mgr, _, _, project = obs_setup
    with pytest.raises(ValueError, match="Invalid type"):
        obs_mgr.save(
            title="Bad",
            content="Content",
            type="invalid_type",
            project=project.name,
        )
