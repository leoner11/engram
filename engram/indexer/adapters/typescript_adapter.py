"""TypeScript language adapter — function/class/interface/type extraction."""

from __future__ import annotations

import re
from pathlib import Path

from engram.indexer.adapters import LanguageAdapter
from engram.indexer.extractor import NodeRecord, RawEdge, _node_text, _find_child_by_field, _find_child_by_type
from engram.indexer.hasher import hash_source


class TypeScriptAdapter(LanguageAdapter):
    """TypeScript/TSX extraction adapter."""

    language = "typescript"

    def extract(self, tree, source: bytes, file_path: str) -> tuple[list[NodeRecord], list[RawEdge]]:
        self.file_path = file_path
        self.source = source
        self.nodes: list[NodeRecord] = []
        self.raw_edges: list[RawEdge] = []

        root = tree.root_node

        # FILE node
        file_source = source.decode("utf-8", errors="replace")
        self.nodes.append(NodeRecord(
            id=file_path,
            kind="FILE",
            name=Path(file_path).name,
            file_path=file_path,
            line_start=1,
            line_end=file_source.count("\n") + 1,
            language=self.language,
            source_hash=hash_source(file_source),
            full_source=file_source,
        ))

        # Walk top-level statements
        for child in root.children:
            self._extract_top_level(child, parent_class=None)

        return self.nodes, self.raw_edges

    def _extract_top_level(self, node, parent_class: str | None):
        """Process a top-level or class-level statement."""
        ntype = node.type

        # Export statement wrapping a declaration
        if ntype == "export_statement":
            for child in node.children:
                if child.type in ("function_declaration", "class_declaration",
                                   "interface_declaration", "type_alias_declaration",
                                   "enum_declaration", "lexical_declaration",
                                   "abstract_class_declaration"):
                    self._extract_top_level(child, parent_class)
                    # Mark last extracted node as exported
                    if self.nodes and self.nodes[-1].kind != "FILE":
                        self.nodes[-1].is_exported = True
            return

        if ntype == "function_declaration":
            self._extract_function_declaration(node, parent_class)
        elif ntype in ("class_declaration", "abstract_class_declaration"):
            self._extract_class(node)
        elif ntype == "interface_declaration":
            self._extract_interface(node)
        elif ntype == "type_alias_declaration":
            self._extract_type_alias(node)
        elif ntype == "enum_declaration":
            self._extract_enum(node)
        elif ntype == "lexical_declaration":
            self._extract_lexical_declaration(node, parent_class)
        elif ntype == "import_statement":
            self._extract_import(node)
        elif ntype == "method_definition":
            self._extract_method(node, parent_class)

    def _extract_function_declaration(self, node, parent_class: str | None):
        """Extract: function foo(...) { ... }"""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        qualified = f"{parent_class}.{name}" if parent_class else name
        node_id = f"{self.file_path}::{qualified}"

        params = _find_child_by_field(node, "parameters")
        params_text = _node_text(params, self.source) if params else "()"
        return_type = _find_child_by_field(node, "return_type")
        ret_text = f": {_node_text(return_type, self.source)}" if return_type else ""

        full_source = _node_text(node, self.source)
        docstring = self._get_preceding_jsdoc(node)

        func = NodeRecord(
            id=node_id, kind="FUNCTION", name=qualified,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=f"function {qualified}{params_text}{ret_text}",
            docstring=docstring,
            source_hash=hash_source(full_source),
            full_source=full_source,
        )
        self.nodes.append(func)

        # DEFINES edge
        parent_id = f"{self.file_path}::{parent_class}" if parent_class else self.file_path
        self.raw_edges.append(RawEdge(source_id=parent_id, target_name=name, kind="DEFINES"))

        # Extract calls and type annotations from body
        body = _find_child_by_field(node, "body")
        if body:
            self._extract_calls(body, node_id)
        self._extract_ts_type_annotations(node, node_id)

    def _extract_lexical_declaration(self, node, parent_class: str | None):
        """Extract: const foo = (...) => { ... } or const foo = function() { ... }"""
        for child in node.children:
            if child.type == "variable_declarator":
                name_node = _find_child_by_field(child, "name")
                value_node = _find_child_by_field(child, "value")
                if not name_node or not value_node:
                    continue
                if value_node.type not in ("arrow_function", "function"):
                    continue

                name = _node_text(name_node, self.source)
                qualified = f"{parent_class}.{name}" if parent_class else name
                node_id = f"{self.file_path}::{qualified}"

                params = _find_child_by_field(value_node, "parameters")
                params_text = _node_text(params, self.source) if params else "()"
                return_type = _find_child_by_field(value_node, "return_type")
                ret_text = f": {_node_text(return_type, self.source)}" if return_type else ""

                full_source = _node_text(node, self.source)
                docstring = self._get_preceding_jsdoc(node)

                func = NodeRecord(
                    id=node_id, kind="FUNCTION", name=qualified,
                    file_path=self.file_path,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    language=self.language,
                    signature=f"const {qualified} = {params_text} =>{ret_text}",
                    docstring=docstring,
                    source_hash=hash_source(full_source),
                    full_source=full_source,
                )
                self.nodes.append(func)

                parent_id = f"{self.file_path}::{parent_class}" if parent_class else self.file_path
                self.raw_edges.append(RawEdge(source_id=parent_id, target_name=name, kind="DEFINES"))

                body = _find_child_by_field(value_node, "body")
                if body:
                    self._extract_calls(body, node_id)
                self._extract_ts_type_annotations(value_node, node_id)

    def _extract_class(self, node):
        """Extract class declaration + methods."""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        node_id = f"{self.file_path}::{name}"

        full_source = _node_text(node, self.source)
        docstring = self._get_preceding_jsdoc(node)

        # Heritage (extends/implements)
        heritage = ""
        for child in node.children:
            if child.type == "class_heritage":
                heritage = " " + _node_text(child, self.source)
                # Extract EXTENDS edges
                for hc in child.children:
                    if hc.type in ("extends_clause", "implements_clause"):
                        for type_node in hc.children:
                            if type_node.type in ("type_identifier", "identifier"):
                                base = _node_text(type_node, self.source)
                                self.raw_edges.append(RawEdge(
                                    source_id=node_id, target_name=base,
                                    kind="EXTENDS", metadata={"base_class": base},
                                    context="base_class",
                                ))

        cls = NodeRecord(
            id=node_id, kind="CLASS", name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=f"class {name}{heritage}",
            docstring=docstring,
            source_hash=hash_source(full_source),
            full_source=full_source,
        )
        self.nodes.append(cls)
        self.raw_edges.append(RawEdge(source_id=self.file_path, target_name=name, kind="DEFINES"))

        # Extract methods from class body
        body = _find_child_by_field(node, "body")
        if body:
            for child in body.children:
                if child.type == "method_definition":
                    self._extract_method(child, name)
                elif child.type == "public_field_definition":
                    pass  # Could extract class properties in future

    def _extract_method(self, node, parent_class: str | None):
        """Extract a method definition inside a class."""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        qualified = f"{parent_class}.{name}" if parent_class else name
        node_id = f"{self.file_path}::{qualified}"

        params = _find_child_by_field(node, "parameters")
        params_text = _node_text(params, self.source) if params else "()"
        return_type = _find_child_by_field(node, "return_type")
        ret_text = f": {_node_text(return_type, self.source)}" if return_type else ""

        full_source = _node_text(node, self.source)
        docstring = self._get_preceding_jsdoc(node)

        method = NodeRecord(
            id=node_id, kind="FUNCTION", name=qualified,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=f"{qualified}{params_text}{ret_text}",
            docstring=docstring,
            source_hash=hash_source(full_source),
            full_source=full_source,
        )
        self.nodes.append(method)

        if parent_class:
            self.raw_edges.append(RawEdge(
                source_id=f"{self.file_path}::{parent_class}",
                target_name=name, kind="DEFINES",
            ))

        body = _find_child_by_field(node, "body")
        if body:
            self._extract_calls(body, node_id)
        self._extract_ts_type_annotations(node, node_id)

    def _extract_interface(self, node):
        """Extract: interface Foo { ... }"""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        node_id = f"{self.file_path}::{name}"
        full_source = _node_text(node, self.source)

        iface = NodeRecord(
            id=node_id, kind="TYPE", name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=f"interface {name}",
            docstring=self._get_preceding_jsdoc(node),
            source_hash=hash_source(full_source),
            full_source=full_source,
            is_exported=False,
        )
        self.nodes.append(iface)
        self.raw_edges.append(RawEdge(source_id=self.file_path, target_name=name, kind="DEFINES"))

        # Check extends
        for child in node.children:
            if child.type == "extends_type_clause":
                for tc in child.children:
                    if tc.type in ("type_identifier", "identifier"):
                        base = _node_text(tc, self.source)
                        self.raw_edges.append(RawEdge(
                            source_id=node_id, target_name=base,
                            kind="EXTENDS", metadata={"base_class": base},
                        ))

    def _extract_type_alias(self, node):
        """Extract: type Foo = ..."""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        node_id = f"{self.file_path}::{name}"
        full_source = _node_text(node, self.source)

        ta = NodeRecord(
            id=node_id, kind="TYPE", name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=f"type {name}",
            docstring=self._get_preceding_jsdoc(node),
            source_hash=hash_source(full_source),
            full_source=full_source,
        )
        self.nodes.append(ta)
        self.raw_edges.append(RawEdge(source_id=self.file_path, target_name=name, kind="DEFINES"))

    def _extract_enum(self, node):
        """Extract: enum Foo { ... }"""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        node_id = f"{self.file_path}::{name}"
        full_source = _node_text(node, self.source)

        en = NodeRecord(
            id=node_id, kind="TYPE", name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=f"enum {name}",
            source_hash=hash_source(full_source),
            full_source=full_source,
        )
        self.nodes.append(en)
        self.raw_edges.append(RawEdge(source_id=self.file_path, target_name=name, kind="DEFINES"))

    def _extract_import(self, node):
        """Extract: import { Foo } from './bar'"""
        file_id = self.file_path
        source_node = _find_child_by_field(node, "source")
        if not source_node:
            return
        module_raw = _node_text(source_node, self.source).strip("\"'`")

        symbols = []
        for child in node.children:
            if child.type == "import_clause":
                for ic_child in child.children:
                    if ic_child.type == "identifier":
                        symbols.append(_node_text(ic_child, self.source))  # default import
                    elif ic_child.type == "named_imports":
                        for spec in ic_child.children:
                            if spec.type == "import_specifier":
                                name_n = _find_child_by_field(spec, "name")
                                if name_n:
                                    symbols.append(_node_text(name_n, self.source))
                    elif ic_child.type == "namespace_import":
                        alias = _find_child_by_type(ic_child, "identifier")
                        if alias:
                            symbols.append(_node_text(alias, self.source))

        if symbols or module_raw:
            self.raw_edges.append(RawEdge(
                source_id=file_id,
                target_name=module_raw,
                kind="IMPORTS",
                metadata={"symbols": symbols, "is_from": True, "module": module_raw},
                context="import",
            ))

    def _extract_calls(self, body_node, function_id: str):
        """Extract call expressions from function body."""
        calls: dict[str, list[int]] = {}

        def _walk(node):
            if node.type == "call_expression":
                func = _find_child_by_field(node, "function")
                if func:
                    target = _node_text(func, self.source)
                    line = node.start_point[0] + 1
                    calls.setdefault(target, []).append(line)
            for child in node.children:
                _walk(child)

        _walk(body_node)

        for target, lines in calls.items():
            self.raw_edges.append(RawEdge(
                source_id=function_id,
                target_name=target,
                kind="CALLS",
                metadata={"call_sites": sorted(lines)},
                context="call",
            ))

    def _extract_ts_type_annotations(self, func_node, function_id: str):
        """Extract type references from parameters and return type."""
        builtins = {"string", "number", "boolean", "void", "any", "never",
                     "unknown", "null", "undefined", "object", "symbol", "bigint",
                     "String", "Number", "Boolean", "Array", "Object", "Promise",
                     "Record", "Partial", "Required", "Readonly", "Pick", "Omit",
                     "Map", "Set"}

        params = _find_child_by_field(func_node, "parameters")
        if params:
            text = _node_text(params, self.source)
            identifiers = re.findall(r'\b([A-Z][a-zA-Z0-9_]*)\b', text)
            for ident in identifiers:
                if ident not in builtins:
                    self.raw_edges.append(RawEdge(
                        source_id=function_id, target_name=ident,
                        kind="USES_TYPE",
                        metadata={"usage_pattern": "unknown", "accessed_fields": []},
                        context="annotation",
                    ))

        return_type = _find_child_by_field(func_node, "return_type")
        if return_type:
            text = _node_text(return_type, self.source)
            identifiers = re.findall(r'\b([A-Z][a-zA-Z0-9_]*)\b', text)
            for ident in identifiers:
                if ident not in builtins:
                    self.raw_edges.append(RawEdge(
                        source_id=function_id, target_name=ident,
                        kind="USES_TYPE",
                        metadata={"usage_pattern": "unknown", "accessed_fields": []},
                        context="annotation",
                    ))

    def _get_preceding_jsdoc(self, node) -> str | None:
        """Get JSDoc comment (/** ... */) preceding a declaration."""
        prev = node.prev_sibling
        if prev and prev.type == "comment":
            text = _node_text(prev, self.source)
            if text.startswith("/**"):
                cleaned = text.strip("/* \n")
                lines = [l.strip().lstrip("* ") for l in cleaned.split("\n")]
                return "\n".join(l for l in lines if l).strip()
        return None

    def resolve_import_path(self, module_name: str, from_file: str, project_root: Path) -> str | None:
        """TypeScript import resolution — relative paths only."""
        if not module_name.startswith("."):
            return None  # Third-party / bare specifier

        from_dir = (project_root / from_file).parent
        resolved = from_dir / module_name

        suffixes = [".ts", ".tsx", "/index.ts", "/index.tsx", ".js", ".jsx", "/index.js"]
        for suffix in suffixes:
            candidate = resolved.parent / (resolved.name + suffix)
            if candidate.exists():
                try:
                    return str(candidate.relative_to(project_root))
                except ValueError:
                    pass
        return None

    def get_extensions(self) -> list[str]:
        return [".ts", ".tsx"]
