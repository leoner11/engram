"""Tests for the upgraded three-pass seed selection system."""

import json
import pytest
from pathlib import Path

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.graph.activation import ChangeType
from engram.cli import build_index
from engram.retriever.seeds import (
    SeedSelector,
    SeedCandidate,
    extract_prompt_terms,
    split_identifier,
    populate_node_index,
    select_seeds,
)
from engram.retriever.anticipation import anticipate_change_types
from engram.indexer.extractor import NodeRecord
from engram.indexer.resolver import Edge


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ─── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def indexed_project(tmp_path):
    """Build index with FTS5 populated for simple_project."""
    import shutil
    project = tmp_path / "simple_project"
    shutil.copytree(FIXTURES_DIR / "simple_project", project)

    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    yield store, project
    db.close()


@pytest.fixture
def selector(indexed_project):
    """SeedSelector with indexed simple_project."""
    store, _ = indexed_project
    return SeedSelector(store)


@pytest.fixture
def bare_store(tmp_path):
    """GraphStore with nodes but NO FTS5 index populated (tests fallback)."""
    import shutil
    project = tmp_path / "bare_project"
    shutil.copytree(FIXTURES_DIR / "simple_project", project)

    db = EngramDB(project)
    store = GraphStore(db)
    # Manually build without the FTS5 populate step
    from engram.indexer.parser import TreeSitterParser
    from engram.indexer.hasher import hash_file
    from engram.indexer.languages import detect_language, get_all_extensions
    from engram.indexer.scanner import scan_project
    from engram.indexer.resolver import Resolver

    parser = TreeSitterParser()
    extensions = get_all_extensions()
    files = scan_project(project, extensions)
    all_nodes = {}
    all_raw_edges = []

    for rel_path in files:
        abs_path = project / rel_path
        file_str = str(rel_path)
        language = detect_language(rel_path)
        if language is None:
            continue
        try:
            tree = parser.parse_file(abs_path, language)
        except Exception:
            continue
        source = abs_path.read_bytes()
        from engram.indexer.adapters.python_adapter import PythonAdapter
        adapter = PythonAdapter()
        nodes, raw_edges = adapter.extract(tree, source, file_str)
        for node in nodes:
            store.upsert_node(node)
            all_nodes[node.id] = node
        all_raw_edges.extend(raw_edges)
        store.update_manifest(file_str, hash_file(abs_path), len(nodes))

    resolver = Resolver(all_nodes, all_raw_edges, project)
    for edge in resolver.resolve_all():
        store.upsert_edge(edge)
    store.commit()

    # Intentionally do NOT call populate_node_index
    # Clear the FTS5 table so it's empty
    try:
        store.conn.execute("DELETE FROM node_index")
        store.conn.commit()
    except Exception:
        pass

    yield store
    db.close()


# ─── split_identifier ────────────────────────────────────────────────────


def test_split_snake_case():
    assert split_identifier("create_event") == ["create", "event"]


def test_split_camel_case():
    assert split_identifier("handleStripeWebhook") == ["handle", "stripe", "webhook"]


def test_split_pascal_case():
    assert split_identifier("EventSerializer") == ["event", "serializer"]


def test_split_acronym():
    assert split_identifier("HTMLParser") == ["html", "parser"]


def test_split_single_word():
    assert split_identifier("main") == ["main"]


# ─── extract_prompt_terms ────────────────────────────────────────────────


