import os
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(_BACKEND_DIR / ".env")

DATA_DIR = Path(os.environ.get("DATA_DIR", _BACKEND_DIR / "data"))
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
VAULT_INDEX_PATH = DATA_DIR / "vault" / "index.json"
UPLOAD_SESSIONS_DIR = DATA_DIR / "vault" / "uploads"

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-session-secret-change-me")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
API_PUBLIC_URL = os.environ.get("API_PUBLIC_URL", "http://localhost:1234")
# Prefer API_PUBLIC_URL for the OAuth callback (local backend); override via env.
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    f"{API_PUBLIC_URL.rstrip('/')}/api/auth/google/callback",
)


def cors_allow_origins() -> list[str]:
    """Origins allowed for credentialed browser calls.

    Always include both localhost and 127.0.0.1 variants of FRONTEND_URL so
    opening either host against a localhost API does not fail preflight.
    """
    primary = FRONTEND_URL.rstrip("/")
    origins = {primary}
    if "://localhost" in primary:
        origins.add(primary.replace("://localhost", "://127.0.0.1", 1))
    elif "://127.0.0.1" in primary:
        origins.add(primary.replace("://127.0.0.1", "://localhost", 1))
    extra = os.environ.get("CORS_ALLOW_ORIGINS", "")
    for part in extra.split(","):
        part = part.strip().rstrip("/")
        if not part:
            continue
        origins.add(part)
    return sorted(origins)


CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", str(16 * 1024 * 1024)))
CHUNK_SIZE_LARGE = int(os.environ.get("CHUNK_SIZE_LARGE", str(16 * 1024 * 1024)))
CHUNK_SIZE_LARGE_THRESHOLD = int(
    os.environ.get("CHUNK_SIZE_LARGE_THRESHOLD", str(100 * 1024 * 1024))
)


def choose_chunk_size(file_size: int | None) -> int:
    """Pick plaintext shard size from declared upload size."""
    if file_size is not None and file_size >= CHUNK_SIZE_LARGE_THRESHOLD:
        return CHUNK_SIZE_LARGE
    return CHUNK_SIZE


# Max concurrent shard workers within one vault job (upload vs download).
# Default 1 (safe for MEGA-only). Raise independently when mixing providers.
# Upload RAM ≈ (SHARD_UPLOAD_CONCURRENCY + UPLOAD_QUEUE_DEPTH) × chunk size.
SHARD_UPLOAD_CONCURRENCY = int(os.environ.get("SHARD_UPLOAD_CONCURRENCY", "1"))
SHARD_DOWNLOAD_CONCURRENCY = int(os.environ.get("SHARD_DOWNLOAD_CONCURRENCY", "1"))
# Extra hand-off slots between the HTTP reader and encrypt workers (default 0).
# Live plaintext shards ≤ SHARD_UPLOAD_CONCURRENCY + UPLOAD_QUEUE_DEPTH (+ one partial HTTP buffer).
UPLOAD_QUEUE_DEPTH = int(os.environ.get("UPLOAD_QUEUE_DEPTH", "0"))
# Max decrypted/encrypted shard buffers during streaming download.
DOWNLOAD_QUEUE_DEPTH = int(os.environ.get("DOWNLOAD_QUEUE_DEPTH", "4"))
# Treat an account as full at this usage ratio (0.8 or 80 both mean 80%).
_raw_full = os.environ.get("STORAGE_FULL_RATIO", "0.8").strip()
STORAGE_FULL_RATIO = float(_raw_full)
if STORAGE_FULL_RATIO > 1:
    STORAGE_FULL_RATIO /= 100.0
STORAGE_FULL_RATIO = min(1.0, max(0.01, STORAGE_FULL_RATIO))
# Max parallel MEGA clients (logins) reused per account for uploads.
# Keep at 1 even when SHARD_UPLOAD_CONCURRENCY>1: MEGA rate-limits parallel same-account uploads.
MEGA_ACCOUNT_UPLOAD_CONCURRENCY = int(os.environ.get("MEGA_ACCOUNT_UPLOAD_CONCURRENCY", "1"))
# Max parallel MEGA shard downloads per account (independent of upload concurrency).
MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY = int(
    os.environ.get("MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY", "1")
)
# Max parallel Google Drive shard uploads/downloads per account.
# Defaults follow the matching SHARD_*_CONCURRENCY (capped by the job worker pool).
GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY = int(
    os.environ.get("GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY", str(SHARD_UPLOAD_CONCURRENCY))
)
GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY = int(
    os.environ.get("GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY", str(SHARD_DOWNLOAD_CONCURRENCY))
)
# Close idle MEGA sessions after this many seconds.
MEGA_SESSION_TTL_SECONDS = int(os.environ.get("MEGA_SESSION_TTL_SECONDS", "600"))
# Hard timeout for a single provider shard upload/download/delete (seconds).
PROVIDER_OP_TIMEOUT = int(os.environ.get("PROVIDER_OP_TIMEOUT", "120"))
# Remote folder name for opaque shards on Drive / MEGA (created if missing).
SHARD_FOLDER_NAME = os.environ.get("SHARD_FOLDER_NAME", "cloud-storage-vault")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def google_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
