from __future__ import annotations

from pathlib import Path

from storage import vault_index


def test_load_default_when_missing(data_dir: Path) -> None:
    index = vault_index.load_index()
    assert index["version"] == 1
    assert index["kdf_salt"] is None
    assert index["files"] == []


def test_upsert_find_remove(data_dir: Path) -> None:
    index = vault_index.load_index()
    entry = {"id": "f_1", "path": "/a.txt", "size": 3, "shards": []}
    vault_index.upsert_file(index, entry)
    vault_index.save_index(index)

    loaded = vault_index.load_index()
    assert vault_index.find_file(loaded, "f_1")["path"] == "/a.txt"

    removed = vault_index.remove_file(loaded, "f_1")
    assert removed is not None
    vault_index.save_index(loaded)
    assert vault_index.find_file(vault_index.load_index(), "f_1") is None


def test_upsert_replaces_same_id(data_dir: Path) -> None:
    index = vault_index.load_index()
    vault_index.upsert_file(index, {"id": "f_1", "path": "/a.txt", "size": 1})
    vault_index.upsert_file(index, {"id": "f_1", "path": "/b.txt", "size": 2})
    assert len(index["files"]) == 1
    assert index["files"][0]["path"] == "/b.txt"
