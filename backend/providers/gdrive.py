from __future__ import annotations

import io
import threading
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_SCOPES, SHARD_FOLDER_NAME


class GDriveProvider:
    """Drive provider with a per-thread API client so shard ops can run in parallel.

    googleapiclient's HTTP transport is not thread-safe, and OAuth Credentials
    refresh is not either, so each worker thread gets its own credentials copy
    and discovery service.
    """

    def __init__(self, credentials: dict[str, Any]) -> None:
        self._token = credentials.get("token")
        self._refresh_token = credentials.get("refresh_token")
        self._local = threading.local()
        self._folder_id: str | None = None
        self._folder_lock = threading.Lock()

    def _thread_credentials(self) -> Credentials:
        creds = getattr(self._local, "creds", None)
        if creds is None:
            creds = Credentials(
                token=self._token,
                refresh_token=self._refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=GOOGLE_CLIENT_ID,
                client_secret=GOOGLE_CLIENT_SECRET,
                scopes=GOOGLE_SCOPES,
            )
            self._local.creds = creds
        return creds

    def _service(self) -> Resource:
        service = getattr(self._local, "service", None)
        if service is None:
            service = build(
                "drive",
                "v3",
                credentials=self._thread_credentials(),
                cache_discovery=False,
            )
            self._local.service = service
        return service

    def _ensure_folder(self) -> str:
        if self._folder_id:
            return self._folder_id

        with self._folder_lock:
            if self._folder_id:
                return self._folder_id

            # Escape single quotes in folder name for Drive query syntax.
            name = SHARD_FOLDER_NAME.replace("'", "\\'")
            query = (
                f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
                "and trashed = false"
            )
            service = self._service()
            found = (
                service.files()
                .list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
                .execute()
            )
            files = found.get("files", [])
            if files:
                self._folder_id = files[0]["id"]
                return self._folder_id

            created = (
                service.files()
                .create(
                    body={
                        "name": SHARD_FOLDER_NAME,
                        "mimeType": "application/vnd.google-apps.folder",
                    },
                    fields="id",
                )
                .execute()
            )
            self._folder_id = created["id"]
            return self._folder_id

    def upload(self, remote_id: str, data: bytes) -> str:
        folder_id = self._ensure_folder()
        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype="application/octet-stream",
            resumable=True,
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

    def download(self, remote_id: str) -> bytes:
        request = self._service().files().get_media(fileId=remote_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    def delete(self, remote_id: str) -> None:
        try:
            self._service().files().delete(fileId=remote_id).execute()
        except Exception:
            pass

    def quota(self) -> dict[str, int | None]:
        try:
            about = self._service().about().get(fields="storageQuota").execute()
            q = about.get("storageQuota", {})
            used = int(q["usage"]) if q.get("usage") is not None else None
            total = int(q["limit"]) if q.get("limit") is not None else None
            return {"used": used, "total": total}
        except Exception:
            return {"used": None, "total": None}
