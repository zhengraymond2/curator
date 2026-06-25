from __future__ import annotations

import sqlite3
from pathlib import Path

from .plan import utc_now_iso


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  root TEXT NOT NULL,
  mode TEXT NOT NULL,
  started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT,
  first_seen_run_id TEXT,
  last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_name_size ON files(name, size);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);

CREATE TABLE IF NOT EXISTS operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  op_type TEXT NOT NULL,
  src TEXT,
  dest TEXT,
  reason TEXT,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS trash_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  trash_path TEXT NOT NULL UNIQUE,
  original_path TEXT NOT NULL,
  preserved_path TEXT,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}'
);
"""


def catalog_path(library_root: Path) -> Path:
    return library_root.expanduser().resolve() / ".curator" / "catalog.sqlite"


def connect_catalog(library_root: Path) -> sqlite3.Connection:
    path = catalog_path(library_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def record_scan_run(connection: sqlite3.Connection, *, run_id: str, root: Path, mode: str) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO scan_runs(run_id, root, mode, started_at)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, str(root.expanduser().resolve()), mode, utc_now_iso()),
    )
    connection.commit()


def upsert_file(
    connection: sqlite3.Connection,
    *,
    path: Path,
    name: str,
    size: int,
    sha256: str | None,
    run_id: str | None,
) -> None:
    now = utc_now_iso()
    connection.execute(
        """
        INSERT INTO files(path, name, size, sha256, first_seen_run_id, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          name=excluded.name,
          size=excluded.size,
          sha256=excluded.sha256,
          last_seen_at=excluded.last_seen_at
        """,
        (str(path.expanduser().resolve()), name, size, sha256, run_id, now),
    )
    connection.commit()


def record_operation(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    op_type: str,
    src: str | None,
    dest: str | None,
    reason: str | None,
    metadata_json: str = "{}",
) -> None:
    connection.execute(
        """
        INSERT INTO operations(run_id, op_type, src, dest, reason, created_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, op_type, src, dest, reason, utc_now_iso(), metadata_json),
    )
    connection.commit()


def fetch_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = ["scan_runs", "files", "operations", "trash_entries"]
    counts: dict[str, int] = {}
    for table in tables:
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        counts[table] = int(row["count"])
    return counts
