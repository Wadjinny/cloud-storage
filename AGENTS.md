# AGENTS.md

Instructions for AI agents working in this repository.

## Project overview

Encrypted sharded vault web app. Users connect Google Drive and/or MEGA, unlock a passphrase-protected vault, and upload/download files. Files are encrypted per chunk (AES-256-GCM), striped across connected accounts. Metadata lives only on the backend.

## Tech stack

### Frontend (`frontend/`)

| Concern | Tool | Notes |
| -------------- | ---------------------------- | ----- |
| Package manager| **pnpm** | Never use `npm` or `yarn`. |
| Build/dev tool | **Vite** 8 | Dev calls API via `VITE_API_BASE` (no proxy). |
| UI library | **React** 19 | Function components + hooks only. |
| Routing | **React Router** 8 (data mode)| Import from `react-router`. |
| Server state | **TanStack Query** 5 | Accounts + vault queries. |
| Panels | **react-resizable-panels** | `Group` / `Panel` / `Separator`. |
| Styling | **Compact** + Tailwind 4 | Dark only; tokens in `src/index.css`. |
| Language | **TypeScript** (~6) | Strict. |
| Linter | **oxlint** | `frontend/.oxlintrc.json`. |

### Backend (`backend/`)

| Concern | Tool | Notes |
| -------------- | ---------------------------- | ----- |
| Package manager| **uv** | `uv add` / `uv sync` only. |
| Web framework | **FastAPI** | Session cookies for vault unlock. |
| Crypto | **argon2-cffi**, **cryptography** | Argon2id master key; per-chunk AES-256-GCM. |
| Google | **google-api-python-client** | OAuth + Drive `drive.file`. |
| MEGA | **async-mega-py** | Email/password login; temp files for upload/download. |
| Persistence | JSON under `DATA_DIR` | `accounts.json`, `vault/index.json` (SQLite later). |

## Commands

### Frontend

Run from `frontend/`:

- `pnpm install`
- `pnpm dev`
- `pnpm build`
- `pnpm lint`

### Backend

Run from `backend/`:

- `uv sync`
- `uv run main.py` — `localhost:1234`
- `uv add <pkg>` / `uv add --dev <pkg>`
- `uv run pytest -m "not live"` — unit + mocked API tests (temp `DATA_DIR`, memory provider)
- `uv run pytest -m live` — real MEGA/Drive smoke tests (env credentials; see `tests/test_live.py`)

## Architecture

```
frontend Workspace (dev: VITE_API_BASE=http://localhost:1234; prod: same-origin /api via Caddy)
 → /api/accounts, /api/auth/google|mega, /api/vault/*
backend
 routes → services/vault (encrypt, shard, place)
 → providers/gdrive | mega
 → storage/accounts.json + vault/index.json
```

