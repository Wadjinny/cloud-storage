# cloud-storage

Encrypted, sharded personal vault that spreads opaque file chunks across Google Drive and MEGA. Metadata and the vault index live on the backend; cloud accounts only store ciphertext shards.

**Live:** [cloud-storage.w-ilyas.site](https://cloud-storage.w-ilyas.site)

## Stack

| Layer | Tools |
| ----- | ----- |
| Frontend | pnpm, Vite 8, React 19, React Router 8, TanStack Query 5, react-resizable-panels, Tailwind 4, Compact dark UI |
| Backend | uv, FastAPI, Argon2id + per-chunk AES-256-GCM, Google Drive API, async-mega-py |

## Features (MVP)

- Connect **Google Drive** (OAuth) and **MEGA** (email/password) from the UI
- Unlock a vault with a passphrase (keys stay in the server session)
- Upload encrypts, splits into ~16 MiB shards (MEGA-tuned, serial by default), and stripes across connected accounts
- Download reassembles and decrypts; delete removes shards + index entry
- One account is enough; more accounts increase stripe width

## Run locally

**Backend** (http://localhost:1234)

```bash
cd backend
uv sync
cp .env.example .env   # set SESSION_SECRET; fill Google OAuth to enable Drive
uv run main.py
```

**Frontend** (calls the API via `VITE_API_BASE`, no Vite proxy)

```bash
cd frontend
pnpm install
# optional: cp .env.example .env.development
pnpm dev
```


Open http://localhost:5173 (use `localhost`, not `127.0.0.1`, so session cookies stay same-site with `http://localhost:1234`). Prod builds leave `VITE_API_BASE` empty and hit same-origin `/api` through Caddy.

## Google OAuth setup

1. Create a Google Cloud project and enable the Google Drive API.
2. Configure an OAuth consent screen.
3. Create an OAuth client ID (Web application).
4. Add authorized redirect URIs:
   - Local: `http://localhost:1234/api/auth/google/callback` (`localhost` and `127.0.0.1` are different to Google).
   - Prod: `https://cloud-storage.w-ilyas.site/api/auth/google/callback`
5. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `FRONTEND_URL`, `API_PUBLIC_URL`, and `GOOGLE_REDIRECT_URI` in `backend/.env`.

Without Google env vars, **+ Google** returns 503; MEGA still works.

## MEGA

Use **+ MEGA** in the accounts panel. Credentials are stored in `backend/data/accounts.json` (trusted-server model). Login is verified against MEGA before saving.

## Data on disk

Backend writes under `DATA_DIR` (default `backend/data/`):

- `accounts.json` — provider credentials
- `vault/index.json` — encrypted-file map, KDF salt, passphrase verifier

Both are gitignored. Losing the index orphans cloud shards.

## Deploy

Push to `main` runs `.github/workflows/deploy.yml`. Compose expects Google env vars and a `./data` volume (see `compose.yml`).

Agent conventions: [`AGENTS.md`](./AGENTS.md).
