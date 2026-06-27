# Papertrail — Admin Dashboard

A LAN-only web dashboard for seeing and managing every device, served by the bridge
itself at **`GET /admin`** (a single static page, styled like the pala-note portal).
It talks to a token-gated `/api/admin/*` JSON API.

> **Keep it off the internet.** The dashboard + admin API are for the LAN only. Caddy
> exposes **only** `POST /api/devices/*/events`; it must never proxy `/admin` or
> `/api/admin/*` (the shipped `Caddyfile.example` documents this).

---

## Enabling it

The admin surface is **disabled until you set a token**:

| state | behaviour |
|-------|-----------|
| `PAPERTRAIL_ADMIN_TOKEN` **unset/empty** | every `/api/admin/*` returns `503` (disabled) — never accidentally open |
| `PAPERTRAIL_ADMIN_TOKEN` **set** | `/api/admin/*` requires `Authorization: Bearer <that token>` (`401` otherwise, constant-time compare) |

```bash
# generate a strong admin token
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set it in the environment (see [`deploy.md`](deploy.md)):

```yaml
# docker-compose.yml
environment:
  PAPERTRAIL_ADMIN_TOKEN: ${PAPERTRAIL_ADMIN_TOKEN:-}   # from your .env / secret
```

Then open `http://<homelab-ip>:8000/admin`, paste the token once (kept in
`localStorage`; "sign out" clears it). The `/admin` HTML page itself needs no auth —
it carries no data; every API call from it sends the token.

---

## What it does

**Dashboard.** A card per device: device id, **online/offline** (last-seen within
2.5× its poll interval), **battery %** (danger badge when low), **RSSI**, **firmware**,
a live **250×122 ePaper preview** of the screen it's showing right now (all 5 layouts,
with the red `alert` banner), relative last-seen, and its poll interval. Auto-refreshes
every ~10s.

**Manage.** Per device you can **send a test event** (layout composer with a live
preview), **set the poll interval** (`PATCH /config`, clamped 30–3600s), and open an
**events drawer** — recent events with channel/priority/expiry, each deletable to clear
a stuck screen.

**Devices & tokens.** **Create** a device (id, channels, fallback screen, intervals),
**delete** one (cascades its tokens + events), and **mint / list / revoke** tokens. A
minted token's plaintext is shown **once** (copy it then — only its `sha256` is stored);
the list only ever shows a preview (`pt_dev_…last4`), never the full secret.

> This replaces hand-editing `seed.json` for everything after the first run — devices
> and tokens can be created/revoked live.

---

## Admin API (under the `admin` OpenAPI tag)

| method + path | does |
|---|---|
| `GET /admin` | the dashboard HTML (no auth) |
| `GET /api/admin/devices` | all devices + telemetry + `online` + current screen |
| `POST /api/admin/devices` | create (`409` on dup, validates fallback) |
| `PATCH /api/admin/devices/{id}` | update channels / fallback / intervals |
| `DELETE /api/admin/devices/{id}` | delete + cascade |
| `GET /api/admin/devices/{id}/events?limit=` | recent events |
| `POST /api/admin/devices/{id}/events` | push an event (same validation as ingest) |
| `DELETE /api/admin/devices/{id}/events/{event_id}` | remove an event |
| `PATCH /api/admin/devices/{id}/config` | set poll_interval |
| `GET /api/admin/tokens` | list (preview only) |
| `POST /api/admin/tokens` | mint (returns plaintext once) |
| `DELETE /api/admin/tokens/{id}` | revoke |

Full shapes are in the regenerated [`openapi.json`](openapi.json) / the live `/docs`.

---

## Permanent screens (a note)

`ttl_seconds` is optional on every event — **omit it (or send `0`) and the event never
expires** (sticky until replaced or deleted); a positive value expires after N seconds
(≤ 7 days). So a wifi-QR or ambient status pushed with no TTL simply stays. To make a
screen the device's *idle default* (shown whenever nothing else is active), set it as
the device **fallback** here. See [`roadmap.md`](roadmap.md).
