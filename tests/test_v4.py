"""Tests for v4: patterns, benchmarking, summarizer."""

import pytest
import shutil
import json
from pathlib import Path

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.cli import build_index


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def v4_project(tmp_path):
    project = tmp_path / "test_project"
    shutil.copytree(FIXTURES_DIR / "simple_project", project)
    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)
    return store, db, project


# --- Patterns ---

def test_pattern_detector_no_framework(v4_project):
    store, _, _ = v4_project
    from engram.patterns import PatternDetector
    detector = PatternDetector(store)
    # simple_project doesn't use django/fastapi/flask
    fw = detector.detect_framework()
    assert fw is None


def test_pattern_detector_detects_generic(v4_project):
    store, _, _ = v4_project
    from engram.patterns import PatternDetector
    detector = PatternDetector(store)
    patterns = detector.detect_patterns()
    # May match generic patterns (repository/service)
    # At minimum should not crash
    assert isinstance(patterns, list)


def test_pattern_catalog_save_and_get(v4_project):
    store, _, _ = v4_project
    from engram.patterns import PatternCatalog, StructuralPattern
    catalog = PatternCatalog(store)

    pattern = StructuralPattern(
        id="test/custom-pattern",
        name="Custom test pattern",
        framework=None,
        description="A test pattern",
        node_pattern={"anchor": {"kind": "FUNCTION"}},
        implicit_edges=[],
        confidence=0.9,
    )
    catalog.save_learned(pattern)

    all_patterns = catalog.get_all()
    custom = [p for p in all_patterns if p.id == "test/custom-pattern"]
    assert len(custom) == 1
    assert custom[0].confidence == 0.9


def test_pattern_export_import(v4_project, tmp_path):
    store, _, _ = v4_project
    from engram.patterns import PatternCatalog, StructuralPattern
    catalog = PatternCatalog(store)

    pattern = StructuralPattern(
        id="test/exportable",
        name="Exportable pattern",
        framework=None,
        description="For export test",
        node_pattern={"anchor": {"kind": "CLASS"}},
        implicit_edges=[],
        confidence=0.75,
    )
    catalog.save_learned(pattern)

    export_path = tmp_path / "patterns.json"
    catalog.export_patterns(export_path)
    assert export_path.exists()

    data = json.loads(export_path.read_text())
    assert len(data) >= 1  # Builtins + our custom one

    # Import into a fresh project
    project2 = tmp_path / "project2"
    shutil.copytree(FIXTURES_DIR / "simple_project", project2)
    db2 = EngramDB(project2)
    build_index(project2, db2, force=True)
    store2 = GraphStore(db2)
    catalog2 = PatternCatalog(store2)

    imported = catalog2.import_patterns(export_path)
    assert imported >= 1
    db2.close()


# --- Benchmarking ---

def test_benchmark_runner(v4_project):
    store, _, project = v4_project
    from engram.bench import BenchmarkRunner, BenchmarkEvaluator
    from engram.retriever.assembler import ContextAssembler

    assembler = ContextAssembler(store, project_root=project)
    runner = BenchmarkRunner(store, assembler)

    task = {
        "id": "test-task",
        "prompt": "fix save_order logic",
        "seeds": ["repository.py::save_order"],
        "change_types": ["BODY_MODIFICATION"],
        "expected_affected": [
            {"node": "repository.py::save_order", "reason": "seed"},
            {"node": "service.py::process_order", "reason": "caller"},
        ],
        "expected_excluded": [
            {"node": "models.py::OrderItem", "reason": "unrelated"},
        ],
    }

    results = runner.run_task(task)
    assert len(results) == 4  # engram + 3 baselines
    assert all(r.context_tokens > 0 for r in results)

    # Evaluate
    evaluator = BenchmarkEvaluator()
    metrics = evaluator.evaluate(results, task)
    assert len(metrics) == 4

    # Engram should find the right nodes (high recall matters more than savings on tiny fixtures)
    engram_m = [m for m in metrics if m.method == "engram"][0]
    assert engram_m.recall >= 0.5  # At least half of expected nodes found
    assert engram_m.precision > 0  # Non-zero precision


def test_benchmark_reporter(v4_project, tmp_path):
    store, _, project = v4_project
    from engram.bench import BenchmarkRunner, BenchmarkEvaluator, BenchmarkReporter
    from engram.retriever.assembler import ContextAssembler

    assembler = ContextAssembler(store, project_root=project)
    runner = BenchmarkRunner(store, assembler)
    evaluator = BenchmarkEvaluator()
    reporter = BenchmarkReporter()

    task = {
        "id": "test-task",
        "prompt": "fix save_order logic",
        "seeds": ["repository.py::save_order"],
        "expected_affected": [{"node": "repository.py::save_order", "reason": "seed"}],
        "expected_excluded": [],
    }

    results = runner.run_task(task)
    evals = evaluator.evaluate(results, task)

    output = tmp_path / "bench_output"
    report = reporter.generate_report([evals], [task], output)

    assert (output / "report.md").exists()
    assert (output / "results.json").exists()
    assert "Engram" in report


# --- Summarizer ---

def test_summarizer_should_skip_trivial(v4_project):
    store, _, _ = v4_project
    from engram.summarizer import LLMSummarizer

    summarizer = LLMSummarizer()
    node = store.get_node("utils.py::format_currency")
    # format_currency has a docstring and is short — should be skipped
    assert node is not None
    result = summarizer._should_summarize(node)
    # It's short (< 5 lines), so should be skipped
    assert result is False


def test_summarizer_should_summarize_complex(v4_project):
    store, _, _ = v4_project
    from engram.summarizer import LLMSummarizer

    summarizer = LLMSummarizer()
    node = store.get_node("service.py::process_order")
    assert node is not None
    # process_order is > 5 lines, has a docstring but let's check
    lines = node.full_source.splitlines()
    if len(lines) >= 5 and (not node.docstring or len(node.docstring) <= 50):
        assert summarizer._should_summarize(node) is True
