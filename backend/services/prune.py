from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from providers.factory import get_provider
from services import vault as vault_service
from services.upload_pipeline import upload_pipelines
from storage import accounts as accounts_store
from storage import auth_context
from storage import upload_sessions
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


def _analyze_account(
    account: dict[str, Any], live_ids: set[str] | None = None
) -> dict[str, Any]:
    """Compare vault keep-set to provider children for one account (no deletes)."""
    account_id = account["id"]
    base: dict[str, Any] = {
        "account_id": account_id,
        "label": account.get("label") or account_id,
        "provider": account.get("provider") or "",
        "scanned": 0,
        "kept": 0,
        "orphans": [],
        "stale_sessions": [],
        "error": None,
    }
    try:
        keep = vault_index.keep_refs_for_account(account_id)
        keep_names = set(keep["names"])
        keep_folder_ids = set(keep["folder_ids"])
        keep_shard_ids = set(keep["shard_remote_ids"])

        # Only in-flight uploads are protected. Abandoned "active" resume sessions
        # (process crash, closed tab, failed job never marked failed) are stale.
        if live_ids is None:
            live_ids = upload_pipelines.live_session_ids(
                user_id=auth_context.require_user_id()
            )
        stale_sessions: list[dict[str, str]] = []
        for sess in upload_sessions.list_for_account(account_id):
            if sess["status"] == "active" and sess["id"] in live_ids:
                keep_names.add(sess["id"])
                keep_folder_ids.add(sess["remote_folder_id"])
                continue
            report_status = (
                "abandoned" if sess["status"] == "active" else sess["status"]
            )
            stale_sessions.append(
                {
                    "id": sess["id"],
                    "status": report_status,
                    "filename": sess.get("filename") or sess["id"],
                }
            )
        base["stale_sessions"] = stale_sessions

        provider = get_provider(account)
        children = provider.list_vault_children()
        base["scanned"] = len(children)

        orphans: list[dict[str, str]] = []
        kept = 0
        for child in children:
            child_id = child["id"]
            name = child.get("name") or ""
            kind = child.get("kind") or "file"
            if child_id in keep_folder_ids or name in keep_names:
                kept += 1
                continue
            if kind == "file" and child_id in keep_shard_ids:
                kept += 1
                continue
            orphans.append(
                {
                    "id": child_id,
                    "name": name or child_id,
                    "kind": kind,
                }
            )
        base["orphans"] = orphans
        base["kept"] = kept
    except Exception as exc:
        log.exception("prune analyze failed account=%s", account_id)
        base["error"] = str(exc)
    return base


def analyze_all(on_progress: ProgressCb | None = None) -> dict[str, Any]:
    """Scan every connected account for orphan remotes and stale resume sessions."""
    accounts = accounts_store.load_accounts()
    total = max(len(accounts), 1)
    live_ids = upload_pipelines.live_session_ids(user_id=auth_context.require_user_id())
    rows: list[dict[str, Any]] = []
    for i, account in enumerate(accounts, start=1):
        label = account.get("label") or account["id"]
        _emit(
            on_progress,
            "listing",
            i - 1,
            total,
            f"Analyzing {label}…",
        )
        rows.append(_analyze_account(account, live_ids))

    total_orphans = sum(len(r["orphans"]) for r in rows)
    # Deduplicate sessions that span multiple accounts.
    stale_ids = {
        s["id"] for r in rows for s in (r.get("stale_sessions") or [])
    }
    errors = sum(1 for r in rows if r.get("error"))
    _emit(
        on_progress,
        "done",
        len(accounts),
        total,
        f"Found {total_orphans} orphan(s), {len(stale_ids)} stale session(s)",
    )
    return {
        "accounts": rows,
        "total_orphans": total_orphans,
        "total_stale_sessions": len(stale_ids),
        "account_count": len(rows),
        "errors": errors,
    }


def _discard_stale_sessions(
    analyses: list[dict[str, Any]],
) -> tuple[int, set[str]]:
    """Discard unique stale resume sessions; return count and deleted folder ids."""
    discarded = 0
    folder_ids: set[str] = set()
    seen: set[str] = set()
    for analysis in analyses:
        for sess in analysis.get("stale_sessions") or []:
            sid = sess["id"]
            if sid in seen:
                continue
            seen.add(sid)
            full = upload_sessions.load(sid)
            if full is None:
                continue
            folder_ids.update((full.get("remote_folders") or {}).values())
            vault_service.discard_upload_session(sid)
            discarded += 1
    return discarded, folder_ids


def _delete_orphans(
    account: dict[str, Any],
    orphans: list[dict[str, str]],
    on_progress: ProgressCb | None,
    *,
    progress_base: int,
    progress_total: int,
) -> tuple[int, int]:
    """Delete listed orphan remotes. Returns (deleted, errors)."""
    if not orphans:
        return 0, 0
    provider = get_provider(account)
    deleted = 0
    errors = 0
    for i, child in enumerate(orphans, start=1):
        _emit(
            on_progress,
            "deleting",
            progress_base + i - 1,
            progress_total,
            f"{account.get('label')}: deleting {child.get('name') or child['id']}…",
        )
        try:
            provider.delete(child["id"])
            deleted += 1
        except Exception:
            errors += 1
            log.exception(
                "prune delete failed account=%s remote=%s name=%s",
                account["id"],
                child["id"],
                child.get("name"),
            )
    return deleted, errors


