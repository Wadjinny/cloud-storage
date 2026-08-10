# AGENTS.md

Instructions for AI agents working in this repository.

## Project overview

Encrypted sharded vault web app. Users sign in with username + passphrase, connect Google Drive and/or MEGA, and upload/download files. Files are encrypted per chunk (AES-256-GCM), striped across that user's connected accounts. Metadata and encrypted account credentials live in SQLite.

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
| Web framework | **FastAPI** | Session cookies for login/unlock. |
| Crypto | **argon2-cffi**, **cryptography** | Argon2id master key; per-chunk AES-256-GCM; account credentials sealed with master key. |
| Google | **google-api-python-client** | OAuth + Drive `drive.file`. |
| MEGA | **async-mega-py** | Email/password login; temp files for upload/download. |
| Persistence | **SQLite** at `DATA_DIR/app.db` | Users, accounts, folders, files, shards, upload sessions, thumb metadata. Entity tables carry `created_at` / `updated_at`; `storage/db.py` runs versioned migrations on startup. |
| Thumbnails | **Pillow**, **PyMuPDF**; system **ffmpeg**, **LibreOffice** | Encrypted WebP thumbs on one provider (prefer Drive). |

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
 → /api/auth/login|logout, /api/accounts, /api/auth/google|mega, /api/vault/*
backend
 routes → services/vault (encrypt, shard, place)
 → providers/gdrive | mega
 → storage/db.py (SQLite) + users / accounts / vault_index / upload_sessions
```

- Multi-user: each user has isolated accounts, vault files, shards, and upload sessions.
- Login = unlock: `POST /api/auth/login` with `{username, passphrase}` derives the master key (Argon2id), checks the verifier, and stores `user_id` + `master_key` in the signed session cookie.
- Bootstrap user: on startup, if `INIT_USERNAME` / `INIT_PASSPHRASE` are set and the user is missing, create them. No public registration yet.
- Account credentials are encrypted at rest (`credentials_enc`) with the user's master key; decrypted only after login.
- Trust model A: passphrase unlock puts the master key in the server session.
- Stripe placement: round-robin across **enabled** accounts that still fit under `STORAGE_FULL_RATIO` (default 0.8). The round-robin cursor is **shared per user** across concurrent upload jobs so parallel small (single-shard) files spread across accounts instead of all landing on the first one. Accounts at or above that usage are skipped; unknown quotas stay eligible. Upload fails early when known placeable headroom cannot hold the file. `PATCH /api/accounts/{id}` with `{enabled: false}` disables an account for new uploads only (downloads, health, prune still use it). UI **On/Off** toggle on each account card.

- Shard balance: when total shard count is known, each placeable account is soft-capped at `ceil(total_shards / placeable_count) * (1 + SHARD_BALANCE_SLACK)` (default slack 20%) so fast providers cannot monopolize. Already-uploaded shards count on resume. If every candidate is at the cap, placement relaxes and finishes anyway. Unknown size skips balancing.
- Chunk size: `config.choose_chunk_size(size)` (default **16 MiB**, tuned for MEGA). Stored per file for download.
- Upload: `POST /upload/start` then `PUT /upload/{job_id}` streams the body; live plaintext shards are capped at `SHARD_UPLOAD_CONCURRENCY` (+ optional `UPLOAD_QUEUE_DEPTH`) via a **shared** semaphore across all in-flight upload jobs, so RAM stays O(concurrency × chunk size) even when several files upload at once. The UI starts up to `min(shard_upload_concurrency, 3)` files in parallel (from `GET /api/vault/status`, capped by `MAX_PARALLEL_UPLOAD_FILES`) so small files fill free shard slots without starving PUT bodies behind per-file SSE streams (browser ~6 HTTP/1.1 conns/host). Cancel via `POST /upload/{job_id}/cancel` (also wired from the transfer bar).
- Job progress: `GET /jobs/{job_id}/events` is an SSE stream (replaces polling). Snapshot still available at `GET /jobs/{job_id}`.
- Download: `POST /download` then `GET /download/{job_id}/stream` yields plaintext chunks (prefetch bounded by `DOWNLOAD_QUEUE_DEPTH`). Worker pool size is `SHARD_DOWNLOAD_CONCURRENCY`. Each shard is an independent AEAD message (`AES-256-GCM-CHUNK`). Frontend uses the File System Access API when available, otherwise a same-origin `<a download>` so the browser streams to disk. Multi-select (or any folder) uses `POST /download/zip` then the same stream URL: members are packed with **stream-zip** (deflate level 0, store-like). Zip **writes** stay sequential; up to `ZIP_PREFETCH_FILES` members may download/decrypt ahead in parallel (whole-file buffers capped by `ZIP_PREFETCH_MAX_FILE_BYTES`; larger members stream when reached). Job stats expose `zip_file_index` / `zip_prefetching` for the download queue. Archive paths are relative to the common parent folder; the zip filename is derived from that base (e.g. `Bulletins_de_paie.zip`). Single-file download stays non-zip.
- Shard upload/download within a job runs with bounded concurrency (`SHARD_UPLOAD_CONCURRENCY` / `SHARD_DOWNLOAD_CONCURRENCY`, default **1** each).
- MEGA: pool of reused logins per account (size = max of upload/download caps) on a dedicated loop thread. Idle TTL `MEGA_SESSION_TTL_SECONDS`. Upload cap: `MEGA_ACCOUNT_UPLOAD_CONCURRENCY` (default **1**). Download cap: `MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY` (default **1**).
- Google Drive: per-thread API clients so shards on the same account can run in parallel, capped by `GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY` / `GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY` (defaults follow the matching `SHARD_*_CONCURRENCY`, and never exceed the job worker pool).
- Shards land under a remote folder `cloud-storage-vault` (configurable via `SHARD_FOLDER_NAME`). New uploads nest **opaque vault-dir ids** then a **per-file subfolder**: `cloud-storage-vault/{dir_id}/…/{file_id}/shard_*` (files at `/` stay `…/{file_id}/`). Folder delete uses one remote folder remove per account when the tree has no legacy flat remotes and no files that were moved out while leaving remotes behind; otherwise it falls back to per-file deletes. File move to a new parent relocates per-file remote folders with provider `move_item` (Drive parents / MEGA move) then updates SQLite; same-parent rename and folder moves stay DB-only. Copy (`POST /api/vault/copy`) clones ciphertext (Drive `copy_item`, MEGA download+reupload) into new file ids without re-encrypting; runs as a cancelable job. No migration of older flat `{file_id}` layouts. Explicit **cancel** (and single-file delete) still removes the per-file subfolder in one call per account; provider/transient upload **errors keep** the folders and a resume session in SQLite (`upload_sessions*`). `POST /api/vault/upload/resume` continues the same session (skips done shards); the frontend auto-retries once with a byte-offset body.
- Account prune: `POST /api/accounts/prune/analyze` scans every connected account (orphans + stale resume sessions) without deleting; UI shows a confirm dialog. `POST /api/accounts/prune` then deletes orphans under each vault root and clears failed/abandoned resume sessions (including `active` sessions with no live upload pipeline, e.g. after a crash). Keeps indexed files + in-flight uploads only. Vault file index is not modified. Both are background jobs on the shared SSE stream.
- Shard health: `POST /api/accounts/health` checks that every indexed shard (and ready thumbnail) still exists on its provider via cheap `exists(remote_id)` metadata lookups. Uses a slim SQL checklist, parallel checks (`HEALTH_CHECK_CONCURRENCY`, default 8), and a one-shot MEGA filesystem cache per account. Reports missing / account errors; does not repair. Background job + SSE; Accounts **Health** button. Cancel via `POST /api/vault/jobs/{job_id}/cancel`.
- Folder delete: prefers a **fast** path (one remote folder remove per account for the vault-dir tree). The **per_file** fallback (legacy / moved-out remotes) deletes files in parallel (`DELETE_CONCURRENCY`, default 8) with shared providers. File/folder delete jobs are cancelable via the same jobs cancel endpoint (partial deletes already applied stay applied).
- Account disconnect: `DELETE /api/accounts/{id}` fails with structured **409** `account_in_use` when shards/thumbs remain. `POST /api/accounts/{id}/evacuate` moves those ciphertext objects onto other enabled placeable accounts (auto placement), cleans source remotes, then deletes the account. Cancelable job (`evacuate`); UI offers **Move data & disconnect**. Early check compares estimated cipher bytes to `placeable_total()` (same soft-cap rules as upload). Pipelined download→upload uses `SHARD_DOWNLOAD_CONCURRENCY` / `SHARD_UPLOAD_CONCURRENCY` with live buffers capped by those plus `DOWNLOAD_QUEUE_DEPTH`; progress is byte-based.
- Account management lock (per user): while that user has a running job / upload pipeline / download session, connecting (Google/MEGA) and disconnecting accounts return **409**. `GET /api/vault/status` includes `accounts_locked`. Jobs store `user_id` for this check.
- Thumbnails: during upload (stream from byte 0), plaintext is teed to `DATA_DIR/tmp/{file_id}.spool`, then a WebP preview is generated (images/Pillow, PDF/PyMuPDF, video/ffmpeg, Office/LibreOffice→PDF). **Video** only spools a prefix (`THUMB_VIDEO_SPOOL_BYTES`, default 32 MiB); image/PDF/office spool the full file (office skipped above `THUMB_MAX_SOURCE_BYTES`). Encrypted with a separate AES-GCM key (wrapped by the master key) and stored as a single remote object on a preferred **placeable Google Drive** account (most headroom; ties → first connected; else any placeable). No server-side thumb cache. `GET /api/vault/files/{id}/thumbnail` decrypts and returns `image/webp` with `Cache-Control: private, max-age=THUMB_CACHE_MAX_AGE` (default 1 day). Listing includes `thumb_status` (`none`|`ready`|`failed`|`unsupported`); UI shows the image or a type icon. Thumb failures never fail the upload. System packages: `ffmpeg`, `libreoffice` (optional; missing tools → `failed` for those types).
- Connecting Drive/MEGA and listing accounts require an unlocked session; new accounts attach to the logged-in user.
- Folders: first-class `folders` rows plus path-derived dirs from files. `POST /api/vault/mkdir`, `POST /api/vault/move`, `POST /api/vault/copy` (ciphertext clone job), `POST /api/vault/dirs/delete` (recursive file delete job). Upload goes into the current path; drag-drop moves (Alt+drop copies) entries onto folders or breadcrumbs.
- No warning when only one account is connected.

## Env vars

| Var | Purpose |
| --- | ------- |
| `SESSION_SECRET` | Cookie signing |
| `INIT_USERNAME` / `INIT_PASSPHRASE` | Bootstrap user created on startup if missing |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth |
| `FRONTEND_URL` | Frontend origin; CORS + post-OAuth redirect |
| `API_PUBLIC_URL` | Backend public origin (local: `http://localhost:1234`) |
| `GOOGLE_REDIRECT_URI` | OAuth callback (local: `{API_PUBLIC_URL}/api/auth/google/callback`) |
| `VITE_API_BASE` | Frontend API origin (dev: `http://localhost:1234`; omit in prod for same-origin `/api`) |
| `DATA_DIR` | SQLite root (`app.db` by default) |
| `DB_PATH` | Optional override for the SQLite file path |
| `SHARD_UPLOAD_CONCURRENCY` | Max parallel shard workers per upload job (default 1) |
| `SHARD_DOWNLOAD_CONCURRENCY` | Max parallel shard workers per download job (default 1) |
| `UPLOAD_QUEUE_DEPTH` | Extra live plaintext slots beyond `SHARD_UPLOAD_CONCURRENCY` (default 0) |
| `DOWNLOAD_QUEUE_DEPTH` | Max ciphertext shards buffered while streaming download (default 4) |
| `ZIP_PREFETCH_FILES` | Max zip members downloading at once during archive build (default 3; includes current) |
| `ZIP_PREFETCH_MAX_FILE_BYTES` | Max size for whole-file zip prefetch buffers (default 32 MiB); larger stream live |
| `STORAGE_FULL_RATIO` | Usage ratio (or percent) at which an account is treated as full for placement (default `0.8` / 80%) |
| `SHARD_BALANCE_SLACK` | Extra share above equal split for per-account shard balance (default `0.2` / 20%) |
| `MEGA_ACCOUNT_UPLOAD_CONCURRENCY` | Max parallel MEGA shard uploads per account (default 1) |
| `MEGA_ACCOUNT_DOWNLOAD_CONCURRENCY` | Max parallel MEGA shard downloads per account (default 1) |
| `GDRIVE_ACCOUNT_UPLOAD_CONCURRENCY` | Max parallel Drive shard uploads per account (default = `SHARD_UPLOAD_CONCURRENCY`) |
| `GDRIVE_ACCOUNT_DOWNLOAD_CONCURRENCY` | Max parallel Drive shard downloads per account (default = `SHARD_DOWNLOAD_CONCURRENCY`) |
| `HEALTH_CHECK_CONCURRENCY` | Max parallel `exists` checks during shard health (default 8) |
| `DELETE_CONCURRENCY` | Max parallel per-file deletes in folder delete fallback (default 8) |
| `MEGA_SESSION_TTL_SECONDS` | Idle MEGA session lifetime (default 600) |
| `PROVIDER_OP_TIMEOUT` | Per-shard Drive/MEGA op timeout seconds (default 120) |
| `PROVIDER_OP_RETRIES` | Retries for transient provider/network errors on upload/download (default 3) |
| `UVICORN_RELOAD` | Set `0` when doing large local uploads (reload kills in-flight jobs) |
| `SHARD_FOLDER_NAME` | Remote folder for opaque shards |
| `THUMB_CACHE_MAX_AGE` | Browser `Cache-Control` max-age for thumbnails (default 86400) |
| `THUMB_MAX_EDGE` | Max thumbnail edge in pixels (default 256) |
| `THUMB_MAX_SOURCE_BYTES` | Skip office thumbs above this size (default 100 MiB; video uncapped) |
| `THUMB_VIDEO_SPOOL_BYTES` | Max plaintext prefix teed for video thumbs (default 32 MiB) |
| `THUMB_GEN_TIMEOUT` | ffmpeg / LibreOffice timeout seconds (default 60) |
| `PREVIEW_MAX_MB` | Max file size in MiB for in-browser image/PDF preview (default `5`); exposed on `GET /api/vault/status` as `preview_max_bytes` |
| `VIDEO_PREVIEW_MAX_MB` | Max file size in MiB for in-browser video preview (default `100`); exposed on `GET /api/vault/status` as `video_preview_max_bytes` |
| `HISTORY_RETENTION_DAYS` | How long to keep activity history rows (default `30`) |

## Conventions

### Compact UI

- Dark only; no theme toggle.
- Full viewport; panel bodies scroll.
- Accent blue = interaction; teal monospace = live data.
- Uppercase micro-headers.

### Routing

- Workspace at `/` and activity history at `/history` in `frontend/src/router.tsx`.
- Import from `react-router`.
- Locked state shows a sign-in screen (username + passphrase).

### Backend layout

- Routers under `backend/routes/`, registered in `main.py`.
- Activity history: `storage/activity.py` + `GET /api/activity`; jobs and sync routes call `activity.record`.
- Providers implement upload/download/delete/quota.
- Do not store vault file metadata on cloud providers.
- Auth context (`storage/auth_context.py`) carries `user_id` + `master_key` for the request / background job.

### General

- TypeScript only in frontend source.
- After edits: `pnpm lint` and `pnpm build` must pass.
