from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _v1_schema(conn: sqlite3.Connection) -> None:
    """Pre-timestamp schema (before migration v2)."""
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE users (
          id TEXT PRIMARY KEY,
          username TEXT NOT NULL UNIQUE,
          kdf_salt TEXT NOT NULL,
          verifier TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE accounts (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          provider TEXT NOT NULL,
          label TEXT NOT NULL,
          credentials_enc TEXT NOT NULL,
          UNIQUE (user_id, provider, label)
        );
        CREATE TABLE folders (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          path TEXT NOT NULL,
          mtime INTEGER NOT NULL,
          UNIQUE (user_id, path)
        );
        CREATE TABLE files (
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
        CREATE TABLE upload_sessions (
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
        """
    )


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migrate_v2_adds_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _v1_schema(conn)
    conn.execute(
        """
        INSERT INTO users (id, username, kdf_salt, verifier, created_at)
        VALUES ('usr_1', 'alice', 'salt', 'ver', 1000)
        """
    )
    conn.execute(
        """
        INSERT INTO accounts (id, user_id, provider, label, credentials_enc)
        VALUES ('acc_1', 'usr_1', 'mega', 'a', 'enc')
        """
    )
    conn.execute(
        """
        INSERT INTO folders (id, user_id, path, mtime)
        VALUES ('dir_1', 'usr_1', '/docs', 2000)
        """
    )
    conn.execute(
        """
        INSERT INTO files (
          id, user_id, path, size, mtime, chunk_size, crypto_alg, wrapped_file_key
        ) VALUES ('file_1', 'usr_1', '/docs/a.txt', 10, 3000, 64, 'AES-256-GCM-CHUNK', 'wk')
        """
    )
    conn.execute(
        """
        INSERT INTO upload_sessions (
          id, user_id, status, path, filename, size, chunk_size,
          crypto_alg, wrapped_file_key, error, updated_at
        ) VALUES (
          'up_1', 'usr_1', 'active', '/', 'a.txt', 10, 64,
          'AES-256-GCM-CHUNK', 'wk', NULL, 4000
        )
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("storage.db.DATA_DIR", tmp_path)
    monkeypatch.setattr("storage.db.DB_PATH", db_path)
    from storage.db import init_db, reset_for_tests

    reset_for_tests()
    init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    assert "updated_at" in _cols(conn, "users")
    assert {"created_at", "updated_at"} <= _cols(conn, "accounts")
    assert {"created_at", "updated_at"} <= _cols(conn, "folders")
    assert {"created_at", "updated_at"} <= _cols(conn, "files")
    assert "created_at" in _cols(conn, "upload_sessions")

    user = conn.execute("SELECT * FROM users WHERE id = 'usr_1'").fetchone()
    assert user["updated_at"] == 1000

    acc = conn.execute("SELECT * FROM accounts WHERE id = 'acc_1'").fetchone()
    assert acc["created_at"] is not None
    assert acc["updated_at"] == acc["created_at"]

    folder = conn.execute("SELECT * FROM folders WHERE id = 'dir_1'").fetchone()
    assert folder["created_at"] == 2000
    assert folder["updated_at"] == 2000

    file_row = conn.execute("SELECT * FROM files WHERE id = 'file_1'").fetchone()
    assert file_row["created_at"] == 3000
    assert file_row["updated_at"] == 3000

    sess = conn.execute("SELECT * FROM upload_sessions WHERE id = 'up_1'").fetchone()
    assert sess["created_at"] == 4000

    ver = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    assert int(ver["v"]) >= 2

    # Idempotent: second init does not fail or rewrite clocks.
    conn.close()
    reset_for_tests()
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    folder2 = conn.execute("SELECT * FROM folders WHERE id = 'dir_1'").fetchone()
    assert folder2["created_at"] == 2000
    conn.close()


def test_fresh_db_has_timestamp_columns(data_dir: Path) -> None:
    conn = sqlite3.connect(str(data_dir / "app.db"))
    conn.row_factory = sqlite3.Row
    for table in ("users", "accounts", "folders", "files"):
        cols = _cols(conn, table)
        assert "created_at" in cols
        assert "updated_at" in cols
    assert "created_at" in _cols(conn, "upload_sessions")
    ver = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    assert int(ver["v"]) == 4
    assert "thumb_status" in _cols(conn, "files")
    assert "enabled" in _cols(conn, "accounts")
    conn.close()