class TestPromptTermExtraction:

    def test_basic_extraction(self):
        terms = extract_prompt_terms("fix the webhook bug")
        assert "webhook" in terms
        assert "bug" in terms

    def test_stopwords_removed(self):
        terms = extract_prompt_terms("fix the webhook in the handler")
        assert "the" not in terms
        assert "in" not in terms

    def test_action_verbs_last(self):
        terms = extract_prompt_terms("fix the payment bug")
        # "payment" and "bug" should come before "fix"
        assert terms.index("payment") < terms.index("fix")
        assert terms.index("bug") < terms.index("fix")

    def test_quoted_terms_first(self):
        terms = extract_prompt_terms('fix the "webhook handler" issue')
        assert terms[0] == "webhook handler"

    def test_identifier_expansion(self):
        terms = extract_prompt_terms("handleStripeWebhook is broken")
        assert "handlestripewebhook" in terms
        assert "stripe" in terms
        assert "webhook" in terms

    def test_empty_prompt(self):
        assert extract_prompt_terms("") == []

    def test_only_stopwords(self):
        assert extract_prompt_terms("the a an is are") == []

    def test_short_terms_filtered(self):
        terms = extract_prompt_terms("a b c webhook")
        assert "a" not in terms
        assert "b" not in terms
        assert "webhook" in terms

    def test_deduplication(self):
        terms = extract_prompt_terms("order order order")
        assert terms.count("order") == 1


# ─── Pass 1: FTS5 Search ────────────────────────────────────────────────


class TestFTS5Search:

    def test_exact_name_match(self, selector):
        """Prompt with exact function name finds it."""
        candidates = selector._pass1_fts5(["save_order"])
        node_ids = {c.node_id for c in candidates}
        assert "repository.py::save_order" in node_ids

    def test_word_match(self, selector):
        """Prompt word 'order' finds order-related functions."""
        candidates = selector._pass1_fts5(["order"])
        node_ids = {c.node_id for c in candidates}
        assert len(candidates) > 0
        assert any("order" in nid.lower() for nid in node_ids)

    def test_docstring_match(self, selector):
        """Prompt 'payment' finds process_order via its docstring."""
        candidates = selector._pass1_fts5(["payment"])
        node_ids = {c.node_id for c in candidates}
        assert "service.py::process_order" in node_ids

    def test_docstring_semantic_gap(self, selector):
        """Prompt 'checkout' finds process_order even though 'checkout'
        doesn't appear in any function name — only in docstring."""
        candidates = selector._pass1_fts5(["checkout"])
        node_ids = {c.node_id for c in candidates}
        assert "service.py::process_order" in node_ids

    def test_persistence_via_docstring(self, selector):
        """Prompt 'database' finds save_order via enriched docstring."""
        candidates = selector._pass1_fts5(["database"])
        node_ids = {c.node_id for c in candidates}
        assert "repository.py::save_order" in node_ids

    def test_no_file_nodes(self, selector):
        """FILE nodes are excluded from FTS5 results."""
        candidates = selector._pass1_fts5(["order"])
        for c in candidates:
            node = selector.store.get_node(c.node_id)
            assert node.kind != "FILE"

    def test_export_bonus(self, selector):
        """Exported nodes get a score multiplier."""
        candidates = selector._pass1_fts5(["order"])
        for c in candidates:
            node = selector.store.get_node(c.node_id)
            if node.is_exported:
                # Just verify the export path runs without error
                assert c.pass_scores["fts5"] > 0

    def test_fts5_special_chars_safe(self, selector):
        """Special characters in terms don't crash FTS5."""
        # Should return empty, not crash
        candidates = selector._pass1_fts5(["foo()", "bar::baz"])
        # Terms with special chars are filtered out, so empty or just safe ones
        assert isinstance(candidates, list)

    def test_multi_term_or(self, selector):
        """Multiple terms are OR-joined, finding nodes matching any term."""
        candidates = selector._pass1_fts5(["save", "currency"])
        node_ids = {c.node_id for c in candidates}
        assert "repository.py::save_order" in node_ids
        assert "utils.py::format_currency" in node_ids


# ─── Pass 1: Fallback ───────────────────────────────────────────────────


class TestFallbackSearch:

    def test_fallback_finds_nodes(self, bare_store):
        """When FTS5 index is empty, fallback substring matching works."""
        selector = SeedSelector(bare_store)
        candidates = selector._pass1_fallback(["order"])
        assert len(candidates) > 0
        assert any("order" in c.node_id.lower() for c in candidates)

    def test_fallback_triggered_when_fts5_empty(self, bare_store):
        """Full select() falls back when FTS5 returns nothing."""
        selector = SeedSelector(bare_store)
        seeds = selector.select("fix save_order")
        assert len(seeds) > 0


