"""File discovery for indexing. Respects .gitignore and exclusion patterns."""

from pathlib import Path

import pathspec


# Always skip these directories regardless of config
ALWAYS_SKIP = {
    ".git", ".engram", "__pycache__", "node_modules",
    ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "dist", "build", ".tox", ".eggs",
    "*.egg-info",
}


def _load_ignore_patterns(root: Path) -> pathspec.PathSpec:
    """Load patterns from .gitignore and .engramignore."""
    patterns = list(ALWAYS_SKIP)

    for ignore_file in [".gitignore", ".engramignore"]:
        path = root / ignore_file
        if path.exists():
            patterns.extend(
                line.strip()
                for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            )

    return pathspec.PathSpec.from_lines("gitignore", patterns)


def scan_project(root: Path, extensions: list[str] | None = None) -> list[Path]:
    """
    Walk root directory, return list of source files.

    Args:
        root: Project root directory.
        extensions: File extensions to include (e.g., [".py"]).
                    If None, uses Python extensions [".py"].

    Returns:
        List of paths relative to root.
    """
    if extensions is None:
        extensions = [".py"]

    ignore_spec = _load_ignore_patterns(root)
    results = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in extensions:
            continue

        rel_path = path.relative_to(root)
        rel_str = str(rel_path)

        # Check if any parent directory should be skipped
        if ignore_spec.match_file(rel_str):
            continue

        results.append(rel_path)

    return results
