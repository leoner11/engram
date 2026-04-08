"""Language-specific mappings for tree-sitter AST node types."""

from pathlib import Path


LANGUAGE_MAP = {
    "python": {
        "extensions": [".py"],
        "grammar_package": "tree_sitter_python",
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "grammar_package": "tree_sitter_typescript",
    },
    "javascript": {
        "extensions": [".js", ".jsx"],
        "grammar_package": "tree_sitter_javascript",
    },
    "dart": {
        "extensions": [".dart"],
        "grammar_package": "tree_sitter_dart",
    },
}


def detect_language(path: Path) -> str | None:
    """Return language key based on file extension, or None if unsupported."""
    suffix = path.suffix
    for lang, config in LANGUAGE_MAP.items():
        if suffix in config["extensions"]:
            return lang
    return None


def get_extensions(language: str) -> list[str]:
    """Return file extensions for a language."""
    config = LANGUAGE_MAP.get(language)
    if config is None:
        return []
    return config["extensions"]


def get_all_extensions() -> list[str]:
    """Return all known source file extensions across all languages."""
    exts = []
    for config in LANGUAGE_MAP.values():
        exts.extend(config["extensions"])
    return exts


def get_supported_languages() -> list[str]:
    """Return list of supported language names."""
    return list(LANGUAGE_MAP.keys())
