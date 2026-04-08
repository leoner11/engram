"""Tests for edge resolver."""

import pytest
from engram.graph.store import GraphStore


def test_resolver_resolves_calls(simple_store):
    store, project = simple_store
    # service.py::process_order calls save_order (from repository)
    edges = store.get_edges_from("service.py::process_order")
    call_edges = [e for e in edges if e.kind == "CALLS"]
    call_targets = {e.target_id for e in call_edges}
    assert "repository.py::save_order" in call_targets


def test_resolver_resolves_imports(simple_store):
    store, project = simple_store
    # service.py imports from models, repository, utils
    edges = store.get_edges_from("service.py")
    import_edges = [e for e in edges if e.kind == "IMPORTS"]
    assert len(import_edges) >= 1


def test_resolver_self_calls(simple_store):
    store, project = simple_store
    # Order.total calls item.total via self — but these are on OrderItem instances
    # At least verify methods exist
    node = store.get_node("models.py::Order.total")
    assert node is not None
    assert node.kind == "FUNCTION"


def test_resolver_drops_stdlib(simple_store):
    store, project = simple_store
    # uuid, datetime are stdlib — should not appear as edge targets
    all_edges_from_service = store.get_edges_from("service.py")
    targets = {e.target_id for e in all_edges_from_service}
    # No node IDs should point to uuid or datetime modules
    for t in targets:
        assert "uuid" not in t or "::" in t  # uuid as a module won't be a node


def test_resolver_defines_edges(simple_store):
    store, project = simple_store
    # File defines its top-level functions
    edges = store.get_edges_from("utils.py")
    defines = [e for e in edges if e.kind == "DEFINES"]
    defined_names = {e.target_id for e in defines}
    assert "utils.py::format_currency" in defined_names
    assert "utils.py::validate_user_id" in defined_names


# ─── Phase E: Usage pattern inference + TS path aliases ────────────────


def test_usage_pattern_inference(simple_store):
    """USES_TYPE edges should have inferred usage_pattern, not 'unknown'."""
    store, project = simple_store
    all_nodes = store.get_all_nodes()

    for node_id, node in all_nodes.items():
        edges = store.get_edges_from(node_id)
        for edge in edges:
            if edge.kind == "USES_TYPE":
                pattern = edge.metadata.get("usage_pattern", "unknown")
                assert pattern in ("exhaustive", "partial", "passthrough"), \
                    f"Edge {edge.source_id}→{edge.target_id} has usage_pattern={pattern}"


def test_exhaustive_detection():
    """Direct test: accessing all fields of a class → exhaustive."""
    from engram.indexer.resolver import Resolver
    from engram.indexer.extractor import NodeRecord, RawEdge
    from pathlib import Path

    nodes = {
        'f.py': NodeRecord(id='f.py', kind='FILE', name='f.py', file_path='f.py',
                           line_start=1, line_end=1, language='python', signature='', docstring='',
                           source_hash='x', is_exported=False, decorators=[], full_source=''),
        'f.py::Item': NodeRecord(id='f.py::Item', kind='CLASS', name='Item', file_path='f.py',
                                  line_start=1, line_end=5, language='python', signature='class Item',
                                  docstring='', source_hash='x', is_exported=False, decorators=[],
                                  full_source='class Item:\n    name: str\n    price: float\n'),
        'f.py::use': NodeRecord(id='f.py::use', kind='FUNCTION', name='use', file_path='f.py',
                                 line_start=6, line_end=8, language='python', signature='def use()',
                                 docstring='', source_hash='x', is_exported=False, decorators=[], full_source=''),
    }
    raw = [RawEdge(source_id='f.py::use', target_name='Item', kind='USES_TYPE',
                   metadata={'usage_pattern': 'unknown', 'accessed_fields': ['name', 'price']})]

    resolver = Resolver(nodes, raw, Path('.'))
    resolved = resolver.resolve_all()
    uses_type = [e for e in resolved if e.kind == 'USES_TYPE']
    assert len(uses_type) == 1
    assert uses_type[0].metadata['usage_pattern'] == 'exhaustive'


def test_passthrough_detection():
    """No fields accessed → passthrough."""
    from engram.indexer.resolver import Resolver
    from engram.indexer.extractor import NodeRecord, RawEdge
    from pathlib import Path

    nodes = {
        'f.py': NodeRecord(id='f.py', kind='FILE', name='f.py', file_path='f.py',
                           line_start=1, line_end=1, language='python', signature='', docstring='',
                           source_hash='x', is_exported=False, decorators=[], full_source=''),
        'f.py::Item': NodeRecord(id='f.py::Item', kind='CLASS', name='Item', file_path='f.py',
                                  line_start=1, line_end=5, language='python', signature='class Item',
                                  docstring='', source_hash='x', is_exported=False, decorators=[],
                                  full_source='class Item:\n    name: str\n'),
        'f.py::forward': NodeRecord(id='f.py::forward', kind='FUNCTION', name='forward', file_path='f.py',
                                     line_start=6, line_end=8, language='python', signature='def forward()',
                                     docstring='', source_hash='x', is_exported=False, decorators=[], full_source=''),
    }
    raw = [RawEdge(source_id='f.py::forward', target_name='Item', kind='USES_TYPE',
                   metadata={'usage_pattern': 'unknown', 'accessed_fields': []})]

    resolver = Resolver(nodes, raw, Path('.'))
    resolved = resolver.resolve_all()
    uses_type = [e for e in resolved if e.kind == 'USES_TYPE']
    assert len(uses_type) == 1
    assert uses_type[0].metadata['usage_pattern'] == 'passthrough'


def test_ts_path_alias_loading(tmp_path):
    """Resolver loads tsconfig.json path aliases."""
    from engram.indexer.resolver import Resolver
    from pathlib import Path
    import json

    # Create a tsconfig.json
    tsconfig = {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {
                "@/*": ["src/*"],
                "@components/*": ["src/components/*"],
            }
        }
    }
    (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig))

    resolver = Resolver({}, [], tmp_path)
    assert "@" in resolver._ts_path_aliases
    assert resolver._ts_path_aliases["@"] == "src"


def test_ts_resolve_relative_import():
    """TS relative import ./foo resolves to foo.ts."""
    from engram.indexer.resolver import Resolver
    from engram.indexer.extractor import NodeRecord
    from pathlib import Path

    nodes = {
        'src/hooks/useEvents.ts': NodeRecord(id='src/hooks/useEvents.ts', kind='FILE',
            name='useEvents.ts', file_path='src/hooks/useEvents.ts',
            line_start=1, line_end=10, language='typescript', signature='', docstring='',
            source_hash='x', is_exported=False, decorators=[], full_source=''),
        'src/hooks/utils.ts': NodeRecord(id='src/hooks/utils.ts', kind='FILE',
            name='utils.ts', file_path='src/hooks/utils.ts',
            line_start=1, line_end=10, language='typescript', signature='', docstring='',
            source_hash='x', is_exported=False, decorators=[], full_source=''),
    }

    resolver = Resolver(nodes, [], Path('.'))
    result = resolver._resolve_ts_import('./utils', 'src/hooks/useEvents.ts')
    assert result == 'src/hooks/utils.ts'
