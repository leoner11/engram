"""AST → Nodes + Raw Edges. The core extraction engine for Python."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from engram.indexer.hasher import hash_source


@dataclass
class NodeRecord:
    id: str              # "src/api/views.py::create_event"
    kind: str            # FILE | FUNCTION | CLASS | TYPE
    name: str            # "create_event"
    file_path: str       # "src/api/views.py"
    line_start: int
    line_end: int
    language: str
    signature: str | None = None
    docstring: str | None = None
    source_hash: str = ""
    is_exported: bool = False
    decorators: list[str] = field(default_factory=list)
    full_source: str = ""

    @property
    def summary(self) -> str:
        sig = self.signature or self.name
        if self.docstring:
            first_line = self.docstring.split("\n")[0].strip()
            if first_line:
                return f'{sig} — "{first_line}"'
        return sig


@dataclass
class RawEdge:
    source_id: str       # Node ID of the source
    target_name: str     # Unresolved target name
    kind: str            # IMPORTS | CALLS | USES_TYPE | DEFINES | EXTENDS
    metadata: dict = field(default_factory=dict)
    context: str = ""    # "import" | "call" | "annotation" | "base_class"


def _node_text(node, source: bytes) -> str:
    """Extract text from a tree-sitter node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _find_children_by_type(node, type_name: str) -> list:
    """Find all direct children of a given type."""
    return [c for c in node.children if c.type == type_name]


def _find_child_by_type(node, type_name: str):
    """Find first direct child of a given type."""
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _find_child_by_field(node, field_name: str):
    """Find child by field name."""
    return node.child_by_field_name(field_name)


