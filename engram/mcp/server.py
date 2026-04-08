"""Engram MCP server: stdio transport, auto-detect project root."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.memory.sessions import SessionManager
from engram.memory.observations import ObservationManager
from engram.memory.search import MemorySearch
from engram.patterns import PatternCatalog, PatternMatcher
from engram.retriever.assembler import ContextAssembler
from engram.retriever.conventions import ConventionFinder
from engram.snapshot import SnapshotGenerator
from engram.mcp.tools import register_tools


class EngramMCPServer:
    """MCP server exposing Engram tools over stdio."""

    def __init__(self, root: Path | None = None):
        self.root = root or self._detect_root()
        self.db = EngramDB(self.root)
        self.store = GraphStore(self.db)
        self.session_mgr = SessionManager(self.store)
        self.obs_mgr = ObservationManager(self.store, self.session_mgr)
        self.mem_search = MemorySearch(self.store)
        self.pattern_catalog = PatternCatalog(self.store)
        self.pattern_matcher = PatternMatcher(self.store, self.pattern_catalog)
        self.convention_finder = ConventionFinder(self.store)
        self.snapshot_gen = SnapshotGenerator(
            self.store, self.obs_mgr, self.root.name,
        )
        self.assembler = ContextAssembler(
            self.store,
            project_root=self.root,
            memory_search=self.mem_search,
            pattern_matcher=self.pattern_matcher,
            convention_finder=self.convention_finder,
            snapshot_gen=self.snapshot_gen,
        )
        self.last_query: dict | None = None  # {prompt, seeds, max_tokens, confidence}
        self.last_verified_at: float | None = None  # timestamp of last engram_verify call
        self.server = Server("engram")
        register_tools(self.server, self)

    def _detect_root(self) -> Path:
        """Walk up from cwd looking for .engram/ or .git/."""
        current = Path.cwd()
        for parent in [current] + list(current.parents):
            if (parent / ".engram").exists():
                return parent
            if (parent / ".git").exists():
                return parent
        return current

    async def run(self):
        """Start the MCP server on stdio transport."""
        async with stdio_server() as streams:
            await self.server.run(
                streams[0], streams[1], self.server.create_initialization_options()
            )
