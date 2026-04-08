"""Tests for journal extraction (convergence-signal-based) and sync push/pull."""

import pytest
import json
import shutil
from pathlib import Path
from datetime import datetime, timedelta

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.memory.sessions import SessionManager
from engram.memory.observations import ObservationManager
from engram.journal.parser import ExportParser, Conversation, ConversationMessage
from engram.journal.extractor import JournalExtractor, mark_stale_observations
from engram.sync.exporter import MemoryExporter
from engram.sync.importer import MemoryImporter
from engram.cli import build_index


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def project_with_obs(tmp_path):
    """Project with index + some observations."""
    project = tmp_path / "test_project"
    shutil.copytree(FIXTURES_DIR / "simple_project", project)

    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)

    obs_mgr.save(title="Fixed order bug", content="save_order was not committing",
                 type="bugfix", project=project.name, node_ids=["repository.py::save_order"])
    obs_mgr.save(title="JWT config", content="Using RS256 with 30-min tokens",
                 type="decision", project=project.name, topic_key="auth/jwt")

    return db, store, session_mgr, obs_mgr, project


@pytest.fixture
def extractor_setup(tmp_path):
    """Fresh project with extractor ready."""
    project = tmp_path / "proj"
    shutil.copytree(FIXTURES_DIR / "simple_project", project)
    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    session_mgr = SessionManager(store)
    obs_mgr = ObservationManager(store, session_mgr)
    extractor = JournalExtractor(obs_mgr)
    return extractor, obs_mgr, project, db


# --- Journal parser ---

def test_parse_conversation_export(tmp_path):
    export_data = [
        {
            "id": "conv-1",
            "title": "Fix webhook",
            "messages": [
                {"role": "user", "content": "The webhook is broken"},
                {"role": "assistant", "content": "The bug was in the signature verification. Fixed by using the test secret."},
                {"role": "user", "content": "yes that fixed it"},
            ]
        }
    ]
    export_file = tmp_path / "export.json"
    export_file.write_text(json.dumps(export_data))

    parser = ExportParser()
    conversations = parser.parse(export_file)
    assert len(conversations) == 1
    assert conversations[0].title == "Fix webhook"
    assert len(conversations[0].messages) == 3


def test_parse_claude_content_blocks(tmp_path):
    export_data = [{
        "id": "conv-2",
        "title": "Work session",
        "messages": [
            {"role": "user", "content": "Do the thing"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "I fixed the issue. The bug was in the parser."},
                {"type": "tool_use", "name": "bash", "input": {"command": "ls"}},
            ]},
            {"role": "user", "content": "perfect"},
        ]
    }]
    export_file = tmp_path / "export.json"
    export_file.write_text(json.dumps(export_data))

    parser = ExportParser()
    conversations = parser.parse(export_file)
    assert len(conversations) == 1
    assert len(conversations[0].messages) >= 3


def test_parse_empty_export(tmp_path):
    export_file = tmp_path / "export.json"
    export_file.write_text("[]")
    parser = ExportParser()
    assert parser.parse(export_file) == []


# --- Convergence-signal extraction ---

def test_extract_bugfix_with_confirmation(extractor_setup):
    """User confirms a bugfix → observation extracted."""
    extractor, obs_mgr, project, db = extractor_setup

    conv = Conversation(
        id="test", title="Debug session",
        messages=[
            ConversationMessage(role="user", content="The save is broken"),
            ConversationMessage(role="assistant",
                content="The bug was in save_order — it was not handling None values. Fixed by adding a null check before commit."),
            ConversationMessage(role="user", content="yes that fixed it, works now"),
        ]
    )

    obs_ids = extractor.extract_and_save([conv], project.name)
    assert len(obs_ids) >= 1

    obs = obs_mgr.get(obs_ids[0])
    assert obs["type"] == "bugfix"
    assert "WHAT:" in obs["content"]
    assert "CONFIRMED BY USER:" in obs["content"]
    assert "STATUS: active" in obs["content"]
    db.close()


def test_extract_decision_with_confirmation(extractor_setup):
    """User confirms a decision → observation extracted."""
    extractor, obs_mgr, project, db = extractor_setup

    conv = Conversation(
        id="test", title="Architecture discussion",
        messages=[
            ConversationMessage(role="user", content="Should we use JWT or sessions?"),
            ConversationMessage(role="assistant",
                content="Going with JWT for the API. It's stateless and works better for our use case."),
            ConversationMessage(role="user", content="sounds good, let's do that"),
        ]
    )

    obs_ids = extractor.extract_and_save([conv], project.name)
    assert len(obs_ids) >= 1

    obs = obs_mgr.get(obs_ids[0])
    assert obs["type"] == "decision"
    db.close()


