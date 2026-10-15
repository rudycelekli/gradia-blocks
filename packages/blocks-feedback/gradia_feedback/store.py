import json
import os
import sqlite3
import hashlib
import re
import stat
import uuid
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
CREATE TABLE IF NOT EXISTS attachments (
 id TEXT NOT NULL, tenant TEXT NOT NULL, feedback_id TEXT NOT NULL,
 kind TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, data BLOB, storage_key TEXT,
 PRIMARY KEY (tenant, feedback_id, id),
 FOREIGN KEY (tenant,feedback_id) REFERENCES feedback(tenant,id));
CREATE TABLE IF NOT EXISTS block_metadata (
 key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path, attachment_storage=None):
        from .contracts import AttachmentStorageSettings
        self.attachment_storage = AttachmentStorageSettings.model_validate(attachment_storage or {})
        self.media_directory = None
        if self.attachment_storage.backend == "filesystem":
            raw = self.attachment_storage.directory
            if not raw or not Path(raw).is_absolute():
                raise ValueError("Filesystem attachment directory must be absolute")
            directory = Path(raw)
            if any(part.is_symlink() for part in [directory, *directory.parents]):
                raise ValueError("Attachment directory cannot contain symlinks")
            directory.mkdir(mode=0o700, exist_ok=True)
            if not directory.is_dir() or directory.stat().st_mode & 0o077:
                raise ValueError("Attachment directory must be private (chmod 700)")
            self.media_directory = directory
        elif self.attachment_storage.directory is not None:
            raise ValueError("Directory is only valid for filesystem storage")
        self._staged = {}
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
            # Additive media extension; the original feedback/audio rows are unchanged.
            # Persist host-only storage identity so a restart cannot silently detach
            # acknowledged bytes by switching backend or private directory.
            media_identity = json.dumps({"backend": self.attachment_storage.backend, "directory": str(self.media_directory) if self.media_directory is not None else None}, sort_keys=True)
            conn.execute("INSERT OR IGNORE INTO block_metadata VALUES ('media_schema_version','2')")
            media_version = conn.execute("SELECT value FROM block_metadata WHERE key='media_schema_version'").fetchone()[0]
            if media_version != "2":
                raise ValueError("Unsupported attachment schema version")
            conn.execute("INSERT OR IGNORE INTO block_metadata VALUES ('attachment_storage_identity',?)", (media_identity,))
            configured = conn.execute("SELECT value FROM block_metadata WHERE key='attachment_storage_identity'").fetchone()[0]
            if configured != media_identity:
                raise ValueError("Attachment storage configuration changed; stop writers and explicitly migrate database/media before reopening")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        staged = []
        self._staged[id(conn)] = staged
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = FULL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            for name in staged:
                try:
                    (self.media_directory / name).unlink(missing_ok=True)
                except OSError:
                    pass  # Crash/cleanup failures are discoverable by explicit orphan reconciliation.
            raise
        finally:
            self._staged.pop(id(conn), None)
            conn.close()

    def save_attachment(self, conn, tenant, feedback_id, item, data):
        identity = str(uuid.uuid4())
        digest = hashlib.sha256(data).hexdigest()
        storage_key = None
        blob = data
        if self.media_directory is not None:
            storage_key = uuid.uuid4().hex
            target = self.media_directory / storage_key
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self._staged[id(conn)].append(storage_key)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(self.media_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            blob = None
        conn.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?)", (identity, tenant, feedback_id, item.kind, item.mime_type, len(data), digest, blob, storage_key))

    def read_attachment(self, row):
        if row["storage_key"] is None:
            data = row["data"]
        else:
            key = row["storage_key"]
            if self.media_directory is None or re.fullmatch(r"[a-f0-9]{32}", key) is None:
                raise OSError("Attachment storage is unavailable")
            descriptor = os.open(self.media_directory / key, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                mode = os.fstat(stream.fileno()).st_mode
                if not stat.S_ISREG(mode) or mode & 0o077:
                    raise OSError("Attachment permissions are not private")
                data = stream.read(row["size_bytes"] + 1)
        if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise OSError("Attachment integrity check failed")
        return data

    def orphan_files(self):
        """Inspect only. Never delete automatically: another process may be staging a file.

        A host may remove returned unreferenced files during an exclusive maintenance
        window after stopping all writers. Process death before SQLite commit can
        leave private unreferenced files; acknowledged records are committed only
        after fsync of their files. This is not a distributed transaction.
        """
        if self.media_directory is None:
            return []
        with self.connection() as conn:
            referenced = {row[0] for row in conn.execute("SELECT storage_key FROM attachments WHERE storage_key IS NOT NULL")}
        return sorted(p.name for p in self.media_directory.iterdir() if re.fullmatch(r"[a-f0-9]{32}", p.name) and p.name not in referenced)

    def record(self, conn, row):
        result = record(row)
        rows = conn.execute("SELECT id,kind,mime_type,size_bytes,sha256 FROM attachments WHERE tenant=? AND feedback_id=? ORDER BY id", (row["tenant"], row["id"])).fetchall()
        result["attachments"] = [dict(item) for item in rows]
        return result


def record(row):
    result = {name: row[name] for name in ("id", "revision", "status", "category", "message", "assignee", "created_at", "updated_at")}
    result["context"] = json.loads(row["context"])
    result["audio"] = None if row["audio"] is None else {"mime_type": row["audio_mime"], "size_bytes": len(row["audio"]), "sha256": row["audio_sha"]}
    return result
