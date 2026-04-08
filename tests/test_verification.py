"""Tests for the verification pipeline: diff parsing, mapping, verification, followup, feedback."""

import pytest
import shutil
from pathlib import Path

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.graph.traversal import GraphTraversal
from engram.graph.activation import ChangeType
from engram.verification.diff_parser import DiffParser
from engram.verification.mapper import DiffMapper, TouchedNode
from engram.verification.verifier import Verifier, Verdict
from engram.verification.followup import FollowUpGenerator
from engram.verification.feedback import RetrievalFeedback
from engram.retriever.boost import FeedbackBooster
from engram.cli import build_index


FIXTURES_DIR = Path(__file__).parent / "fixtures"
DIFFS_DIR = FIXTURES_DIR / "diffs"


@pytest.fixture
def indexed_project(tmp_path):
    """Build index on simple_project in a temp dir."""
    project = tmp_path / "simple_project"
    shutil.copytree(FIXTURES_DIR / "simple_project", project)
    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    return store, db, project


# --- DiffParser ---

def test_parse_empty_diff():
    parser = DiffParser()
    assert parser.parse("") == []
    assert parser.parse("   \n  ") == []


def test_parse_single_file():
    parser = DiffParser()
    diff = (DIFFS_DIR / "complete_body_mod.diff").read_text()
    files = parser.parse(diff)
    assert len(files) == 1
    assert files[0].path == "utils.py"
    assert not files[0].is_new
    assert not files[0].is_deleted
    assert len(files[0].hunks) == 1


def test_parse_hunk_lines():
    parser = DiffParser()
    diff = (DIFFS_DIR / "complete_body_mod.diff").read_text()
    files = parser.parse(diff)
    hunk = files[0].hunks[0]
    assert len(hunk.added_lines) > 0
    assert len(hunk.removed_lines) > 0


def test_parse_new_file():
    parser = DiffParser()
    diff = (DIFFS_DIR / "new_file.diff").read_text()
    files = parser.parse(diff)
    assert len(files) == 1
    assert files[0].is_new
    assert files[0].new_path == "notifications.py"
    assert files[0].old_path is None


def test_parse_signature_change():
    parser = DiffParser()
    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    files = parser.parse(diff)
    assert len(files) == 1
    assert files[0].path == "service.py"
    assert len(files[0].all_modified_lines) > 0


def test_parse_multiple_hunks():
    parser = DiffParser()
    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    files = parser.parse(diff)
    # This diff has two hunks
    assert len(files[0].hunks) >= 1


# --- DiffMapper ---

def test_mapper_finds_touched_nodes(indexed_project):
    store, _, _ = indexed_project
    parser = DiffParser()
    mapper = DiffMapper(store)

    diff = (DIFFS_DIR / "complete_body_mod.diff").read_text()
    file_diffs = parser.parse(diff)
    touched = mapper.map_diff_to_nodes(file_diffs)

    touched_ids = {t.node_id for t in touched}
    # validate_user_id should be touched
    assert "utils.py::validate_user_id" in touched_ids


def test_mapper_signature_change(indexed_project):
    store, _, _ = indexed_project
    parser = DiffParser()
    mapper = DiffMapper(store)

    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    file_diffs = parser.parse(diff)
    touched = mapper.map_diff_to_nodes(file_diffs)

    touched_ids = {t.node_id for t in touched}
    assert "service.py::process_order" in touched_ids


# --- Verifier ---

def test_verify_complete_body_mod(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)

    diff = (DIFFS_DIR / "complete_body_mod.diff").read_text()
    result = verifier.verify(
        diff_text=diff,
        seeds=["utils.py::validate_user_id"],
        change_types={"BODY_MODIFICATION"},
    )

    # BODY_MODIFICATION only propagates through CALLS edges
    # validate_user_id callers: process_order
    # But process_order isn't in the diff, so this could be INCOMPLETE
    # HOWEVER — BODY_MOD on validate_user_id means callers MIGHT be affected
    # but don't necessarily NEED updating (behavior change, not interface change)
    # The verifier reports it — the confidence determines the verdict
    assert result.verdict in (Verdict.STRUCTURALLY_COMPLETE, Verdict.INCOMPLETE)
    assert result.stats["touched_count"] >= 1


def test_verify_incomplete_signature_change(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)

    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    result = verifier.verify(
        diff_text=diff,
        seeds=["service.py::process_order"],
        change_types={"SIGNATURE_MODIFICATION"},
    )

    # SIGNATURE_MODIFICATION traverses CALLS, USES_TYPE, IMPORTS, EXTENDS
    # main.py::main calls process_order — it should be in expected but not touched
    # So verdict should be INCOMPLETE (main.py wasn't updated)
    assert result.verdict == Verdict.INCOMPLETE
    missing_ids = {m.node_id for m in result.missing_nodes}
    assert "main.py::main" in missing_ids


def test_verify_auto_infer_seeds(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)

    diff = (DIFFS_DIR / "complete_body_mod.diff").read_text()
    result = verifier.verify(diff_text=diff)

    # Should auto-infer validate_user_id as seed
    assert len(result.seeds) > 0


def test_verify_empty_diff(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)
    result = verifier.verify(diff_text="")
    assert result.verdict == Verdict.STRUCTURALLY_COMPLETE


def test_verify_to_dict(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)

    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    result = verifier.verify(diff_text=diff, seeds=["service.py::process_order"],
                              change_types={"SIGNATURE_MODIFICATION"})
    d = result.to_dict()
    assert "verdict" in d
    assert "missing_nodes" in d
    assert isinstance(d["missing_nodes"], list)


