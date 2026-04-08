"""SQLite database manager for Engram. Owns connection lifecycle and schema."""

import sqlite3
from pathlib import Path


class EngramDB:
    """Manages the .engram/engram.db SQLite database."""

    def __init__(self, root: Path):
        self.root = root
        self.db_dir = root / ".engram"
        self.db_path = self.db_dir / "engram.db"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Return a connection with WAL mode and Row factory."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self.init_schema()
        return self._conn

    def init_schema(self):
        """Create tables and indexes if they don't exist."""
        conn = self._conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS nodes (
                id          TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                name        TEXT NOT NULL,
                file_path   TEXT NOT NULL,
                line_start  INTEGER NOT NULL,
                line_end    INTEGER NOT NULL,
                language    TEXT NOT NULL,
                signature   TEXT,
                docstring   TEXT,
                source_hash TEXT NOT NULL,
                is_exported BOOLEAN DEFAULT 0,
                decorators  TEXT,
                summary     TEXT,
                full_source TEXT,
                summary_source TEXT DEFAULT 'ast'
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
            CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
            CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);

            CREATE TABLE IF NOT EXISTS edges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id   TEXT NOT NULL REFERENCES nodes(id),
                target_id   TEXT NOT NULL REFERENCES nodes(id),
                kind        TEXT NOT NULL,
                metadata    TEXT,
                UNIQUE(source_id, target_id, kind)
            );

            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
            CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);

            CREATE TABLE IF NOT EXISTS manifest (
                file_path   TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                indexed_at  TEXT NOT NULL DEFAULT (datetime('now')),
                node_count  INTEGER DEFAULT 0
            );

            -- v1: Memory layer tables
            CREATE TABLE IF NOT EXISTS sessions (
                id          TEXT PRIMARY KEY,
                project     TEXT NOT NULL,
                started_at  TEXT NOT NULL DEFAULT (datetime('now')),
                ended_at    TEXT,
                summary     TEXT
            );

            CREATE TABLE IF NOT EXISTS observations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES sessions(id),
                type        TEXT NOT NULL,
                title       TEXT NOT NULL,
                content     TEXT NOT NULL,
                project     TEXT,
                topic_key   TEXT,
                source_hash TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS observation_nodes (
                observation_id INTEGER NOT NULL REFERENCES observations(id),
                node_id        TEXT NOT NULL,
                source         TEXT NOT NULL DEFAULT 'auto',
                PRIMARY KEY (observation_id, node_id)
            );

            CREATE INDEX IF NOT EXISTS idx_observations_session ON observations(session_id);
            CREATE INDEX IF NOT EXISTS idx_observations_type ON observations(type);
            CREATE INDEX IF NOT EXISTS idx_observations_topic_key ON observations(topic_key);
            CREATE INDEX IF NOT EXISTS idx_observations_project ON observations(project);

            -- v3: Verification + feedback tables
            CREATE TABLE IF NOT EXISTS retrieval_feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash  TEXT NOT NULL,
                change_type TEXT NOT NULL,
                missed_node TEXT NOT NULL,
                edge_kind   TEXT NOT NULL,
                depth       INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_change_type ON retrieval_feedback(change_type);
            CREATE INDEX IF NOT EXISTS idx_feedback_missed_node ON retrieval_feedback(missed_node);

            CREATE TABLE IF NOT EXISTS verification_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                seeds           TEXT NOT NULL,
                change_types    TEXT NOT NULL,
                touched_count   INTEGER NOT NULL,
                expected_count  INTEGER NOT NULL,
                missing_count   INTEGER NOT NULL,
                verdict         TEXT NOT NULL,
                missing_nodes   TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- v5: Seed selection history
            CREATE TABLE IF NOT EXISTS seed_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                query_hash  TEXT NOT NULL,
                prompt_terms TEXT NOT NULL,
                seed_ids    TEXT NOT NULL,
                file_paths  TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_seed_history_created ON seed_history(created_at);

            -- v4: Patterns table
            CREATE TABLE IF NOT EXISTS patterns (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                framework   TEXT,
                description TEXT NOT NULL,
                node_pattern TEXT NOT NULL,
                implicit_edges TEXT NOT NULL,
                priority_hints TEXT,
                source_project TEXT,
                confidence  REAL NOT NULL DEFAULT 0.5,
                is_builtin  BOOLEAN DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        # FTS5 virtual table (separate because IF NOT EXISTS syntax differs)
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
                    title, content, type, project,
                    content='observations', content_rowid='id'
                )
            """)
        except Exception:
            pass  # Already exists

        # FTS5 index for seed selection (v5)
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS node_index USING fts5(
                    node_id,
                    name,
                    signature,
                    docstring,
                    decorators,
                    source_preview,
                    tokenize='porter unicode61'
                )
            """)
        except Exception:
            pass  # Already exists

        # Migration: add summary_source column if missing (v0 → v4 upgrade)
        try:
            conn.execute("SELECT summary_source FROM nodes LIMIT 1")
        except Exception:
            try:
                conn.execute("ALTER TABLE nodes ADD COLUMN summary_source TEXT DEFAULT 'ast'")
            except Exception:
                pass
        conn.commit()

    def reset(self):
        """Drop and recreate all tables. Used by --force."""
        conn = self.connect()
        conn.executescript("""
            DROP TABLE IF EXISTS observation_nodes;
            DROP TABLE IF EXISTS observations_fts;
            DROP TABLE IF EXISTS observations;
            DROP TABLE IF EXISTS sessions;
            DROP TABLE IF EXISTS retrieval_feedback;
            DROP TABLE IF EXISTS verification_results;
            DROP TABLE IF EXISTS patterns;
            DROP TABLE IF EXISTS node_index;
            DROP TABLE IF EXISTS seed_history;
            DROP TABLE IF EXISTS edges;
            DROP TABLE IF EXISTS nodes;
            DROP TABLE IF EXISTS manifest;
        """)
        conn.commit()
        self.init_schema()

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def exists(self) -> bool:
        """Check if the database file exists and has tables."""
        if not self.db_path.exists():
            return False
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
            )
            result = cursor.fetchone() is not None
            conn.close()
            return result
        except sqlite3.Error:
            return False