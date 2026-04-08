"""Python language adapter — wraps PythonExtractor in the adapter interface."""

from __future__ import annotations

from pathlib import Path

from engram.indexer.adapters import LanguageAdapter
from engram.indexer.extractor import PythonExtractor, NodeRecord, RawEdge


class PythonAdapter(LanguageAdapter):
    """Python-specific extraction via PythonExtractor."""

    language = "python"

    def extract(self, tree, source: bytes, file_path: str) -> tuple[list[NodeRecord], list[RawEdge]]:
        extractor = PythonExtractor(file_path, source, tree)
        return extractor.extract()

    def resolve_import_path(self, module_name: str, from_file: str, project_root: Path) -> str | None:
        """Python import resolution: dot notation → file path."""
        if module_name.startswith("."):
            return self._resolve_relative(module_name, from_file, project_root)

        parts = module_name.split(".")
        candidates = [
            "/".join(parts) + ".py",
            "/".join(parts) + "/__init__.py",
        ]
        for c in list(candidates):
            candidates.append("src/" + c)

        for c in candidates:
            if (project_root / c).exists():
                return c
        return None

    def _resolve_relative(self, module_name: str, from_file: str, project_root: Path) -> str | None:
        dots = len(module_name) - len(module_name.lstrip("."))
        remainder = module_name[dots:]
        from_dir = Path(from_file).parent
        for _ in range(dots - 1):
            from_dir = from_dir.parent

        if remainder:
            parts = remainder.split(".")
            base = from_dir / "/".join(parts)
        else:
            base = from_dir

        candidates = [str(base) + ".py", str(base / "__init__.py")]
        for c in candidates:
            c = str(Path(c))
            if (project_root / c).exists():
                return c
        return None

    def get_extensions(self) -> list[str]:
        return [".py"]