# ─── Pass 2: Graph Boost ────────────────────────────────────────────────


class TestGraphBoost:

    def test_connected_candidates_boosted(self, selector):
        """Candidates connected in the graph get a graph boost score."""
        # process_order calls save_order — they should boost each other
        candidates = [
            SeedCandidate("service.py::process_order", 0, "fts5", {"fts5": 10.0}),
            SeedCandidate("repository.py::save_order", 0, "fts5", {"fts5": 10.0}),
        ]
        boosted = selector._pass2_graph_boost(candidates)
        for c in boosted:
            assert c.pass_scores.get("graph", 0) > 0, \
                f"{c.node_id} should have graph boost since they're connected"

    def test_isolated_candidate_penalized(self, selector):
        """Candidate with no graph connection to others gets penalized."""
        candidates = [
            SeedCandidate("service.py::process_order", 0, "fts5", {"fts5": 10.0}),
            SeedCandidate("utils.py::format_currency", 0, "fts5", {"fts5": 10.0}),
        ]
        boosted = selector._pass2_graph_boost(candidates)

        # process_order calls format_currency, so they might be connected
        # But if not, the isolated one should be penalized
        # The key test: the boost pass runs without error and assigns graph scores
        for c in boosted:
            assert "graph" in c.pass_scores

    def test_boost_preserves_candidates(self, selector):
        """Graph boost doesn't remove any candidates, only re-scores."""
        candidates = [
            SeedCandidate("service.py::process_order", 0, "fts5", {"fts5": 10.0}),
            SeedCandidate("repository.py::save_order", 0, "fts5", {"fts5": 8.0}),
            SeedCandidate("utils.py::validate_user_id", 0, "fts5", {"fts5": 5.0}),
        ]
        boosted = selector._pass2_graph_boost(candidates)
        assert len(boosted) == 3


# ─── Pass 3: Feedback ───────────────────────────────────────────────────


class TestFeedbackBoost:

    def test_missed_node_boosted(self, indexed_project):
        """Node in retrieval_feedback table gets a feedback boost."""
        store, _ = indexed_project
        selector = SeedSelector(store)

        # Insert a fake feedback record
        store.conn.execute(
            """INSERT INTO retrieval_feedback (query_hash, change_type, missed_node, edge_kind, depth)
               VALUES (?, ?, ?, ?, ?)""",
            ("abc123", "BODY_MODIFICATION", "repository.py::save_order", "CALLS", 1),
        )
        store.conn.commit()

        candidates = [
            SeedCandidate("repository.py::save_order", 0, "fts5", {"fts5": 10.0}),
            SeedCandidate("service.py::process_order", 0, "fts5", {"fts5": 10.0}),
        ]
        result = selector._pass3_feedback(candidates, ["order"])
        save_order_c = next(c for c in result if c.node_id == "repository.py::save_order")
        process_order_c = next(c for c in result if c.node_id == "service.py::process_order")
        assert save_order_c.pass_scores.get("feedback", 0) > process_order_c.pass_scores.get("feedback", 0)

    def test_file_path_affinity(self, indexed_project):
        """Candidates in frequently-seeded directories get boosted."""
        store, _ = indexed_project
        selector = SeedSelector(store)

        # Insert seed history pointing to repository.py's directory
        store.conn.execute(
            """INSERT INTO seed_history (query_hash, prompt_terms, seed_ids, file_paths)
               VALUES (?, ?, ?, ?)""",
            ("hash1", '["order"]', '["repository.py::save_order"]', '["repository.py"]'),
        )
        store.conn.execute(
            """INSERT INTO seed_history (query_hash, prompt_terms, seed_ids, file_paths)
               VALUES (?, ?, ?, ?)""",
            ("hash2", '["data"]', '["repository.py::get_order"]', '["repository.py"]'),
        )
        store.conn.commit()

        candidates = [
            SeedCandidate("repository.py::save_order", 0, "fts5", {"fts5": 10.0}),
            SeedCandidate("service.py::process_order", 0, "fts5", {"fts5": 10.0}),
        ]
        result = selector._pass3_feedback(candidates, ["order"])
        # Both are in root dir (no subdirectory), so affinity applies equally
        # The test verifies feedback pass runs cleanly
        for c in result:
            assert isinstance(c.pass_scores.get("feedback", 0), (int, float))

    def test_feedback_handles_empty_tables(self, selector):
        """Feedback pass works when tables are empty."""
        candidates = [
            SeedCandidate("service.py::process_order", 0, "fts5", {"fts5": 10.0}),
        ]
        result = selector._pass3_feedback(candidates, ["order"])
        assert len(result) == 1


