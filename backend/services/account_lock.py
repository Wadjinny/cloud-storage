from __future__ import annotations

from services.download_session import download_sessions
from services.jobs import jobs
from services.upload_pipeline import upload_pipelines


def accounts_manage_blocked_reason(user_id: str) -> str | None:
    """Return a human reason if provider account management is locked for ``user_id``."""
    if jobs.has_running_for_user(user_id):
        return "Account management is locked while an operation is running"
    if upload_pipelines.has_for_user(user_id):
        return "Account management is locked while an upload is in progress"
    if download_sessions.has_for_user(user_id):
        return "Account management is locked while a download is in progress"
    return None


def accounts_manageable(user_id: str) -> bool:
    return accounts_manage_blocked_reason(user_id) is None
