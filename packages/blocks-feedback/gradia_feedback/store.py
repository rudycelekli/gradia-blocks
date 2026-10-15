import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
 id TEXT NOT NULL, tenant TEXT NOT NULL, actor TEXT NOT NULL,
 revision INTEGER NOT NULL, status TEXT NOT NULL, category TEXT NOT NULL,
 message TEXT NOT NULL, context TEXT NOT NULL, assignee TEXT,
 audio BLOB, audio_mime TEXT, audio_sha TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY (tenant,id));
CREATE TABLE IF NOT EXISTS idempotency (
 tenant TEXT NOT NULL, actor TEXT NOT NULL, key TEXT NOT NULL,
 digest TEXT NOT NULL, response TEXT NOT NULL,
 PRIMARY KEY (tenant,actor,key));
CREATE TABLE IF NOT EXISTS events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 tenant TEXT NOT NULL, feedback_id TEXT NOT NULL,
 payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS block_metadata (
 key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path):
        self.path = str(Path(path))
        if self.path == ":memory:":
            raise ValueError("Use a persistent SQLite file")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            if Path(self.path).is_symlink():
                raise ValueError("Database path cannot be a symlink")
            if os.name == "posix" and Path(self.path).stat().st_mode & 0o077:
                raise ValueError("Existing database must be owner-readable only (chmod 600)")
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO block_metadata VALUES ('schema_version','1')")
            version = conn.execute("SELECT value FROM block_metadata WHERE key='schema_version'").fetchone()[0]
            if version != "1":
                raise ValueError("Unsupported database schema version")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = FULL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def record(row):
    result = {name: row[name] for name in ("id", "revision", "status", "category", "message", "assignee", "created_at", "updated_at")}
    result["context"] = json.loads(row["context"])
    result["audio"] = None if row["audio"] is None else {"mime_type": row["audio_mime"], "size_bytes": len(row["audio"]), "sha256": row["audio_sha"]}
    return result
