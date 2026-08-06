import os
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(_BACKEND_DIR / ".env")

DATA_DIR = Path(os.environ.get("DATA_DIR", _BACKEND_DIR / "data"))
ACCOUNTS_PATH = DATA_DIR / "accounts.json"
VAULT_INDEX_PATH = DATA_DIR / "vault" / "index.json"

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-session-secret-change-me")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:5173")
# Local OAuth must callback through the Vite (or Caddy) origin so the session cookie matches.
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    f"{FRONTEND_URL.rstrip('/')}/api/auth/google/callback",
)

CHUNK_SIZE = 4 * 1024 * 1024
# Max concurrent shard upload/download/delete workers within one vault job.
SHARD_CONCURRENCY = int(os.environ.get("SHARD_CONCURRENCY", "6"))
# Max plaintext chunks buffered between HTTP reader and encrypt/upload workers.
UPLOAD_QUEUE_DEPTH = int(os.environ.get("UPLOAD_QUEUE_DEPTH", "3"))
# Max decrypted/encrypted shard buffers during streaming download.
DOWNLOAD_QUEUE_DEPTH = int(os.environ.get("DOWNLOAD_QUEUE_DEPTH", "4"))
# Treat an account as full at this usage ratio (0.8 or 80 both mean 80%).
_raw_full = os.environ.get("STORAGE_FULL_RATIO", "0.8").strip()
STORAGE_FULL_RATIO = float(_raw_full)
if STORAGE_FULL_RATIO > 1:
    STORAGE_FULL_RATIO /= 100.0
STORAGE_FULL_RATIO = min(1.0, max(0.01, STORAGE_FULL_RATIO))
# Max parallel MEGA clients (logins) reused per account.
MEGA_ACCOUNT_CONCURRENCY = int(os.environ.get("MEGA_ACCOUNT_CONCURRENCY", "3"))
# Close idle MEGA sessions after this many seconds.
MEGA_SESSION_TTL_SECONDS = int(os.environ.get("MEGA_SESSION_TTL_SECONDS", "600"))
# Remote folder name for opaque shards on Drive / MEGA (created if missing).
SHARD_FOLDER_NAME = os.environ.get("SHARD_FOLDER_NAME", "cloud-storage-vault")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def google_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
