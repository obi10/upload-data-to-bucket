from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=60)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
        return connection

    @contextmanager
    def transaction(self):
        connection = self._connection()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _initialize(self) -> None:
        with self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS contents (
                    sha256 TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    source_path TEXT,
                    object_name TEXT,
                    member_name TEXT,
                    object_sha256 TEXT,
                    object_size INTEGER,
                    etag TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    manifest_object TEXT,
                    error_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS paths (
                    snapshot_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    sha256 TEXT,
                    size INTEGER,
                    mtime_ns INTEGER,
                    error TEXT,
                    PRIMARY KEY(snapshot_id, relative_path),
                    FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    object_name TEXT PRIMARY KEY,
                    local_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    upload_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS upload_parts (
                    object_name TEXT NOT NULL,
                    part_number INTEGER NOT NULL,
                    etag TEXT NOT NULL,
                    md5 TEXT NOT NULL,
                    PRIMARY KEY(object_name, part_number)
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def create_snapshot(self, snapshot_id: str, kind: str, source: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO snapshots(id,kind,source,state,created_at) VALUES(?,?,?,?,?)",
                (snapshot_id, kind, source, "scanning", utc_now()),
            )

    def set_snapshot_state(self, snapshot_id: str, state: str, *, manifest=None, errors=None):
        fields = ["state=?"]
        values: list[object] = [state]
        if manifest is not None:
            fields.append("manifest_object=?")
            values.append(manifest)
        if errors is not None:
            fields.append("error_count=?")
            values.append(errors)
        values.append(snapshot_id)
        with self.transaction() as db:
            db.execute(f"UPDATE snapshots SET {', '.join(fields)} WHERE id=?", values)

    def upsert_content(self, sha256: str, size: int, source_path: str) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO contents(sha256,size,source_path,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET
                   source_path=CASE WHEN contents.status='uploaded' THEN contents.source_path ELSE excluded.source_path END,
                   size=excluded.size, updated_at=excluded.updated_at""",
                (sha256, size, source_path, utc_now()),
            )

    def add_path(self, snapshot_id: str, relative_path: str, entry_type: str,
                 sha256=None, size=None, mtime_ns=None, error=None) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT OR REPLACE INTO paths
                   (snapshot_id,relative_path,entry_type,sha256,size,mtime_ns,error)
                   VALUES(?,?,?,?,?,?,?)""",
                (snapshot_id, relative_path, entry_type, sha256, size, mtime_ns, error),
            )

    def pending_contents(self):
        return self._connection().execute(
            "SELECT * FROM contents WHERE status!='uploaded' ORDER BY size DESC"
        ).fetchall()

    def content(self, sha256: str):
        return self._connection().execute(
            "SELECT * FROM contents WHERE sha256=?", (sha256,)
        ).fetchone()

    def mark_content_uploaded(self, sha256: str, object_name: str, member_name: str | None,
                              object_sha256: str, object_size: int, etag: str | None) -> None:
        with self.transaction() as db:
            db.execute(
                """UPDATE contents SET object_name=?,member_name=?,object_sha256=?,
                   object_size=?,etag=?,status='uploaded',updated_at=? WHERE sha256=?""",
                (object_name, member_name, object_sha256, object_size, etag, utc_now(), sha256),
            )

    def uploaded_contents(self):
        return self._connection().execute(
            "SELECT * FROM contents WHERE status='uploaded' ORDER BY sha256"
        ).fetchall()

    def snapshot_paths(self, snapshot_id: str):
        return self._connection().execute(
            "SELECT * FROM paths WHERE snapshot_id=? ORDER BY relative_path", (snapshot_id,)
        ).fetchall()

    def clear_snapshot_paths(self, snapshot_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM paths WHERE snapshot_id=?", (snapshot_id,))

    def snapshot_manifest_entries(self, snapshot_id: str):
        return self._connection().execute(
            """SELECT p.*,c.object_name,c.member_name,c.object_sha256,c.object_size,c.etag
               FROM paths p LEFT JOIN contents c ON c.sha256=p.sha256
               WHERE p.snapshot_id=? ORDER BY p.relative_path""",
            (snapshot_id,),
        ).fetchall()

    def latest_snapshot(self, kind: str | None = None):
        if kind:
            return self._connection().execute(
                "SELECT * FROM snapshots WHERE kind=? ORDER BY created_at DESC LIMIT 1", (kind,)
            ).fetchone()
        return self._connection().execute(
            "SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    def set_meta(self, key: str, value) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, payload),
            )

    def get_meta(self, key: str, default=None):
        row = self._connection().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def get_upload(self, object_name: str):
        return self._connection().execute(
            "SELECT * FROM uploads WHERE object_name=?", (object_name,)
        ).fetchone()

    def save_upload(self, object_name: str, local_path: str, sha256: str, size: int,
                    upload_id: str | None, status="uploading") -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT OR REPLACE INTO uploads
                   (object_name,local_path,sha256,size,upload_id,status,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (object_name, local_path, sha256, size, upload_id, status, utc_now()),
            )

    def save_part(self, object_name: str, number: int, etag: str, md5: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO upload_parts(object_name,part_number,etag,md5) VALUES(?,?,?,?)",
                (object_name, number, etag, md5),
            )

    def parts(self, object_name: str):
        return self._connection().execute(
            "SELECT * FROM upload_parts WHERE object_name=? ORDER BY part_number", (object_name,)
        ).fetchall()

    def finish_upload(self, object_name: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE uploads SET status='uploaded',updated_at=? WHERE object_name=?", (utc_now(), object_name))
            db.execute("DELETE FROM upload_parts WHERE object_name=?", (object_name,))
