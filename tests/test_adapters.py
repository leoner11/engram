"""Tests for TypeScript/JavaScript adapters and multi-language indexing."""

import pytest
import shutil
from pathlib import Path

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.indexer.parser import TreeSitterParser
from engram.indexer.adapters.typescript_adapter import TypeScriptAdapter
from engram.indexer.adapters.javascript_adapter import JavaScriptAdapter
from engram.cli import build_index


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def ts_parser():
    return TreeSitterParser()


@pytest.fixture
def ts_adapter():
    return TypeScriptAdapter()


def _extract_ts(parser, adapter, source: str, file_path: str = "test.ts"):
    source_bytes = source.encode()
    tree = parser.parse_bytes(source_bytes, "typescript")
    return adapter.extract(tree, source_bytes, file_path)


# --- TypeScript extraction ---

def test_ts_function_declaration(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
function greet(name: string): string {
  return `Hello ${name}`;
}
''')
    funcs = [n for n in nodes if n.kind == "FUNCTION"]
    assert len(funcs) == 1
    assert funcs[0].name == "greet"
    assert "string" in funcs[0].signature


def test_ts_arrow_function(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
const handleClick = (event: MouseEvent): void => {
  console.log(event);
};
''')
    funcs = [n for n in nodes if n.kind == "FUNCTION"]
    assert len(funcs) == 1
    assert funcs[0].name == "handleClick"


def test_ts_class(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
class UserService {
  getUser(id: string): User {
    return this.users.get(id);
  }
  
  addUser(user: User): void {
    this.users.set(user.id, user);
  }
}
''')
    classes = [n for n in nodes if n.kind == "CLASS"]
    assert len(classes) == 1
    assert classes[0].name == "UserService"

    methods = [n for n in nodes if n.kind == "FUNCTION" and "." in n.name]
    assert len(methods) == 2
    method_names = {m.name for m in methods}
    assert "UserService.getUser" in method_names
    assert "UserService.addUser" in method_names


def test_ts_interface(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
interface User {
  id: string;
  name: string;
}
''')
    types = [n for n in nodes if n.kind == "TYPE"]
    assert len(types) == 1
    assert types[0].name == "User"


def test_ts_type_alias(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
type Config = {
  port: number;
  host: string;
};
''')
    types = [n for n in nodes if n.kind == "TYPE"]
    assert len(types) == 1
    assert types[0].name == "Config"


def test_ts_enum(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
enum Status {
  Active = "active",
  Inactive = "inactive",
}
''')
    types = [n for n in nodes if n.kind == "TYPE"]
    assert len(types) == 1
    assert types[0].name == "Status"


def test_ts_export_detection(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
export function publicFunc(): void {}

function privateFunc(): void {}

export class PublicClass {}
''')
    funcs = {n.name: n for n in nodes if n.kind in ("FUNCTION", "CLASS")}
    assert funcs["publicFunc"].is_exported is True
    assert funcs["privateFunc"].is_exported is False
    assert funcs["PublicClass"].is_exported is True


def test_ts_imports(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
import { User, UserRole } from "./models";
import defaultExport from "./utils";
''')
    imports = [e for e in edges if e.kind == "IMPORTS"]
    assert len(imports) >= 1
    symbols = []
    for imp in imports:
        symbols.extend(imp.metadata.get("symbols", []))
    assert "User" in symbols


def test_ts_calls(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
function main(): void {
  const user = createUser("Alice");
  console.log(user);
  validateEmail(user.email);
}
''')
    calls = [e for e in edges if e.kind == "CALLS"]
    targets = {c.target_name for c in calls}
    assert "createUser" in targets
    assert "validateEmail" in targets


def test_ts_type_usage(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
function processUser(user: User): Response {
  return { ok: true };
}
''')
    type_edges = [e for e in edges if e.kind == "USES_TYPE"]
    type_names = {e.target_name for e in type_edges}
    assert "User" in type_names
    assert "Response" in type_names


def test_ts_inheritance(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
class AdminService extends UserService {
  promote(id: string): void {}
}
''')
    extends = [e for e in edges if e.kind == "EXTENDS"]
    assert len(extends) >= 1
    assert extends[0].target_name == "UserService"


def test_ts_jsdoc(ts_parser, ts_adapter):
    nodes, edges = _extract_ts(ts_parser, ts_adapter, '''
/** Creates a new user with defaults. */
function createUser(name: string): User {
  return { id: "1", name };
}
''')
    funcs = [n for n in nodes if n.kind == "FUNCTION"]
    assert len(funcs) == 1
    assert funcs[0].docstring is not None
    assert "Creates a new user" in funcs[0].docstring


# --- JavaScript adapter ---

def test_js_inherits_ts(ts_parser):
    adapter = JavaScriptAdapter()
    source = b'''
function hello(name) {
  return "Hello " + name;
}

const greet = (name) => {
  return hello(name);
};
'''
    tree = ts_parser.parse_bytes(source, "javascript")
    nodes, edges = adapter.extract(tree, source, "test.js")
    funcs = [n for n in nodes if n.kind == "FUNCTION"]
    assert len(funcs) == 2


# --- Full build on TypeScript fixture ---

def test_ts_project_build(tmp_path):
    project = tmp_path / "ts_project"
    shutil.copytree(FIXTURES_DIR / "typescript_project", project)

    db = EngramDB(project)
    stats = build_index(project, db, force=True)

    assert stats["files_scanned"] == 3
    assert stats["node_count"] >= 10  # models + service + app
    assert stats["edge_count"] >= 10
    db.close()


def test_ts_project_query(tmp_path):
    project = tmp_path / "ts_project"
    shutil.copytree(FIXTURES_DIR / "typescript_project", project)

    db = EngramDB(project)
    build_index(project, db, force=True)
    store = GraphStore(db)

    from engram.retriever.assembler import ContextAssembler
    assembler = ContextAssembler(store, project_root=project)
    package = assembler.assemble("fix createUser function")

    assert len(package.seeds) > 0
    assert len(package.nodes) > 0
    seed_ids = {cn.node.id for cn in package.nodes if cn.depth == 0}
    assert any("createUser" in sid for sid in seed_ids)
    db.close()
