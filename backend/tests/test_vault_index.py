from __future__ import annotations

from pathlib import Path

from services import crypto
from storage import auth_context
from storage import users as users_store
from storage import vault_index
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


def _scope():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key)


def _entry(file_id: str, path: str, size: int) -> dict:
    return {
        "id": file_id,
        "path": path,
        "size": size,
        "mtime": 1,
        "chunk_size": 64,
        "crypto": {
            "alg": "AES-256-GCM-CHUNK",
            "wrapped_file_key": "dGVzdA==",
        },
        "remote_folders": {},
        "shards": [],
    }


def test_load_default_when_missing(data_dir: Path) -> None:
    with _scope():
        index = vault_index.load_index()
    assert index["version"] == 1
    assert index["files"] == []


def test_upsert_find_remove(data_dir: Path) -> None:
    with _scope():
        index = vault_index.load_index()
        entry = _entry("f_1", "/a.txt", 3)
        vault_index.upsert_file(index, entry)
        vault_index.save_index(index)

        loaded = vault_index.load_index()
        assert vault_index.find_file(loaded, "f_1")["path"] == "/a.txt"

        removed = vault_index.remove_file(loaded, "f_1")
        assert removed is not None
        vault_index.save_index(loaded)
        assert vault_index.find_file(vault_index.load_index(), "f_1") is None


def test_upsert_replaces_same_id(data_dir: Path) -> None:
    with _scope():
        index = vault_index.load_index()
        vault_index.upsert_file(index, _entry("f_1", "/a.txt", 1))
        vault_index.upsert_file(index, _entry("f_1", "/b.txt", 2))
        assert len(index["files"]) == 1
        assert index["files"][0]["path"] == "/b.txt"
