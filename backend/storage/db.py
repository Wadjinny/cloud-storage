from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from config import DATA_DIR, DB_PATH

log = logging.getLogger(__name__)

_lock = threading.Lock()
_initialized = False

# Backslash-escape LIKE wildcards so a literal '_' or '%' in a user path is not
# treated as a pattern. Pair with `LIKE ? ESCAPE '\\'` in the query.
LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` for use as a literal in a LIKE pattern."""
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )

# Bump when adding a migration. Fresh installs still run migrations (idempotent).
SCHEMA_VERSION = 7

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  kdf_salt TEXT NOT NULL,
  verifier TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  label TEXT NOT NULL,
  credentials_enc TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  auth_state TEXT NOT NULL DEFAULT 'ok',
  auth_error TEXT,
  auth_renewed_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (user_id, provider, label)
);

CREATE TABLE IF NOT EXISTS folders (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  mtime INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (user_id, path)
);

CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime INTEGER NOT NULL,
  chunk_size INTEGER NOT NULL,
  crypto_alg TEXT NOT NULL,
  wrapped_file_key TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  thumb_status TEXT NOT NULL DEFAULT 'none',
  thumb_account_id TEXT,
  thumb_remote_id TEXT,
  thumb_wrapped_key TEXT,
  thumb_mime TEXT,
  thumb_bytes INTEGER,
  thumb_error TEXT,
  remote_vault_dir_id TEXT,
  UNIQUE (user_id, path)
);

CREATE TABLE IF NOT EXISTS folder_remote_folders (
  folder_id TEXT NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL,
  remote_folder_id TEXT NOT NULL,
  PRIMARY KEY (folder_id, account_id)
);

CREATE TABLE IF NOT EXISTS file_remote_folders (
  file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL,
  remote_folder_id TEXT NOT NULL,
  PRIMARY KEY (file_id, account_id)
);

CREATE TABLE IF NOT EXISTS shards (
  file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  i INTEGER NOT NULL,
  account_id TEXT NOT NULL,
  remote_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  nonce TEXT NOT NULL,
  PRIMARY KEY (file_id, i)
);

CREATE TABLE IF NOT EXISTS upload_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  path TEXT NOT NULL,
  filename TEXT NOT NULL,
  size INTEGER,
  chunk_size INTEGER NOT NULL,
  crypto_alg TEXT NOT NULL,
  wrapped_file_key TEXT NOT NULL,
  error TEXT,
  remote_vault_dir_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_session_remote_folders (
  session_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL,
  remote_folder_id TEXT NOT NULL,
  PRIMARY KEY (session_id, account_id)
);

CREATE TABLE IF NOT EXISTS upload_session_shards (
  session_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
  i INTEGER NOT NULL,
  account_id TEXT NOT NULL,
  remote_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  nonce TEXT NOT NULL,
  PRIMARY KEY (session_id, i)
);

CREATE TABLE IF NOT EXISTS activity_events (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  path TEXT,
  detail TEXT,
  meta_json TEXT,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_user_created
  ON activity_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_created
  ON activity_events(created_at);
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Thread-safe short-lived connection for one logical transaction."""
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    decl: str,
) -> bool:
    """ADD COLUMN when missing. Returns True if a column was added."""
    if not _table_exists(conn, table):
        return False
    if column in _table_columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


def _current_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_migrations"):
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
    return int(row["v"]) if row else 0


def _record_migration(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (version, int(time.time())),
    )


def _migrate_v2_timestamps(conn: sqlite3.Connection) -> None:
    """Add created_at / updated_at to entity tables; backfill from existing clocks."""
    now = int(time.time())

    # users: created_at already existed; add updated_at
    if _add_column_if_missing(conn, "users", "updated_at", "INTEGER"):
        conn.execute(
            "UPDATE users SET updated_at = COALESCE(created_at, ?) WHERE updated_at IS NULL",
            (now,),
        )

    # accounts
    added_acc_c = _add_column_if_missing(conn, "accounts", "created_at", "INTEGER")
    added_acc_u = _add_column_if_missing(conn, "accounts", "updated_at", "INTEGER")
    if added_acc_c or added_acc_u:
        conn.execute(
            """
            UPDATE accounts
            SET created_at = COALESCE(created_at, ?),
                updated_at = COALESCE(updated_at, created_at, ?)
            """,
            (now, now),
        )

    # folders: backfill from mtime
    added_fol_c = _add_column_if_missing(conn, "folders", "created_at", "INTEGER")
    added_fol_u = _add_column_if_missing(conn, "folders", "updated_at", "INTEGER")
    if added_fol_c or added_fol_u:
        conn.execute(
            """
            UPDATE folders
            SET created_at = COALESCE(created_at, mtime, ?),
                updated_at = COALESCE(updated_at, mtime, created_at, ?)
            """,
            (now, now),
        )

    # files: backfill from mtime
    added_file_c = _add_column_if_missing(conn, "files", "created_at", "INTEGER")
    added_file_u = _add_column_if_missing(conn, "files", "updated_at", "INTEGER")
    if added_file_c or added_file_u:
        conn.execute(
            """
            UPDATE files
            SET created_at = COALESCE(created_at, mtime, ?),
                updated_at = COALESCE(updated_at, mtime, created_at, ?)
            """,
            (now, now),
        )

    # upload_sessions: created_at from updated_at
    if _add_column_if_missing(conn, "upload_sessions", "created_at", "INTEGER"):
        conn.execute(
            """
            UPDATE upload_sessions
            SET created_at = COALESCE(created_at, updated_at, ?)
            """,
            (now,),
        )


def _migrate_v3_thumbnails(conn: sqlite3.Connection) -> None:
    """Add encrypted remote thumbnail metadata columns on files."""
    _add_column_if_missing(conn, "files", "thumb_status", "TEXT NOT NULL DEFAULT 'none'")
    _add_column_if_missing(conn, "files", "thumb_account_id", "TEXT")
    _add_column_if_missing(conn, "files", "thumb_remote_id", "TEXT")
    _add_column_if_missing(conn, "files", "thumb_wrapped_key", "TEXT")
    _add_column_if_missing(conn, "files", "thumb_mime", "TEXT")
    _add_column_if_missing(conn, "files", "thumb_bytes", "INTEGER")
    _add_column_if_missing(conn, "files", "thumb_error", "TEXT")
    conn.execute(
        "UPDATE files SET thumb_status = 'none' WHERE thumb_status IS NULL OR thumb_status = ''"
    )


def _migrate_v4_account_enabled(conn: sqlite3.Connection) -> None:
    """Per-account upload placement toggle (1 = used for new shards)."""
    _add_column_if_missing(conn, "accounts", "enabled", "INTEGER NOT NULL DEFAULT 1")
    conn.execute("UPDATE accounts SET enabled = 1 WHERE enabled IS NULL")


def _migrate_v5_folder_remotes(conn: sqlite3.Connection) -> None:
    """Opaque vault-dir folders on providers + placement dir on files/sessions."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folder_remote_folders (
          folder_id TEXT NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
          account_id TEXT NOT NULL,
          remote_folder_id TEXT NOT NULL,
          PRIMARY KEY (folder_id, account_id)
        )
        """
    )
    _add_column_if_missing(conn, "files", "remote_vault_dir_id", "TEXT")
    _add_column_if_missing(conn, "upload_sessions", "remote_vault_dir_id", "TEXT")


def _migrate_v6_activity_events(conn: sqlite3.Connection) -> None:
    """Per-user activity history (upload/download/delete/auth/accounts)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_events (
          id TEXT PRIMARY KEY,
          user_id TEXT,
          action TEXT NOT NULL,
          status TEXT NOT NULL,
          path TEXT,
          detail TEXT,
          meta_json TEXT,
          created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_activity_user_created
          ON activity_events(user_id, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_activity_created
          ON activity_events(created_at)
        """
    )


def _migrate_v7_account_auth_state(conn: sqlite3.Connection) -> None:
    """Track OAuth credential health so a dead refresh token reads as 'reconnect'."""
    _add_column_if_missing(conn, "accounts", "auth_state", "TEXT NOT NULL DEFAULT 'ok'")
    _add_column_if_missing(conn, "accounts", "auth_error", "TEXT")
    _add_column_if_missing(conn, "accounts", "auth_renewed_at", "INTEGER")
    conn.execute(
        "UPDATE accounts SET auth_state = 'ok' WHERE auth_state IS NULL OR auth_state = ''"
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v2_timestamps,
    3: _migrate_v3_thumbnails,
    4: _migrate_v4_account_enabled,
    5: _migrate_v5_folder_remotes,
    6: _migrate_v6_activity_events,
    7: _migrate_v7_account_auth_state,
}


def _run_migrations(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at INTEGER NOT NULL
        );
        """
    )
    current = _current_version(conn)
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        log.info("Applying DB migration v%s", version)
        MIGRATIONS[version](conn)
        _record_migration(conn, version)
        current = version


def init_db(path: Path | None = None) -> None:
    global _initialized
    db_path = path or DB_PATH
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect(db_path)
        try:
            # CREATE IF NOT EXISTS for brand-new tables (full current schema).
            conn.executescript(SCHEMA)
            # ALTER / backfill for existing DBs.
            _run_migrations(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        _initialized = True


def reset_for_tests() -> None:
    """Drop the initialized flag so the next init_db recreates schema on a new path."""
    global _initialized
    with _lock:
        _initialized = False
