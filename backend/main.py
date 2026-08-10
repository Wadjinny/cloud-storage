from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import SESSION_SECRET, cors_allow_origins
from routes.accounts import router as accounts_router
from routes.activity import router as activity_router
from routes.auth import router as auth_router
from routes.auth_google import router as auth_google_router
from routes.auth_mega import router as auth_mega_router
from routes.vault import router as vault_router
from storage.db import init_db
from storage.users import ensure_init_user


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    ensure_init_user()
    yield


app = FastAPI(title="cloud-storage", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(activity_router)
app.include_router(auth_google_router)
app.include_router(auth_mega_router)
app.include_router(vault_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import logging
    import os

    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    # Reloading mid-upload kills the job. Set UVICORN_RELOAD=0 for large transfers.
    reload = os.environ.get("UVICORN_RELOAD", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    # Listen on loopback so both http://localhost:1234 and http://127.0.0.1:1234 work.
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=1234,
        reload=reload,
        reload_excludes=["**/tests/**", "**/data/**", "**/.venv/**", "**/__pycache__/**"],
    )


if __name__ == "__main__":
    main()
