# papertrail server

The always-on bridge: ingest `pico-paper.v1` webhook events, resolve the current
screen per device, and serve it to the Waveshare Pico over LAN HTTP with
ETag / `If-None-Match` short-circuiting. Contract: [`../SCHEMA.md`](../SCHEMA.md).

## Layout

| file | role |
|------|------|
| `schema.py`  | pydantic v2 models — strict envelope + per-layout content |
| `store.py`   | stdlib `sqlite3` store (tokens / devices / events) |
| `resolve.py` | `current(device)` resolution + canonical-JSON ETag |
| `auth.py`    | sha256 tokens, `hmac.compare_digest`, in-memory rate bucket |
| `app.py`     | FastAPI: `POST /events`, `GET /current`, `PATCH /config`, `GET /status`, `GET /healthz`, `/admin` + `/api/admin/*` |
| `static/`    | admin dashboard frontend (`admin.html`), served at `GET /admin` |

`GET /current` returns an additive `control` block (`poll_interval`) and accepts
optional telemetry query params (`batt`/`rssi`/`fw`/`up`, best-effort, never
`4xx` the poll). `PATCH /config` sets `poll_interval` (clamped `[30,3600]`);
`GET /status` returns stored telemetry. See [`../SCHEMA.md`](../SCHEMA.md) §8.

> **Single worker only.** The rate limiter is in-process (see `auth.py`); run
> `uvicorn --workers 1`. Horizontal scale-out needs a shared store.

## Admin backend (LAN-only)

`GET /admin` serves a dashboard (`static/admin.html`) to add/edit/remove devices,
push & clear test events, mint/revoke tokens, and watch live telemetry. The page
itself carries no data and needs no auth; it prompts for the admin token, keeps
it in `localStorage`, and sends it as `Authorization: Bearer <token>` on every
`/api/admin/*` call.

- Set `PAPERTRAIL_ADMIN_TOKEN` to a long random secret. **Unset ⇒ every
  `/api/admin/*` returns `503`** (disabled, never silently open). A
  missing/wrong token ⇒ `401` (constant-time compare).
- Minted tokens: the server generates the secret, stores **only its sha256**, and
  returns the plaintext **once** — copy it immediately, it is never shown again.
  Listings show a non-secret preview (`pt_dev_…a1b2`), never the full token.
- **Keep it off the internet.** `/admin` and `/api/admin/*` are LAN-only; Caddy
  must expose only `POST /events` (see `Caddyfile.example`). The admin token is a
  second line of defence, not a substitute for network isolation.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

cp secrets.example.py secrets.py     # edit, put REAL tokens
python secrets.py                    # -> writes seed.json (gitignored)

PAPERTRAIL_DB=./papertrail.db PAPERTRAIL_SEED_FILE=./seed.json \
  uvicorn server.app:app --host 0.0.0.0 --port 8000
```

The Pico polls `http://<lan-ip>:8000/api/devices/<id>/current` directly over
LAN HTTP. External webhook sources go through Caddy (`Caddyfile.example`) for TLS.

## Docker

```bash
docker compose up --build   # mounts ./seed.json, persists sqlite in a volume
```

## Test

```bash
pip install -r requirements-dev.txt
pytest -q
```

## OpenAPI

```bash
python dump_openapi.py     # builds the app on a throwaway db, writes ../docs/openapi.json
```
