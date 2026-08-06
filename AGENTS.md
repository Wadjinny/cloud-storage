# AGENTS.md

Instructions for AI agents working in this repository.

## Project overview

Encrypted sharded vault web app. Users connect Google Drive and/or MEGA, unlock a passphrase-protected vault, and upload/download files. Files are encrypted per chunk (AES-256-GCM), striped across connected accounts. Metadata lives only on the backend.

## Tech stack

### Frontend (`frontend/`)

| Concern | Tool | Notes |
| -------------- | ---------------------------- | ----- |
| Package manager| **pnpm** | Never use `npm` or `yarn`. |
| Build/dev tool | **Vite** 8 | Proxy `/api` → `127.0.0.1:1234`. |
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
- `uv run main.py` — `127.0.0.1:1234`
- `uv add <pkg>` / `uv add --dev <pkg>`

## Architecture

```
frontend Workspace
  → /api/accounts, /api/auth/google|mega, /api/vault/*
backend
  routes → services/vault (encrypt, shard, place)
        → providers/gdrive | mega
        → storage/accounts.json + vault/index.json
```

- Trust model A: passphrase unlock derives master key into the server session.
- Stripe placement: round-robin across accounts that still fit under `STORAGE_FULL_RATIO` (default 0.8). Accounts at or above that usage are skipped; unknown quotas stay eligible. Upload fails early when known placeable headroom cannot hold the file.
- Chunk size: 4 MiB (`config.CHUNK_SIZE`).
- Upload: `POST /upload/start` then `PUT /upload/{job_id}` streams the body through a bounded chunk queue (`UPLOAD_QUEUE_DEPTH`) into per-chunk encrypt/upload. No full-file spool. Cancel via `POST /upload/{job_id}/cancel` (also wired from the transfer bar).
- Job progress: `GET /jobs/{job_id}/events` is an SSE stream (replaces polling). Snapshot still available at `GET /jobs/{job_id}`.
- Download: `POST /download` then `GET /download/{job_id}/stream` yields plaintext chunks (prefetch bounded by `DOWNLOAD_QUEUE_DEPTH`). Each shard is an independent AEAD message (`AES-256-GCM-CHUNK`). Frontend uses the File System Access API when available, otherwise a same-origin `<a download>` so the browser streams to disk.
- Shard upload/download/delete within a job runs with bounded concurrency (`SHARD_CONCURRENCY`, default 6).
- MEGA: pool of reused logins per account (`MEGA_ACCOUNT_CONCURRENCY`, default 3) on a dedicated loop thread; same-account shard ops run in parallel up to that pool size. Idle TTL `MEGA_SESSION_TTL_SECONDS`.
- Google Drive: per-thread API clients so shards on the same account upload/download/delete in parallel (up to `SHARD_CONCURRENCY`).
- Shards land in a remote folder `cloud-storage-vault` (configurable via `SHARD_FOLDER_NAME`), created if missing.
- No warning when only one account is connected.

## Env vars

| Var | Purpose |
| --- | ------- |
| `SESSION_SECRET` | Cookie signing |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth |
| `FRONTEND_URL` | Frontend origin; post-OAuth redirect |
| `GOOGLE_REDIRECT_URI` | OAuth callback (local: `{FRONTEND_URL}/api/auth/google/callback` via Vite proxy) |
| `DATA_DIR` | Accounts + vault index root |
| `SHARD_CONCURRENCY` | Max parallel shard workers per job (default 6) |
| `UPLOAD_QUEUE_DEPTH` | Max plaintext chunks buffered while streaming upload (default 3) |
| `DOWNLOAD_QUEUE_DEPTH` | Max ciphertext shards buffered while streaming download (default 4) |
| `STORAGE_FULL_RATIO` | Usage ratio (or percent) at which an account is treated as full for placement (default `0.8` / 80%) |
| `MEGA_ACCOUNT_CONCURRENCY` | Max parallel MEGA clients per account (default 3) |
| `MEGA_SESSION_TTL_SECONDS` | Idle MEGA session lifetime (default 600) |
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
