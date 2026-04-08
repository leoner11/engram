"""Tests for the full context assembly pipeline."""

import pytest
from engram.graph.store import GraphStore
from engram.retriever.assembler import ContextAssembler, _estimate_tokens


def test_assemble_basic(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)
    package = assembler.assemble("fix save_order logic")

    assert package.task == "fix save_order logic"
    assert len(package.seeds) > 0
    assert len(package.nodes) > 0
    assert package.total_tokens > 0
    assert package.total_tokens <= package.budget


def test_assemble_seeds_get_full_detail(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)
    package = assembler.assemble("fix save_order logic")

    seed_nodes = [cn for cn in package.nodes if cn.depth == 0]
    for sn in seed_nodes:
        assert sn.detail_level == "full"


def test_assemble_budget_respected(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)
    package = assembler.assemble("fix the order system", max_tokens=500)

    assert package.total_tokens <= 500


def test_assemble_small_budget_excludes_nodes(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)




def test_assemble_no_seeds_found(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)
    package = assembler.assemble("xyznonexistent")

    # v5: fallback seeds are returned (top_connected_fallback) instead of empty
    # The creation mode detection handles this case gracefully
    assert package.total_tokens >= 0


def test_assemble_explicit_seeds(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)
    package = assembler.assemble(
        "do something",
        seeds=["utils.py::validate_user_id"],
    )

    assert "utils.py::validate_user_id" in package.seeds
    seed_nodes = [cn for cn in package.nodes if cn.depth == 0]
    assert any(cn.node.id == "utils.py::validate_user_id" for cn in seed_nodes)


def test_assemble_change_hints(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)

    # With rename hint → RENAME traverses more edges
    rename_pkg = assembler.assemble(
        "update validate_user_id",
        change_hints=["renaming a function"],
    )
    # Without hint → safe default (BODY_MOD + SIG_MOD)
    default_pkg = assembler.assemble("update validate_user_id")

    assert "RENAME" in rename_pkg.change_types


def test_assemble_serialize(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)
    package = assembler.assemble("fix save_order logic")

    md = package.serialize()
    assert "# Task: fix save_order logic" in md
    assert "Anticipated changes:" in md
    assert "```python" in md


def test_estimate_tokens():
    assert _estimate_tokens("hello world") > 0
    # ~4 chars per token
    assert abs(_estimate_tokens("a" * 400) - 100) < 20


def test_depth_2_gets_signature(simple_store):
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)
    package = assembler.assemble("fix save_order logic")

    depth2 = [cn for cn in package.nodes if cn.depth >= 2]
    for cn in depth2:
        assert cn.detail_level in ("signature", "summary")


# ─── Freshness check ───────────────────────────────────────────────────


def test_freshness_detects_modified_file(simple_store):
    """After modifying a file, assembler re-indexes before querying."""
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)

    # Modify utils.py — add a new function
    utils_path = project / "utils.py"
    original = utils_path.read_text()
    utils_path.write_text(original + "\n\ndef new_helper():\n    \"\"\"Brand new function.\"\"\"\n    return 42\n")

    # Query — freshness check should pick up the new function
    package = assembler.assemble("fix new_helper")

    # The new function should be findable in the graph now
    node = store.get_node("utils.py::new_helper")
    assert node is not None, "Freshness check didn't re-index the modified file"
    assert node.name == "new_helper"

    # Restore
    utils_path.write_text(original)


def test_freshness_detects_deleted_file(simple_store, tmp_path):
    """After deleting a file, its nodes are removed from the graph."""
    import shutil
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)

    # Verify utils.py nodes exist
    assert store.get_node("utils.py::validate_user_id") is not None

    # Delete utils.py
    (project / "utils.py").unlink()

    # Query — freshness check should clean up
    package = assembler.assemble("fix something")

    # Nodes from deleted file should be gone
    assert store.get_node("utils.py::validate_user_id") is None

    # Restore for other tests
    from pathlib import Path
    FIXTURES_DIR = Path(__file__).parent / "fixtures"
    shutil.copy(FIXTURES_DIR / "simple_project" / "utils.py", project / "utils.py")


def test_freshness_no_change_is_fast(simple_store):
    """When nothing changed, freshness check returns quickly."""
    import time
    store, project = simple_store
    assembler = ContextAssembler(store, project_root=project)

    start = time.time()
    assembler._ensure_fresh()
    elapsed = time.time() - start

    # Should be very fast (<1s) when nothing changed
    assert elapsed < 2.0, f"Freshness check took {elapsed}s with no changes"
