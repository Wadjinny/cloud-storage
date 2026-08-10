from __future__ import annotations

import io
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from config import (
    DELETE_CONCURRENCY,
    GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY,
    GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_SCOPES,
    HEALTH_CHECK_CONCURRENCY,
    PROVIDER_OP_RETRIES,
    PROVIDER_OP_TIMEOUT,
    SHARD_FOLDER_NAME,
)
from providers.retry import call_with_retries
from storage import accounts as accounts_store

log = logging.getLogger(__name__)


def _timeout_pool_workers() -> int:
    """Enough workers for parallel shard/health/delete ops on one Drive account."""
    return max(
        1,
        GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY,
        GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY,
        HEALTH_CHECK_CONCURRENCY,
        DELETE_CONCURRENCY,
    )


def _parse_expiry(raw: Any) -> datetime | None:
    """Parse expiry as naive UTC (google-auth compares against naive utcnow())."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        exp = raw
    else:
        try:
            exp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    if exp.tzinfo is not None:
        exp = exp.astimezone(timezone.utc).replace(tzinfo=None)
    return exp


class GDriveProvider:
    """Drive provider with a per-thread API client so shard ops can run in parallel.

    googleapiclient's HTTP transport is not thread-safe, so each worker thread gets
    its own discovery service. OAuth credentials are shared and refreshed under a
    lock, then persisted so later shards/processes do not 401-refresh every call.
    """

    def __init__(
        self,
        credentials: dict[str, Any],
        *,
        account_id: str | None = None,
        user_id: str | None = None,
        master_key: bytes | None = None,
    ) -> None:
        # Empty string tokens break google-auth (treated as present but invalid).
        self._account_id = account_id
        self._user_id = user_id
        self._master_key = master_key
        self._refresh_token = credentials.get("refresh_token") or None
        self._creds_lock = threading.Lock()
        self._creds = Credentials(
            token=credentials.get("token") or None,
            refresh_token=self._refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=GOOGLE_SCOPES,
            expiry=_parse_expiry(credentials.get("expiry")),
        )
        self._persisted_token = self._creds.token
        self._local = threading.local()
        self._folder_id: str | None = None
        self._folder_lock = threading.Lock()
        # Pool sized for parallel shard/health ops; thread-local Drive clients stick.
        self._timeout_pool = ThreadPoolExecutor(
            max_workers=_timeout_pool_workers(),
            thread_name_prefix="gdrive-op",
        )
        self._ensure_fresh_credentials()

    def _persist_credentials(self) -> None:
        if not self._account_id:
            return
        token = self._creds.token
        if token == self._persisted_token and self._creds.expiry is not None:
            return
        expiry = self._creds.expiry
        if expiry is not None and expiry.tzinfo is not None:
            expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        accounts_store.update_credentials(
            self._account_id,
            {
                "token": token,
                "refresh_token": self._creds.refresh_token or self._refresh_token,
                "expiry": expiry.isoformat() if expiry is not None else None,
            },
            user_id=self._user_id,
            master_key=self._master_key,
        )
        self._persisted_token = token

    def _ensure_fresh_credentials(self) -> Credentials:
        """Refresh before use when expired or when expiry was never stored."""
        with self._creds_lock:
            creds = self._creds
            # Token without expiry looks "valid" to google-auth but is often stale.
            needs_refresh = bool(creds.refresh_token) and (
                not creds.valid or creds.expiry is None
            )
            if needs_refresh:
                log.info("gdrive refreshing access token (proactive)")
                creds.refresh(Request())
                self._persist_credentials()
            return creds

    def _thread_credentials(self) -> Credentials:
        return self._ensure_fresh_credentials()

    def _service(self) -> Resource:
        service = getattr(self._local, "service", None)
        if service is None:
            # Socket-level timeout so a hung Drive upload cannot block forever.
            http = AuthorizedHttp(
                self._thread_credentials(),
                http=httplib2.Http(timeout=PROVIDER_OP_TIMEOUT),
            )
            service = build(
                "drive",
                "v3",
                http=http,
                cache_discovery=False,
            )
            self._local.service = service
        return service

    def _drop_thread_service(self) -> None:
        if hasattr(self._local, "service"):
            del self._local.service

    def _find_or_create_folder(self, name: str, *, parent_id: str | None = None) -> str:
        """Find or create a Drive folder by name (optionally under parent_id)."""
        escaped = name.replace("'", "\\'")
        query = (
            f"name = '{escaped}' and mimeType = 'application/vnd.google-apps.folder' "
            "and trashed = false"
        )
        if parent_id is not None:
            query += f" and '{parent_id}' in parents"
        service = self._service()
        found = (
            service.files()
            .list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
            .execute()
        )
        files = found.get("files", [])
        if files:
            return files[0]["id"]

        body: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id is not None:
            body["parents"] = [parent_id]
        created = service.files().create(body=body, fields="id").execute()
        return created["id"]

    def _ensure_folder(self) -> str:
        if self._folder_id:
            return self._folder_id

        with self._folder_lock:
            if self._folder_id:
                return self._folder_id
            self._folder_id = self._find_or_create_folder(SHARD_FOLDER_NAME)
            return self._folder_id

    def ensure_child_folder(self, name: str, *, parent_id: str | None = None) -> str:
        """Opaque folder under ``parent_id``, or under the vault root when omitted."""

        def _do() -> str:
            parent = parent_id if parent_id is not None else self._ensure_folder()
            return self._find_or_create_folder(name, parent_id=parent)

        return self._run_timed(
            _do, op=f"ensure_child_folder name={name} parent={parent_id or 'vault'}"
        )

    def ensure_file_folder(self, file_id: str, *, parent_id: str | None = None) -> str:
        """Per-upload folder (cancel deletes this in one call)."""
        return self.ensure_child_folder(file_id, parent_id=parent_id)

    def list_vault_children(self) -> list[dict[str, str]]:
        """List immediate children of the vault root folder."""

        def _do() -> list[dict[str, str]]:
            root_id = self._ensure_folder()
            service = self._service()
            out: list[dict[str, str]] = []
            page_token: str | None = None
            folder_mime = "application/vnd.google-apps.folder"
            query = f"'{root_id}' in parents and trashed = false"
            while True:
                response = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="nextPageToken, files(id, name, mimeType)",
                        pageSize=100,
                        pageToken=page_token,
                    )
                    .execute()
                )
                for item in response.get("files", []):
                    kind = "folder" if item.get("mimeType") == folder_mime else "file"
                    out.append(
                        {
                            "id": item["id"],
                            "name": item.get("name") or "",
                            "kind": kind,
                        }
                    )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            return out

        return self._run_timed(_do, op="list_vault_children")

    def _run_timed(self, fn, *, op: str):  # noqa: ANN001
        """Run a Drive call with a hard timeout; do not block forever on shutdown."""
        t0 = time.monotonic()
        fut = self._timeout_pool.submit(fn)
        try:
            result = fut.result(timeout=PROVIDER_OP_TIMEOUT)
        except TimeoutError as exc:
            log.error("gdrive %s timed out after %ss", op, PROVIDER_OP_TIMEOUT)
            # Drop the per-thread service and replace the pool so the next op
            # is not queued behind the hung thread.
            self._drop_thread_service()
            old_pool = self._timeout_pool
            self._timeout_pool = ThreadPoolExecutor(
                max_workers=_timeout_pool_workers(),
                thread_name_prefix="gdrive-op",
            )
            old_pool.shutdown(wait=False, cancel_futures=True)
            raise TimeoutError(
                f"Google Drive operation timed out after {PROVIDER_OP_TIMEOUT}s"
            ) from exc
        # Persist if AuthorizedHttp refreshed after a rare 401.
        with self._creds_lock:
            self._persist_credentials()
        log.debug("gdrive %s ok elapsed=%.2fs", op, time.monotonic() - t0)
        return result

    def upload(self, remote_id: str, data: bytes, *, parent_id: str | None = None) -> str:
        log.info("gdrive upload start name=%s bytes=%s", remote_id, len(data))
        t0 = time.monotonic()

        def _once() -> str:
            def _do() -> str:
                folder_id = parent_id if parent_id is not None else self._ensure_folder()
                media = MediaIoBaseUpload(
                    io.BytesIO(data),
                    mimetype="application/octet-stream",
                    resumable=False,
                )
                created = (
                    self._service()
                    .files()
                    .create(
                        body={"name": remote_id, "parents": [folder_id]},
                        media_body=media,
                        fields="id",
                    )
                    .execute()
                )
                return created["id"]

            return self._run_timed(_do, op=f"upload name={remote_id}")

        try:
            file_id = call_with_retries(
                _once,
                op=f"gdrive upload name={remote_id}",
                attempts=PROVIDER_OP_RETRIES,
            )
        except Exception:
            log.exception(
                "gdrive upload failed name=%s bytes=%s elapsed=%.2fs",
                remote_id,
                len(data),
                time.monotonic() - t0,
            )
            raise
        log.info(
            "gdrive upload done name=%s id=%s bytes=%s elapsed=%.2fs",
            remote_id,
            file_id,
            len(data),
            time.monotonic() - t0,
        )
        return file_id

    def download(self, remote_id: str) -> bytes:
        def _once() -> bytes:
            def _do() -> bytes:
                request = self._service().files().get_media(fileId=remote_id)
                buffer = io.BytesIO()
                downloader = MediaIoBaseDownload(buffer, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                return buffer.getvalue()

            return self._run_timed(_do, op=f"download id={remote_id}")

        return call_with_retries(
            _once,
            op=f"gdrive download id={remote_id}",
            attempts=PROVIDER_OP_RETRIES,
        )

    def exists(self, remote_id: str) -> bool:
        def _do() -> bool:
            try:
                self._service().files().get(fileId=remote_id, fields="id").execute()
                return True
            except Exception as exc:
                if self._is_not_found(exc):
                    return False
                raise

        return self._run_timed(_do, op=f"exists id={remote_id}")

    def _is_not_found(self, exc: BaseException) -> bool:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 404:
            return True
        err = str(exc).lower()
        return "404" in err or "not found" in err or "file not found" in err

    def _get_mime_type(self, remote_id: str) -> str | None:
        """Return mime type, or None if the file is already gone."""

        def _do() -> str | None:
            try:
                meta = (
                    self._service()
                    .files()
                    .get(fileId=remote_id, fields="id, mimeType")
                    .execute()
                )
            except Exception as exc:
                if self._is_not_found(exc):
                    return None
                raise
            return meta.get("mimeType") or ""

        return self._run_timed(_do, op=f"get mime id={remote_id}")

    def _list_direct_children(self, parent_id: str) -> list[dict[str, str]]:
        """List immediate children; each list page is individually timed."""
        out: list[dict[str, str]] = []
        page_token: str | None = None
        query = f"'{parent_id}' in parents and trashed = false"

        while True:
            token = page_token

            def _do(page: str | None = token) -> tuple[list[dict[str, str]], str | None]:
                response = (
                    self._service()
                    .files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="nextPageToken, files(id, name, mimeType)",
                        pageSize=100,
                        pageToken=page,
                    )
                    .execute()
                )
                page_items: list[dict[str, str]] = []
                for item in response.get("files", []):
                    page_items.append(
                        {
                            "id": item["id"],
                            "name": item.get("name") or "",
                            "mimeType": item.get("mimeType") or "",
                        }
                    )
                return page_items, response.get("nextPageToken")

            items, page_token = self._run_timed(
                _do, op=f"list children parent={parent_id}"
            )
            out.extend(items)
            if not page_token:
                break
        return out

    def _delete_one(self, remote_id: str) -> None:
        def _do() -> None:
            try:
                self._service().files().delete(fileId=remote_id).execute()
            except Exception as exc:
                if self._is_not_found(exc):
                    return
                raise

        self._run_timed(_do, op=f"delete id={remote_id}")

    def delete(self, remote_id: str) -> None:
        """Delete a file/folder. Folders are emptied first (drive.file scope).

        Each Drive API call has its own ``PROVIDER_OP_TIMEOUT`` so a deep
        vault-dir tree is not killed by a single wall-clock budget.
        """
        folder_mime = "application/vnd.google-apps.folder"
        try:
            mime = self._get_mime_type(remote_id)
            if mime is None:
                return
            if mime == folder_mime:
                for child in self._list_direct_children(remote_id):
                    self.delete(child["id"])
            self._delete_one(remote_id)
        except TimeoutError:
            log.warning("gdrive delete timed out id=%s", remote_id)
            raise
        except Exception as exc:
            if self._is_not_found(exc):
                return
            log.warning("gdrive delete failed id=%s: %s", remote_id, exc)
            raise

    def vault_root(self) -> str:
        return self._ensure_folder()

    def move_item(self, remote_id: str, *, parent_id: str) -> None:
        def _once() -> None:
            def _do() -> None:
                meta = (
                    self._service()
                    .files()
                    .get(fileId=remote_id, fields="parents")
                    .execute()
                )
                prev_parents = ",".join(meta.get("parents") or [])
                body: dict[str, Any] = {}
                kwargs: dict[str, Any] = {
                    "fileId": remote_id,
                    "addParents": parent_id,
                    "fields": "id, parents",
                    "body": body,
                }
                if prev_parents:
                    kwargs["removeParents"] = prev_parents
                self._service().files().update(**kwargs).execute()

            self._run_timed(_do, op=f"move id={remote_id} → {parent_id}")

        call_with_retries(
            _once,
            op=f"gdrive move id={remote_id}",
            attempts=PROVIDER_OP_RETRIES,
        )

    def copy_item(self, remote_id: str, *, parent_id: str, name: str) -> str:
        def _once() -> str:
            def _do() -> str:
                created = (
                    self._service()
                    .files()
                    .copy(
                        fileId=remote_id,
                        body={"name": name, "parents": [parent_id]},
                        fields="id",
                    )
                    .execute()
                )
                return created["id"]

            return self._run_timed(_do, op=f"copy id={remote_id} → {parent_id}")

        return call_with_retries(
            _once,
            op=f"gdrive copy id={remote_id}",
            attempts=PROVIDER_OP_RETRIES,
        )

    def quota(self) -> dict[str, int | None]:
        def _do() -> dict[str, int | None]:
            try:
                about = self._service().about().get(fields="storageQuota").execute()
                q = about.get("storageQuota", {})
                used = int(q["usage"]) if q.get("usage") is not None else None
                total = int(q["limit"]) if q.get("limit") is not None else None
                return {"used": used, "total": total}
            except Exception:
                return {"used": None, "total": None}

        try:
            return self._run_timed(_do, op="quota")
        except TimeoutError:
            return {"used": None, "total": None}
