"""SHA256 hashing for files and source text. Used for incremental indexing."""

import hashlib
from pathlib import Path


def hash_file(path: Path) -> str:
    """SHA256 of file contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_source(source: str) -> str:
    """SHA256 of source text string."""
    return hashlib.sha256(source.encode()).hexdigest()
