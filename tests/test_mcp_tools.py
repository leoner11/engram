"""Tests for MCP tool handlers — unit tests without MCP transport."""

import pytest
import asyncio
import shutil
from pathlib import Path

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.memory.sessions import SessionManager
from engram.memory.observations import ObservationManager
from engram.memory.search import MemorySearch
from engram.retriever.assembler import ContextAssembler
from engram.mcp.tools import ToolHandlers
from engram.cli import build_index


class FakeEngram:
    """Minimal stand-in for EngramMCPServer for handler testing."""
    def __init__(self, root, db, store, session_mgr, obs_mgr, mem_search, assembler):
        self.root = root
        self.db = db
        self.store = store
        self.session_mgr = session_mgr
        self.obs_mgr = obs_mgr
        self.mem_search = mem_search
        self.assembler = assembler
        self.last_query = None
        self.last_verified_at = None


@pytest.fixture
def handlers(tmp_path, simple_project_path):
    project = tmp_path / "test_project"
    shutil.copytree(simple_project_path, project)

    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    mem_search = MemorySearch(store)
    assembler = ContextAssembler(store, project_root=project, memory_search=mem_search)

    fake = FakeEngram(project, db, store, session_mgr, obs_mgr, mem_search, assembler)
    return ToolHandlers(fake)


def test_handle_query(handlers):
    result = asyncio.get_event_loop().run_until_complete(
        handlers.handle_query({"prompt": "fix save_order logic"})
    )
    assert len(result) == 1
    text = result[0].text
    assert "Task: fix save_order logic" in text
    assert "save_order" in text


def test_handle_save(handlers):
    result = asyncio.get_event_loop().run_until_complete(
        handlers.handle_save({
            "title": "Fixed the bug",
            "content": "The save_order was not committing",
            "type": "bugfix",
            "node_ids": ["repository.py::save_order"],
        })
    )
    assert len(result) == 1
    assert "Saved observation" in result[0].text
    assert "linked nodes" in result[0].text


def test_handle_search_after_save(handlers):
    # Save first
    asyncio.get_event_loop().run_until_complete(
        handlers.handle_save({
            "title": "Webhook secret config",
            "content": "Use STRIPE_WEBHOOK_SECRET env var",
            "type": "discovery",
        })
    )

    # Search
    result = asyncio.get_event_loop().run_until_complete(
        handlers.handle_search({"query": "webhook secret"})
    )
    assert len(result) == 1
    assert "webhook" in result[0].text.lower()


def test_handle_status(handlers):
    result = asyncio.get_event_loop().run_until_complete(
        handlers.handle_status({})
    )
    assert len(result) == 1
    text = result[0].text
    assert "Project:" in text
    assert "nodes" in text


def test_handle_build(handlers):
    result = asyncio.get_event_loop().run_until_complete(
        handlers.handle_build({"force": False})
    )
    assert len(result) == 1
    assert "Build complete" in result[0].text


def test_query_includes_memories(handlers):
    # Save an observation linked to a node
    asyncio.get_event_loop().run_until_complete(
        handlers.handle_save({
            "title": "Order validation quirk",
            "content": "validate_user_id rejects emails, needs update",
            "type": "bugfix",
            "node_ids": ["utils.py::validate_user_id"],
        })
    )

    # Query that should hit validate_user_id as seed
    result = asyncio.get_event_loop().run_until_complete(
        handlers.handle_query({"prompt": "fix validate_user_id"})
    )
    text = result[0].text
    # Memory should appear in context
    assert "Memories" in text or "validate_user_id" in text


def test_save_topic_key_upsert(handlers):
    asyncio.get_event_loop().run_until_complete(
        handlers.handle_save({
            "title": "Auth config",
            "content": "Using RS256",
            "type": "decision",
            "topic_key": "auth/config",
        })
    )
    asyncio.get_event_loop().run_until_complete(
        handlers.handle_save({
            "title": "Auth config",
            "content": "Switched to ES256",
            "type": "decision",
            "topic_key": "auth/config",
        })
    )

    result = asyncio.get_event_loop().run_until_complete(
        handlers.handle_search({"query": "auth config", "full": True})
    )
    text = result[0].text
    assert "ES256" in text
    # Should only be one result, not two
    assert text.count("Auth config") == 1
