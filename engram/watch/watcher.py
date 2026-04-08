"""File system watcher: auto-rebuild index on changes with debounce."""

from __future__ import annotations

import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from engram.db import EngramDB
from engram.graph.store import GraphStore
from engram.indexer.languages import detect_language
from engram.indexer.scanner import ALWAYS_SKIP


class EngramWatcher:
    """Watch project directory for changes, re-index incrementally."""

    def __init__(self, root: Path, db: EngramDB):
        self.root = root
        self.db = db
        self.store = GraphStore(db)
        self.observer = Observer()
        self._debounce_delay = 0.5
        self._pending_changes: dict[str, float] = {}

    def start(self):
        """Start watching. Blocks until Ctrl+C."""
        handler = _ChangeHandler(self)
        self.observer.schedule(handler, str(self.root), recursive=True)
        self.observer.start()
        print(f"Watching {self.root} for changes... (Ctrl+C to stop)")
        try:
            while True:
                self._process_pending()
                time.sleep(self._debounce_delay)
        except KeyboardInterrupt:
            self.observer.stop()
            print("\nStopped watching.")
        self.observer.join()

    def _process_pending(self):
        """Process pending changes after debounce window."""
        now = time.time()
        ready = {
            path: ts for path, ts in self._pending_changes.items()
            if now - ts >= self._debounce_delay
        }
        if not ready:
            return

        for path in ready:
            del self._pending_changes[path]

        self._reindex_files(list(ready.keys()))

    def _reindex_files(self, paths: list[str]):
        """Incremental re-index for changed files."""
        from engram.cli import build_index

        t0 = time.time()
        stats = build_index(self.root, self.db, force=False)
        elapsed = time.time() - t0

        if stats.get("files_changed", 0) > 0:
            print(
                f"  Re-indexed: {stats['files_changed']} file(s) "
                f"({stats.get('node_count', 0)} nodes, {stats.get('edge_count', 0)} edges) "
                f"in {elapsed:.2f}s"
            )


class _ChangeHandler(FileSystemEventHandler):
    """Watchdog event handler — filters and queues relevant file events."""

    def __init__(self, watcher: EngramWatcher):
        self.watcher = watcher

    def on_any_event(self, event):
        if event.is_directory:
            return

        path = Path(event.src_path)

        # Skip excluded directories
        for part in path.parts:
            if part in ALWAYS_SKIP or part.endswith(".egg-info"):
                return

        # Skip non-source files
        if detect_language(path) is None:
            return

        try:
            rel_path = str(path.relative_to(self.watcher.root))
        except ValueError:
            return

        self.watcher._pending_changes[rel_path] = time.time()
