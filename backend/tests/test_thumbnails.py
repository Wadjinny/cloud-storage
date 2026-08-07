from __future__ import annotations

import io
import time
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from providers.memory import MemoryProvider
from services import crypto
from services.thumbnails.classify import classify_filename
from services.thumbnails.generate import generate_webp
from services.thumbnails.store import pack_encrypted, unpack_encrypted
from storage import auth_context
from storage import users as users_store
from storage import vault_index
from tests.conftest import TEST_PASSPHRASE, TEST_USERNAME


def _scope():
    user = users_store.get_by_username(TEST_USERNAME)
    assert user is not None
    master_key = crypto.derive_master_key(TEST_PASSPHRASE, crypto.b64d(user["kdf_salt"]))
    return auth_context.auth_scope(user["id"], master_key), master_key


def _png_bytes(color: tuple[int, int, int] = (40, 120, 200)) -> bytes:
    im = Image.new("RGB", (64, 48), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_classify_filename() -> None:
    assert classify_filename("a.PNG") == "image"
    assert classify_filename("x.pdf") == "pdf"
    assert classify_filename("v.mp4") == "video"
    assert classify_filename("clip.mkv") == "video"
    assert classify_filename("d.docx") == "office"
    assert classify_filename("z.bin") == "unsupported"


def test_video_thumb_from_prefix_only(tmp_path: Path) -> None:
    """ffmpeg only needs an early prefix; large videos must not require a full spool."""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        return
    full = tmp_path / "full.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:d=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(full),
        ],
        check=True,
    )
    prefix = tmp_path / "prefix.mkv"
    data = full.read_bytes()
    # Keep well under a typical film size; still enough for the first keyframe.
    prefix.write_bytes(data[: min(len(data), 256 * 1024)])
    webp, kind = generate_webp(prefix, "movie.mkv")
    assert kind == "video"
    assert webp[:4] == b"RIFF"


def test_generate_webp_from_png(tmp_path: Path) -> None:
    path = tmp_path / "sample.png"
    path.write_bytes(_png_bytes())
    webp, kind = generate_webp(path, "sample.png")
    assert kind == "image"
    assert webp[:4] == b"RIFF"
    assert b"WEBP" in webp[:16]


def test_pack_unpack_roundtrip() -> None:
    key = crypto.new_file_key()
    plain = b"hello-thumb"
    blob = pack_encrypted(key, plain)
    assert unpack_encrypted(key, blob) == plain


def test_upload_image_gets_thumbnail(
    unlocked_client: TestClient,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    png = _png_bytes()
    start = unlocked_client.post(
        "/api/vault/upload/start",
        json={"filename": "photo.png", "size": len(png)},
    )
    assert start.status_code == 200, start.text
    job_id = start.json()["job_id"]
    put = unlocked_client.put(
        f"/api/vault/upload/{job_id}",
        content=png,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert put.status_code == 200, put.text

    body: dict = {}
    for _ in range(80):
        snap = unlocked_client.get(f"/api/vault/jobs/{job_id}")
        body = snap.json()
        if body["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert body["status"] == "done", body
    file_id = body["result"]["id"]

    listing = unlocked_client.get("/api/vault/files?path=/").json()
    row = next(e for e in listing if e["id"] == file_id)
    assert row["thumb_status"] == "ready"

    thumb = unlocked_client.get(f"/api/vault/files/{file_id}/thumbnail")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"].startswith("image/webp")
    assert "max-age=" in thumb.headers.get("cache-control", "")
    assert thumb.content[:4] == b"RIFF"

    with _scope()[0]:
        entry = vault_index.find_file(None, file_id)
        assert entry is not None
        assert entry["thumb_status"] == "ready"


def test_upload_unsupported_marks_status(
    unlocked_client: TestClient,
    memory_account: dict,
) -> None:
    MemoryProvider.reset()
    payload = b"not-an-image"
    start = unlocked_client.post(
        "/api/vault/upload/start",
        json={"filename": "notes.bin", "size": len(payload)},
    )
    assert start.status_code == 200
    job_id = start.json()["job_id"]
    put = unlocked_client.put(f"/api/vault/upload/{job_id}", content=payload)
    assert put.status_code == 200, put.text

    body: dict = {}
    for _ in range(80):
        snap = unlocked_client.get(f"/api/vault/jobs/{job_id}")
        body = snap.json()
        if body["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert body["status"] == "done", body
    file_id = body["result"]["id"]
    listing = unlocked_client.get("/api/vault/files?path=/").json()
    row = next(e for e in listing if e["id"] == file_id)
    assert row["thumb_status"] == "unsupported"
    thumb = unlocked_client.get(f"/api/vault/files/{file_id}/thumbnail")
    assert thumb.status_code == 404