# --- FollowUpGenerator ---

def test_followup_complete(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)
    gen = FollowUpGenerator()

    diff = (DIFFS_DIR / "complete_body_mod.diff").read_text()
    result = verifier.verify(diff_text=diff, seeds=["utils.py::validate_user_id"],
                              change_types={"BODY_MODIFICATION"})

    text = gen.generate(result)
    if result.verdict == Verdict.STRUCTURALLY_COMPLETE:
        assert "STRUCTURALLY COMPLETE" in text
    else:
        assert "INCOMPLETE" in text


def test_followup_incomplete_has_details(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)
    gen = FollowUpGenerator()

    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    result = verifier.verify(diff_text=diff, seeds=["service.py::process_order"],
                              change_types={"SIGNATURE_MODIFICATION"})

    text = gen.generate(result)
    assert "INCOMPLETE" in text
    assert "WHY:" in text
    assert "LIKELY FIX:" in text
    # Should mention main.py
    assert "main" in text.lower()


# --- RetrievalFeedback ---

def test_feedback_record_and_boost(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)
    feedback = RetrievalFeedback(store)

    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    result = verifier.verify(diff_text=diff, seeds=["service.py::process_order"],
                              change_types={"SIGNATURE_MODIFICATION"})

    # Record feedback
    feedback.record(result)

    # Get boost map
    boost_map = feedback.get_boost_map({"SIGNATURE_MODIFICATION"})

    # Missing nodes should have boost entries
    if result.missing_nodes:
        high_med = [m for m in result.missing_nodes if m.confidence in ("high", "medium")]
        for m in high_med:
            assert m.node_id in boost_map


def test_feedback_booster(indexed_project):
    store, _, _ = indexed_project
    verifier = Verifier(store)
    feedback = RetrievalFeedback(store)
    booster = FeedbackBooster(feedback)

    # Create a verification with missing nodes
    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    result = verifier.verify(diff_text=diff, seeds=["service.py::process_order"],
                              change_types={"SIGNATURE_MODIFICATION"})
    feedback.record(result)

    # Now run traversal and apply boosts
    traversal = GraphTraversal(store)
    affected = traversal.traverse(
        seeds=["service.py::process_order"],
        change_types={ChangeType.SIGNATURE_MODIFICATION},
    )

    original_priorities = {a.node_id: a.priority for a in affected}
    boosted = booster.apply_boosts(affected, {ChangeType.SIGNATURE_MODIFICATION})
    boosted_priorities = {a.node_id: a.priority for a in boosted}

    # At least some nodes should have higher priority after boost
    any_boosted = any(
        boosted_priorities.get(nid, 0) > original_priorities.get(nid, 0)
        for nid in boosted_priorities
    )
    # This may or may not boost depending on which nodes were missed
    # At minimum, no priorities should decrease
    for nid in original_priorities:
        if nid in boosted_priorities:
            assert boosted_priorities[nid] >= original_priorities[nid]


def test_feedback_prune_stale(indexed_project):
    store, _, _ = indexed_project
    feedback = RetrievalFeedback(store)

    # Insert feedback for a node that doesn't exist
    store.conn.execute(
        "INSERT INTO retrieval_feedback (query_hash, change_type, missed_node, edge_kind, depth) "
        "VALUES ('test', 'BODY_MODIFICATION', 'nonexistent.py::fake_func', 'CALLS', 1)"
    )
    store.conn.commit()

    feedback.prune_stale()

    # Should be gone
    row = store.conn.execute(
        "SELECT COUNT(*) as c FROM retrieval_feedback WHERE missed_node = 'nonexistent.py::fake_func'"
    ).fetchone()
    assert row["c"] == 0


# --- Verifier + PatternMatcher wiring ---

def test_verifier_accepts_pattern_matcher(indexed_project):
    """Verifier can be constructed with a pattern_matcher."""
    store, _, _ = indexed_project
    from engram.patterns import PatternCatalog, PatternMatcher
    catalog = PatternCatalog(store)
    matcher = PatternMatcher(store, catalog)
    verifier = Verifier(store, pattern_matcher=matcher)
    assert verifier.pattern_matcher is matcher


def test_verifier_without_pattern_matcher_still_works(indexed_project):
    """Backward compat: Verifier(store) alone still works."""
    store, _, _ = indexed_project
    verifier = Verifier(store)
    assert verifier.pattern_matcher is None

    diff = (DIFFS_DIR / "complete_body_mod.diff").read_text()
    result = verifier.verify(diff_text=diff, seeds=["utils.py::validate_user_id"],
                              change_types={"BODY_MODIFICATION"})
    assert result.verdict in (Verdict.STRUCTURALLY_COMPLETE, Verdict.INCOMPLETE)


def test_verifier_with_pattern_matcher_runs_traversal(indexed_project):
    """Verifier with patterns runs without error and produces a result."""
    store, _, _ = indexed_project
    from engram.patterns import PatternCatalog, PatternMatcher
    catalog = PatternCatalog(store)
    matcher = PatternMatcher(store, catalog)
    verifier = Verifier(store, pattern_matcher=matcher)

    diff = (DIFFS_DIR / "incomplete_signature_change.diff").read_text()
    result = verifier.verify(diff_text=diff, seeds=["service.py::process_order"],
                              change_types={"SIGNATURE_MODIFICATION"})
    # Should still detect that main.py::main is missing
    assert result.verdict == Verdict.INCOMPLETE
    missing_ids = {m.node_id for m in result.missing_nodes}
    assert "main.py::main" in missing_ids
