"""Shared test fixtures."""

import pytest
from pathlib import Path

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.cli import build_index


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def simple_project_path():
    return FIXTURES_DIR / "simple_project"


@pytest.fixture
def mixed_project_path():
    return FIXTURES_DIR / "mixed_project"


@pytest.fixture
def simple_project_db(simple_project_path, tmp_path):
    """Build index for simple_project in a temp directory."""
    import shutil
    # Copy fixture to temp so .engram/ doesn't pollute fixtures
    project = tmp_path / "simple_project"
    shutil.copytree(simple_project_path, project)

    db = EngramDB(project)
    build_index(project, db, force=True)
    yield db, project
    db.close()


@pytest.fixture
def simple_store(simple_project_db):
    """GraphStore for the simple project."""
    db, project = simple_project_db
    return GraphStore(db), project