def _apply_account(
    account: dict[str, Any],
    analysis: dict[str, Any],
    on_progress: ProgressCb | None,
    *,
    progress_base: int,
    progress_total: int,
    discarded_folder_ids: set[str] | None = None,
    discarded_sessions: int | None = None,
) -> dict[str, Any]:
    account_id = account["id"]
    orphans = list(analysis.get("orphans") or [])

    if analysis.get("error"):
        return {
            "account_id": account_id,
            "label": analysis.get("label") or account_id,
            "provider": analysis.get("provider") or "",
            "deleted": 0,
            "kept": analysis.get("kept") or 0,
            "errors": 1,
            "discarded_sessions": 0,
            "error": analysis["error"],
        }

    session_discarded = 0
    if discarded_folder_ids is None:
        # Single-account path: discard this account's stale sessions first.
        session_discarded, discarded_folder_ids = _discard_stale_sessions([analysis])
    else:
        session_discarded = discarded_sessions or 0

    remaining = [c for c in orphans if c["id"] not in discarded_folder_ids]
    deleted, errors = _delete_orphans(
        account,
        remaining,
        on_progress,
        progress_base=progress_base,
        progress_total=progress_total,
    )
    # Folders removed via session discard still count as cleaned for this account.
    deleted += sum(1 for o in orphans if o["id"] in discarded_folder_ids)

    return {
        "account_id": account_id,
        "label": analysis.get("label") or account_id,
        "provider": analysis.get("provider") or "",
        "deleted": deleted,
        "kept": analysis.get("kept") or 0,
        "errors": errors,
        "discarded_sessions": session_discarded,
        "error": None,
    }


def prune_all(on_progress: ProgressCb | None = None) -> dict[str, Any]:
    """Re-analyze and delete orphans on every account."""
    accounts = accounts_store.load_accounts()
    if not accounts:
        _emit(on_progress, "done", 0, 1, "No accounts")
        return {
            "accounts": [],
            "deleted": 0,
            "discarded_sessions": 0,
            "errors": 0,
            "account_count": 0,
        }

    live_ids = upload_pipelines.live_session_ids(user_id=auth_context.require_user_id())
    analyses: list[dict[str, Any]] = []
    for i, account in enumerate(accounts, start=1):
        label = account.get("label") or account["id"]
        _emit(on_progress, "listing", i - 1, len(accounts), f"Analyzing {label}…")
        analyses.append(_analyze_account(account, live_ids))

    discarded_sessions, discarded_folder_ids = _discard_stale_sessions(analyses)

    delete_total = sum(
        len([o for o in (a.get("orphans") or []) if o["id"] not in discarded_folder_ids])
        for a in analyses
    )
    progress_total = max(delete_total, 1)
    results: list[dict[str, Any]] = []
    progress_base = 0
    by_id = {a["id"]: a for a in accounts}
    for analysis in analyses:
        account = by_id.get(analysis["account_id"])
        if account is None:
            continue
        remaining_n = len(
            [
                o
                for o in (analysis.get("orphans") or [])
                if o["id"] not in discarded_folder_ids
            ]
        )
        results.append(
            _apply_account(
                account,
                analysis,
                on_progress,
                progress_base=progress_base,
                progress_total=progress_total,
                discarded_folder_ids=discarded_folder_ids,
                discarded_sessions=0,
            )
        )
        progress_base += remaining_n

    deleted = sum(r["deleted"] for r in results)
    errors = sum(r["errors"] for r in results)
    _emit(
        on_progress,
        "done",
        deleted,
        max(deleted, 1),
        f"Pruned {deleted} orphan(s); cleared {discarded_sessions} stale session(s)",
    )
    return {
        "accounts": results,
        "deleted": deleted,
        "discarded_sessions": discarded_sessions,
        "errors": errors,
        "account_count": len(results),
    }


def prune_account(
    account_id: str,
    on_progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """Delete orphans on a single account (tests / compatibility)."""
    account = accounts_store.get_account(account_id)
    if account is None:
        raise FileNotFoundError(f"Account not found: {account_id}")
    analysis = _analyze_account(account)
    if analysis.get("error"):
        raise RuntimeError(analysis["error"])
    applied = _apply_account(
        account,
        analysis,
        on_progress,
        progress_base=0,
        progress_total=max(len(analysis.get("orphans") or []), 1),
    )
    _emit(
        on_progress,
        "done",
        applied["deleted"],
        max(applied["deleted"], 1),
        f"Pruned {applied['deleted']} orphan(s); kept {applied['kept']}",
    )
    return {
        "account_id": account_id,
        "scanned": analysis["scanned"],
        "deleted": applied["deleted"],
        "kept": applied["kept"],
        "errors": applied["errors"],
        "discarded_sessions": applied["discarded_sessions"],
    }