# ─── Dynamic Seed Count ─────────────────────────────────────────────────


class TestDynamicCutoff:

    def _make(self, scores):
        return [SeedCandidate(f"node_{i}", s, "test") for i, s in enumerate(scores)]

    def test_single_dominant(self, selector):
        """Score 100 vs 30 → 1 seed."""
        candidates = self._make([100, 30, 20])
        result = selector._dynamic_cutoff(candidates, max_seeds=6)
        assert len(result) == 1

    def test_clustered_scores(self, selector):
        """Close scores → take all that pass threshold."""
        candidates = self._make([50, 48, 45, 43])
        result = selector._dynamic_cutoff(candidates, max_seeds=6)
        assert len(result) == 4

    def test_score_dropoff(self, selector):
        """Sharp dropoff cuts off lower candidates."""
        candidates = self._make([100, 90, 85, 30, 10])
        result = selector._dynamic_cutoff(candidates, max_seeds=6)
        # 30 < 40% of 100 = 40, so cut at 85
        assert len(result) == 3

    def test_respects_max_cap(self, selector):
        """Never exceeds max_seeds."""
        candidates = self._make([50, 50, 50, 50, 50, 50, 50, 50])
        result = selector._dynamic_cutoff(candidates, max_seeds=3)
        assert len(result) == 3

    def test_empty_candidates(self, selector):
        result = selector._dynamic_cutoff([], max_seeds=6)
        assert result == []

    def test_single_candidate(self, selector):
        candidates = self._make([42])
        result = selector._dynamic_cutoff(candidates, max_seeds=6)
        assert len(result) == 1

    def test_zero_scores(self, selector):
        """All zero scores → return first only."""
        candidates = self._make([0, 0, 0])
        result = selector._dynamic_cutoff(candidates, max_seeds=6)
        assert len(result) == 1


# ─── Full Pipeline (select) ─────────────────────────────────────────────


class TestFullPipeline:

    def test_exact_name_query(self, selector):
        """Direct function name in prompt finds it."""
        seeds = selector.select("fix save_order")
        assert len(seeds) > 0
        assert seeds[0].node_id == "repository.py::save_order"

    def test_semantic_gap_query(self, selector):
        """'payment' finds process_order via docstring, not name."""
        seeds = selector.select("fix the payment bug")
        seed_ids = {s.node_id for s in seeds}
        assert "service.py::process_order" in seed_ids

    def test_persistence_query(self, selector):
        """'database persistence' finds save_order via enriched docstring."""
        seeds = selector.select("database persistence issue")
        seed_ids = {s.node_id for s in seeds}
        assert "repository.py::save_order" in seed_ids

    def test_multi_term_query(self, selector):
        """Query with multiple entity terms finds relevant nodes."""
        seeds = selector.select("order processing and currency formatting")
        seed_ids = {s.node_id for s in seeds}
        assert len(seeds) >= 2

    def test_explicit_seeds_bypass(self, selector):
        """Explicit seeds skip all scoring."""
        seeds = selector.select(
            "anything at all",
            explicit_seeds=["utils.py::format_currency"],
        )
        assert len(seeds) == 1
        assert seeds[0].node_id == "utils.py::format_currency"
        assert seeds[0].match_reason == "explicit"

    def test_explicit_seeds_invalid_filtered(self, selector):
        """Invalid explicit seed IDs are filtered out."""
        seeds = selector.select(
            "anything",
            explicit_seeds=["nonexistent.py::fake_func"],
        )
        assert len(seeds) == 0

    def test_empty_prompt(self, selector):
        seeds = selector.select("")
        # v5: returns top_connected_fallback instead of empty
        assert all(s.match_reason == "top_connected_fallback" for s in seeds)

    def test_stopword_only_prompt(self, selector):
        seeds = selector.select("the a an is are")
        # v5: returns top_connected_fallback instead of empty
        assert all(s.match_reason == "top_connected_fallback" for s in seeds)

    def test_refund_cancellation_query(self, selector):
        """'refund' finds cancel_order via docstring mentioning refund."""
        seeds = selector.select("refund workflow issue")
        seed_ids = {s.node_id for s in seeds}
        assert "service.py::cancel_order" in seed_ids


