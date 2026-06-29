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

**Dashboard.** A card per device: device id and a badge row — **online/offline**
(last-seen within 2.5× its poll interval), **battery %** (danger badge when low),
**RSSI**, **firmware** (a green "current" pill or a danger "update pending" pill when it
differs from the bridge manifest), and **poll cadence** — plus a live **250×122 ePaper
preview** of the screen it's showing right now (all 6 layouts incl. `image`, with the red
`alert` banner), relative last-seen, and a **battery sparkline with a "~N days left"**
runtime estimate (lazily fetched per card). Auto-refreshes every ~10s.

**Manage.** Per device you can fire a **one-shot action** — **Reboot**, **Clear** (wipe
to the fallback screen), or **Force refresh** (a full redraw to clear ghosting). These
queue on the bridge and reach the device on its **next poll** (deliver-once, and they
bust a `304` so they land even on an otherwise-unchanged screen). You can also **send a
test event** (a layout composer for all 6 layouts incl. `image`, with a live preview and
**invert / full-refresh** render-hint checkboxes), **set the poll interval**
(`PATCH /config`, clamped 30–3600s), tune the **battery thresholds** (low-battery % at
which the badge turns red, clamped 2–95, plus the low-battery poll cadence — both pushed
to the device in the control block), edit a device's **quiet-hours** window (start/end
hour, blank = off; inside it the bridge stretches the poll interval), and open an
**events drawer** — recent events with channel/kind/expiry; **click any event to expand
it to its raw payload + a rendered ePaper preview** of that screen, and each is deletable
to clear a stuck screen.

**Diagnostics.** A system view (`GET /api/admin/diag`): the DB **schema version**,
**table row counts** (devices / tokens / events / battery samples), and a per-device
table of **online**, reported **firmware vs the bridge manifest** (flags drift or a stuck
device), any **pending action**, and the device's **quiet-hours** window.

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
| `GET /api/admin/firmware` | latest bundled firmware manifest version |
| `GET /api/admin/diag` | diagnostics snapshot: schema version, table counts, per-device online + firmware drift |
| `GET /api/admin/devices` | all devices + telemetry + `online` + current screen |
| `POST /api/admin/devices` | create (`409` on dup, validates fallback) |
| `PATCH /api/admin/devices/{id}` | update channels / fallback / intervals |
| `DELETE /api/admin/devices/{id}` | delete + cascade |
| `GET /api/admin/devices/{id}/events?limit=` | recent events |
| `POST /api/admin/devices/{id}/events` | push an event (same validation as ingest) |
| `DELETE /api/admin/devices/{id}/events/{event_id}` | remove an event |
| `PATCH /api/admin/devices/{id}/config` | set `poll_interval` and/or quiet hours (`quiet_start_h` / `quiet_end_h`) |
| `POST /api/admin/devices/{id}/action` | queue a one-shot action (`reboot` / `clear` / `force_full_refresh`) — `202` |
| `GET /api/admin/devices/{id}/battery?limit=` | battery time-series + `days_remaining` estimate |
| `GET /api/admin/tokens` | list (preview only) |
| `POST /api/admin/tokens` | mint (returns plaintext once) |
| `DELETE /api/admin/tokens/{id}` | revoke |

All `/api/admin/*` routes are **admin-token-gated and LAN-only** (Caddy never proxies
them — see the callout at the top); the one-shot **action**, **diag**, and **battery**
routes follow the same posture as the rest.

Full shapes are in the regenerated [`openapi.json`](openapi.json) / the live `/docs`.

---

## Permanent screens (a note)

`kind=base` events are **sticky** screens; `kind=interrupt` events are temporary
overlays. `ttl_seconds` applies only to interrupts — **omit it (or send `0`) and the
server uses the default 300s TTL**; a positive value expires the overlay after N seconds
(capped at 7 days). Base events ignore `ttl_seconds` entirely and persist until replaced
or deleted. So a wifi-QR or ambient status pushed as a base simply stays. To make a
screen the device's *idle default* (shown whenever nothing else is active), set it as the
device **fallback** here. See [`roadmap.md`](roadmap.md).
