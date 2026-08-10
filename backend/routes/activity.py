from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from config import HISTORY_RETENTION_DAYS
from deps import require_unlocked
from storage import activity as activity_store
from storage import auth_context

router = APIRouter(prefix="/api/activity", tags=["activity"])


@router.get("")
def list_activity(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    before: int | None = Query(default=None, ge=0),
    action: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    require_unlocked(request)
    user_id = auth_context.require_user_id()
    try:
        events = activity_store.list_events(
            user_id,
            limit=limit,
            before=before,
            action=action.strip() if action else None,
            status=status.strip() if status else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "events": events,
        "retention_days": HISTORY_RETENTION_DAYS,
    }