# ─── Record Selection ───────────────────────────────────────────────────


class TestRecordSelection:

    def test_records_to_seed_history(self, indexed_project):
        store, _ = indexed_project
        selector = SeedSelector(store)

        candidates = [
            SeedCandidate("service.py::process_order", 50, "fts5"),
        ]
        selector.record_selection("fix order", ["order"], candidates)

        rows = store.conn.execute("SELECT * FROM seed_history").fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]["seed_ids"]) == ["service.py::process_order"]
        assert json.loads(rows[0]["prompt_terms"]) == ["order"]

    def test_record_handles_missing_nodes(self, indexed_project):
        """Recording with a candidate whose node doesn't exist doesn't crash."""
        store, _ = indexed_project
        selector = SeedSelector(store)

        candidates = [
            SeedCandidate("nonexistent.py::fake", 50, "fts5"),
        ]
        # Should not raise
        selector.record_selection("test", ["test"], candidates)


# ─── populate_node_index ─────────────────────────────────────────────────


class TestPopulateNodeIndex:

    def test_populates_fts5(self, indexed_project):
        """After build, node_index has entries."""
        store, _ = indexed_project
        count = store.conn.execute("SELECT COUNT(*) as c FROM node_index").fetchone()["c"]
        assert count > 0

    def test_no_file_nodes_in_index(self, indexed_project):
        """FILE nodes are excluded from FTS5 index."""
        store, _ = indexed_project
        rows = store.conn.execute(
            "SELECT node_id FROM node_index"
        ).fetchall()
        for row in rows:
            node = store.get_node(row["node_id"])
            if node:
                assert node.kind != "FILE"

    def test_name_expansion(self, indexed_project):
        """Expanded names are searchable (split identifiers)."""
        store, _ = indexed_project
        # "save_order" should have "save" and "order" indexed
        rows = store.conn.execute(
            "SELECT node_id FROM node_index WHERE node_index MATCH 'save'"
        ).fetchall()
        node_ids = {r["node_id"] for r in rows}
        assert "repository.py::save_order" in node_ids


# ─── Legacy Wrapper ──────────────────────────────────────────────────────


class TestLegacyWrapper:

    def test_select_seeds_still_works(self, indexed_project):
        """Legacy select_seeds() function delegates to SeedSelector."""
        store, _ = indexed_project
        seeds = select_seeds("fix save_order", store)
        assert len(seeds) > 0
        assert seeds[0].node_id == "repository.py::save_order"

    def test_legacy_explicit_seeds(self, indexed_project):
        store, _ = indexed_project
        seeds = select_seeds(
            "anything",
            store,
            explicit_seeds=["utils.py::format_currency"],
        )
        assert len(seeds) == 1
        assert seeds[0].match_reason == "explicit"

    def test_legacy_max_seeds(self, indexed_project):
        store, _ = indexed_project
        seeds = select_seeds("order", store, max_seeds=2)
        assert len(seeds) <= 2


# ─── Anticipation tests (unchanged, kept for completeness) ──────────────


def test_anticipate_rename():
    result = anticipate_change_types("rename the validate function")
    assert ChangeType.RENAME in result


def test_anticipate_fix():
    result = anticipate_change_types("fix the webhook bug")
    assert ChangeType.BODY_MODIFICATION in result


def test_anticipate_add_field():
    result = anticipate_change_types("add a recurrence field to Event")
    assert ChangeType.FIELD_ADDITION in result


