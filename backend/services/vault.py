from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import PurePosixPath
from typing import Any, BinaryIO

from config import CHUNK_SIZE, DOWNLOAD_QUEUE_DEPTH, SHARD_CONCURRENCY, STORAGE_FULL_RATIO
from providers.factory import get_provider
from services import crypto
from services.capacity import PlacementPlanner, cipher_size
from storage import accounts as accounts_store
from storage import vault_index

ProgressFn = Callable[[str, int, int, str], None]


def _progress(cb: ProgressFn | None, phase: str, current: int, total: int, message: str) -> None:
    if cb:
        cb(phase, current, total, message)


def _providers_by_id(account_list: list[dict[str, Any]]) -> dict[str, Any]:
    return {a["id"]: get_provider(a) for a in account_list}


def unlock(passphrase: str) -> bytes:
    index = vault_index.load_index()
    if index.get("kdf_salt") is None:
        salt = crypto.new_salt()
        master_key = crypto.derive_master_key(passphrase, salt)
        index["kdf_salt"] = crypto.b64e(salt)
        index["verifier"] = crypto.make_verifier(master_key)
        index.setdefault("files", [])
        vault_index.save_index(index)
        return master_key

    salt = crypto.b64d(index["kdf_salt"])
    master_key = crypto.derive_master_key(passphrase, salt)
    if not crypto.check_verifier(master_key, index["verifier"]):
        raise ValueError("Invalid passphrase")
    return master_key


def list_entries(path: str = "/") -> list[dict[str, Any]]:
    index = vault_index.load_index()
    return _list_entries_clean(index, _normalize_dir(path))


def get_file_detail(file_id: str) -> dict[str, Any]:
    index = vault_index.load_index()
    entry = vault_index.find_file(index, file_id)
    if entry is None:
        raise FileNotFoundError("File not found")

    account_map = {a["id"]: a for a in accounts_store.load_accounts()}
    shards = sorted(entry.get("shards", []), key=lambda s: s["i"])
    by_account: dict[str, dict[str, Any]] = {}
    for shard_rec in shards:
        account_id = shard_rec["account_id"]
        bucket = by_account.get(account_id)
        if bucket is None:
            account = account_map.get(account_id)
            bucket = {
                "account_id": account_id,
                "provider": account["provider"] if account else "unknown",
                "label": account["label"] if account else "(missing account)",
                "count": 0,
                "shard_indices": [],
            }
            by_account[account_id] = bucket
        bucket["count"] += 1
        bucket["shard_indices"].append(shard_rec["i"])

    return {
        "id": entry["id"],
        "path": entry["path"],
        "name": PurePosixPath(entry["path"]).name,
        "size": entry["size"],
        "mtime": entry.get("mtime"),
        "chunk_size": entry.get("chunk_size"),
        "alg": entry.get("crypto", {}).get("alg"),
        "shard_count": len(shards),
        "shards_by_account": sorted(
            by_account.values(),
            key=lambda b: (b["provider"], b["label"]),
        ),
        "shards": [
            {
                "i": s["i"],
                "account_id": s["account_id"],
                "provider": account_map[s["account_id"]]["provider"]
                if s["account_id"] in account_map
                else "unknown",
                "label": account_map[s["account_id"]]["label"]
                if s["account_id"] in account_map
                else "(missing account)",
                "remote_id": s["remote_id"],
                "sha256": s["sha256"],
            }
            for s in shards
        ],
    }


def _list_entries_clean(index: dict[str, Any], base: str) -> list[dict[str, Any]]:
    dirs: set[str] = set()
    files: list[dict[str, Any]] = []
    base_parts = () if base == "/" else tuple(p for p in PurePosixPath(base).parts if p != "/")

    for entry in index.get("files", []):
        clean_parts = tuple(p for p in PurePosixPath(entry["path"]).parts if p != "/")
        if clean_parts[: len(base_parts)] != base_parts:
            continue
        rest = clean_parts[len(base_parts) :]
        if not rest:
            continue
        if len(rest) == 1:
            files.append(
                {
                    "id": entry["id"],
                    "name": rest[0],
                    "path": entry["path"],
                    "size": entry["size"],
                    "mtime": entry.get("mtime"),
                    "kind": "file",
                    "shard_count": len(entry.get("shards", [])),
                }
            )
        else:
            dirs.add(rest[0])

    result: list[dict[str, Any]] = [
        {
            "id": f"dir:{base.rstrip('/')}/{name}" if base != "/" else f"dir:/{name}",
            "name": name,
            "path": f"{base.rstrip('/')}/{name}" if base != "/" else f"/{name}",
            "size": None,
            "mtime": None,
            "kind": "dir",
            "shard_count": 0,
        }
        for name in sorted(dirs)
    ]
    result.extend(sorted(files, key=lambda f: f["name"]))
    return result