- Trust model A: passphrase unlock derives master key into the server session.
- Stripe placement: round-robin across accounts that still fit under `STORAGE_FULL_RATIO` (default 0.8). Accounts at or above that usage are skipped; unknown quotas stay eligible. Upload fails early when known placeable headroom cannot hold the file.
- Chunk size: `config.choose_chunk_size(size)` (default **16 MiB**, tuned for MEGA). Stored per file in the vault index for download.
- Upload: `POST /upload/start` then `PUT /upload/{job_id}` streams the body; live plaintext shards are capped at `SHARD_UPLOAD_CONCURRENCY` (+ optional `UPLOAD_QUEUE_DEPTH`) via a semaphore, so RAM stays O(concurrency × chunk size) regardless of file size. Cancel via `POST /upload/{job_id}/cancel` (also wired from the transfer bar).
- Job progress: `GET /jobs/{job_id}/events` is an SSE stream (replaces polling). Snapshot still available at `GET /jobs/{job_id}`.
- Download: `POST /download` then `GET /download/{job_id}/stream` yields plaintext chunks (prefetch bounded by `DOWNLOAD_QUEUE_DEPTH`). Worker pool size is `SHARD_DOWNLOAD_CONCURRENCY`. Each shard is an independent AEAD message (`AES-256-GCM-CHUNK`). Frontend uses the File System Access API when available, otherwise a same-origin `<a download>` so the browser streams to disk.
- Shard upload/download within a job runs with bounded concurrency (`SHARD_UPLOAD_CONCURRENCY` / `SHARD_DOWNLOAD_CONCURRENCY`, default **1** each).
- MEGA: pool of reused logins per account (size = max of upload/download caps) on a dedicated loop thread. Idle TTL `MEGA_SESSION_TTL_SECONDS`. Upload cap: `MEGA_ACCOUNT_UPLOAD_CONCURRENCY` (default **1**). Download cap: `MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY` (default **1**).
- Google Drive: per-thread API clients so shards on the same account can run in parallel, capped by `GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY` / `GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY` (defaults follow the matching `SHARD_*_CONCURRENCY`, and never exceed the job worker pool).
- Shards land under a remote folder `cloud-storage-vault` (configurable via `SHARD_FOLDER_NAME`), in a **per-file subfolder** (`{file_id}`). Explicit **cancel** (and vault file delete) removes that subfolder in one call per account; provider/transient upload **errors keep** the folders and a resume session under `DATA_DIR/vault/uploads/{file_id}.json`. `POST /api/vault/upload/resume` continues the same session (skips done shards); the frontend auto-retries once with a byte-offset body. Legacy entries without `remote_folders` still delete shard-by-shard.
- No warning when only one account is connected.

## Env vars

| Var | Purpose |
| --- | ------- |
| `SESSION_SECRET` | Cookie signing |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth |
| `FRONTEND_URL` | Frontend origin; CORS + post-OAuth redirect |
| `API_PUBLIC_URL` | Backend public origin (local: `http://localhost:1234`) |
| `GOOGLE_REDIRECT_URI` | OAuth callback (local: `{API_PUBLIC_URL}/api/auth/google/callback`) |
| `VITE_API_BASE` | Frontend API origin (dev: `http://localhost:1234`; omit in prod for same-origin `/api`) |
| `DATA_DIR` | Accounts + vault index root |
| `SHARD_UPLOAD_CONCURRENCY` | Max parallel shard workers per upload job (default 1) |
| `SHARD_DOWNLOAD_CONCURRENCY` | Max parallel shard workers per download job (default 1) |
| `UPLOAD_QUEUE_DEPTH` | Extra live plaintext slots beyond `SHARD_UPLOAD_CONCURRENCY` (default 0) |
| `DOWNLOAD_QUEUE_DEPTH` | Max ciphertext shards buffered while streaming download (default 4) |
| `STORAGE_FULL_RATIO` | Usage ratio (or percent) at which an account is treated as full for placement (default `0.8` / 80%) |
| `MEGA_ACCOUNT_UPLOAD_CONCURRENCY` | Max parallel MEGA shard uploads per account (default 1) |
| `MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY` | Max parallel MEGA shard downloads per account (default 1) |
| `GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY` | Max parallel Drive shard uploads per account (default = `SHARD_UPLOAD_CONCURRENCY`) |
| `GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY` | Max parallel Drive shard downloads per account (default = `SHARD_DOWNLOAD_CONCURRENCY`) |
| `MEGA_SESSION_TTL_SECONDS` | Idle MEGA session lifetime (default 600) |
| `PROVIDER_OP_TIMEOUT` | Per-shard Drive/MEGA op timeout seconds (default 120) |
| `UVICORN_RELOAD` | Set `0` when doing large local uploads (reload kills in-flight jobs) |
| `SHARD_FOLDER_NAME` | Remote folder for opaque shards |

## Conventions

### Compact UI

- Dark only; no theme toggle.
- Full viewport; panel bodies scroll.
- Accent blue = interaction; teal monospace = live data.
- Uppercase micro-headers.

### Routing

- Single workspace route at `/` in `frontend/src/router.tsx`.
- Import from `react-router`.

### Backend layout

- Routers under `backend/routes/`, registered in `main.py`.
- Providers implement upload/download/delete/quota.
- Do not store vault file metadata on cloud providers.

### General

- TypeScript only in frontend source.
- After edits: `pnpm lint` and `pnpm build` must pass.