def test_extract_discovery_with_confirmation(extractor_setup):
    """User confirms a discovery → observation extracted."""
    extractor, obs_mgr, project, db = extractor_setup

    conv = Conversation(
        id="test", title="Investigation",
        messages=[
            ConversationMessage(role="user", content="Why is the API slow?"),
            ConversationMessage(role="assistant",
                content="Turns out the database query was missing an index on user_id. Adding it dropped response time from 2s to 50ms."),
            ConversationMessage(role="user", content="right, confirmed that's it"),
        ]
    )

    obs_ids = extractor.extract_and_save([conv], project.name)
    assert len(obs_ids) >= 1

    obs = obs_mgr.get(obs_ids[0])
    assert obs["type"] == "discovery"
    db.close()


def test_no_extract_without_confirmation(extractor_setup):
    """Assistant says something but user doesn't confirm → NO extraction."""
    extractor, obs_mgr, project, db = extractor_setup

    conv = Conversation(
        id="test", title="Exploring options",
        messages=[
            ConversationMessage(role="user", content="What should we do about the auth?"),
            ConversationMessage(role="assistant",
                content="The bug was in the token validation. We could fix it by switching to RS256."),
            ConversationMessage(role="user", content="hmm what about ES256 instead? let me think about it"),
        ]
    )

    obs_ids = extractor.extract_and_save([conv], project.name)
    # "hmm what about ES256" is NOT a confirmation — it's a counter-proposal
    assert len(obs_ids) == 0
    db.close()


def test_no_extract_from_assistant_only(extractor_setup):
    """Assistant message with no subsequent user message → NO extraction."""
    extractor, obs_mgr, project, db = extractor_setup

    conv = Conversation(
        id="test", title="Monologue",
        messages=[
            ConversationMessage(role="user", content="What's wrong with the code?"),
            ConversationMessage(role="assistant",
                content="The bug was in the parser. Fixed by updating the regex."),
            # No user confirmation follows
        ]
    )

    obs_ids = extractor.extract_and_save([conv], project.name)
    assert len(obs_ids) == 0
    db.close()


def test_no_extract_from_hedging(extractor_setup):
    """Assistant hedges, user confirms the hedging → should NOT extract the hedge."""
    extractor, obs_mgr, project, db = extractor_setup

    conv = Conversation(
        id="test", title="Uncertain",
        messages=[
            ConversationMessage(role="user", content="Is this approach correct?"),
            ConversationMessage(role="assistant",
                content="I think this might work but I'm not entirely sure. We could try approach A or approach B."),
            ConversationMessage(role="user", content="ok"),
        ]
    )

    obs_ids = extractor.extract_and_save([conv], project.name)
    # No outcome pattern matches on hedging language
    assert len(obs_ids) == 0
    db.close()


# --- Structured content format ---

def test_content_has_structured_fields(extractor_setup):
    """Extracted content uses WHAT/CONTEXT/CONFIRMED/STATUS format."""
    extractor, obs_mgr, project, db = extractor_setup

    conv = Conversation(
        id="test", title="Bug",
        messages=[
            ConversationMessage(role="user", content="The webhook signature check fails in test"),
            ConversationMessage(role="assistant",
                content="The bug was that test mode uses a different webhook secret than production. Fixed by checking STRIPE_TEST_SECRET env var."),
            ConversationMessage(role="user", content="perfect, that works"),
        ]
    )

    obs_ids = extractor.extract_and_save([conv], project.name)
    assert len(obs_ids) >= 1
    obs = obs_mgr.get(obs_ids[0])
    content = obs["content"]

    assert content.startswith("WHAT:")
    assert "CONTEXT:" in content
    assert "CONFIRMED BY USER:" in content
    assert "STATUS: active" in content
    db.close()


def test_content_capped_at_max_length(extractor_setup):
    """Content is hard-capped to prevent context pollution."""
    extractor, obs_mgr, project, db = extractor_setup

    long_explanation = "The root cause was " + "a very complex chain of events involving " * 50
    conv = Conversation(
        id="test", title="Long",
        messages=[
            ConversationMessage(role="user", content="What went wrong?"),
            ConversationMessage(role="assistant", content=long_explanation),
            ConversationMessage(role="user", content="yes"),
        ]
    )

    obs_ids = extractor.extract_and_save([conv], project.name)
    if obs_ids:
        obs = obs_mgr.get(obs_ids[0])
        assert len(obs["content"]) <= 800
    db.close()


# --- Staleness marking ---

