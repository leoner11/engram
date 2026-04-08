"""Context assembly pipeline: prompt → task-adaptive, token-budgeted context package."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from engram.db import EngramDB
from engram.graph.activation import ChangeType
from engram.graph.store import GraphStore
from engram.graph.traversal import AffectedNode, GraphTraversal
from engram.indexer.extractor import NodeRecord
from engram.indexer.resolver import Edge
from engram.retriever.anticipation import anticipate_change_types
from engram.retriever.excerpt import extract_excerpt
from engram.retriever.seeds import SeedCandidate, SeedSelector, extract_prompt_terms


@dataclass
class ContextNode:
    node: NodeRecord
    detail_level: str       # "full" | "excerpt" | "signature" | "summary"
    content: str
    priority: float
    depth: int
    token_estimate: int
    reason: str = ""


@dataclass
class ContextMemory:
    observation_id: int
    title: str
    content: str
    type: str
    linked_nodes: list[str]
    token_estimate: int


@dataclass
class ContextConfidence:
    """Confidence assessment for the context package."""
    score: float                    # 0.0-1.0 overall confidence
    seed_quality: float             # % of seeds from real FTS matches vs fallback
    budget_utilization: float       # % of token budget actually used
    seed_clustering: float          # how connected the seeds are to each other
    directory_coverage: float       # % of project directories represented
    missing_directories: list[str]  # directories NOT in results but in project
    warnings: list[str]             # human-readable warnings for the agent


@dataclass
class ContextPackage:
    task: str
    change_types: set[str]
    seeds: list[str]
    nodes: list[ContextNode]
    total_tokens: int
    budget: int
    stats: dict = field(default_factory=dict)
    memories: list[ContextMemory] = field(default_factory=list)
    snapshot: str = ""
    confidence: ContextConfidence | None = None

    def serialize(self) -> str:
        """Serialize to structured markdown."""
        lines = []
        lines.append(f"# Task: {self.task}")

        mode = self.stats.get("mode", "maintenance")
        ct_str = ", ".join(sorted(self.change_types))
        lines.append(f"## Mode: {mode} | Anticipated changes: {ct_str}")
        seeds_str = ", ".join(self.seeds)
        lines.append(f"## Seeds: {seeds_str}")
        seed_scores = self.stats.get("seed_scores", [])
        if seed_scores:
            for s in seed_scores:
                lines.append(f"  - {s['node_id']} (score: {s['score']}, match: {s['reason']})")
        lines.append(f"## Token budget: {self.budget} (used: {self.total_tokens})")
        included = self.stats.get("nodes_included", 0)
        excluded = self.stats.get("nodes_excluded", 0)
        lines.append(f"## Nodes: {included} included, {excluded} excluded")
        lines.append("")

        # Confidence assessment FIRST — decision-critical info before any code
        if self.confidence:
            c = self.confidence
            pct = int(c.score * 100)
            if pct >= 80:
                recommendation = "PROCEED"
            elif pct >= 55:
                recommendation = "VERIFY_GAPS"
            else:
                recommendation = "SKIP_AND_READ"
            lines.append(f"## Context Confidence: {pct}% | Recommendation: {recommendation}")
            if c.warnings:
                for w in c.warnings:
                    lines.append(f"- ⚠ {w}")
            if c.missing_directories:
                dirs = ", ".join(c.missing_directories[:5])
                lines.append(f"- Directories not in context: {dirs}")
            lines.append("")

        # Snapshot — project identity
        if self.snapshot:
            lines.append("## Project Context")
            lines.append(self.snapshot)
            lines.append("")

        # Memories — past decisions and discoveries
        if self.memories:
            lines.append("## Relevant History")
            lines.append("")
            for mem in self.memories:
                lines.append(f"#### [{mem.type}] {mem.title}")
                lines.append(mem.content)
                if mem.linked_nodes:
                    lines.append(f"Linked to: {', '.join(mem.linked_nodes)}")
                lines.append("")

        # Group nodes by file
        by_file: dict[str, list[ContextNode]] = {}
        for cn in self.nodes:
            by_file.setdefault(cn.node.file_path, []).append(cn)

        for file_path in sorted(by_file):
            lines.append(f"### File: {file_path}")
            lines.append("")

            for cn in sorted(by_file[file_path], key=lambda c: -c.priority):
                tag = f"[{cn.detail_level}]"
                depth_tag = f"(depth: {cn.depth})"
                lines.append(f"#### {cn.node.id} {tag} {depth_tag}")
                if cn.reason:
                    lines.append(f"# Reason: {cn.reason}")
                lines.append(f"```python")
                lines.append(cn.content)
                lines.append(f"```")
                lines.append("")

        return "\n".join(lines)

    def serialize_worker(self) -> str:
        """
        Serialize for worker agents (Haiku, Gemini, cheaper models).

        Strips all Engram internals — no depth labels, no edge reasons, no seed scores,
        no confidence metadata. Outputs clean code with plain English context
        that any model can act on without knowing what Engram is.

        Budget is tighter by design: worker gets code + task instructions,
        not framework metadata.
        """
        lines = []

        # Plain task header — no Engram jargon
        lines.append(f"# Task: {self.task}")
        lines.append("")

        # Relevant history — workers benefit from past decisions
        if self.memories:
            lines.append("## Relevant context from project history")
            lines.append("")
            for mem in self.memories:
                lines.append(f"**{mem.title}** ({mem.type})")
                lines.append(mem.content)
                lines.append("")

        # Group nodes — seeds first, then dependents
        seed_set = set(self.seeds)
        seed_nodes = [cn for cn in self.nodes if cn.node.id in seed_set]
        dependent_nodes = [cn for cn in self.nodes if cn.node.id not in seed_set]

        if seed_nodes:
            lines.append("## Files to modify")
            lines.append("")
            by_file: dict[str, list[ContextNode]] = {}
            for cn in seed_nodes:
                by_file.setdefault(cn.node.file_path, []).append(cn)
            for file_path in sorted(by_file):
                lines.append(f"### {file_path}")
                lines.append("")
                for cn in sorted(by_file[file_path], key=lambda c: -c.priority):
                    lines.append(f"#### `{cn.node.name}`")
                    lines.append(f"```python")
                    lines.append(cn.content)
                    lines.append(f"```")
                    lines.append("")

        if dependent_nodes:
            lines.append("## Files that may need updating")
            lines.append("")
            lines.append("These call or depend on the code above. Check if your changes affect them.")
            lines.append("")
            dep_by_file: dict[str, list[ContextNode]] = {}
            for cn in dependent_nodes:
                dep_by_file.setdefault(cn.node.file_path, []).append(cn)
            for file_path in sorted(dep_by_file):
                lines.append(f"### {file_path}")
                lines.append("")
                for cn in sorted(dep_by_file[file_path], key=lambda c: -c.priority):
                    # Plain English reason derived from internal reason string
                    plain_reason = _plain_reason(cn.reason)
                    lines.append(f"#### `{cn.node.name}` — {plain_reason}")
                    lines.append(f"```python")
                    lines.append(cn.content)
                    lines.append(f"```")
                    lines.append("")

        lines.append("## Instructions")
        lines.append("- Make only the changes needed for the task above")
        lines.append("- Output a unified diff or the complete modified functions")
        lines.append("- Do not modify files outside the sections listed above")

        return "\n".join(lines)


def _plain_reason(reason: str) -> str:
    """Convert internal Engram reason strings to plain English for worker output."""
    if not reason:
        return "may be affected"
    r = reason.lower()
    if "calls" in r:
        # "affected — calls serialize" → "calls this function"
        match = reason.split("calls ")[-1] if "calls " in reason else ""
        return f"calls `{match}`" if match else "calls modified code"
    if "via bridge" in r:
        return "connected via cross-language bridge"
    if "import" in r:
        return "imports modified code"
    if "extends" in r or "uses_type" in r:
        return "uses this type"
    if "transitive" in r:
        return "indirectly depends on modified code"
    if "seed" in r:
        return "target of this task"
    return "may be affected by this change"


def _estimate_tokens(text: str) -> int:
    """Estimate token count. len/4 heuristic, or tiktoken if available."""
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model("cl100k_base")
        return len(enc.encode(text))
    except (ImportError, Exception):
        return len(text) // 4


class ContextAssembler:
    """The main context assembly pipeline.

    Dual-mode:
    - Maintenance mode (existing): seeds → traverse → detail levels → budget → memory
    - Creation mode (v5): snapshot → memory → conventions → graph (if seeds exist)
    """

    def __init__(self, store: GraphStore, project_root: Path | None = None,
                 memory_search=None, pattern_matcher=None,
                 convention_finder=None, snapshot_gen=None):
        self.store = store
        self.project_root = project_root
        self.traversal = GraphTraversal(store)
        self.memory_search = memory_search  # Optional: MemorySearch instance for v1+
        self.pattern_matcher = pattern_matcher  # Optional: PatternMatcher for v4+
        self.convention_finder = convention_finder  # Optional: ConventionFinder for v5+
        self.snapshot_gen = snapshot_gen  # Optional: SnapshotGenerator for v5+

    def _ensure_fresh(self):
        """Validate graph freshness before querying. Re-indexes changed files.

        Compares manifest hashes against current file hashes. For any
        changed/new/deleted files, re-parses with tree-sitter and updates
        the graph in-place. Typically takes <500ms for a few changed files.
        """
        if self.project_root is None:
            return

        try:
            from engram.indexer.scanner import scan_project
            from engram.indexer.hasher import hash_file
            from engram.indexer.languages import get_all_extensions, detect_language
            from engram.indexer.parser import TreeSitterParser
            from engram.indexer.resolver import Resolver
            from engram.retriever.seeds import populate_node_index

            extensions = get_all_extensions()
            files = scan_project(self.project_root, extensions)

            # Build current hash map
            current_hashes: dict[str, str] = {}
            for rel_path in files:
                abs_path = self.project_root / rel_path
                current_hashes[str(rel_path)] = hash_file(abs_path)

            # Compare against manifest
            changed, new, deleted = self.store.get_stale_files(current_hashes)

            if not changed and not new and not deleted:
                return  # Graph is fresh

            # Re-index only what changed
            parser = TreeSitterParser()
            all_nodes = self.store.get_all_nodes()
            all_raw_edges = []
            files_updated = 0

            for file_str in changed + new:
                rel_path = Path(file_str)
                abs_path = self.project_root / rel_path
                language = detect_language(rel_path)
                if language is None:
                    continue

                # Clean old data
                self.store.delete_edges_by_file(file_str)
                self.store.delete_nodes_by_file(file_str)

                # Re-parse
                try:
                    tree = parser.parse_file(abs_path, language)
                    source = abs_path.read_bytes()

                    from engram.cli import _get_adapter
                    adapter = _get_adapter(language)
                    if adapter is None:
                        continue

                    nodes, raw_edges = adapter.extract(tree, source, file_str)

                    for node in nodes:
                        self.store.upsert_node(node)
                        all_nodes[node.id] = node

                    all_raw_edges.extend(raw_edges)
                    self.store.update_manifest(file_str, current_hashes[file_str], len(nodes))
                    files_updated += 1
                except Exception:
                    continue

            # Clean deleted files
            for file_str in deleted:
                self.store.delete_edges_by_file(file_str)
                self.store.delete_nodes_by_file(file_str)
                self.store.delete_manifest(file_str)

            # Re-resolve edges for changed files
            if all_raw_edges:
                # Need all existing nodes for cross-file resolution
                existing = self.store.get_all_nodes()
                for nid, node in existing.items():
                    if nid not in all_nodes:
                        all_nodes[nid] = node

                resolver = Resolver(all_nodes, all_raw_edges, self.project_root)
                for edge in resolver.resolve_all():
                    self.store.upsert_edge(edge)

            # Rebuild FTS5 index if anything changed
            if files_updated > 0 or deleted:
                populate_node_index(self.store)

            # Rebuild bridges if config exists
            from engram.bridges import build_bridges
            build_bridges(self.project_root, self.store)

            self.store.commit()

        except Exception:
            pass  # Freshness check is best-effort — don't block the query

    def assemble(
        self,
        prompt: str,
        max_tokens: int = 16000,
        change_hints: list[str] | None = None,
        change_type: str | None = None,
        seeds: list[str] | None = None,
    ) -> ContextPackage:
        """
        Full pipeline:
        0. Freshness validation
        0.5. Snapshot (pre-allocated, always included)
        1. Seed selection
        2. Change type anticipation
        2.5. Mode detection (creation vs maintenance)
        3-6. Delegated to mode-specific assembly
        """
        # Step 0: Ensure graph is fresh
        self._ensure_fresh()

        # Step 0.5: Snapshot (always, pre-allocated)
        snapshot_content = ""
        snapshot_tokens = 0
        if self.snapshot_gen:
            try:
                snapshot_content = self.snapshot_gen.get_or_generate()
                snapshot_tokens = _estimate_tokens(snapshot_content)
            except Exception:
                pass  # Snapshot is best-effort

        effective_budget = max_tokens - snapshot_tokens

        # Step 1: Seed selection
        selector = SeedSelector(self.store)
        seed_candidates = selector.select(prompt, explicit_seeds=seeds)

        # Step 2: Change type anticipation
        anticipated = anticipate_change_types(prompt, change_hints, change_type)

        # Step 2.5: Mode detection
        is_creation = self._is_creation_mode(anticipated, seed_candidates)

        if is_creation:
            return self._assemble_creation_mode(
                prompt, max_tokens, effective_budget, anticipated,
                seed_candidates, snapshot_content, snapshot_tokens,
            )

        # Record seed selection for future feedback (maintenance mode)
        if seed_candidates:
            terms = extract_prompt_terms(prompt)
            selector.record_selection(prompt, terms, seed_candidates)

        return self._assemble_maintenance_mode(
            prompt, max_tokens, effective_budget, anticipated,
            seed_candidates, snapshot_content, snapshot_tokens,
            change_hints, change_type,
        )

    def _is_creation_mode(
        self,
        anticipated: set[ChangeType],
        seed_candidates: list[SeedCandidate],
    ) -> bool:
        """Detect if this query is creation-mode (new feature, weak seeds)."""
        if ChangeType.ADDITION not in anticipated:
            return False

        # Strong explicit seeds → not creation mode
        if any(s.match_reason == "explicit" for s in seed_candidates):
            return False

        # All seeds from fallback → creation mode
        if not seed_candidates or all(
            s.match_reason == "top_connected_fallback" for s in seed_candidates
        ):
            return True

        # Pure ADDITION with any seeds → creation mode
        # (even if FTS found something, the task is about creating, not modifying)
        if anticipated == {ChangeType.ADDITION}:
            return True

        return False

    def _assemble_creation_mode(
        self,
        prompt: str,
        total_budget: int,
        effective_budget: int,
        anticipated: set[ChangeType],
        seed_candidates: list[SeedCandidate],
        snapshot_content: str,
        snapshot_tokens: int,
    ) -> ContextPackage:
        """Assembly path optimized for building new features.

        Priority order:
        1. Snapshot (already allocated)
        2. Memory — past decisions, architecture notes, relevant discoveries
        3. Convention examples — "here's an existing thing like what you're building"
        4. Graph traversal — only if we have real seeds
        """
        tokens_used = snapshot_tokens
        context_nodes: list[ContextNode] = []
        memories: list[ContextMemory] = []

        # --- Phase 1: Memory (up to 40% of effective budget) ---
        memory_budget = int(effective_budget * 0.4)
        if self.memory_search is not None:
            mem_results = self.memory_search.search(
                query=prompt,
                limit=15,
                # No affected_node_ids — wide search for creation mode
            )
            mem_tokens = 0
            for mr in mem_results:
                content = mr.get("content", "")
                title = mr.get("title", "")
                token_cost = _estimate_tokens(title + "\n" + content)
                if mem_tokens + token_cost > memory_budget:
                    break

                links = self.store.conn.execute(
                    "SELECT node_id FROM observation_nodes WHERE observation_id = ?",
                    (mr["id"],),
                ).fetchall()
                linked = [l["node_id"] for l in links]

                memories.append(ContextMemory(
                    observation_id=mr["id"],
                    title=title,
                    content=content,
                    type=mr.get("type", ""),
                    linked_nodes=linked,
                    token_estimate=token_cost,
                ))
                mem_tokens += token_cost

            tokens_used += mem_tokens

        # --- Phase 2: Convention examples (up to 30% of effective budget) ---
        convention_budget = int(effective_budget * 0.3)
        if self.convention_finder:
            try:
                conventions = self.convention_finder.find_siblings(
                    prompt=prompt,
                    seed_candidates=seed_candidates,
                    limit=5,
                )
            except Exception:
                conventions = []

            conv_tokens = 0
            for i, conv_node in enumerate(conventions):
                # First convention at full detail, rest at signature
                if i == 0 and conv_node.full_source:
                    detail = "full"
                    content = conv_node.full_source
                else:
                    detail = "signature"
                    content = conv_node.signature or conv_node.name

                token_cost = _estimate_tokens(content)
                if conv_tokens + token_cost > convention_budget:
                    # Try downgrading first one to signature if it's too big
                    if i == 0 and detail == "full":
                        detail = "signature"
                        content = conv_node.signature or conv_node.name
                        token_cost = _estimate_tokens(content)
                        if conv_tokens + token_cost > convention_budget:
                            break
                    else:
                        break

                context_nodes.append(ContextNode(
                    node=conv_node,
                    detail_level=detail,
                    content=content,
                    priority=500.0 - i * 50,
                    depth=0,
                    token_estimate=token_cost,
                    reason="convention example — follow this pattern" if i == 0
                           else "convention — similar existing code",
                ))
                conv_tokens += token_cost

            tokens_used += conv_tokens

        # --- Phase 3: Graph traversal with remaining budget ---
        real_seeds = [s for s in seed_candidates
                      if s.match_reason != "top_connected_fallback"]
        remaining = effective_budget - (tokens_used - snapshot_tokens)

        if real_seeds and remaining > 500:
            seed_ids = [s.node_id for s in real_seeds[:3]]
            affected = self.traversal.traverse(
                seed_ids, anticipated, max_depth=1,
            )

            for affected_node in affected:
                if affected_node.depth == 0:
                    # Skip seeds that are already in convention examples
                    if any(cn.node.id == affected_node.node_id for cn in context_nodes):
                        continue
                node = self.store.get_node(affected_node.node_id)
                if node is None:
                    continue

                content = node.signature or node.name
                token_cost = _estimate_tokens(content)
                if tokens_used + token_cost > total_budget:
                    break

                context_nodes.append(ContextNode(
                    node=node,
                    detail_level="signature",
                    content=content,
                    priority=affected_node.priority,
                    depth=affected_node.depth,
                    token_estimate=token_cost,
                    reason="graph neighbor — may need integration",
                ))
                tokens_used += token_cost

        # Compute confidence assessment
        confidence = self._compute_confidence(
            seed_candidates, context_nodes, tokens_used,
            total_budget,
        )

        return ContextPackage(
            task=prompt,
            change_types={ct.value for ct in anticipated},
            seeds=[s.node_id for s in seed_candidates],
            nodes=context_nodes,
            total_tokens=tokens_used,
            budget=total_budget,
            stats={
                "mode": "creation",
                "nodes_included": len(context_nodes),
                "nodes_excluded": 0,
                "memories_included": len(memories),
                "convention_examples": len([n for n in context_nodes
                                            if "convention" in n.reason]),
                "seed_scores": [
                    {"node_id": s.node_id, "score": round(s.score, 1), "reason": s.match_reason}
                    for s in seed_candidates
                ],
            },
            memories=memories,
            snapshot=snapshot_content,
            confidence=confidence,
        )

    def _assemble_maintenance_mode(
        self,
        prompt: str,
        total_budget: int,
        effective_budget: int,
        anticipated: set[ChangeType],
        seed_candidates: list[SeedCandidate],
        snapshot_content: str,
        snapshot_tokens: int,
        change_hints: list[str] | None = None,
        change_type: str | None = None,
    ) -> ContextPackage:
        """Original assembly path: graph-first, memory as supplement."""
        if not seed_candidates:
            return ContextPackage(
                task=prompt,
                change_types={ct.value for ct in anticipated},
                seeds=[],
                nodes=[],
                total_tokens=snapshot_tokens,
                budget=total_budget,
                stats={
                    "mode": "maintenance",
                    "nodes_included": 0, "nodes_excluded": 0,
                    "warning": "No seed nodes found",
                },
                snapshot=snapshot_content,
            )

        seed_ids = [s.node_id for s in seed_candidates]

        # Get pattern-implied implicit edges
        extra_edges = []
        if self.pattern_matcher:
            try:
                extra_edges = self.pattern_matcher.get_implicit_edges()
            except Exception:
                pass

        # Graph traversal
        affected = self.traversal.traverse(
            seed_ids, anticipated, max_depth=2,
            extra_edges=extra_edges or None,
        )

        # Detail level assignment + token budgeting
        context_nodes: list[ContextNode] = []
        tokens_used = snapshot_tokens
        nodes_excluded = 0

        for affected_node in affected:
            node = self.store.get_node(affected_node.node_id)
            if node is None:
                continue

            detail, content, reason = self._assign_detail(
                node, affected_node, seed_ids, seed_candidates
            )

            token_cost = _estimate_tokens(content)

            # Budget check with downgrade chain
            if tokens_used + token_cost > total_budget:
                detail, content = self._downgrade(node, detail)
                token_cost = _estimate_tokens(content)
                if tokens_used + token_cost > total_budget:
                    detail, content = self._downgrade(node, detail)
                    token_cost = _estimate_tokens(content)
                    if tokens_used + token_cost > total_budget:
                        nodes_excluded += 1
                        continue

            context_nodes.append(ContextNode(
                node=node,
                detail_level=detail,
                content=content,
                priority=affected_node.priority,
                depth=affected_node.depth,
                token_estimate=token_cost,
                reason=reason,
            ))
            tokens_used += token_cost

        # Memory retrieval (v1 behavior, with node-boost)
        memories: list[ContextMemory] = []
        if self.memory_search is not None:
            affected_ids = {a.node_id for a in affected}
            memory_budget = self._allocate_memory_budget(total_budget, tokens_used)

            if memory_budget > 0:
                mem_results = self.memory_search.search(
                    query=prompt,
                    affected_node_ids=affected_ids,
                    limit=10,
                )
                mem_tokens = 0
                for mr in mem_results:
                    content = mr.get("content", "")
                    title = mr.get("title", "")
                    token_cost = _estimate_tokens(title + "\n" + content)
                    if mem_tokens + token_cost > memory_budget:
                        break

                    links = self.store.conn.execute(
                        "SELECT node_id FROM observation_nodes WHERE observation_id = ?",
                        (mr["id"],),
                    ).fetchall()
                    linked = [l["node_id"] for l in links]

                    memories.append(ContextMemory(
                        observation_id=mr["id"],
                        title=title,
                        content=content,
                        type=mr.get("type", ""),
                        linked_nodes=linked,
                        token_estimate=token_cost,
                    ))
                    mem_tokens += token_cost
                    tokens_used += token_cost

        # Compute confidence assessment
        confidence = self._compute_confidence(
            seed_candidates, context_nodes, tokens_used, total_budget,
        )

        return ContextPackage(
            task=prompt,
            change_types={ct.value for ct in anticipated},
            seeds=seed_ids,
            nodes=context_nodes,
            total_tokens=tokens_used,
            budget=total_budget,
            stats={
                "mode": "maintenance",
                "nodes_included": len(context_nodes),
                "nodes_excluded": nodes_excluded,
                "files_touched": len({cn.node.file_path for cn in context_nodes}),
                "memories_included": len(memories),
                "seed_scores": [
                    {"node_id": s.node_id, "score": round(s.score, 1), "reason": s.match_reason}
                    for s in seed_candidates
                ],
            },
            memories=memories,
            snapshot=snapshot_content,
            confidence=confidence,
        )

    def _allocate_memory_budget(self, total_budget: int, code_tokens_used: int) -> int:
        """Memory gets min(20% of total budget, 3000 tokens, remaining budget)."""
        max_memory = min(total_budget * 0.2, 3000)
        remaining = total_budget - code_tokens_used
        return int(min(max_memory, max(remaining, 0)))

    def _compute_confidence(
        self,
        seed_candidates: list[SeedCandidate],
        context_nodes: list[ContextNode],
        tokens_used: int,
        total_budget: int,
    ) -> ContextConfidence:
        """Compute confidence assessment for the context package.

        Composite score from:
        1. Seed quality — FTS/filepath matches vs fallback
        2. Budget utilization — how much of budget was filled
        3. Seed clustering — are seeds connected to each other
        4. Directory coverage — what % of project dirs are represented
        """
        warnings: list[str] = []

        # 1. Seed quality: % of seeds from real matches
        if seed_candidates:
            real_seeds = sum(
                1 for s in seed_candidates
                if s.match_reason not in ("top_connected_fallback",)
            )
            seed_quality = real_seeds / len(seed_candidates)
        else:
            seed_quality = 0.0

        if seed_quality == 0.0:
            warnings.append(
                "All seeds are fallback (no FTS match) — context may be unfocused"
            )
        elif seed_quality < 0.5:
            warnings.append(
                f"Only {int(seed_quality*100)}% of seeds matched by name — "
                "some context may be irrelevant"
            )

        # 2. Budget utilization
        budget_util = tokens_used / total_budget if total_budget > 0 else 0.0
        # Very low utilization suggests thin results
        if budget_util < 0.15 and total_budget > 4000:
            warnings.append(
                f"Only {int(budget_util*100)}% of budget used — "
                "graph found little relevant code"
            )

        # 3. Seed clustering: check if seeds share edges within 2 hops
        seed_ids = [s.node_id for s in seed_candidates
                    if s.match_reason != "top_connected_fallback"]
        if len(seed_ids) >= 2:
            connected_pairs = 0
            total_pairs = 0
            for i, sid1 in enumerate(seed_ids):
                neighbors_1 = set()
                for edge in self.store.get_edges_from(sid1):
                    neighbors_1.add(edge.target_id)
                for edge in self.store.get_edges_to(sid1):
                    neighbors_1.add(edge.source_id)

                for sid2 in seed_ids[i+1:]:
                    total_pairs += 1
                    if sid2 in neighbors_1:
                        connected_pairs += 1
                        continue
                    # Check 2-hop
                    neighbors_2 = set()
                    for edge in self.store.get_edges_from(sid2):
                        neighbors_2.add(edge.target_id)
                    for edge in self.store.get_edges_to(sid2):
                        neighbors_2.add(edge.source_id)
                    if neighbors_1 & neighbors_2:
                        connected_pairs += 1

            clustering = connected_pairs / total_pairs if total_pairs > 0 else 1.0
        else:
            clustering = 1.0  # single seed = trivially clustered

        if clustering < 0.3 and len(seed_ids) >= 2:
            warnings.append(
                "Seeds are in different subsystems — context may be scattered"
            )

        # 4. Directory coverage
        all_nodes = self.store.get_all_nodes()
        project_dirs: set[str] = set()
        for node in all_nodes.values():
            if node.kind == "FILE":
                continue
            parts = node.file_path.split("/")
            if len(parts) > 1:
                project_dirs.add(parts[0])

        context_dirs: set[str] = set()
        for cn in context_nodes:
            parts = cn.node.file_path.split("/")
            if len(parts) > 1:
                context_dirs.add(parts[0])

        if project_dirs:
            dir_coverage = len(context_dirs & project_dirs) / len(project_dirs)
        else:
            dir_coverage = 1.0

        missing_dirs = sorted(project_dirs - context_dirs)

        # Composite score (weighted average)
        score = (
            seed_quality * 0.35 +
            min(budget_util * 2, 1.0) * 0.20 +  # cap at 1.0, penalize < 50%
            clustering * 0.25 +
            dir_coverage * 0.20
        )
        score = max(0.0, min(1.0, score))

        return ContextConfidence(
            score=score,
            seed_quality=seed_quality,
            budget_utilization=budget_util,
            seed_clustering=clustering,
            directory_coverage=dir_coverage,
            missing_directories=missing_dirs,
            warnings=warnings,
        )

    def _assign_detail(
        self,
        node: NodeRecord,
        affected: AffectedNode,
        seed_ids: list[str],
        seed_candidates: list[SeedCandidate],
    ) -> tuple[str, str, str]:
        """Assign detail level and generate content for a node."""
        # Bridge label prefix — prepended to reason when traversed via API_BRIDGE
        bridge_prefix = ""
        if affected.reached_via == "API_BRIDGE":
            # Find which bridge connected this node
            bridge_edges = self.store.get_edges_to(node.id)
            bridge_name = next(
                (e.metadata.get("bridge_name", "bridge") for e in bridge_edges if e.kind == "API_BRIDGE"),
                "bridge",
            )
            bridge_prefix = f"[via bridge: {bridge_name}] "

        # Seeds always get full
        if affected.depth == 0:
            return "full", node.full_source, f"{bridge_prefix}seed node — directly referenced in prompt"

        # Depth 1 — try excerpt first for nodes that call the seed
        if affected.depth == 1:
            if affected.reached_via == "API_BRIDGE":
                # Bridge targets get full source so agent sees the connected file
                if node.full_source and len(node.full_source.splitlines()) < 150:
                    return "full", node.full_source, f"{bridge_prefix}included via cross-language bridge"
                return "signature", node.signature or node.name, f"{bridge_prefix}included via cross-language bridge"

            if affected.reached_via == "CALLS":
                # Try to find the edge to check direction
                for seed_id in seed_ids:
                    edges_to_seed = self.store.get_edges_from(node.id)
                    for edge in edges_to_seed:
                        if edge.target_id == seed_id and edge.kind == "CALLS":
                            # This node calls the seed — try excerpt
                            seed_node = self.store.get_node(seed_id)
                            seed_name = seed_node.name if seed_node else seed_id
                            excerpt_text = extract_excerpt(node, seed_name, edge)
                            if excerpt_text:
                                return "excerpt", excerpt_text, f"affected — calls {seed_name}"

                # No excerpt possible — use full if small enough
                if node.full_source and len(node.full_source.splitlines()) < 100:
                    return "full", node.full_source, f"affected — {affected.reached_via} edge"
                return "signature", node.signature or node.name, f"affected — {affected.reached_via} edge"

            elif affected.reached_via in ("USES_TYPE", "EXTENDS"):
                return "signature", node.signature or node.name, f"affected — {affected.reached_via} edge"
            elif affected.reached_via == "IMPORTS":
                return "signature", node.signature or node.name, f"affected — import dependency"
            else:
                return "signature", node.signature or node.name, f"affected — {affected.reached_via} edge"

        # Depth 2+
        if affected.depth >= 2:
            return "signature", node.signature or node.name, f"transitive dependency (depth {affected.depth})"

        return "summary", node.summary, "context"

    def _downgrade(self, node: NodeRecord, current_level: str) -> tuple[str, str]:
        """Downgrade detail level: full → excerpt → signature → summary."""
        if current_level == "full":
            return "signature", node.signature or node.name
        elif current_level == "excerpt":
            return "signature", node.signature or node.name
        elif current_level == "signature":
            return "summary", node.summary
        else:
            return "summary", node.summary