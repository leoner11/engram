"""MCP tool definitions and handlers for Engram."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.server import Server
from mcp.types import Tool, TextContent

if TYPE_CHECKING:
    from engram.mcp.server import EngramMCPServer


TOOLS = [
    Tool(
        name="engram_query",
        description=(
            "Get task-adaptive context for a coding task. Returns structurally relevant "
            "code at appropriate detail levels (full source, excerpt, signature, summary) "
            "within a token budget. Uses change-type-aware graph traversal to find code "
            "that would be affected by the task. RECOMMENDED: pass seeds=[] with the "
            "node IDs of files/functions you're about to edit — auto-detection from "
            "prompt text is a fallback and may pick wrong starting points. Pass "
            "change_hints to describe what you're doing in plain English. "
            "Use output_mode='worker' to generate a clean context package for cheaper "
            "models (Haiku, Gemini, etc.) — strips Engram internals, outputs plain English."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What you're trying to do, e.g. 'fix the webhook renewal bug'",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Token budget (default: 16000 for agent mode, 8000 for worker mode)",
                },
                "change_hints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Natural language hints about what kind of change you're making, "
                        "e.g. ['renaming a function', 'adding a new field']. "
                        "Helps Engram retrieve more precisely relevant context."
                    ),
                },
                "seeds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Explicit seed node IDs to start traversal from, "
                        "e.g. ['src/api/webhooks.py::handle_stripe_webhook']. "
                        "If omitted, seeds are auto-detected from the prompt."
                    ),
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["agent", "worker"],
                    "default": "agent",
                    "description": (
                        "agent (default): full Engram output with confidence scores, "
                        "seed metadata, depth labels — for orchestrator models that "
                        "understand Engram. "
                        "worker: clean output with plain English context, no Engram "
                        "internals — pass this directly to cheaper models as their "
                        "task context."
                    ),
                },
            },
            "required": ["prompt"],
        },
    ),
    Tool(
        name="engram_search",
        description=(
            "Search project memories (observations, decisions, bugfixes, discoveries). "
            "Returns matching observations ranked by relevance. Use progressive mode "
            "to see titles first, then request full content for specific observations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — supports natural language",
                },
                "type": {
                    "type": "string",
                    "enum": ["decision", "bugfix", "architecture", "discovery", "preference", "issue"],
                    "description": "Filter by observation type",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 10)",
                    "default": 10,
                },
                "full": {
                    "type": "boolean",
                    "description": "If false (default), returns titles + snippets only. If true, returns full content.",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="engram_save",
        description=(
            "Store a project observation — a decision, bugfix, discovery, or other knowledge "
            "that should persist across sessions. Pass node_ids of code entities this relates "
            "to (recommended — you know what you just edited). If omitted, Engram will "
            "attempt to auto-link based on entity names mentioned in the text. "
            "Use topic_key for knowledge that evolves: same topic_key updates existing "
            "observation instead of creating duplicates."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short summary, e.g. 'Stripe webhook signatures differ in test mode'",
                },
                "content": {
                    "type": "string",
                    "description": "Full details of the observation",
                },
                "type": {
                    "type": "string",
                    "enum": ["decision", "bugfix", "architecture", "discovery", "preference", "issue"],
                    "description": "Category of observation",
                },
                "topic_key": {
                    "type": "string",
                    "description": (
                        "Stable key for upsert, e.g. 'auth/jwt-config'. "
                        "Same topic_key + project = update, not duplicate."
                    ),
                },
                "node_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Node IDs of related code entities, "
                        "e.g. ['src/api/webhooks.py::handle_stripe_webhook']. "
                        "Recommended: pass the IDs of code you just edited."
                    ),
                },
            },
            "required": ["title", "content", "type"],
        },
    ),
    Tool(
        name="engram_status",
        description="Get project overview: node count, edge count, file count, languages, recent sessions, stale files.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="engram_build",
        description=(
            "Trigger a full or incremental index rebuild. Use after major code changes "
            "or when engram_status reports stale files."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Force full rebuild (drop and recreate index)",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="engram_snapshot",
        description=(
            "Get the project's architectural snapshot — a concise summary of the "
            "stack, structure, conventions, and past decisions. Call this at the "
            "start of a session to quickly understand the project without reading "
            "files. Returns cached snapshot or generates a new one. Note: "
            "engram_query also includes the snapshot automatically."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "description": "Force regenerate the snapshot (default: false)",
                    "default": False,
                },
            },
        },
    ),
    Tool(
        name="engram_verify",
        description=(
            "Verify structural completeness of a code patch. Pass the unified diff "
            "text and Engram will check if all affected nodes were updated. Returns "
            "STRUCTURALLY_COMPLETE if the patch looks good, or INCOMPLETE with a list "
            "of missing updates and suggested fixes. Call this AFTER generating a patch "
            "to catch missed updates before committing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "diff_text": {
                    "type": "string",
                    "description": "The unified diff to verify",
                },
                "seeds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Seed node IDs. Auto-inferred if omitted.",
                },
                "change_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Change types, e.g. ['SIGNATURE_MODIFICATION']. Auto-inferred if omitted.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Original task prompt (helps with change type inference)",
                },
            },
            "required": ["diff_text"],
        },
    ),
    Tool(
        name="engram_find_nodes",
        description=(
            "Find and rank candidate seed nodes for a task WITHOUT running the full "
            "context query. Use this BEFORE engram_query to confirm you have the right "
            "starting points — especially in unfamiliar codebases. Returns ranked "
            "candidates with match scores and file locations. Pass the confirmed node IDs "
            "as explicit seeds to engram_query to get precise context. "
            "This breaks the chicken-and-egg problem: you don't need to know node IDs "
            "upfront — describe what you want to change and this tool finds them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What you're trying to change, e.g. 'fix the webhook renewal bug' or 'update the student list view'",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max candidates to return (default: 10)",
                    "default": 10,
                },
            },
            "required": ["prompt"],
        },
    ),
    Tool(
        name="engram_suggest_bridges",
        description=(
            "declarations. Detects Django/Flask render() calls → templates, fetch('/api/...') "
            "→ backend views, and similar patterns. Returns a list of suggestions with "
            "confidence scores. Pass confirmed=[0,1,2] to auto-generate engram.yaml entries "
            "for specific suggestions. Pass confirmed='all' to accept all HIGH confidence ones."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "confirmed": {
                    "description": (
                        "Indices of suggestions to confirm and write to engram.yaml. "
                        "Pass a list like [0, 2] to confirm specific suggestions, "
                        "'all' to confirm all high-confidence (>=0.8) ones, "
                        "or omit to just view suggestions without writing anything."
                    ),
                },
                "min_confidence": {
                    "type": "number",
                    "description": "Minimum confidence threshold to show (0.0-1.0, default 0.5)",
                    "default": 0.5,
                },
            },
        },
    ),
]


def register_tools(server: Server, engram: "EngramMCPServer"):
    """Register all Engram tools with the MCP server."""
    handlers = ToolHandlers(engram)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        handler_map = {
            "engram_query": handlers.handle_query,
            "engram_find_nodes": handlers.handle_find_nodes,
            "engram_search": handlers.handle_search,
            "engram_save": handlers.handle_save,
            "engram_status": handlers.handle_status,
            "engram_build": handlers.handle_build,
            "engram_snapshot": handlers.handle_snapshot,
            "engram_verify": handlers.handle_verify,
            "engram_suggest_bridges": handlers.handle_suggest_bridges,
        }
        handler = handler_map.get(name)
        if handler is None:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        return await handler(arguments or {})


class ToolHandlers:
    """Handler implementations for each Engram MCP tool."""

    def __init__(self, engram: "EngramMCPServer"):
        self.engram = engram

    async def handle_query(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_query."""
        output_mode = arguments.get("output_mode", "agent")
        max_tokens = arguments.get("max_tokens", 8000 if output_mode == "worker" else 16000)

        package = self.engram.assembler.assemble(
            prompt=arguments["prompt"],
            max_tokens=max_tokens,
            change_hints=arguments.get("change_hints"),
            seeds=arguments.get("seeds"),
        )
        # Remember this query so suggest_bridges can re-run it after confirming
        import time
        self.engram.last_query = {
            "prompt": arguments["prompt"],
            "seeds": arguments.get("seeds"),
            "max_tokens": max_tokens,
            "confidence": package.confidence.score if package.confidence else None,
            "nodes_included": package.stats.get("nodes_included", 0),
            "_queried_at": time.time(),
        }

        if output_mode == "worker":
            text = package.serialize_worker()
        else:
            text = package.serialize()

        return [TextContent(type="text", text=text)]

    async def handle_find_nodes(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_find_nodes."""
        from engram.retriever.seeds import SeedSelector, extract_prompt_terms

        prompt = arguments["prompt"]
        limit = arguments.get("limit", 10)

        selector = SeedSelector(self.engram.store)
        candidates = selector.find_candidates(prompt, limit=limit)

        if not candidates:
            return [TextContent(
                type="text",
                text=(
                    "No matching nodes found. Try engram_status to see what's indexed, "
                    "or run engram_build if the project hasn't been indexed yet."
                ),
            )]

        # Build output with node metadata
        terms = extract_prompt_terms(prompt)
        lines = [
            f"Found {len(candidates)} candidate nodes for: \"{prompt}\"",
            f"Search terms used: {', '.join(terms) if terms else '(none — showing top connected)'}",
            "",
            "Copy the node IDs you want into engram_query as explicit seeds.",
            "",
        ]

        top_score = candidates[0].score if candidates else 1.0

        for i, c in enumerate(candidates):
            node = self.engram.store.get_node(c.node_id)
            if not node:
                continue

            # Confidence bar: ████░░ style
            pct = c.score / top_score if top_score > 0 else 0
            bars = int(pct * 8)
            bar = "█" * bars + "░" * (8 - bars)

            # Pass breakdown
            pass_parts = []
            if "fts5" in c.pass_scores:
                pass_parts.append(f"text:{c.pass_scores['fts5']:.1f}")
            if "graph" in c.pass_scores:
                pass_parts.append(f"graph:{c.pass_scores['graph']:.1f}")
            if "feedback" in c.pass_scores:
                pass_parts.append(f"history:{c.pass_scores['feedback']:.1f}")
            if "fallback" in c.pass_scores:
                pass_parts.append(f"fallback:{c.pass_scores['fallback']:.1f}")
            passes = " + ".join(pass_parts) if pass_parts else c.match_reason

            sig = node.signature or node.name
            lines.append(f"[{i}] {bar} {c.node_id}")
            lines.append(f"     {node.file_path}  line {node.line_start}")
            lines.append(f"     {sig[:80]}")
            lines.append(f"     score: {c.score:.1f} ({passes})")
            lines.append("")

        lines.append("Usage: engram_query(prompt=..., seeds=['node_id_1', 'node_id_2'])")

        return [TextContent(type="text", text="\n".join(lines))]

    async def handle_search(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_search."""
        full = arguments.get("full", False)
        if full:
            results = self.engram.mem_search.search(
                query=arguments["query"],
                type=arguments.get("type"),
                limit=arguments.get("limit", 10),
            )
        else:
            results = self.engram.mem_search.search_progressive(
                query=arguments["query"],
                type=arguments.get("type"),
                limit=arguments.get("limit", 10),
            )
        return [TextContent(type="text", text=self._format_search_results(results, full))]

    async def handle_save(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_save."""
        project = self.engram.root.name
        obs_id = self.engram.obs_mgr.save(
            title=arguments["title"],
            content=arguments["content"],
            type=arguments["type"],
            project=project,
            topic_key=arguments.get("topic_key"),
            node_ids=arguments.get("node_ids"),
        )
        obs = self.engram.obs_mgr.get(obs_id)
        node_count = len(obs.get("linked_nodes", [])) if obs else 0
        return [TextContent(
            type="text",
            text=f'Saved observation #{obs_id}: "{arguments["title"]}" ({node_count} linked nodes)',
        )]

    async def handle_status(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_status."""
        stats = self.engram.store.get_stats()
        project = self.engram.root.name
        sessions = self.engram.session_mgr.get_recent(project, limit=3)

        # Count observations
        obs_count = self.engram.store.conn.execute(
            "SELECT COUNT(*) as c FROM observations"
        ).fetchone()["c"]

        lines = [
            f"Project: {project}",
            f"Index: {stats['file_count']} files, {stats['node_count']} nodes, {stats['edge_count']} edges",
            f"  {stats['function_count']} functions, {stats['class_count']} classes",
            f"Languages: {', '.join(stats['languages']) if stats['languages'] else 'none'}",
            f"Observations: {obs_count} total",
        ]

        # Last task verification state
        import time
        last_query = self.engram.last_query
        last_verified = self.engram.last_verified_at
        if last_query:
            prompt_preview = last_query["prompt"][:60]
            nodes = last_query.get("nodes_included", "?")
            conf = last_query.get("confidence")
            conf_str = f"{int(conf * 100)}%" if conf else "unknown"
            if last_verified and last_verified > (last_query.get("_queried_at", 0)):
                verify_str = "✓ verified"
            else:
                verify_str = "⚠ unverified — run engram_verify before committing"
            lines.append(f"Last task: \"{prompt_preview}\"")
            lines.append(f"  Context: {nodes} nodes, confidence {conf_str} | {verify_str}")
        else:
            lines.append("Last task: none this session")

        if sessions:
            lines.append("Recent sessions:")
            for s in sessions:
                summary = (s.get("summary") or "")[:60]
                ended = "active" if s.get("ended_at") is None else s["ended_at"]
                lines.append(f"  [{s['id']}] {ended} — {summary}")

        return [TextContent(type="text", text="\n".join(lines))]

    async def handle_build(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_build."""
        from engram.cli import build_index

        force = arguments.get("force", False)
        stats = build_index(self.engram.root, self.engram.db, force=force)

        # Refresh snapshot after rebuild
        try:
            self.engram.snapshot_gen.get_or_generate(force_refresh=True)
        except Exception:
            pass  # Snapshot refresh is best-effort

        return [TextContent(
            type="text",
            text=(
                f"Build complete in {stats['time']}s\n"
                f"Files: {stats['files_scanned']} scanned, {stats['files_changed']} changed\n"
                f"Nodes: {stats.get('node_count', 0)}, Edges: {stats.get('edge_count', 0)}"
            ),
        )]

    async def handle_snapshot(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_snapshot."""
        refresh = arguments.get("refresh", False)
        content = self.engram.snapshot_gen.get_or_generate(force_refresh=refresh)
        return [TextContent(type="text", text=content)]

    async def handle_verify(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_verify."""
        from engram.verification.verifier import Verifier, Verdict
        from engram.verification.followup import FollowUpGenerator
        from engram.verification.feedback import RetrievalFeedback

        verifier = Verifier(self.engram.store,
                            pattern_matcher=getattr(self.engram, 'pattern_matcher', None))
        result = verifier.verify(
            diff_text=arguments["diff_text"],
            seeds=arguments.get("seeds"),
            change_types=set(arguments["change_types"]) if arguments.get("change_types") else None,
            prompt=arguments.get("prompt"),
        )

        # Record feedback
        try:
            feedback = RetrievalFeedback(self.engram.store)
            feedback.record(result)
        except Exception:
            pass

        # Track verification timestamp for status reporting
        import time
        self.engram.last_verified_at = time.time()

        if result.verdict == Verdict.STRUCTURALLY_COMPLETE:
            # Build breakdown by node kind
            from collections import Counter
            kind_counts: Counter = Counter()
            files_touched: set = set()
            for tn in result.touched_nodes:
                node = self.engram.store.get_node(tn.node_id)
                kind = node.kind.lower() if node else "node"
                kind_counts[kind] += 1
                files_touched.add(tn.file_path)

            breakdown = ", ".join(f"{count} {kind}s" for kind, count in sorted(kind_counts.items()))
            files_str = f"{len(files_touched)} file{'s' if len(files_touched) != 1 else ''}"
            change_types_str = ", ".join(result.change_types) if result.change_types else "unknown"

            return [TextContent(
                type="text",
                text=(
                    f"✓ Patch is STRUCTURALLY COMPLETE.\n"
                    f"  Checked: {breakdown} across {files_str}\n"
                    f"  Change types verified: {change_types_str}\n"
                    f"  Seeds: {', '.join(result.seeds)}"
                ),
            )]
        else:
            gen = FollowUpGenerator()
            followup = gen.generate(result)
            return [TextContent(type="text", text=followup)]

    async def handle_suggest_bridges(self, arguments: dict) -> list[TextContent]:
        """Handler for engram_suggest_bridges."""
        from engram.bridges_suggest import suggest_bridges, suggestions_to_yaml, merge_into_config

        min_confidence = arguments.get("min_confidence", 0.5)
        confirmed = arguments.get("confirmed")

        suggestions = suggest_bridges(self.engram.root, self.engram.store)
        filtered = [s for s in suggestions if s.confidence >= min_confidence]

        if not filtered:
            return [TextContent(
                type="text",
                text="No bridge suggestions found. Try running engram_build first, or lower min_confidence.",
            )]

        # Resolve confirmed indices
        confirmed_indices: list[int] | None = None
        if confirmed == "all":
            confirmed_indices = [i for i, s in enumerate(filtered) if s.confidence >= 0.8]
        elif isinstance(confirmed, list):
            confirmed_indices = [int(i) for i in confirmed if int(i) < len(filtered)]

        # If confirming, write to engram.yaml and rebuild immediately
        written_count = 0
        if confirmed_indices is not None and confirmed_indices:
            to_confirm = [filtered[i] for i in confirmed_indices]
            new_yaml = suggestions_to_yaml(to_confirm)
            final_yaml = merge_into_config(self.engram.root, new_yaml)
            yaml_path = self.engram.root / "engram.yaml"
            yaml_path.write_text(final_yaml, encoding="utf-8")
            written_count = len(to_confirm)

            from engram.bridges import build_bridges
            build_bridges(self.engram.root, self.engram.store)

        # Format suggestions list
        lines = [f"Bridge suggestions ({len(filtered)} found):\n"]
        for i, s in enumerate(filtered):
            conf_label = "HIGH" if s.confidence >= 0.8 else ("MED" if s.confidence >= 0.6 else "LOW")
            confirmed_marker = " ✓ written" if (confirmed_indices is not None and i in confirmed_indices) else ""
            lines.append(f"[{i}] {conf_label} ({int(s.confidence * 100)}%){confirmed_marker}")
            lines.append(f"    from: {s.from_node}")
            lines.append(f"    to:   {s.to_file}" + (f"  ({s.to_node})" if s.to_node else ""))
            lines.append(f"    why:  {s.reason}")
            lines.append(f"    name: \"{s.bridge_name}\"")
            lines.append("")

        if written_count:
            lines.append(f"✓ Wrote {written_count} bridge(s) to engram.yaml and rebuilt edges.")

            # Auto re-query with same seeds to show coverage improvement
            last = self.engram.last_query
            if last:
                try:
                    new_package = self.engram.assembler.assemble(
                        prompt=last["prompt"],
                        max_tokens=last["max_tokens"],
                        seeds=last["seeds"],
                    )
                    old_pct = int((last["confidence"] or 0) * 100)
                    new_conf = new_package.confidence
                    new_pct = int(new_conf.score * 100) if new_conf else old_pct
                    delta = new_pct - old_pct
                    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
                    lines.append(f"Coverage: {old_pct}% {arrow} {new_pct}% ({delta:+d}%)")
                    old_nodes = last.get("nodes_included", 0)
                    new_nodes = new_package.stats.get("nodes_included", 0)
                    if new_nodes != old_nodes:
                        lines.append(f"Nodes in context: {old_nodes} → {new_nodes} ({new_nodes - old_nodes:+d})")
                    # Update stored query with new confidence
                    self.engram.last_query["confidence"] = new_conf.score if new_conf else None
                    self.engram.last_query["nodes_included"] = new_nodes
                except Exception:
                    pass  # Re-query is best-effort
            else:
                lines.append("Run engram_query again with the same seeds to see coverage improvement.")
        else:
            lines.append("To confirm: call engram_suggest_bridges with confirmed=[0,1,...] or confirmed='all'")

        return [TextContent(type="text", text="\n".join(lines))]

    def _format_search_results(self, results: list[dict], full: bool) -> str:
        """Format search results as readable text."""
        if not results:
            return "No matching observations found."

        lines = []
        for i, r in enumerate(results, 1):
            if full:
                lines.append(f"{i}. [{r.get('type', '')}] {r.get('title', '')}")
                lines.append(f"   {r.get('content', '')}")
                # Get linked nodes
                links = self.engram.store.conn.execute(
                    "SELECT node_id FROM observation_nodes WHERE observation_id = ?",
                    (r["id"],),
                ).fetchall()
                if links:
                    linked = ", ".join(l["node_id"] for l in links)
                    lines.append(f"   Linked to: {linked}")
                lines.append("")
            else:
                snippet = r.get("snippet", r.get("content", "")[:100])
                node_count = r.get("linked_nodes", 0)
                lines.append(f'{i}. [{r.get("type", "")}] {r.get("title", "")}')
                lines.append(f'   "{snippet}" ({node_count} linked nodes)')
                lines.append("")

        return "\n".join(lines)