def _normalize_dir(path: str) -> str:
    if not path or path == "/":
        return "/"
    p = PurePosixPath("/" + path.strip("/"))
    return str(p)


def _join_path(dir_path: str, name: str) -> str:
    base = _normalize_dir(dir_path)
    if base == "/":
        return f"/{name}"
    return f"{base}/{name}"


def _worker_count(n_tasks: int) -> int:
    return max(1, min(SHARD_CONCURRENCY, max(1, n_tasks)))


def _iter_plaintext_chunks(source: BinaryIO) -> Any:
    """Yield CHUNK_SIZE plaintext pieces from a binary stream (one empty chunk if empty)."""
    saw_data = False
    while True:
        plain = source.read(CHUNK_SIZE)
        if plain:
            saw_data = True
            yield plain
            if len(plain) < CHUNK_SIZE:
                break
        elif saw_data:
            break
        else:
            yield b""
            break


def upload_file(
    master_key: bytes,
    dir_path: str,
    filename: str,
    chunks: Any,
    *,
    size: int | None = None,
    on_progress: ProgressFn | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Encrypt and upload from an iterable of plaintext chunks.

    ``chunks`` yields up to CHUNK_SIZE bytes each (final chunk may be shorter;
    empty files yield a single ``b""``). At most ~SHARD_CONCURRENCY uploads run
    at once; callers should bound how many chunk buffers exist upstream.
    """
    account_list = accounts_store.load_accounts()
    if not account_list:
        raise ValueError("Connect at least one account before uploading")

    planner = PlacementPlanner.from_accounts(account_list)
    file_id = f"f_{secrets.token_hex(8)}"
    file_key = crypto.new_file_key()
    providers_by_id = _providers_by_id(account_list)

    if size is not None and size >= 0:
        expected_chunks = max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE) if size > 0 else 1
        total_steps = max(1, size) + 1
        # Plaintext plus one GCM tag per chunk.
        estimated_cipher = size + cipher_size(0) * expected_chunks
        placeable = planner.placeable_total()
        if placeable is not None and estimated_cipher > placeable:
            ratio_pct = int(round(STORAGE_FULL_RATIO * 100))
            raise ValueError(
                f"Not enough free space under the {ratio_pct}% full threshold "
                f"(need ~{estimated_cipher} bytes, ~{placeable} bytes placeable)"
            )
    else:
        expected_chunks = None
        total_steps = 1

    _progress(on_progress, "upload_shard", 0, total_steps, "Encrypting and uploading…")

    shard_records: dict[int, dict[str, Any]] = {}
    done_bytes = 0
    done_chunks = 0
    progress_lock = threading.Lock()
    workers = _worker_count(expected_chunks or SHARD_CONCURRENCY)
    in_flight: dict[Future[dict[str, Any]], int] = {}
    uploaded_remote: list[tuple[str, str]] = []  # (account_id, remote_id) for cleanup
    uploaded_lock = threading.Lock()

    def _aborted() -> bool:
        return bool(should_abort and should_abort())

    def _encrypt_upload(i: int, plain: bytes, account_id: str) -> dict[str, Any]:
        nonlocal done_bytes, done_chunks
        if _aborted():
            raise RuntimeError("Upload cancelled")
        ciphertext, nonce = crypto.encrypt_chunk(file_key, plain)
        remote_name = f"shard_{file_id}_{i}"
        remote_id = providers_by_id[account_id].upload(remote_name, ciphertext)
        with uploaded_lock:
            uploaded_remote.append((account_id, remote_id))
        if _aborted():
            raise RuntimeError("Upload cancelled")
        record = {
            "i": i,
            "account_id": account_id,
            "remote_id": remote_id,
            "sha256": hashlib.sha256(ciphertext).hexdigest(),
            "nonce": crypto.b64e(nonce),
        }
        with progress_lock:
            done_chunks += 1
            done_bytes += len(plain)
            if size is not None and size >= 0:
                current = min(done_bytes, max(1, size))
                msg = f"Uploaded chunk {done_chunks}" + (
                    f"/{expected_chunks}" if expected_chunks else ""
                )
                _progress(on_progress, "upload_shard", current, total_steps, f"{msg}…")
            else:
                _progress(
                    on_progress,
                    "upload_shard",
                    done_chunks,
                    max(done_chunks + 1, total_steps),
                    f"Uploaded chunk {done_chunks}…",
                )
        return record

    chunk_index = 0
    plaintext_size = 0

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for plain in chunks:
                if _aborted():
                    raise RuntimeError("Upload cancelled")
                plaintext_size += len(plain)
                account_id = planner.pick(cipher_size(len(plain)))

                while len(in_flight) >= workers:
                    fut = next(as_completed(in_flight))
                    i = in_flight.pop(fut)
                    shard_records[i] = fut.result()
                    if _aborted():
                        raise RuntimeError("Upload cancelled")

                fut = pool.submit(_encrypt_upload, chunk_index, plain, account_id)
                in_flight[fut] = chunk_index
                chunk_index += 1

            for fut in as_completed(list(in_flight.keys())):
                i = in_flight.pop(fut)
                shard_records[i] = fut.result()
                if _aborted():
                    raise RuntimeError("Upload cancelled")
    except Exception:
        for account_id, remote_id in list(uploaded_remote):
            try:
                providers_by_id[account_id].delete(remote_id)
            except Exception:
                pass
        raise

    if _aborted():
        raise RuntimeError("Upload cancelled")

    ordered = [shard_records[i] for i in range(chunk_index)]
    if size is not None and size >= 0:
        total_steps = max(1, size) + 1
    else:
        total_steps = plaintext_size + 1 if plaintext_size > 0 else 2

    _progress(on_progress, "index", total_steps - 1, total_steps, "Updating vault index…")
    entry = {
        "id": file_id,
        "path": _join_path(dir_path, filename),
        "size": plaintext_size,
        "mtime": int(time.time()),
        "chunk_size": CHUNK_SIZE,
        "crypto": {
            "alg": crypto.ALG_CHUNK,
            "wrapped_file_key": crypto.wrap_file_key(master_key, file_key),
        },
        "shards": ordered,
    }

    index = vault_index.load_index()
    index["files"] = [f for f in index.get("files", []) if f["path"] != entry["path"]]
    vault_index.upsert_file(index, entry)
    vault_index.save_index(index)
    _progress(on_progress, "done", total_steps, total_steps, "Done")
    return entry


def upload_file_from_stream(
    master_key: bytes,
    dir_path: str,
    filename: str,
    source: BinaryIO,
    *,
    size: int | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    return upload_file(
        master_key,
        dir_path,
        filename,
        _iter_plaintext_chunks(source),
        size=size,
        on_progress=on_progress,
    )


def download_file(
    master_key: bytes,
    file_id: str,
    on_progress: ProgressFn | None = None,
) -> tuple[str, str]:
    """Fetch and decrypt to a temp file. Returns (filename, temp_path)."""
    fd, out_path = tempfile.mkstemp(prefix="vault-dl-", suffix=".bin")
    os.close(fd)
    try:
        name = None
        with open(out_path, "wb") as out:
            for name, chunk in _iter_download_named(master_key, file_id, on_progress):
                out.write(chunk)
        assert name is not None
        return name, out_path
    except Exception:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise


def iter_download(
    master_key: bytes,
    file_id: str,
    on_progress: ProgressFn | None = None,
) -> Any:
    """Yield plaintext chunks while fetching/decrypting with bounded buffers."""
    for _name, chunk in _iter_download_named(master_key, file_id, on_progress):
        yield chunk


def _iter_download_named(
    master_key: bytes,
    file_id: str,
    on_progress: ProgressFn | None = None,
) -> Any:
    index = vault_index.load_index()
    entry = vault_index.find_file(index, file_id)
    if entry is None:
        raise FileNotFoundError("File not found")

    alg = entry.get("crypto", {}).get("alg")
    if alg != crypto.ALG_CHUNK:
        raise ValueError(f"Unsupported crypto algorithm: {alg!r}")

    account_list = accounts_store.load_accounts()
    account_map = {a["id"]: a for a in account_list}
    providers_by_id = _providers_by_id(account_list)
    shard_list = sorted(entry["shards"], key=lambda s: s["i"])
    total_steps = max(1, len(shard_list))
    name = PurePosixPath(entry["path"]).name
    file_key = crypto.unwrap_file_key(master_key, entry["crypto"]["wrapped_file_key"])

    yield from _iter_chunked_named(
        name,
        shard_list,
        account_map,
        providers_by_id,
        file_key,
        total_steps,
        on_progress,
    )


def _iter_chunked_named(
    name: str,
    shard_list: list[dict[str, Any]],
    account_map: dict[str, Any],
    providers_by_id: dict[str, Any],
    file_key: bytes,
    total_steps: int,
    on_progress: ProgressFn | None,
) -> Any:
    if not shard_list:
        _progress(on_progress, "done", 1, 1, "Done")
        yield name, b""
        return

    _progress(on_progress, "fetch_shard", 0, total_steps, "Fetching shards…")
    workers = _worker_count(len(shard_list))
    max_buffered = max(workers, DOWNLOAD_QUEUE_DEPTH)
    done_count = 0
    progress_lock = threading.Lock()

    def _fetch(step: int, shard_rec: dict[str, Any]) -> tuple[int, bytes]:
        nonlocal done_count
        data = _fetch_shard(shard_rec, account_map, providers_by_id)
        with progress_lock:
            done_count += 1
            _progress(
                on_progress,
                "fetch_shard",
                done_count,
                total_steps,
                f"Fetched {done_count}/{len(shard_list)} shards…",
            )
        return step, data

    n = len(shard_list)
    next_submit = 0
    next_yield = 0
    in_flight: dict[Future[tuple[int, bytes]], int] = {}
    ready: dict[int, bytes] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while next_yield < n:
            while next_submit < n and (len(in_flight) + len(ready)) < max_buffered:
                fut = pool.submit(_fetch, next_submit, shard_list[next_submit])
                in_flight[fut] = next_submit
                next_submit += 1

            if not in_flight and next_yield not in ready:
                break

            if in_flight:
                fut = next(as_completed(in_flight))
                step = in_flight.pop(fut)
                _step, data = fut.result()
                ready[step] = data

            while next_yield in ready:
                cipher = ready.pop(next_yield)
                nonce = crypto.b64d(shard_list[next_yield]["nonce"])
                plain = crypto.decrypt_chunk(file_key, cipher, nonce)
                yield name, plain
                next_yield += 1

    _progress(on_progress, "done", total_steps, total_steps, "Done")


def _fetch_shard(
    shard_rec: dict[str, Any],
    account_map: dict[str, Any],
    providers_by_id: dict[str, Any],
) -> bytes:
    account = account_map.get(shard_rec["account_id"])
    if account is None:
        raise RuntimeError(f"Missing account for shard {shard_rec['i']}")
    data = providers_by_id[account["id"]].download(shard_rec["remote_id"])
    digest = hashlib.sha256(data).hexdigest()
    if digest != shard_rec["sha256"]:
        raise RuntimeError(f"Shard {shard_rec['i']} hash mismatch")
    return data


def delete_file(file_id: str, on_progress: ProgressFn | None = None) -> None:
    index = vault_index.load_index()
    entry = vault_index.remove_file(index, file_id)
    if entry is None:
        raise FileNotFoundError("File not found")

    shard_list = entry.get("shards", [])
    total_steps = len(shard_list) + 1  # shards + index
    account_list = accounts_store.load_accounts()
    account_map = {a["id"]: a for a in account_list}
    providers_by_id = _providers_by_id(
        [a for a in account_list if any(s["account_id"] == a["id"] for s in shard_list)]
    )
    done_count = 0
    progress_lock = threading.Lock()

    def _delete_one(shard_rec: dict[str, Any]) -> None:
        nonlocal done_count
        account = account_map.get(shard_rec["account_id"])
        if account is not None:
            try:
                providers_by_id[account["id"]].delete(shard_rec["remote_id"])
            except Exception:
                pass
        with progress_lock:
            done_count += 1
            _progress(
                on_progress,
                "delete_shard",
                done_count,
                total_steps,
                f"Deleted {done_count}/{len(shard_list)} shards…",
            )

    if shard_list:
        _progress(on_progress, "delete_shard", 0, total_steps, "Deleting shards…")
        with ThreadPoolExecutor(max_workers=_worker_count(len(shard_list))) as pool:
            list(pool.map(_delete_one, shard_list))

    _progress(on_progress, "index", total_steps - 1, total_steps, "Updating vault index…")
    vault_index.save_index(index)
    _progress(on_progress, "done", total_steps, total_steps, "Done")
