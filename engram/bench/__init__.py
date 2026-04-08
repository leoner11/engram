"""Benchmarking suite: measure token savings, retrieval precision, and verification accuracy."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from engram.graph.store import GraphStore
from engram.retriever.assembler import ContextAssembler, _estimate_tokens


@dataclass
class BenchmarkResult:
    task_id: str
    method: str
    context_tokens: int
    included_nodes: set[str]
    time_ms: int


@dataclass
class EvalMetrics:
    method: str
    tokens: int
    savings: float  # vs raw baseline
    precision: float
    recall: float
    f1: float
    noise: float
    time_ms: int


def load_task(task_path: Path) -> dict:
    """Load a benchmark task YAML file."""
    return yaml.safe_load(task_path.read_text())


# --- Baselines ---

class RawBaseline:
    """Include full source of all files related to seeds."""

    def assemble(self, seeds: list[str], store: GraphStore) -> tuple[str, set[str]]:
        files = set()
        for sid in seeds:
            node = store.get_node(sid)
            if node:
                files.add(node.file_path)

        content_parts = []
        included = set()
        for fp in sorted(files):
            nodes = store.get_nodes_by_file(fp)
            for n in nodes:
                if n.kind != "FILE":
                    content_parts.append(n.full_source)
                    included.add(n.id)

        return "\n\n".join(content_parts), included


class LSPBaseline:
    """One-hop from seeds, no change-type awareness, full detail."""

    def assemble(self, seeds: list[str], store: GraphStore, budget: int = 16000) -> tuple[str, set[str]]:
        included = set(seeds)
        for sid in seeds:
            for edge in store.get_edges_from(sid):
                included.add(edge.target_id)
            for edge in store.get_edges_to(sid):
                included.add(edge.source_id)

        parts = []
        tokens = 0
        result_ids = set()
        for nid in sorted(included):
            node = store.get_node(nid)
            if node and node.kind != "FILE":
                cost = _estimate_tokens(node.full_source)
                if tokens + cost <= budget:
                    parts.append(node.full_source)
                    result_ids.add(nid)
                    tokens += cost

        return "\n\n".join(parts), result_ids


class ClaudeMDBaseline:
    """Flat project summary — no task-adaptive retrieval."""

    def assemble(self, store: GraphStore, budget: int = 16000) -> tuple[str, set[str]]:
        from engram.export import export_brain
        brain = export_brain(store, format="md")
        # Truncate to budget
        if _estimate_tokens(brain) > budget:
            brain = brain[:budget * 4]  # Rough char estimate
        included = {n.id for n in store.get_all_nodes().values() if n.is_exported and n.kind != "FILE"}
        return brain, included


# --- Runner ---

class BenchmarkRunner:
    """Execute benchmark tasks across methods."""

    def __init__(self, store: GraphStore, assembler: ContextAssembler):
        self.store = store
        self.assembler = assembler

    def run_task(self, task: dict, budget: int = 16000) -> list[BenchmarkResult]:
        results = []
        seeds = task.get("seeds", [])
        prompt = task.get("prompt", "")
        task_id = task.get("id", "unknown")

        # Engram
        t0 = time.time()
        pkg = self.assembler.assemble(prompt=prompt, max_tokens=budget, seeds=seeds)
        t1 = time.time()
        engram_nodes = {cn.node.id for cn in pkg.nodes}
        results.append(BenchmarkResult(
            task_id=task_id, method="engram",
            context_tokens=pkg.total_tokens,
            included_nodes=engram_nodes,
            time_ms=int((t1 - t0) * 1000),
        ))

        # Raw baseline
        t0 = time.time()
        raw_text, raw_nodes = RawBaseline().assemble(seeds, self.store)
        t1 = time.time()
        results.append(BenchmarkResult(
            task_id=task_id, method="baseline_raw",
            context_tokens=_estimate_tokens(raw_text),
            included_nodes=raw_nodes,
            time_ms=int((t1 - t0) * 1000),
        ))

        # LSP baseline
        t0 = time.time()
        lsp_text, lsp_nodes = LSPBaseline().assemble(seeds, self.store, budget)
        t1 = time.time()
        results.append(BenchmarkResult(
            task_id=task_id, method="baseline_lsp",
            context_tokens=_estimate_tokens(lsp_text),
            included_nodes=lsp_nodes,
            time_ms=int((t1 - t0) * 1000),
        ))

        # CLAUDE.md baseline
        t0 = time.time()
        cmd_text, cmd_nodes = ClaudeMDBaseline().assemble(self.store, budget)
        t1 = time.time()
        results.append(BenchmarkResult(
            task_id=task_id, method="baseline_claudemd",
            context_tokens=_estimate_tokens(cmd_text),
            included_nodes=cmd_nodes,
            time_ms=int((t1 - t0) * 1000),
        ))

        return results


# --- Evaluator ---

class BenchmarkEvaluator:
    """Evaluate benchmark results against ground truth."""

    def evaluate(self, results: list[BenchmarkResult], task: dict) -> list[EvalMetrics]:
        raw_expected = task.get("expected_affected", [])
        if raw_expected and isinstance(raw_expected[0], dict):
            expected = {e["node"] for e in raw_expected}
        else:
            expected = set(raw_expected)

        raw_excluded = task.get("expected_excluded", [])
        excluded = set()
        for e in raw_excluded:
            excluded.add(e["node"] if isinstance(e, dict) else e)

        raw_tokens = max((r.context_tokens for r in results if r.method == "baseline_raw"), default=1)

        metrics = []
        for r in results:
            included = r.included_nodes
            tp = len(included & expected)
            precision = tp / len(included) if included else 0
            recall = tp / len(expected) if expected else 0
            f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0
            noise = len(included & excluded) / len(included) if included else 0
            savings = 1 - (r.context_tokens / raw_tokens) if raw_tokens > 0 else 0

            metrics.append(EvalMetrics(
                method=r.method, tokens=r.context_tokens,
                savings=round(savings, 3), precision=round(precision, 3),
                recall=round(recall, 3), f1=round(f1, 3),
                noise=round(noise, 3), time_ms=r.time_ms,
            ))

        return metrics


# --- Reporter ---

class BenchmarkReporter:
    """Generate benchmark reports."""

    def generate_report(self, all_evals: list[list[EvalMetrics]], tasks: list[dict], output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)

        lines = ["# Engram Benchmark Report", ""]

        # Summary table
        lines.append("## Results by Method")
        lines.append("")
        lines.append("| Task | Method | Tokens | Savings | Precision | Recall | F1 | Noise | Time |")
        lines.append("|------|--------|--------|---------|-----------|--------|-----|-------|------|")

        for task, evals in zip(tasks, all_evals):
            task_id = task.get("id", "?")
            for m in evals:
                lines.append(
                    f"| {task_id} | {m.method} | {m.tokens} | "
                    f"{m.savings:.0%} | {m.precision:.0%} | {m.recall:.0%} | "
                    f"{m.f1:.2f} | {m.noise:.0%} | {m.time_ms}ms |"
                )

        lines.append("")

        # Aggregate
        methods = {}
        for evals in all_evals:
            for m in evals:
                methods.setdefault(m.method, []).append(m)

        lines.append("## Aggregate")
        lines.append("")
        for method, ms in methods.items():
            avg_savings = sum(m.savings for m in ms) / len(ms)
            avg_precision = sum(m.precision for m in ms) / len(ms)
            avg_recall = sum(m.recall for m in ms) / len(ms)
            avg_f1 = sum(m.f1 for m in ms) / len(ms)
            lines.append(f"**{method}**: savings={avg_savings:.0%}, precision={avg_precision:.0%}, "
                         f"recall={avg_recall:.0%}, F1={avg_f1:.2f}")
        lines.append("")

        report = "\n".join(lines)
        (output_dir / "report.md").write_text(report)

        # JSON results
        json_data = []
        for task, evals in zip(tasks, all_evals):
            json_data.append({
                "task": task.get("id"),
                "results": [{"method": m.method, "tokens": m.tokens, "savings": m.savings,
                             "precision": m.precision, "recall": m.recall, "f1": m.f1,
                             "noise": m.noise, "time_ms": m.time_ms} for m in evals],
            })
        (output_dir / "results.json").write_text(json.dumps(json_data, indent=2))

        return report
