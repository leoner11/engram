"""Scope-aware target resolution. Converts RawEdge.target_name → Edge.target_id."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from engram.indexer.extractor import NodeRecord, RawEdge


@dataclass
class Edge:
    source_id: str
    target_id: str
    kind: str
    metadata: dict = field(default_factory=dict)


# Known stdlib top-level modules (not exhaustive, covers the common ones)
STDLIB_MODULES = {
    "os", "sys", "re", "json", "math", "datetime", "time", "pathlib",
    "collections", "functools", "itertools", "typing", "abc", "enum",
    "dataclasses", "contextlib", "logging", "unittest", "hashlib",
    "sqlite3", "io", "copy", "string", "textwrap", "struct", "csv",
    "argparse", "subprocess", "threading", "multiprocessing", "socket",
    "http", "urllib", "email", "html", "xml", "pprint", "traceback",
    "warnings", "inspect", "importlib", "pkgutil", "tempfile", "shutil",
    "glob", "fnmatch", "stat", "uuid", "secrets", "hmac", "base64",
    "pickle", "shelve", "marshal", "configparser", "tomllib",
    "asyncio", "concurrent", "signal", "queue", "heapq", "bisect",
    "array", "decimal", "fractions", "random", "statistics",
    "operator", "weakref", "types", "codecs", "locale",
    "pdb", "dis", "ast", "token", "tokenize", "compileall",
    "platform", "ctypes", "mmap", "select", "selectors",
    "ssl", "ftplib", "smtplib", "imaplib", "poplib",
    "xmlrpc", "zipfile", "tarfile", "gzip", "bz2", "lzma",
    "gettext", "calendar",
}

BUILTIN_CALLS = {
    "print", "len", "range", "isinstance", "issubclass", "type",
    "int", "str", "float", "bool", "list", "dict", "tuple", "set",
    "frozenset", "bytes", "bytearray", "memoryview",
    "abs", "all", "any", "bin", "chr", "dir", "divmod", "enumerate",
    "eval", "exec", "filter", "format", "getattr", "hasattr", "hash",
    "hex", "id", "input", "iter", "map", "max", "min", "next", "oct",
    "open", "ord", "pow", "repr", "reversed", "round", "setattr",
    "slice", "sorted", "sum", "super", "vars", "zip", "callable",
    "classmethod", "staticmethod", "property",
    "breakpoint", "compile", "complex", "delattr",
}


class Resolver:
    """Resolves raw edges to concrete graph edges using scope and import analysis."""

    def __init__(
        self,
        all_nodes: dict[str, NodeRecord],
        all_raw_edges: list[RawEdge],
        project_root: Path,
    ):
        self.all_nodes = all_nodes
        self.all_raw_edges = all_raw_edges
        self.project_root = project_root

        # Build lookup tables
        self.name_to_nodes: dict[str, list[str]] = {}  # name -> [node_ids]
        for node_id, node in all_nodes.items():
            base_name = node.name.split(".")[-1]  # For methods, get just the method name
            self.name_to_nodes.setdefault(node.name, []).append(node_id)
            if base_name != node.name:
                self.name_to_nodes.setdefault(base_name, []).append(node_id)

        # All file paths for fast lookup
        self._all_file_paths = {n.file_path for n in all_nodes.values() if n.kind == "FILE"}

        # Load tsconfig path aliases if available
        self._ts_path_aliases = self._load_tsconfig_paths()

        # Stats
        self.resolved_count = 0
        self.unresolved_stdlib = 0
        self.unresolved_third_party = 0
        self.unresolved_dynamic = 0

        # Build per-file import maps from IMPORTS raw edges
        self.import_map: dict[str, dict[str, str]] = {}  # file -> {symbol: node_id}
        self._build_import_maps()

    def _build_import_maps(self):
        """Build per-file symbol → node_id mapping from IMPORTS edges."""
        for edge in self.all_raw_edges:
            if edge.kind != "IMPORTS":
                continue

            file_path = edge.source_id  # IMPORTS edges come from FILE nodes
            if file_path not in self.import_map:
                self.import_map[file_path] = {}

            module_name = edge.target_name
            symbols = edge.metadata.get("symbols", [])
            is_from = edge.metadata.get("is_from", False)

            # Resolve the module to a file path
            resolved_file = self._resolve_module(module_name, file_path)
            if resolved_file is None:
                # Check if it's stdlib or third-party
                top_module = module_name.lstrip(".").split(".")[0]
                if top_module in STDLIB_MODULES:
                    self.unresolved_stdlib += 1
                else:
                    self.unresolved_third_party += 1
                continue

            if is_from:
                # from foo.bar import Baz, Qux
                for sym in symbols:
                    if sym == "*":
                        continue  # Skip wildcard imports
                    # Try to find sym as a node in the resolved file
                    candidates = [
                        f"{resolved_file}::{sym}",
                        # Also check if it's re-exported from __init__.py
                    ]
                    for cand in candidates:
                        if cand in self.all_nodes:
                            self.import_map[file_path][sym] = cand
                            break
            else:
                # import foo — the module itself
                if resolved_file in self.all_nodes:
                    self.import_map[file_path][module_name.split(".")[-1]] = resolved_file

    def _load_tsconfig_paths(self) -> dict[str, str]:
        """Load path aliases from tsconfig.json if present.

        Returns: {"@/*": "src/*", "@components/*": "src/components/*", ...}
        Resolved to actual directory prefixes for matching.
        """
        aliases = {}
        for tsconfig_name in ["tsconfig.json", "tsconfig.app.json"]:
            tsconfig_path = self.project_root / tsconfig_name
            if tsconfig_path.exists():
                try:
                    import json
                    data = json.loads(tsconfig_path.read_text(encoding="utf-8"))
                    compiler_opts = data.get("compilerOptions", {})
                    paths = compiler_opts.get("paths", {})
                    base_url = compiler_opts.get("baseUrl", ".")

                    for alias_pattern, targets in paths.items():
                        if targets and isinstance(targets, list):
                            # "@/*" → "src/*": strip the /* suffix for prefix matching
                            alias_prefix = alias_pattern.rstrip("*").rstrip("/")
                            target_prefix = targets[0].rstrip("*").rstrip("/")
                            # Combine with baseUrl
                            if base_url != ".":
                                target_prefix = f"{base_url}/{target_prefix}"
                            aliases[alias_prefix] = target_prefix
                except Exception:
                    pass
                break  # Use first found tsconfig
        return aliases

    def _resolve_module(self, module_name: str, from_file: str) -> str | None:
        """Resolve a module name to a file path within the project.

        Handles:
        - Python dot imports: from models import Event
        - Python relative imports: from .models import Event
        - TS/JS relative imports: ./useEvents, ../utils
        - TS path aliases: @/components/Button (via tsconfig.json)
        """
        # Detect language from file extension
        is_ts_js = from_file.endswith(('.ts', '.tsx', '.js', '.jsx'))

        if is_ts_js:
            return self._resolve_ts_import(module_name, from_file)

        # Python: relative imports
        if module_name.startswith("."):
            return self._resolve_relative_import(module_name, from_file)

        # Python: dot notation to path
        parts = module_name.split(".")
        candidates = [
            "/".join(parts) + ".py",
            "/".join(parts) + "/__init__.py",
        ]
        for candidate in list(candidates):
            candidates.append("src/" + candidate)

        for candidate in candidates:
            if candidate in self._all_file_paths:
                return candidate

        return None

    def _resolve_ts_import(self, module_name: str, from_file: str) -> str | None:
        """Resolve TypeScript/JavaScript import specifiers."""
        # Strip quotes if present
        module_name = module_name.strip("'\"")

        # Skip bare package imports (react, lodash, etc.)
        if not module_name.startswith(".") and not module_name.startswith("@/") and not module_name.startswith("~/"):
            # Check tsconfig aliases
            for alias_prefix, target_prefix in self._ts_path_aliases.items():
                if module_name.startswith(alias_prefix):
                    remainder = module_name[len(alias_prefix):].lstrip("/")
                    resolved_base = f"{target_prefix}/{remainder}" if remainder else target_prefix
                    result = self._try_ts_extensions(resolved_base)
                    if result:
                        return result
            # Not a local import and no alias match — third party
            return None

        # Relative imports: ./foo, ../bar
        if module_name.startswith("."):
            from_dir = str(Path(from_file).parent)
            # Resolve the relative path
            if module_name.startswith("./"):
                resolved_base = f"{from_dir}/{module_name[2:]}" if from_dir != "." else module_name[2:]
            elif module_name.startswith("../"):
                parent = str(Path(from_dir).parent)
                remainder = module_name[3:]
                resolved_base = f"{parent}/{remainder}" if parent != "." else remainder
            else:
                resolved_base = module_name

            # Normalize
            resolved_base = str(Path(resolved_base))
            return self._try_ts_extensions(resolved_base)

        # Path alias imports: @/components/Button
        for alias_prefix, target_prefix in self._ts_path_aliases.items():
            if module_name.startswith(alias_prefix):
                remainder = module_name[len(alias_prefix):].lstrip("/")
                resolved_base = f"{target_prefix}/{remainder}" if remainder else target_prefix
                result = self._try_ts_extensions(resolved_base)
                if result:
                    return result

        return None

    def _try_ts_extensions(self, base_path: str) -> str | None:
        """Try multiple TS/JS extensions to find a matching file."""
        candidates = [
            base_path,                  # exact match (already has extension)
            base_path + ".ts",
            base_path + ".tsx",
            base_path + ".js",
            base_path + ".jsx",
            base_path + "/index.ts",
            base_path + "/index.tsx",
            base_path + "/index.js",
            base_path + "/index.jsx",
        ]
        for candidate in candidates:
            normalized = str(Path(candidate))
            if normalized in self._all_file_paths:
                return normalized
        return None

    def _resolve_relative_import(self, module_name: str, from_file: str) -> str | None:
        """Resolve a relative import like .models or ..utils."""
        # Count dots
        dots = 0
        for ch in module_name:
            if ch == ".":
                dots += 1
            else:
                break

        remainder = module_name[dots:]
        from_dir = Path(from_file).parent

        # Go up (dots - 1) directories
        for _ in range(dots - 1):
            from_dir = from_dir.parent

        if remainder:
            parts = remainder.split(".")
            base = from_dir / "/".join(parts)
        else:
            base = from_dir

        candidates = [
            str(base) + ".py",
            str(base / "__init__.py"),
        ]

        for candidate in candidates:
            # Normalize path
            candidate = str(Path(candidate))
            if candidate in self._all_file_paths:
                return candidate

        return None

    def resolve_all(self) -> list[Edge]:
        """Resolve all raw edges, return resolved Edge objects."""
        resolved: list[Edge] = []

        for raw in self.all_raw_edges:
            if raw.kind == "DEFINES":
                # DEFINES edges are already well-formed (source is parent, target is child)
                # Just find the actual target node ID
                edge = self._resolve_defines(raw)
                if edge:
                    resolved.append(edge)
            elif raw.kind == "IMPORTS":
                # IMPORTS edges become edges from file to file
                edge = self._resolve_import_edge(raw)
                if edge:
                    resolved.append(edge)
            elif raw.kind == "CALLS":
                edge = self._resolve_call(raw)
                if edge:
                    resolved.append(edge)
            elif raw.kind == "USES_TYPE":
                edge = self._resolve_type_usage(raw)
                if edge:
                    resolved.append(edge)
            elif raw.kind == "EXTENDS":
                edge = self._resolve_extends(raw)
                if edge:
                    resolved.append(edge)

        self.resolved_count = len(resolved)

        # Post-pass: infer usage_pattern for USES_TYPE edges
        self._infer_usage_patterns(resolved)

        return resolved

    def _infer_usage_patterns(self, edges: list[Edge]):
        """Determine exhaustive/partial/passthrough for USES_TYPE edges.

        For each USES_TYPE edge, compare accessed_fields against the target
        class/type's total field count to classify the usage pattern:
        - exhaustive: accesses all or most (>80%) of the type's fields
        - partial: accesses some specific fields
        - passthrough: no fields accessed (just forwarding the type)
        """
        for edge in edges:
            if edge.kind != "USES_TYPE":
                continue
            if edge.metadata.get("usage_pattern") not in ("unknown", None):
                continue  # Already classified (e.g. from bridge or pattern)

            accessed = edge.metadata.get("accessed_fields", [])
            target_node = self.all_nodes.get(edge.target_id)

            if not accessed:
                edge.metadata["usage_pattern"] = "passthrough"
                continue

            if target_node is None or target_node.kind not in ("CLASS", "TYPE"):
                edge.metadata["usage_pattern"] = "partial"
                continue

            # Count the target's fields: child nodes that are methods/properties
            # or parse from source for dataclass fields
            target_fields = self._count_type_fields(target_node)

            if target_fields == 0:
                edge.metadata["usage_pattern"] = "partial"
            elif len(accessed) >= target_fields * 0.8:
                edge.metadata["usage_pattern"] = "exhaustive"
            else:
                edge.metadata["usage_pattern"] = "partial"

    def _count_type_fields(self, node: NodeRecord) -> int:
        """Count fields/attributes of a class/type node.

        Uses child nodes (methods defined on the class) and simple
        source parsing for dataclass/attr fields.
        """
        import re
        count = 0

        # Count child nodes (methods, properties) defined on this class
        prefix = node.id + "."
        for nid in self.all_nodes:
            if nid.startswith(prefix):
                count += 1

        # Also try to count fields from source
        if node.full_source:
            source = node.full_source

            if node.language in ("typescript", "javascript"):
                # TypeScript/JS interface/type fields: "  fieldName?: Type"
                ts_fields = re.findall(r'^\s+(\w+)\s*[?]?\s*:', source, re.MULTILINE)
                count += len(ts_fields)
            else:
                # Python dataclass/regular class fields: "  field_name: Type" or "  field = value"
                field_lines = re.findall(r'^\s+(\w+)\s*[:=]', source, re.MULTILINE)
                for f in field_lines:
                    if not f.startswith("_") and f not in ("def", "class", "self", "return"):
                        count += 1

        return count

    def _resolve_defines(self, raw: RawEdge) -> Edge | None:
        """Resolve a DEFINES edge."""
        # Source is already a valid node ID (file or class)
        if raw.source_id not in self.all_nodes:
            return None

        # Find the target node
        target_id = None
        source_node = self.all_nodes[raw.source_id]

        if source_node.kind == "FILE":
            # Top-level definition
            target_id = f"{raw.source_id}::{raw.target_name}"
        elif source_node.kind == "CLASS":
            # Method definition
            class_name = source_node.name
            target_id = f"{source_node.file_path}::{class_name}.{raw.target_name}"
        else:
            target_id = f"{source_node.file_path}::{raw.target_name}"

        if target_id in self.all_nodes:
            return Edge(
                source_id=raw.source_id,
                target_id=target_id,
                kind="DEFINES",
                metadata=raw.metadata,
            )
        return None

    def _resolve_import_edge(self, raw: RawEdge) -> Edge | None:
        """Resolve an IMPORTS edge to a file-to-file edge."""
        resolved_file = self._resolve_module(raw.target_name, raw.source_id)
        if resolved_file and resolved_file in self.all_nodes:
            return Edge(
                source_id=raw.source_id,
                target_id=resolved_file,
                kind="IMPORTS",
                metadata=raw.metadata,
            )
        return None

    def _resolve_call(self, raw: RawEdge) -> Edge | None:
        """Resolve a CALLS edge."""
        target_name = raw.target_name
        source_id = raw.source_id
        source_node = self.all_nodes.get(source_id)
        if not source_node:
            return None

        file_path = source_node.file_path

        # Skip builtins
        base_name = target_name.split(".")[-1] if "." in target_name else target_name
        if base_name in BUILTIN_CALLS:
            return None

        # Try resolving in order of specificity

        # 1. self.method() — resolve within the same class
        if target_name.startswith("self."):
            method_name = target_name[5:]  # strip "self."
            # Find which class this function belongs to
            class_name = self._get_parent_class(source_id)
            if class_name:
                candidate = f"{file_path}::{class_name}.{method_name}"
                if candidate in self.all_nodes:
                    return Edge(source_id=source_id, target_id=candidate, kind="CALLS", metadata=raw.metadata)

        # 2. Direct name in same file
        candidate = f"{file_path}::{target_name}"
        if candidate in self.all_nodes:
            return Edge(source_id=source_id, target_id=candidate, kind="CALLS", metadata=raw.metadata)

        # 3. Imported symbol
        file_imports = self.import_map.get(file_path, {})
        if target_name in file_imports:
            return Edge(source_id=source_id, target_id=file_imports[target_name], kind="CALLS", metadata=raw.metadata)

        # 4. Chained attribute call: foo.bar() — try to resolve foo's type
        if "." in target_name and not target_name.startswith("self."):
            parts = target_name.split(".")
            obj_name = parts[0]
            method = parts[-1]

            # Check if obj_name is an imported class, then look for .method
            if obj_name in file_imports:
                obj_node_id = file_imports[obj_name]
                candidate = f"{obj_node_id}.{method}" if obj_node_id in self.all_nodes else None
                # Actually, the node ID for methods uses :: not .
                obj_node = self.all_nodes.get(obj_node_id)
                if obj_node and obj_node.kind == "CLASS":
                    method_id = f"{obj_node.file_path}::{obj_node.name}.{method}"
                    if method_id in self.all_nodes:
                        return Edge(source_id=source_id, target_id=method_id, kind="CALLS", metadata=raw.metadata)

        # 5. Global name lookup (any node with this name)
        if target_name in self.name_to_nodes:
            candidates = self.name_to_nodes[target_name]
            if len(candidates) == 1:
                return Edge(source_id=source_id, target_id=candidates[0], kind="CALLS", metadata=raw.metadata)
            # Ambiguous — prefer same-file, then imported
            for cand in candidates:
                if cand.startswith(file_path + "::"):
                    return Edge(source_id=source_id, target_id=cand, kind="CALLS", metadata=raw.metadata)

        self.unresolved_dynamic += 1
        return None

    def _resolve_type_usage(self, raw: RawEdge) -> Edge | None:
        """Resolve a USES_TYPE edge."""
        source_id = raw.source_id
        source_node = self.all_nodes.get(source_id)
        if not source_node:
            return None

        file_path = source_node.file_path
        target_name = raw.target_name

        # Check import map
        file_imports = self.import_map.get(file_path, {})
        if target_name in file_imports:
            return Edge(source_id=source_id, target_id=file_imports[target_name], kind="USES_TYPE", metadata=raw.metadata)

        # Check same file
        candidate = f"{file_path}::{target_name}"
        if candidate in self.all_nodes:
            return Edge(source_id=source_id, target_id=candidate, kind="USES_TYPE", metadata=raw.metadata)

        # Global lookup
        if target_name in self.name_to_nodes:
            candidates = self.name_to_nodes[target_name]
            if len(candidates) == 1:
                return Edge(source_id=source_id, target_id=candidates[0], kind="USES_TYPE", metadata=raw.metadata)

        return None

    def _resolve_extends(self, raw: RawEdge) -> Edge | None:
        """Resolve an EXTENDS edge."""
        source_id = raw.source_id
        source_node = self.all_nodes.get(source_id)
        if not source_node:
            return None

        file_path = source_node.file_path
        target_name = raw.target_name

        # Check import map
        file_imports = self.import_map.get(file_path, {})
        if target_name in file_imports:
            return Edge(source_id=source_id, target_id=file_imports[target_name], kind="EXTENDS", metadata=raw.metadata)

        # Same file
        candidate = f"{file_path}::{target_name}"
        if candidate in self.all_nodes:
            return Edge(source_id=source_id, target_id=candidate, kind="EXTENDS", metadata=raw.metadata)

        # Third-party base class — drop
        return None

    def _get_parent_class(self, function_id: str) -> str | None:
        """Get the class name for a method node ID."""
        # Node ID pattern: "file.py::ClassName.method_name"
        node = self.all_nodes.get(function_id)
        if node and "." in node.name:
            return node.name.split(".")[0]
        return None

    def get_stats(self) -> dict:
        """Return resolution statistics."""
        return {
            "resolved": self.resolved_count,
            "unresolved_stdlib": self.unresolved_stdlib,
            "unresolved_third_party": self.unresolved_third_party,
            "unresolved_dynamic": self.unresolved_dynamic,
        }
