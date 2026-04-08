"""Tests for change-type-aware graph traversal."""

import pytest
from engram.graph.activation import ChangeType
from engram.graph.store import GraphStore
from engram.graph.traversal import GraphTraversal


def test_body_mod_traverses_callers(simple_store):
    store, _ = simple_store
    traversal = GraphTraversal(store)
    # BODY_MODIFICATION on save_order → should find callers
    affected = traversal.traverse(
        seeds=["repository.py::save_order"],
        change_types={ChangeType.BODY_MODIFICATION},
    )
    affected_ids = {a.node_id for a in affected}
    # process_order and cancel_order both call save_order
    assert "service.py::process_order" in affected_ids
    assert "service.py::cancel_order" in affected_ids


def test_body_mod_does_not_traverse_uses_type(simple_store):
    store, _ = simple_store
    traversal = GraphTraversal(store)
    # BODY_MODIFICATION should NOT traverse USES_TYPE edges
    affected = traversal.traverse(
        seeds=["models.py::Order"],
        change_types={ChangeType.BODY_MODIFICATION},
    )
    affected_ids = {a.node_id for a in affected}
    # Should only have callers (depth 0 = seed itself)
    # USES_TYPE edges should NOT be followed
    for a in affected:
        if a.depth > 0:
            assert a.reached_via in ("CALLS", "SEED"), f"Unexpected edge: {a.reached_via} for {a.node_id}"


def test_rename_traverses_all(simple_store):
    store, _ = simple_store
    traversal = GraphTraversal(store)
    # RENAME traverses ALL edge kinds
    affected = traversal.traverse(
        seeds=["utils.py::validate_user_id"],
        change_types={ChangeType.RENAME},
    )
    affected_ids = {a.node_id for a in affected}
    # process_order calls validate_user_id
    assert "service.py::process_order" in affected_ids


def test_seeds_have_highest_priority(simple_store):
    store, _ = simple_store
    traversal = GraphTraversal(store)
    affected = traversal.traverse(
        seeds=["repository.py::save_order"],
        change_types={ChangeType.BODY_MODIFICATION},
    )
    seed = [a for a in affected if a.node_id == "repository.py::save_order"][0]
    non_seeds = [a for a in affected if a.node_id != "repository.py::save_order"]
    for ns in non_seeds:
        assert seed.priority > ns.priority


def test_multi_label_union(simple_store):
    store, _ = simple_store
    traversal = GraphTraversal(store)
    # Multiple change types → union of affected sets
    body_only = traversal.traverse(
        seeds=["repository.py::get_order"],
        change_types={ChangeType.BODY_MODIFICATION},
    )
    both = traversal.traverse(
        seeds=["repository.py::get_order"],
        change_types={ChangeType.BODY_MODIFICATION, ChangeType.SIGNATURE_MODIFICATION},
    )
    # Union should be >= single
    assert len(both) >= len(body_only)


def test_depth_limit(simple_store):
    store, _ = simple_store
    traversal = GraphTraversal(store)
    affected = traversal.traverse(
        seeds=["utils.py::validate_user_id"],
        change_types={ChangeType.BODY_MODIFICATION},
        max_depth=1,
    )
    for a in affected:
        assert a.depth <= 1
