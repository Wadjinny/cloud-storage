from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from config import DATA_DIR, DB_PATH

_lock = threading.Lock()
_initialized = False

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  kdf_salt TEXT NOT NULL,
  verifier TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  label TEXT NOT NULL,
  credentials_enc TEXT NOT NULL,
  UNIQUE (user_id, provider, label)
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
  UNIQUE (user_id, path)
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


def init_db(path: Path | None = None) -> None:
    global _initialized
    db_path = path or DB_PATH
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = _connect(db_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def reset_for_tests() -> None:
    """Drop the initialized flag so the next init_db recreates schema on a new path."""
    global _initialized
    with _lock:
        _initialized = False
