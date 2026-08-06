from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import FRONTEND_URL, SESSION_SECRET
from routes.accounts import router as accounts_router
from routes.auth_google import router as auth_google_router
from routes.auth_mega import router as auth_mega_router
from routes.vault import router as vault_router

app = FastAPI(title="cloud-storage")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL.rstrip("/")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts_router)
app.include_router(auth_google_router)
app.include_router(auth_mega_router)
app.include_router(vault_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=1234, reload=True)


if __name__ == "__main__":
    main()
