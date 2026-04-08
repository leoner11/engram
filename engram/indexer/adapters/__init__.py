"""Base interface for language-specific AST extraction adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from engram.indexer.extractor import NodeRecord, RawEdge


class LanguageAdapter(ABC):
    """
    Base class for language-specific AST extraction.

    Each adapter extracts nodes and raw edges from a tree-sitter AST
    for its specific language.
    """

    language: str = ""  # Override in subclass

    @abstractmethod
    def extract(self, tree, source: bytes, file_path: str) -> tuple[list[NodeRecord], list[RawEdge]]:
        """Extract all nodes and raw edges from the AST."""

    @abstractmethod
    def resolve_import_path(self, module_name: str, from_file: str, project_root: Path) -> str | None:
        """
        Resolve an import module name to a file path within the project.
        Returns None if stdlib/third-party.
        """

    def get_extensions(self) -> list[str]:
        """Return file extensions this adapter handles."""
        return []
