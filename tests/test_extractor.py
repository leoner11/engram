"""Tests for AST extractor."""

import pytest
from engram.indexer.parser import TreeSitterParser
from engram.indexer.extractor import PythonExtractor


@pytest.fixture
def parser():
    return TreeSitterParser()


def _extract(parser, source: str, file_path: str = "test.py"):
    source_bytes = source.encode()
    tree = parser.parse_bytes(source_bytes, "python")
    extractor = PythonExtractor(file_path, source_bytes, tree)
    return extractor.extract()


def test_extract_function(parser):
    nodes, edges = _extract(parser, '''
def greet(name: str) -> str:
    """Say hello."""
    return f"Hello {name}"
''')
    funcs = [n for n in nodes if n.kind == "FUNCTION"]
    assert len(funcs) == 1
    assert funcs[0].name == "greet"
    assert "def greet(name: str) -> str" in funcs[0].signature
    assert funcs[0].docstring == "Say hello."


def test_extract_class(parser):
    nodes, edges = _extract(parser, '''
class Animal:
    """A base animal."""
    def speak(self) -> str:
        return "..."
''')
    classes = [n for n in nodes if n.kind == "CLASS"]
    assert len(classes) == 1
    assert classes[0].name == "Animal"
    assert classes[0].docstring == "A base animal."

    methods = [n for n in nodes if n.kind == "FUNCTION"]
    assert len(methods) == 1
    assert methods[0].name == "Animal.speak"


def test_extract_inheritance(parser):
    nodes, edges = _extract(parser, '''
class Dog(Animal):
    def speak(self) -> str:
        return "Woof"
''')
    extends = [e for e in edges if e.kind == "EXTENDS"]
    assert len(extends) == 1
    assert extends[0].target_name == "Animal"


def test_extract_imports(parser):
    nodes, edges = _extract(parser, '''
from models import Order, OrderItem
import utils
''')
    imports = [e for e in edges if e.kind == "IMPORTS"]
    assert len(imports) == 2
    from_import = [e for e in imports if e.metadata.get("is_from")]
    assert len(from_import) == 1
    assert set(from_import[0].metadata["symbols"]) == {"Order", "OrderItem"}


def test_extract_calls(parser):
    nodes, edges = _extract(parser, '''
def process():
    validate()
    save_order(order)
    validate()
''')
    calls = [e for e in edges if e.kind == "CALLS"]
    validate_calls = [c for c in calls if c.target_name == "validate"]
    assert len(validate_calls) == 1  # Aggregated
    assert len(validate_calls[0].metadata["call_sites"]) == 2


def test_extract_type_annotations(parser):
    nodes, edges = _extract(parser, '''
def create(data: EventSchema) -> Event:
    pass
''')
    type_edges = [e for e in edges if e.kind == "USES_TYPE"]
    type_names = {e.target_name for e in type_edges}
    assert "EventSchema" in type_names
    assert "Event" in type_names


def test_extract_decorators(parser):
    nodes, edges = _extract(parser, '''
@staticmethod
@login_required
def protected():
    pass
''')
    funcs = [n for n in nodes if n.kind == "FUNCTION"]
    assert len(funcs) == 1
    assert "@staticmethod" in funcs[0].decorators
    assert "@login_required" in funcs[0].decorators


def test_export_detection_with_all(parser):
    nodes, edges = _extract(parser, '''
__all__ = ["public_func"]

def public_func():
    pass

def _private_func():
    pass

def unlisted_func():
    pass
''')
    funcs = {n.name: n for n in nodes if n.kind == "FUNCTION"}
    assert funcs["public_func"].is_exported is True
    assert funcs["_private_func"].is_exported is False
    assert funcs["unlisted_func"].is_exported is False


def test_export_detection_without_all(parser):
    nodes, edges = _extract(parser, '''
def public_func():
    pass

def _private_func():
    pass
''')
    funcs = {n.name: n for n in nodes if n.kind == "FUNCTION"}
    assert funcs["public_func"].is_exported is True
    assert funcs["_private_func"].is_exported is False


def test_file_node_created(parser):
    nodes, edges = _extract(parser, 'x = 1\n', file_path="app.py")
    file_nodes = [n for n in nodes if n.kind == "FILE"]
    assert len(file_nodes) == 1
    assert file_nodes[0].id == "app.py"


def test_defines_edges(parser):
    nodes, edges = _extract(parser, '''
def foo():
    pass

class Bar:
    def baz(self):
        pass
''')
    defines = [e for e in edges if e.kind == "DEFINES"]
    # FILE defines foo, FILE defines Bar, Bar defines baz
    assert len(defines) == 3


def test_self_method_calls(parser):
    nodes, edges = _extract(parser, '''
class MyClass:
    def run(self):
        self.validate()
        self.save()

    def validate(self):
        pass

    def save(self):
        pass
''')
    calls = [e for e in edges if e.kind == "CALLS"]
    self_calls = [c for c in calls if c.target_name.startswith("self.")]
    assert len(self_calls) == 2


def test_fixture_models(parser, simple_project_path):
    source = (simple_project_path / "models.py").read_bytes()
    tree = parser.parse_bytes(source, "python")
    extractor = PythonExtractor("models.py", source, tree)
    nodes, edges = extractor.extract()

    classes = [n for n in nodes if n.kind == "CLASS"]
    assert len(classes) == 2  # OrderItem, Order

    methods = [n for n in nodes if n.kind == "FUNCTION"]
    assert len(methods) >= 3  # total, total, item_count
