"""JavaScript language adapter — inherits from TypeScript, strips type-specific features."""

from __future__ import annotations

from pathlib import Path

from engram.indexer.adapters.typescript_adapter import TypeScriptAdapter


class JavaScriptAdapter(TypeScriptAdapter):
    """
    JavaScript adapter — 95% same as TypeScript.
    
    Differences:
    - No interface/type_alias/enum declarations
    - No type annotations (USES_TYPE edges are rare)
    - CommonJS require() support
    - .js/.jsx extensions
    """

    language = "javascript"

    def get_extensions(self) -> list[str]:
        return [".js", ".jsx"]

    def resolve_import_path(self, module_name: str, from_file: str, project_root: Path) -> str | None:
        """JavaScript import resolution — same as TS but .js/.jsx extensions."""
        if not module_name.startswith("."):
            return None

        from_dir = (project_root / from_file).parent
        resolved = from_dir / module_name

        suffixes = [".js", ".jsx", "/index.js", "/index.jsx", ".ts", ".tsx"]
        for suffix in suffixes:
            candidate = resolved.parent / (resolved.name + suffix)
            if candidate.exists():
                try:
                    return str(candidate.relative_to(project_root))
                except ValueError:
                    pass
        return None
