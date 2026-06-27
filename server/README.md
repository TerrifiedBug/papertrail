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
| `app.py`     | FastAPI: `POST /events`, `GET /current`, `PATCH /config`, `GET /status`, `GET /healthz` |

`GET /current` returns an additive `control` block (`poll_interval`) and accepts
optional telemetry query params (`batt`/`rssi`/`fw`/`up`, best-effort, never
`4xx` the poll). `PATCH /config` sets `poll_interval` (clamped `[30,3600]`);
`GET /status` returns stored telemetry. See [`../SCHEMA.md`](../SCHEMA.md) §8.

> **Single worker only.** The rate limiter is in-process (see `auth.py`); run
> `uvicorn --workers 1`. Horizontal scale-out needs a shared store.

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
