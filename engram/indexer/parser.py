"""Tree-sitter AST parsing. Thin wrapper around the tree-sitter library."""

from pathlib import Path

from tree_sitter import Language, Parser


class ParseError(Exception):
    """Raised when a file cannot be parsed."""


class TreeSitterParser:
    """Lazy-initialized tree-sitter parser pool."""

    def __init__(self):
        self._parsers: dict[str, Parser] = {}
        self._languages: dict[str, Language] = {}

    def _init_language(self, language: str) -> Language:
        """Initialize a tree-sitter Language from its grammar package."""
        if language in self._languages:
            return self._languages[language]

        if language == "python":
            import tree_sitter_python as ts_python
            lang = Language(ts_python.language())
        elif language == "typescript":
            import tree_sitter_typescript as ts_typescript
            lang = Language(ts_typescript.language_typescript())
        elif language == "javascript":
            import tree_sitter_javascript as ts_javascript
            lang = Language(ts_javascript.language())
        elif language == "dart":
            try:
                import tree_sitter_dart as ts_dart
                lang = Language(ts_dart.language())
            except ImportError:
                raise ParseError(
                    "Dart support requires tree-sitter-dart. "
                    "Install via: pip install tree-sitter-dart"
                )
        else:
            raise ParseError(f"Unsupported language: {language}")

        self._languages[language] = lang
        return lang

    def get_parser(self, language: str) -> Parser:
        """Get or create a parser for the given language."""
        if language not in self._parsers:
            lang = self._init_language(language)
            parser = Parser(lang)
            self._parsers[language] = parser
        return self._parsers[language]

    def parse_file(self, path: Path, language: str) -> "tree_sitter.Tree":
        """
        Parse a file and return the tree-sitter AST.

        tree-sitter is error-tolerant — even files with syntax errors produce
        a partial AST with ERROR nodes. We parse anyway and let the extractor
        skip ERROR subtrees.
        """
        parser = self.get_parser(language)
        try:
            source = path.read_bytes()
        except (OSError, IOError) as e:
            raise ParseError(f"Cannot read {path}: {e}") from e

        tree = parser.parse(source)
        return tree

    def parse_bytes(self, source: bytes, language: str) -> "tree_sitter.Tree":
        """Parse raw bytes and return the AST. Used for testing."""
        parser = self.get_parser(language)
        return parser.parse(source)
