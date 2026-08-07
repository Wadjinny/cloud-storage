from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from providers.factory import get_provider
from storage import accounts as accounts_store
from storage import vault_index

log = logging.getLogger(__name__)

ProgressCb = Callable[[str, int, int, str], None]


def _emit(
    cb: ProgressCb | None,
    phase: str,
    current: int,
    total: int,
    message: str,
) -> None:
    if cb is None:
        return
    cb(phase, current, total, message)


def _shard_items(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for entry in files:
        file_id = entry["id"]
        path = entry.get("path") or file_id
        for shard in entry.get("shards") or []:
            items.append(
                {
                    "kind": "shard",
                    "file_id": file_id,
                    "path": path,
                    "i": int(shard["i"]),
                    "account_id": shard["account_id"],
                    "remote_id": shard["remote_id"],
                }
            )
        thumb = entry.get("thumb") or {}
        if (
            (entry.get("thumb_status") or thumb.get("status")) == "ready"
            and thumb.get("remote_id")
            and thumb.get("account_id")
        ):
            items.append(
                {
                    "kind": "thumb",
                    "file_id": file_id,
                    "path": path,
                    "i": None,
                    "account_id": thumb["account_id"],
                    "remote_id": thumb["remote_id"],
                }
            )
    return items


def check_shards(on_progress: ProgressCb | None = None) -> dict[str, Any]:
    """Verify every indexed shard (and ready thumbnail) still exists on its provider."""
    accounts = {a["id"]: a for a in accounts_store.load_accounts()}
    files = vault_index.load_index().get("files") or []
    items = _shard_items(files)
    total = max(len(items), 1)

    providers: dict[str, Any] = {}
    account_errors: dict[str, str] = {}
    missing: list[dict[str, Any]] = []
    checked = 0
    ok = 0

    _emit(on_progress, "scan", 0, total, f"Checking {len(items)} remote object(s)…")

    for idx, item in enumerate(items):
        label = item["path"]
        if item["kind"] == "thumb":
            msg = f"Checking thumbnail {label}…"
        else:
            msg = f"Checking {label} shard {item['i']}…"
        _emit(on_progress, "scan", idx, total, msg)

        account_id = item["account_id"]
        account = accounts.get(account_id)
        if account is None:
            missing.append(
                {
                    **item,
                    "label": account_id,
                    "provider": "",
                    "reason": "no_account",
                    "error": "Account disconnected",
                }
            )
            continue

        if account_id in account_errors:
            missing.append(
                {
                    **item,
                    "label": account.get("label") or account_id,
                    "provider": account.get("provider") or "",
                    "reason": "account_error",
                    "error": account_errors[account_id],
                }
            )
            continue

        if account_id not in providers:
            try:
                providers[account_id] = get_provider(account)
            except Exception as exc:
                log.exception("health provider init failed account=%s", account_id)
                account_errors[account_id] = str(exc)
                missing.append(
                    {
                        **item,
                        "label": account.get("label") or account_id,
                        "provider": account.get("provider") or "",
                        "reason": "account_error",
                        "error": str(exc),
                    }
                )
                continue

        try:
            present = bool(providers[account_id].exists(item["remote_id"]))
        except Exception as exc:
            log.warning(
                "health exists failed account=%s remote=%s: %s",
                account_id,
                item["remote_id"],
                exc,
            )
            missing.append(
                {
                    **item,
                    "label": account.get("label") or account_id,
                    "provider": account.get("provider") or "",
                    "reason": "check_error",
                    "error": str(exc),
                }
            )
            continue

        checked += 1
        if present:
            ok += 1
        else:
            missing.append(
                {
                    **item,
                    "label": account.get("label") or account_id,
                    "provider": account.get("provider") or "",
                    "reason": "missing",
                    "error": None,
                }
            )

    _emit(
        on_progress,
        "done",
        total,
        total,
        "Health check complete" if not missing else f"{len(missing)} issue(s) found",
    )

    by_file: dict[str, dict[str, Any]] = {}
    for row in missing:
        fid = row["file_id"]
        bucket = by_file.setdefault(
            fid,
            {
                "file_id": fid,
                "path": row["path"],
                "issues": [],
            },
        )
        bucket["issues"].append(row)

    return {
        "file_count": len(files),
        "object_count": len(items),
        "checked": checked,
        "ok": ok,
        "issue_count": len(missing),
        "healthy": len(missing) == 0,
        "missing": missing,
        "files": list(by_file.values()),
        "account_errors": [
            {"account_id": aid, "error": err} for aid, err in account_errors.items()
        ],
    }
