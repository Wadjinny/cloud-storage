from __future__ import annotations

import logging
from typing import Any

from providers.factory import get_provider
from services import crypto
from services.capacity import PlacementPlanner
from storage import accounts as accounts_store
from storage import vault_index

log = logging.getLogger(__name__)

THUMB_MIME = "image/webp"


def pack_encrypted(key: bytes, plaintext: bytes) -> bytes:
    """Opaque blob: nonce || ciphertext(+tag)."""
    return crypto.seal_bytes(key, plaintext)


def unpack_encrypted(key: bytes, blob: bytes) -> bytes:
    return crypto.open_bytes(key, blob)


def pick_thumb_account(
    accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Prefer placeable gdrive by headroom; else any gdrive; else first placeable."""
    account_list = accounts if accounts is not None else accounts_store.load_accounts()
    if not account_list:
        return None

    planner = PlacementPlanner.from_accounts(account_list)
    by_id = {a["id"]: a for a in account_list}
    order_index = {a["id"]: i for i, a in enumerate(account_list)}

    def headroom_of(account: dict[str, Any]) -> int:
        q = next((x for x in planner.quotas if x.account_id == account["id"]), None)
        if q is None:
            return -1
        h = q.headroom()
        return h if h is not None else 2**62

    placeable_ids = {a.account_id for a in planner.placeable_accounts()}
    gdrive = [a for a in account_list if a.get("provider") == "gdrive"]
    placeable_gdrive = [a for a in gdrive if a["id"] in placeable_ids]
    if placeable_gdrive:
        placeable_gdrive.sort(
            key=lambda a: (-headroom_of(a), order_index[a["id"]]),
        )
        return placeable_gdrive[0]
    if gdrive:
        return gdrive[0]
    placeable_any = [by_id[i] for i in placeable_ids if i in by_id]
    if placeable_any:
        placeable_any.sort(key=lambda a: order_index[a["id"]])
        return placeable_any[0]
    return account_list[0]


def upload_thumb_blob(
    *,
    file_id: str,
    account: dict[str, Any],
    remote_folders: dict[str, str],
    blob: bytes,
) -> str:
    """Upload encrypted thumb; return provider remote id.

    Name is always ``thumb_{file_id}.webp.enc`` so providers that key by name
    (MemoryProvider) do not collide across files.
    """
    provider = get_provider(account)
    account_id = account["id"]
    parent_id = remote_folders.get(account_id)
    remote_name = f"thumb_{file_id}.webp.enc"
    return provider.upload(remote_name, blob, parent_id=parent_id)


def persist_ready(
    *,
    file_id: str,
    account_id: str,
    remote_id: str,
    wrapped_key: str,
    size_bytes: int,
) -> None:
    vault_index.update_thumbnail(
        file_id,
        status="ready",
        account_id=account_id,
        remote_id=remote_id,
        wrapped_key=wrapped_key,
        mime=THUMB_MIME,
        size_bytes=size_bytes,
        error=None,
    )


def persist_status(
    file_id: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    vault_index.update_thumbnail(
        file_id,
        status=status,
        account_id=None,
        remote_id=None,
        wrapped_key=None,
        mime=None,
        size_bytes=None,
        error=error,
    )


def load_thumbnail_bytes(
    entry: dict[str, Any],
    master_key: bytes,
) -> bytes:
    """Download and decrypt a ready thumbnail. Raises on missing/error."""
    thumb = entry.get("thumb") or {}
    status = entry.get("thumb_status") or thumb.get("status") or "none"
    if status != "ready":
        raise FileNotFoundError("Thumbnail not ready")
    account_id = thumb.get("account_id") or entry.get("thumb_account_id")
    remote_id = thumb.get("remote_id") or entry.get("thumb_remote_id")
    wrapped = thumb.get("wrapped_key") or entry.get("thumb_wrapped_key")
    if not account_id or not remote_id or not wrapped:
        raise FileNotFoundError("Thumbnail metadata incomplete")
    account = accounts_store.get_account(account_id)
    if account is None:
        raise FileNotFoundError("Thumbnail account missing")
    provider = get_provider(account)
    blob = provider.download(remote_id)
    key = crypto.unwrap_file_key(master_key, wrapped)
    return unpack_encrypted(key, blob)
