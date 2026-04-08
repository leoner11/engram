"""Dart language adapter — function/class/mixin/extension/enum extraction."""

from __future__ import annotations

import re
from pathlib import Path

from engram.indexer.adapters import LanguageAdapter
from engram.indexer.extractor import NodeRecord, RawEdge, _node_text, _find_child_by_field, _find_child_by_type
from engram.indexer.hasher import hash_source


class DartAdapter(LanguageAdapter):
    """Dart/Flutter extraction adapter."""

    language = "dart"

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

        # Walk top-level declarations
        for child in root.children:
            self._extract_top_level(child, parent_class=None)

        return self.nodes, self.raw_edges

    def _extract_top_level(self, node, parent_class: str | None):
        """Process a top-level or class-level declaration."""
        ntype = node.type

        if ntype == "function_signature":
            self._extract_function(node, parent_class)
        elif ntype == "method_definition" or ntype == "function_definition":
            self._extract_function(node, parent_class)
        elif ntype == "class_definition":
            self._extract_class(node)
        elif ntype == "mixin_declaration":
            self._extract_mixin(node)
        elif ntype == "extension_declaration":
            self._extract_extension(node)
        elif ntype == "enum_declaration":
            self._extract_enum(node)
        elif ntype == "import_or_export":
            self._extract_import(node)
        elif ntype in ("top_level_definition", "declaration"):
            # Unwrap declarations that wrap other definitions
            for child in node.children:
                self._extract_top_level(child, parent_class)
        elif ntype == "function_body":
            # Skip — body is handled by function extraction
            pass
        else:
            # Walk children for nested declarations (Dart grammar varies)
            for child in node.children:
                if child.type in (
                    "function_signature", "method_definition", "function_definition",
                    "class_definition", "mixin_declaration", "extension_declaration",
                    "enum_declaration", "import_or_export",
                ):
                    self._extract_top_level(child, parent_class)

    def _extract_function(self, node, parent_class: str | None):
        """Extract a function or method declaration."""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            # Try finding an identifier child directly
            name_node = _find_child_by_type(node, "identifier")
        if not name_node:
            return

        name = _node_text(name_node, self.source)
        qualified = f"{parent_class}.{name}" if parent_class else name
        node_id = f"{self.file_path}::{qualified}"

        # Build signature
        params = _find_child_by_field(node, "parameters")
        if not params:
            params = _find_child_by_type(node, "formal_parameter_list")
        params_text = _node_text(params, self.source) if params else "()"

        return_type = _find_child_by_field(node, "return_type")
        if not return_type:
            return_type = _find_child_by_type(node, "type_identifier")
        ret_text = f" -> {_node_text(return_type, self.source)}" if return_type else ""

        full_source = _node_text(node, self.source)
        docstring = self._get_preceding_doc_comment(node)

        # Detect annotations/decorators
        decorators = self._extract_annotations(node)

        func = NodeRecord(
            id=node_id, kind="FUNCTION", name=qualified,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=f"{qualified}{params_text}{ret_text}",
            docstring=docstring,
            source_hash=hash_source(full_source),
            full_source=full_source,
            decorators=decorators,
        )
        self.nodes.append(func)

        # DEFINES edge
        parent_id = f"{self.file_path}::{parent_class}" if parent_class else self.file_path
        self.raw_edges.append(RawEdge(source_id=parent_id, target_name=name, kind="DEFINES"))

        # Extract calls from body
        body = _find_child_by_field(node, "body")
        if not body:
            body = _find_child_by_type(node, "function_body")
        if body:
            self._extract_calls(body, node_id)

        # Extract type annotations
        self._extract_type_annotations(node, node_id)

    def _extract_class(self, node):
        """Extract class declaration + methods."""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            name_node = _find_child_by_type(node, "identifier")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        node_id = f"{self.file_path}::{name}"

        full_source = _node_text(node, self.source)
        docstring = self._get_preceding_doc_comment(node)
        decorators = self._extract_annotations(node)

        # Heritage (extends/implements/with)
        heritage_parts = []
        source_text = _node_text(node, self.source)

        # Extract extends, implements, with from source text (first line)
        first_line = source_text.split("{")[0] if "{" in source_text else source_text.split("\n")[0]
        heritage = ""
        for keyword in ("extends", "implements", "with"):
            match = re.search(rf'\b{keyword}\s+(\w+(?:\s*,\s*\w+)*)', first_line)
            if match:
                heritage += f" {keyword} {match.group(1)}"
                # Create edges for base classes/interfaces/mixins
                for base in re.findall(r'\w+', match.group(1)):
                    edge_kind = "EXTENDS" if keyword in ("extends", "with") else "USES_TYPE"
                    self.raw_edges.append(RawEdge(
                        source_id=node_id, target_name=base,
                        kind=edge_kind,
                        metadata={"base_class": base, "relation": keyword},
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
            decorators=decorators,
        )
        self.nodes.append(cls)
        self.raw_edges.append(RawEdge(source_id=self.file_path, target_name=name, kind="DEFINES"))

        # Extract methods from class body
        body = _find_child_by_field(node, "body")
        if not body:
            body = _find_child_by_type(node, "class_body")
        if body:
            for child in body.children:
                if child.type in ("method_definition", "function_definition", "function_signature"):
                    self._extract_function(child, name)
                elif child.type in ("declaration", "class_member_definition"):
                    for sub in child.children:
                        if sub.type in ("method_definition", "function_definition", "function_signature"):
                            self._extract_function(sub, name)

    def _extract_mixin(self, node):
        """Extract mixin declaration (Dart specific)."""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            name_node = _find_child_by_type(node, "identifier")
        if not name_node:
            return
        name = _node_text(name_node, self.source)
        node_id = f"{self.file_path}::{name}"

        full_source = _node_text(node, self.source)
        docstring = self._get_preceding_doc_comment(node)

        mixin = NodeRecord(
            id=node_id, kind="CLASS", name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=f"mixin {name}",
            docstring=docstring,
            source_hash=hash_source(full_source),
            full_source=full_source,
        )
        self.nodes.append(mixin)
        self.raw_edges.append(RawEdge(source_id=self.file_path, target_name=name, kind="DEFINES"))

        # Extract methods
        body = _find_child_by_field(node, "body")
        if not body:
            body = _find_child_by_type(node, "class_body")
        if body:
            for child in body.children:
                if child.type in ("method_definition", "function_definition", "function_signature"):
                    self._extract_function(child, name)

    def _extract_extension(self, node):
        """Extract extension declaration (Dart specific)."""
        # Extensions can be named or unnamed
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            name_node = _find_child_by_type(node, "identifier")

        source_text = _node_text(node, self.source)
        if name_node:
            name = _node_text(name_node, self.source)
        else:
            # Unnamed extension — use first line as name
            name = source_text.split("{")[0].strip() if "{" in source_text else "extension"

        node_id = f"{self.file_path}::{name}"
        full_source = source_text

        ext = NodeRecord(
            id=node_id, kind="CLASS", name=name,
            file_path=self.file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            language=self.language,
            signature=source_text.split("{")[0].strip() if "{" in source_text else f"extension {name}",
            source_hash=hash_source(full_source),
            full_source=full_source,
        )
        self.nodes.append(ext)
        self.raw_edges.append(RawEdge(source_id=self.file_path, target_name=name, kind="DEFINES"))

        # Extract on-type reference
        on_match = re.search(r'\bon\s+(\w+)', source_text)
        if on_match:
            on_type = on_match.group(1)
            self.raw_edges.append(RawEdge(
                source_id=node_id, target_name=on_type,
                kind="EXTENDS",
                metadata={"base_class": on_type, "relation": "on"},
                context="base_class",
            ))

        # Extract methods
        body = _find_child_by_field(node, "body")
        if not body:
            body = _find_child_by_type(node, "class_body")
        if body:
            for child in body.children:
                if child.type in ("method_definition", "function_definition", "function_signature"):
                    self._extract_function(child, name)

    def _extract_enum(self, node):
        """Extract enum declaration."""
        name_node = _find_child_by_field(node, "name")
        if not name_node:
            name_node = _find_child_by_type(node, "identifier")
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
        """Extract: import 'package:foo/bar.dart' or import '../relative.dart'"""
        source_text = _node_text(node, self.source)

        # Match import 'package:name/path.dart'
        match = re.match(r"import\s+['\"](.+?)['\"]", source_text)
        if not match:
            return

        import_path = match.group(1)

        # Extract 'as' alias
        alias_match = re.search(r'\bas\s+(\w+)', source_text)

        # Extract 'show' symbols
        show_match = re.search(r'\bshow\s+([\w\s,]+?)(?:\s*;|\s*hide)', source_text)
        if not show_match:
            show_match = re.search(r'\bshow\s+([\w\s,]+)', source_text)
        symbols = []
        if show_match:
            symbols = [s.strip() for s in show_match.group(1).split(",") if s.strip()]

        self.raw_edges.append(RawEdge(
            source_id=self.file_path,
            target_name=import_path,
            kind="IMPORTS",
            metadata={
                "symbols": symbols,
                "is_from": True,
                "module": import_path,
                "alias": alias_match.group(1) if alias_match else None,
            },
            context="import",
        ))

    def _extract_calls(self, body_node, function_id: str):
        """Extract call expressions from function body."""
        calls: dict[str, list[int]] = {}

        def _walk(node):
            # Various Dart call expression node types
            if node.type in ("call_expression", "method_invocation", "function_expression_invocation"):
                func = _find_child_by_field(node, "function")
                if not func:
                    func = _find_child_by_field(node, "name")
                if not func:
                    # Try first identifier child
                    func = _find_child_by_type(node, "identifier")
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

    def _extract_type_annotations(self, func_node, function_id: str):
        """Extract type references from parameters and return type."""
        dart_builtins = {
            "int", "double", "num", "String", "bool", "void", "dynamic",
            "Object", "Null", "Never", "Future", "Stream", "List", "Map",
            "Set", "Iterable", "Duration", "DateTime", "Function", "Type",
            "var", "final", "const",
        }

        source = _node_text(func_node, self.source)
        # Find capitalized identifiers that look like custom types
        identifiers = re.findall(r'\b([A-Z][a-zA-Z0-9_]*)\b', source)
        seen = set()
        for ident in identifiers:
            if ident not in dart_builtins and ident not in seen:
                seen.add(ident)
                self.raw_edges.append(RawEdge(
                    source_id=function_id, target_name=ident,
                    kind="USES_TYPE",
                    metadata={"usage_pattern": "unknown", "accessed_fields": []},
                    context="annotation",
                ))

    def _get_preceding_doc_comment(self, node) -> str | None:
        """Get /// doc comment or /** ... */ block comment preceding a declaration."""
        prev = node.prev_sibling
        doc_lines = []

        # Collect consecutive /// comments going backwards
        while prev and prev.type == "comment":
            text = _node_text(prev, self.source).strip()
            if text.startswith("///"):
                doc_lines.insert(0, text[3:].strip())
                prev = prev.prev_sibling
            elif text.startswith("/**"):
                # Block doc comment
                cleaned = text.strip("/* \n")
                lines = [l.strip().lstrip("* ") for l in cleaned.split("\n")]
                return "\n".join(l for l in lines if l).strip()
            else:
                break

        return "\n".join(doc_lines).strip() if doc_lines else None

    def _extract_annotations(self, node) -> list[str]:
        """Extract @annotations from a declaration."""
        annotations = []
        prev = node.prev_sibling
        while prev and prev.type == "annotation":
            annotations.insert(0, _node_text(prev, self.source).strip())
            prev = prev.prev_sibling
        # Also check metadata children
        for child in node.children:
            if child.type == "annotation" or child.type == "metadata":
                annotations.append(_node_text(child, self.source).strip())
        return annotations

    def resolve_import_path(self, module_name: str, from_file: str, project_root: Path) -> str | None:
        """Dart import resolution."""
        # package: imports — resolve to lib/ directory
        if module_name.startswith("package:"):
            # package:my_app/models/user.dart → lib/models/user.dart
            parts = module_name.split("/", 1)
            if len(parts) == 2:
                remainder = parts[1]
                candidate = Path("lib") / remainder
                if (project_root / candidate).exists():
                    return str(candidate)
            return None

        # dart: imports are stdlib
        if module_name.startswith("dart:"):
            return None

        # Relative imports
        if module_name.startswith(".") or not module_name.startswith("package:"):
            from_dir = (project_root / from_file).parent
            if module_name.endswith(".dart"):
                resolved = from_dir / module_name
            else:
                resolved = from_dir / (module_name + ".dart")
            if resolved.exists():
                try:
                    return str(resolved.relative_to(project_root))
                except ValueError:
                    pass
        return None

    def get_extensions(self) -> list[str]:
        return [".dart"]
