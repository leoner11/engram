"""Tests for tree-sitter parser."""

import pytest
from engram.indexer.parser import TreeSitterParser, ParseError


@pytest.fixture
def parser():
    return TreeSitterParser()


def test_parse_simple_function(parser):
    source = b'def hello():\n    return "world"\n'
    tree = parser.parse_bytes(source, "python")
    assert tree is not None
    assert tree.root_node.type == "module"


def test_parse_class(parser):
    source = b'class Foo:\n    def bar(self):\n        pass\n'
    tree = parser.parse_bytes(source, "python")
    root = tree.root_node
    class_node = root.children[0]
    assert class_node.type == "class_definition"


def test_parse_syntax_error_tolerant(parser):
    source = b'def broken(\n    return\n'
    tree = parser.parse_bytes(source, "python")
    # tree-sitter is error-tolerant, should still return a tree
    assert tree is not None


def test_parse_file(parser, simple_project_path):
    tree = parser.parse_file(simple_project_path / "models.py", "python")
    assert tree is not None


def test_parse_file_not_found(parser, tmp_path):
    with pytest.raises(ParseError):
        parser.parse_file(tmp_path / "nonexistent.py", "python")