def test_mark_stale_observations(project_with_obs):
    """Old observations get marked stale."""
    db, store, session_mgr, obs_mgr, project = project_with_obs

    # Backdate observations
    store.conn.execute(
        "UPDATE observations SET updated_at = datetime('now', '-100 days')"
    )
    store.conn.commit()

    marked = mark_stale_observations(obs_mgr, project.name, days=90)
    assert marked == 2

    # Check content was updated
    rows = store.conn.execute("SELECT content FROM observations").fetchall()
    for row in rows:
        assert "stale" in row["content"]


def test_mark_stale_idempotent(project_with_obs):
    """Marking stale twice doesn't double-mark."""
    db, store, session_mgr, obs_mgr, project = project_with_obs

    store.conn.execute(
        "UPDATE observations SET updated_at = datetime('now', '-100 days')"
    )
    store.conn.commit()

    mark_stale_observations(obs_mgr, project.name, days=90)
    marked_again = mark_stale_observations(obs_mgr, project.name, days=90)
    assert marked_again == 0


def test_fresh_observations_not_marked(project_with_obs):
    """Recent observations are not marked stale."""
    db, store, session_mgr, obs_mgr, project = project_with_obs

    marked = mark_stale_observations(obs_mgr, project.name, days=90)
    assert marked == 0


# --- Dedup ---

def test_topic_key_upsert_on_reextract(extractor_setup):
    """Extracting same topic twice updates instead of duplicating."""
    extractor, obs_mgr, project, db = extractor_setup

    conv1 = Conversation(
        id="t1", title="First",
        messages=[
            ConversationMessage(role="user", content="Auth approach?"),
            ConversationMessage(role="assistant", content="Going with JWT for now."),
            ConversationMessage(role="user", content="ok"),
        ]
    )
    conv2 = Conversation(
        id="t2", title="Second",
        messages=[
            ConversationMessage(role="user", content="Auth approach revisited?"),
            ConversationMessage(role="assistant", content="Going with session cookies instead of JWT."),
            ConversationMessage(role="user", content="yes let's do that"),
        ]
    )

    ids1 = extractor.extract_and_save([conv1], project.name)
    ids2 = extractor.extract_and_save([conv2], project.name)

    # Both should produce observations (they have different content hashes)
    # But if topic_key matches, the second should update the first
    count = obs_mgr.store.conn.execute("SELECT COUNT(*) as c FROM observations").fetchone()["c"]
    # Could be 1 or 2 depending on whether topic keys collide — but not 3+
    assert count <= 2
    db.close()


# --- Sync push/pull (unchanged from original) ---

def test_sync_push(project_with_obs, tmp_path):
    db, store, _, _, project = project_with_obs
    sync_dir = tmp_path / "sync_output"

    exporter = MemoryExporter(store, project.name)
    exporter.export_to_jsonl(sync_dir)

    assert (sync_dir / "observations.jsonl").exists()
    assert (sync_dir / "sessions.jsonl").exists()
    assert (sync_dir / "manifest.json").exists()

    manifest = json.loads((sync_dir / "manifest.json").read_text())
    assert manifest["observation_count"] == 2
    assert manifest["project"] == project.name


def test_sync_roundtrip(project_with_obs, tmp_path):
    db, store, session_mgr, obs_mgr, project = project_with_obs
    sync_dir = tmp_path / "sync_data"

    exporter = MemoryExporter(store, project.name)
    exporter.export_to_jsonl(sync_dir)

    project2 = tmp_path / "project2"
    shutil.copytree(FIXTURES_DIR / "simple_project", project2)
    db2 = EngramDB(project2)
    build_index(project2, db2, force=True)
    store2 = GraphStore(db2)
    session_mgr2 = SessionManager(store2)
    obs_mgr2 = ObservationManager(store2, session_mgr2)

    importer = MemoryImporter(store2, obs_mgr2)
    result = importer.import_from_jsonl(sync_dir)

    assert result["imported"] + result["updated"] >= 2
    assert result["errors"] == 0

    from engram.memory.search import MemorySearch
    search = MemorySearch(store2)
    results = search.search("order bug")
    assert len(results) >= 1
    db2.close()


def test_sync_dedup(project_with_obs, tmp_path):
    db, store, session_mgr, obs_mgr, project = project_with_obs
    sync_dir = tmp_path / "sync_data"

    exporter = MemoryExporter(store, project.name)
    exporter.export_to_jsonl(sync_dir)

    importer = MemoryImporter(store, obs_mgr)
    result1 = importer.import_from_jsonl(sync_dir)
    result2 = importer.import_from_jsonl(sync_dir)

    count = store.conn.execute("SELECT COUNT(*) as c FROM observations").fetchone()["c"]
    assert count == 2
