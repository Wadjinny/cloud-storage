#!/usr/bin/env python3
"""Upload a local file via the running backend, download it, compare SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

BACKEND = os.environ.get("CLOUD_STORAGE_BACKEND", "http://127.0.0.1:1234")
FRAME = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(FRAME)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def wait_job(client: httpx.Client, job_id: str, *, timeout: float = 3600) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/api/vault/jobs/{job_id}")
        r.raise_for_status()
        last = r.json()
        if last["status"] in ("done", "error"):
            return last
        print(
            f"  job {job_id}: {last.get('percent')}% {last.get('message')}",
            flush=True,
        )
        time.sleep(1.0)
    raise TimeoutError(f"job {job_id} timed out; last={last}")


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    username = (
        os.environ.get("INIT_USERNAME")
        or os.environ.get("CLOUD_STORAGE_USERNAME")
        or ""
    ).strip().strip("'\"")
    passphrase = (
        os.environ.get("INIT_PASSPHRASE")
        or os.environ.get("CLOUD_STORAGE_VAULT_PASSPHRASE")
        or ""
    ).strip().strip("'\"")
    if not username or not passphrase:
        print("INIT_USERNAME / INIT_PASSPHRASE missing in .env", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--keep", action="store_true", help="do not delete after check")
    args = parser.parse_args()
    path: Path = args.path.expanduser().resolve()
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    size = path.stat().st_size
    print(f"file={path.name} size={size} ({size / (1024**2):.1f} MiB)", flush=True)
    print("hashing local file…", flush=True)
    before = sha256_file(path)
    print(f"sha256_before={before}", flush=True)

    # timeout=None disables all timeouts (needed for large MEGA/Drive transfers).
    with httpx.Client(base_url=BACKEND, timeout=None, follow_redirects=False) as client:
        unlock = client.post(
            "/api/auth/login",
            json={"username": username, "passphrase": passphrase},
        )
        if unlock.status_code != 200:
            print(f"login failed: {unlock.status_code} {unlock.text}", file=sys.stderr)
            return 1
        print("vault unlocked", flush=True)

        # Remove any prior partial upload with the same path.
        listing = client.get("/api/vault/files", params={"path": "/pytest-msi"})
        if listing.status_code == 200:
            for e in listing.json():
                if e.get("kind") == "file" and e.get("name") == path.name:
                    print(f"removing prior file {e['id']} size={e.get('size')}", flush=True)
                    d = client.delete(f"/api/vault/files/{e['id']}")
                    if d.status_code == 200 and d.json().get("job_id"):
                        wait_job(client, d.json()["job_id"], timeout=600)

        start = client.post(
            "/api/vault/upload/start",
            params={"path": "/pytest-msi"},
            json={"filename": path.name, "size": size},
        )
        if start.status_code != 200:
            print(f"upload start failed: {start.status_code} {start.text}", file=sys.stderr)
            return 1
        start_body = start.json()
        job_id = start_body["job_id"]
        print(
            f"upload job={job_id} chunk_size={start_body.get('chunk_size')}",
            flush=True,
        )

        t0 = time.monotonic()
        # Use a real file object (not a generator) so httpx streams with Content-Length.
        with path.open("rb") as body:
            put = client.put(
                f"/api/vault/upload/{job_id}",
                content=body,
                headers={
                    "Content-Length": str(size),
                    "Content-Type": "application/octet-stream",
                },
            )
        print(
            f"put status={put.status_code} elapsed={time.monotonic() - t0:.1f}s body={put.text[:300]!r}",
            flush=True,
        )
        if put.status_code != 200:
            print(f"upload put failed: {put.text}", file=sys.stderr)
            return 1

        job = wait_job(client, job_id)
        print(f"upload job status={job['status']} message={job.get('message')}", flush=True)
        if job["status"] != "done":
            print(f"upload failed: {job}", file=sys.stderr)
            return 1

        listing = client.get("/api/vault/files", params={"path": "/pytest-msi"})
        listing.raise_for_status()
        entry = next(e for e in listing.json() if e.get("name") == path.name)
        file_id = entry["id"]
        print(f"file_id={file_id} stored_size={entry.get('size')}", flush=True)
        if entry.get("size") != size:
            print(
                f"stored size mismatch: got {entry.get('size')} expected {size}",
                file=sys.stderr,
            )
            return 1

        dl = client.post("/api/vault/download", params={"id": file_id})
        if dl.status_code != 200:
            print(f"download start failed: {dl.status_code} {dl.text}", file=sys.stderr)
            return 1
        dl_job = dl.json()["job_id"]
        print(f"download job={dl_job}", flush=True)

        t1 = time.monotonic()
        h = hashlib.sha256()
        total = 0
        with client.stream("GET", f"/api/vault/download/{dl_job}/stream") as resp:
            print(f"download stream status={resp.status_code}", flush=True)
            resp.raise_for_status()
            for chunk in resp.iter_bytes(FRAME):
                h.update(chunk)
                total += len(chunk)
                if total % (32 * FRAME) < FRAME:
                    print(f"  downloaded {total / (1024**2):.1f} MiB…", flush=True)
        after = h.hexdigest()
        print(f"download bytes={total} elapsed={time.monotonic() - t1:.1f}s", flush=True)
        print(f"sha256_after={after}", flush=True)

        dl_job_final = wait_job(client, dl_job, timeout=120)
        print(f"download job status={dl_job_final['status']}", flush=True)

        ok = before == after and total == size
        print(f"checksum_match={ok}", flush=True)
        if not ok:
            print("MISMATCH", file=sys.stderr)
            return 1

        if not args.keep:
            deleted = client.delete(f"/api/vault/files/{file_id}")
            print(f"cleanup delete status={deleted.status_code}", flush=True)
            if deleted.status_code == 200:
                del_job = deleted.json().get("job_id")
                if del_job:
                    dj = wait_job(client, del_job, timeout=600)
                    print(f"cleanup job status={dj['status']}", flush=True)

    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