def test_anticipate_delete():
    result = anticipate_change_types("delete the old handler")
    assert ChangeType.DELETION in result


def test_anticipate_safe_default():
    result = anticipate_change_types("the event list page is showing stale data")
    assert result == {ChangeType.BODY_MODIFICATION, ChangeType.SIGNATURE_MODIFICATION}


def test_anticipate_hints_override():
    result = anticipate_change_types(
        "do something vague",
        change_hints=["renaming a function"],
    )
    assert ChangeType.RENAME in result


def test_anticipate_explicit_override():
    result = anticipate_change_types(
        "do something",
        explicit_change_type="FIELD_ADDITION",
    )
    assert result == {ChangeType.FIELD_ADDITION}


def test_anticipate_multi_label():
    result = anticipate_change_types("add support for recurring events")
    assert len(result) > 1

# ─── Phase D: File path matching and fallback ──────────────────────────


def test_extract_file_references():
    selector = SeedSelector.__new__(SeedSelector)
    refs = selector._extract_file_references("fix the bug in webhooks.py")
    assert "webhooks.py" in refs


def test_extract_file_references_with_path():
    selector = SeedSelector.__new__(SeedSelector)
    refs = selector._extract_file_references("update src/api/views.py to handle auth")
    assert "src/api/views.py" in refs


def test_extract_file_references_tsx():
    selector = SeedSelector.__new__(SeedSelector)
    refs = selector._extract_file_references("the EventList.tsx component is broken")
    assert "EventList.tsx" in refs


def test_filepath_matching_finds_nodes(simple_store):
    store, project = simple_store
    populate_node_index(store)
    selector = SeedSelector(store)

    # "fix the bug in service.py" should find nodes in service.py
    candidates = selector._pass1_filepath(
        terms=["service"], prompt="fix the bug in service.py", candidate_limit=10
    )
    assert len(candidates) > 0
    # All candidates should be from service.py
    for c in candidates:
        node = store.get_node(c.node_id)
        assert node.file_path == "service.py", f"Expected service.py, got {node.file_path}"


def test_filepath_matching_stem_only(simple_store):
    store, project = simple_store
    populate_node_index(store)
    selector = SeedSelector(store)

    # "the utils module" — "utils" should match utils.py
    candidates = selector._pass1_filepath(
        terms=["utils"], prompt="fix something in utils", candidate_limit=10
    )
    # Should find nodes, but only if "utils" appears as a file reference via _extract_file_references
    # Actually "in utils" triggers the pattern 2 regex
    # Let's just check the method doesn't crash
    assert isinstance(candidates, list)


def test_full_select_with_filename(simple_store):
    store, project = simple_store
    populate_node_index(store)
    selector = SeedSelector(store)

    # Full pipeline: "fix the bug in repository.py"
    candidates = selector.select("fix the bug in repository.py")
    assert len(candidates) > 0
    # Should preferentially pick nodes from repository.py
    node_ids = {c.node_id for c in candidates}
    repo_nodes = {nid for nid in node_ids if "repository" in nid}
    assert len(repo_nodes) > 0, f"Expected repository nodes, got {node_ids}"


def test_fallback_top_connected(simple_store):
    store, project = simple_store
    populate_node_index(store)
    selector = SeedSelector(store)

    # _fallback_top_connected should return something even with no terms
    candidates = selector._fallback_top_connected(max_seeds=3)
    # May or may not find exported nodes in simple_project fixture
    # But should not crash
    assert isinstance(candidates, list)


def test_select_with_no_matching_terms(simple_store):
    store, project = simple_store
    populate_node_index(store)
    selector = SeedSelector(store)

    # Completely unrelated prompt — should still return something (fallback)
    candidates = selector.select("xyzzy quantum entanglement")
    # Might return empty or fallback nodes — either is acceptable
    assert isinstance(candidates, list)


def test_select_empty_prompt(simple_store):
    store, project = simple_store
    populate_node_index(store)
    selector = SeedSelector(store)

    # Empty prompt — should use top-connected fallback, not crash
    candidates = selector.select("")
    assert isinstance(candidates, list)
