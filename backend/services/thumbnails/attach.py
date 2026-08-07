from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from services import crypto
from services.thumbnails.classify import classify_filename
from services.thumbnails.generate import ThumbGenError, generate_webp
from services.thumbnails.store import (
    pack_encrypted,
    persist_ready,
    persist_status,
    pick_thumb_account,
    upload_thumb_blob,
)

log = logging.getLogger(__name__)


def attach_after_upload(
    *,
    file_id: str,
    filename: str,
    spool_path: Path | None,
    master_key: bytes,
    remote_folders: dict[str, str],
    accounts: list[dict[str, Any]] | None = None,
) -> None:
    """Generate, encrypt, and upload a thumbnail. Never raises to callers."""
    try:
        kind = classify_filename(filename)
        if kind == "unsupported":
            persist_status(file_id, "unsupported")
            return
        if spool_path is None or not spool_path.is_file():
            persist_status(file_id, "failed", error="no plaintext spool for thumbnail")
            return

        try:
            webp, _ = generate_webp(spool_path, filename)
        except ThumbGenError as exc:
            persist_status(file_id, "failed", error=str(exc))
            log.warning("thumb gen failed file=%s: %s", file_id, exc)
            return

        account = pick_thumb_account(accounts)
        if account is None:
            persist_status(file_id, "failed", error="no account for thumbnail")
            return

        thumb_key = crypto.new_file_key()
        blob = pack_encrypted(thumb_key, webp)
        remote_id = upload_thumb_blob(
            file_id=file_id,
            account=account,
            remote_folders=remote_folders,
            blob=blob,
        )
        persist_ready(
            file_id=file_id,
            account_id=account["id"],
            remote_id=remote_id,
            wrapped_key=crypto.wrap_file_key(master_key, thumb_key),
            size_bytes=len(webp),
        )
        log.info(
            "thumb ready file=%s account=%s bytes=%s",
            file_id,
            account["id"],
            len(webp),
        )
    except Exception as exc:
        log.exception("thumb attach failed file=%s", file_id)
        try:
            persist_status(file_id, "failed", error=str(exc) or type(exc).__name__)
        except Exception:
            log.exception("thumb status persist failed file=%s", file_id)
