"""LLM-enhanced summaries for code nodes. Optional — requires API key."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from engram.graph.store import GraphStore
from engram.indexer.extractor import NodeRecord


SUMMARY_PROMPT = """Summarize this code in 1-3 sentences. Focus on behavior, dependencies, and non-obvious details. Be specific, not generic. If the docstring already explains it well, keep the summary very short.

Signature: {signature}
Docstring: {docstring}
Source:
{source}

Summary:"""


@dataclass
class SummarizeResult:
    total: int
    skipped: int
    summarized: int
    failed: int
    cost_estimate: float


class LLMSummarizer:
    """Generate LLM-enhanced summaries for code nodes."""

    def __init__(self, provider: str = "anthropic", model: str | None = None):
        self.provider = provider
        self.model = model or ("claude-sonnet-4-20250514" if provider == "anthropic" else "gpt-4o-mini")

    def summarize_node(self, node: NodeRecord) -> str | None:
        """Generate a summary for a single node. Returns None on failure."""
        if not self._should_summarize(node):
            return None

        prompt = SUMMARY_PROMPT.format(
            signature=node.signature or node.name,
            docstring=node.docstring or "(none)",
            source=node.full_source[:2000],
        )

        try:
            if self.provider == "anthropic":
                return self._call_anthropic(prompt)
            elif self.provider == "openai":
                return self._call_openai(prompt)
        except Exception:
            return None

    def _should_summarize(self, node: NodeRecord) -> bool:
        """Decide if this node needs an LLM summary."""
        if node.kind == "FILE":
            return False
        if node.name.startswith("test_") or node.name.startswith("Test"):
            return False
        # Skip if good docstring exists
        if node.docstring and len(node.docstring) > 50:
            return False
        # Skip trivial functions
        if node.full_source and len(node.full_source.splitlines()) < 5:
            return False
        # Skip __init__ with only assignments
        if node.name.endswith(".__init__"):
            lines = [l.strip() for l in node.full_source.splitlines() if l.strip() and not l.strip().startswith("#")]
            if all("self." in l and "=" in l for l in lines[1:]):  # Skip def line
                return False
        return True

    def _call_anthropic(self, prompt: str) -> str:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def _call_openai(self, prompt: str) -> str:
        import openai
        client = openai.OpenAI()
        response = client.chat.completions.create(
            model=self.model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()


class BatchSummarizer:
    """Batch LLM summarization for a whole project."""

    def __init__(self, store: GraphStore, summarizer: LLMSummarizer):
        self.store = store
        self.summarizer = summarizer

    def summarize_project(self, force: bool = False) -> SummarizeResult:
        """Generate LLM summaries for all eligible nodes."""
        all_nodes = self.store.get_all_nodes()
        total = 0
        skipped = 0
        summarized = 0
        failed = 0

        for node_id, node in all_nodes.items():
            if node.kind == "FILE":
                continue
            total += 1

            if not force:
                # Check if already has LLM summary
                row = self.store.conn.execute(
                    "SELECT summary_source FROM nodes WHERE id = ?", (node_id,)
                ).fetchone()
                if row and row[0] == "llm":
                    skipped += 1
                    continue

            summary = self.summarizer.summarize_node(node)
            if summary is None:
                skipped += 1
                continue

            # Update node summary
            self.store.conn.execute(
                "UPDATE nodes SET summary = ? WHERE id = ?",
                (summary, node_id),
            )
            summarized += 1

            # Rate limiting
            if summarized % 10 == 0:
                self.store.conn.commit()
                time.sleep(0.5)

        self.store.conn.commit()

        # Rough cost estimate
        avg_input = 200  # tokens per node
        avg_output = 50
        if self.summarizer.provider == "anthropic":
            cost = summarized * (avg_input * 0.003 + avg_output * 0.015) / 1000
        else:
            cost = summarized * (avg_input * 0.00015 + avg_output * 0.0006) / 1000

        return SummarizeResult(
            total=total, skipped=skipped, summarized=summarized,
            failed=failed, cost_estimate=round(cost, 4),
        )