class PythonExtractor:
    """Extracts nodes and raw edges from a Python tree-sitter AST."""

    def __init__(self, file_path: str, source: bytes, tree):
        self.file_path = file_path
        self.source = source
        self.tree = tree
        self.nodes: list[NodeRecord] = []
        self.raw_edges: list[RawEdge] = []
        self._all_names: set[str] = set()  # Track all defined names for __all__ check
        self._exports: set[str] | None = None  # Populated from __all__ if present

    def extract(self) -> tuple[list[NodeRecord], list[RawEdge]]:
        """Run full extraction. Returns (nodes, raw_edges)."""
        root = self.tree.root_node

        # Create FILE node
        file_source = self.source.decode("utf-8", errors="replace")
        file_node = NodeRecord(
            id=self.file_path,
            kind="FILE",
            name=Path(self.file_path).name,
            file_path=self.file_path,
            line_start=1,
            line_end=file_source.count("\n") + 1,
            language="python",
            source_hash=hash_source(file_source),
            full_source=file_source,
        )
        self.nodes.append(file_node)

        # Detect __all__ for export detection
        self._exports = self._detect_all(root)

        # Walk top-level statements
        for child in root.children:
            self._extract_top_level(child, parent_class=None)

        # Set export flags
        self._set_exports()

        return self.nodes, self.raw_edges

    def _detect_all(self, root) -> set[str] | None:
        """Check for __all__ = [...] and return the listed names."""
        for child in root.children:
            if child.type == "expression_statement":
                expr = _find_child_by_type(child, "assignment")
                if expr is None:
                    continue
                left = _find_child_by_field(expr, "left")
                if left and _node_text(left, self.source) == "__all__":
                    right = _find_child_by_field(expr, "right")
                    if right and right.type == "list":
                        names = set()
                        for item in right.children:
                            if item.type == "string":
                                name = _node_text(item, self.source).strip("\"'")
                                names.add(name)
                        return names
        return None

    def _set_exports(self):
        """Set is_exported on nodes based on __all__ or naming convention."""
        for node in self.nodes:
            if node.kind == "FILE":
                continue
            if self._exports is not None:
                # __all__ defined: only listed names are exported
                node.is_exported = node.name in self._exports
            else:
                # No __all__: top-level non-underscore names are exported
                # Methods are not directly exported
                if "." not in node.name and not node.name.startswith("_"):
                    node.is_exported = True

    def _extract_top_level(self, node, parent_class: str | None):
        """Process a top-level or class-level statement."""
        if node.type == "function_definition":
            self._extract_function(node, parent_class)
        elif node.type == "class_definition":
            self._extract_class(node)
        elif node.type == "decorated_definition":
            # Unwrap the decorated definition
            decorators = self._extract_decorators(node)
            inner = None
            for child in node.children:
                if child.type in ("function_definition", "class_definition"):
                    inner = child
                    break
            if inner is not None:
                if inner.type == "function_definition":
                    self._extract_function(inner, parent_class, decorators=decorators)
                elif inner.type == "class_definition":
                    self._extract_class(inner, decorators=decorators)
        elif node.type in ("import_statement", "import_from_statement"):
            self._extract_import(node)

    def _extract_function(
        self, node, parent_class: str | None, decorators: list[str] | None = None
    ):
        """Extract a function/method node and its edges."""
        name_node = _find_child_by_field(node, "name")
        if name_node is None:
            return
        name = _node_text(name_node, self.source)

        # Build qualified name
        if parent_class:
            qualified = f"{parent_class}.{name}"
            node_id = f"{self.file_path}::{qualified}"
        else:
            qualified = name
            node_id = f"{self.file_path}::{name}"

        # Build signature
        params_node = _find_child_by_field(node, "parameters")
        params_text = _node_text(params_node, self.source) if params_node else "()"
        return_node = _find_child_by_field(node, "return_type")
        return_text = f" -> {_node_text(return_node, self.source)}" if return_node else ""
        signature = f"def {qualified}{params_text}{return_text}"

        # Extract full source
        full_source = _node_text(node, self.source)

        # Docstring
        docstring = self._extract_docstring(node)

        func_record = NodeRecord(
            id=node_id,
            kind="FUNCTION",
            name=qualified,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language="python",
            signature=signature,
            docstring=docstring,
            source_hash=hash_source(full_source),
            decorators=decorators or [],
            full_source=full_source,
        )
        self.nodes.append(func_record)

        # DEFINES edge from parent (file or class)
        parent_id = f"{self.file_path}::{parent_class}" if parent_class else self.file_path
        self.raw_edges.append(RawEdge(
            source_id=parent_id,
            target_name=name,
            kind="DEFINES",
            context="defines",
        ))

        # Extract calls and type usages from function body
        body = _find_child_by_field(node, "body")
        if body:
            self._extract_calls(body, node_id)
            self._extract_type_annotations(node, node_id)

    def _extract_class(self, node, decorators: list[str] | None = None):
        """Extract a class node, its base classes, and its methods."""
        name_node = _find_child_by_field(node, "name")
        if name_node is None:
            return
        name = _node_text(name_node, self.source)
        node_id = f"{self.file_path}::{name}"

        full_source = _node_text(node, self.source)
        docstring = self._extract_docstring(node)

        # Build signature
        superclasses = _find_child_by_field(node, "superclasses")
        if superclasses:
            bases_text = _node_text(superclasses, self.source)
            signature = f"class {name}{bases_text}"
        else:
            signature = f"class {name}"

        class_record = NodeRecord(
            id=node_id,
            kind="CLASS",
            name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language="python",
            signature=signature,
            docstring=docstring,
            source_hash=hash_source(full_source),
            decorators=decorators or [],
            full_source=full_source,
        )
        self.nodes.append(class_record)

        # DEFINES edge from file
        self.raw_edges.append(RawEdge(
            source_id=self.file_path,
            target_name=name,
            kind="DEFINES",
            context="defines",
        ))

        # EXTENDS edges from base classes
        if superclasses:
            for arg in superclasses.children:
                if arg.type in ("identifier", "attribute"):
                    base_name = _node_text(arg, self.source)
                    self.raw_edges.append(RawEdge(
                        source_id=node_id,
                        target_name=base_name,
                        kind="EXTENDS",
                        metadata={"base_class": base_name},
                        context="base_class",
                    ))

        # Extract methods from class body
        body = _find_child_by_field(node, "body")
        if body:
            for child in body.children:
                self._extract_top_level(child, parent_class=name)

    def _extract_import(self, node):
        """Extract import statements as raw edges."""
        file_id = self.file_path

        if node.type == "import_statement":
            # import foo / import foo.bar
            for child in node.children:
                if child.type == "dotted_name":
                    module_name = _node_text(child, self.source)
                    self.raw_edges.append(RawEdge(
                        source_id=file_id,
                        target_name=module_name,
                        kind="IMPORTS",
                        metadata={"symbols": [module_name], "is_from": False},
                        context="import",
                    ))
                elif child.type == "aliased_import":
                    name_node = _find_child_by_type(child, "dotted_name")
                    if name_node:
                        module_name = _node_text(name_node, self.source)
                        alias_node = _find_child_by_field(child, "alias")
                        alias = _node_text(alias_node, self.source) if alias_node else None
                        self.raw_edges.append(RawEdge(
                            source_id=file_id,
                            target_name=module_name,
                            kind="IMPORTS",
                            metadata={"symbols": [module_name], "is_from": False, "alias": alias},
                            context="import",
                        ))

        elif node.type == "import_from_statement":
            # from foo.bar import Baz, Qux
            module_node = _find_child_by_field(node, "module_name")
            if module_node is None:
                # Try dotted_name child
                module_node = _find_child_by_type(node, "dotted_name")
            if module_node is None:
                # relative import like "from . import X" — get relative_import
                module_node = _find_child_by_type(node, "relative_import")

            module_name = _node_text(module_node, self.source) if module_node else ""

            # Collect imported symbols
            symbols = []
            for child in node.children:
                if child.type == "dotted_name" and child != module_node:
                    symbols.append(_node_text(child, self.source))
                elif child.type == "aliased_import":
                    name_node = _find_child_by_type(child, "dotted_name") or _find_child_by_type(child, "identifier")
                    if name_node:
                        symbols.append(_node_text(name_node, self.source))
                elif child.type == "identifier" and child != module_node:
                    text = _node_text(child, self.source)
                    if text not in ("import", "from", "as"):
                        symbols.append(text)
                elif child.type == "wildcard_import":
                    symbols.append("*")

            if symbols:
                self.raw_edges.append(RawEdge(
                    source_id=file_id,
                    target_name=module_name,
                    kind="IMPORTS",
                    metadata={"symbols": symbols, "is_from": True, "module": module_name},
                    context="import",
                ))

    def _extract_calls(self, body_node, function_id: str):
        """Extract call expressions from a function body."""
        calls: dict[str, list[int]] = {}  # target_name -> [line_numbers]

        def _walk_calls(node):
            if node.type == "call":
                func = _find_child_by_field(node, "function")
                if func:
                    target = _node_text(func, self.source)
                    line = node.start_point[0] + 1
                    calls.setdefault(target, []).append(line)
            for child in node.children:
                _walk_calls(child)

        _walk_calls(body_node)

        for target_name, lines in calls.items():
            self.raw_edges.append(RawEdge(
                source_id=function_id,
                target_name=target_name,
                kind="CALLS",
                metadata={"call_sites": sorted(lines)},
                context="call",
            ))

    def _extract_type_annotations(self, func_node, function_id: str):
        """Extract type usages from function parameters and return type."""
        # Parameters
        params = _find_child_by_field(func_node, "parameters")
        if params:
            for param in params.children:
                if param.type in ("typed_parameter", "typed_default_parameter"):
                    type_node = _find_child_by_field(param, "type")
                    if type_node:
                        type_name = _node_text(type_node, self.source)
                        # Strip Optional[], list[], etc. — get the core type
                        core_types = self._extract_core_types(type_name)
                        for t in core_types:
                            self.raw_edges.append(RawEdge(
                                source_id=function_id,
                                target_name=t,
                                kind="USES_TYPE",
                                metadata={"usage_pattern": "unknown", "accessed_fields": []},
                                context="annotation",
                            ))

        # Return type
        return_node = _find_child_by_field(func_node, "return_type")
        if return_node:
            return_text = _node_text(return_node, self.source)
            core_types = self._extract_core_types(return_text)
            for t in core_types:
                self.raw_edges.append(RawEdge(
                    source_id=function_id,
                    target_name=t,
                    kind="USES_TYPE",
                    metadata={"usage_pattern": "unknown", "accessed_fields": []},
                    context="annotation",
                ))

    def _extract_core_types(self, type_text: str) -> list[str]:
        """Extract core type names from annotation strings like Optional[Event] or list[str]."""
        # Strip common wrappers
        builtins = {"str", "int", "float", "bool", "bytes", "None", "Any",
                     "list", "dict", "tuple", "set", "frozenset", "type",
                     "Optional", "Union", "List", "Dict", "Tuple", "Set"}
        # Find all capitalized identifiers that look like custom types
        identifiers = re.findall(r'\b([A-Z][a-zA-Z0-9_]*)\b', type_text)
        return [i for i in identifiers if i not in builtins and i not in (
            "Optional", "Union", "List", "Dict", "Tuple", "Set", "Any",
            "Callable", "Iterator", "Generator", "Coroutine", "Awaitable",
            "Type", "ClassVar", "Final", "Literal", "Protocol",
        )]

    def _extract_decorators(self, decorated_node) -> list[str]:
        """Extract decorator strings from a decorated_definition."""
        decorators = []
        for child in decorated_node.children:
            if child.type == "decorator":
                text = _node_text(child, self.source).strip()
                decorators.append(text)
        return decorators

    def _extract_docstring(self, node) -> str | None:
        """Extract docstring from first expression_statement in body."""
        body = _find_child_by_field(node, "body")
        if body is None:
            return None

        for child in body.children:
            if child.type == "expression_statement":
                string_node = _find_child_by_type(child, "string")
                if string_node:
                    raw = _node_text(string_node, self.source)
                    # Strip triple-quote delimiters
                    for delim in ('"""', "'''", '"', "'"):
                        if raw.startswith(delim) and raw.endswith(delim):
                            raw = raw[len(delim):-len(delim)]
                            break
                    return raw.strip()
                break  # Only check the very first statement
            else:
                break

        return None
