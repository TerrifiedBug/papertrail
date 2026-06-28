# Papertrail — Deploy & Operations

How to run the bridge, terminate TLS with Caddy, mint device/ingest tokens, wire
up channel subscriptions, send each layout, set a device's poll interval, read
its telemetry, and push a once-a-day dashboard summary. The wire contract is
[`SCHEMA.md`](../SCHEMA.md); pixel geometry is [`layout-specs.md`](layout-specs.md);
the generated API spec is [`openapi.json`](openapi.json).

---

## 1. Run the Docker bridge

The bridge is a single **FastAPI + SQLite** container; all state is one SQLite file
under `/data`. Your own Caddy fronts only the public *ingest* endpoint (§2); the
Pico polls the bridge directly over LAN HTTP.

### Homelab — prebuilt image from GHCR (recommended)

CI builds and publishes `ghcr.io/terrifiedbug/papertrail` on every push to `main`
(see [`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml)).
The repo-root [`docker-compose.yml`](../docker-compose.yml) pulls it:

```yaml
# docker-compose.yml (repo root)
services:
  papertrail:
    image: ghcr.io/terrifiedbug/papertrail:latest
    container_name: papertrail
    restart: unless-stopped
    ports: ["8000:8000"]
    volumes: ["./data:/data"]     # ./data/seed.json bootstraps; the sqlite db lives here
```

```bash
mkdir -p data
cp /path/to/seed.json data/seed.json     # real tokens/devices, gitignored (see §3/§4)
docker login ghcr.io                     # private package -> PAT with read:packages
docker compose up -d
docker compose logs -f papertrail
```

The bridge seeds devices + tokens from `data/seed.json` on first run (when the DB is
empty), storing only the `sha256` of each token.

### Local — build from source

For development, `server/docker-compose.yml` builds from the Dockerfile instead of
pulling the image:

```bash
cd server
cp seed.example.json seed.json    # edit in real tokens (gitignored)
docker compose up -d --build
```

Either way the bridge listens on `:8000`. The 8 KiB body cap, the layout allowlist,
and the per-token rate limit are enforced in the app (see [`security.md`](security.md)).

---

## 2. Caddy (TLS for the public ingest only)

Caddy terminates TLS for **internet webhook sources** hitting the ingest endpoint.
It does **not** front the Pico: the Pico is on your LAN and polls the bridge
directly over plain HTTP — MicroPython TLS on a battery device costs RAM and a
fresh handshake every wake for no real gain on a trusted LAN. So the only public,
TLS-terminated path is `POST .../events`; `GET .../current` stays LAN-only.

```caddyfile
# Caddyfile (your existing instance). Point DNS at the host first.
papertrail.terrifiedbug.com {
    encode gzip

    # Only the ingest endpoint is exposed to the internet.
    @ingest path /api/devices/*/events
    handle @ingest {
        request_body { max_size 8KB }       # edge cap; the app also enforces 8 KiB -> 413
        reverse_proxy <homelab-ip>:8000     # or the container name if Caddy shares the net
    }
    handle /healthz { reverse_proxy <homelab-ip>:8000 }

    # Everything else (including /current) is NOT served publicly.
    handle { respond "not found" 404 }
}
```

The Pico's `SERVER_URL` (in `firmware/secrets.py`) is `http://<homelab-ip>:8000` —
**never** the Caddy hostname. Give the homelab box a DHCP reservation / static IP so
that address never changes. Below, the `$BASE` in ingest curl examples is your
public `https://papertrail.terrifiedbug.com`; a quick `GET .../current` for
debugging is run against `http://<homelab-ip>:8000` on the LAN.

---

## 3. Seed a device (`seed.json`)

Devices and tokens are provisioned by a **`seed.json`** the bridge reads on its
**first run** (when the DB is empty). Start from
[`server/seed.example.json`](../server/seed.example.json). A device entry owns its
**channels**, its **fallback** screen (a complete `{layout, content}` shown when no
live event resolves — see [`payloads/device-config.json`](payloads/device-config.json)),
and its poll intervals:

```json
{
  "devices": [
    {
      "id": "kitchen-01",
      "channels": ["home.status", "home.alerts", "home.tasks", "energy", "guest"],
      "fallback": { "layout": "status_card", "content": {
        "title": "Papertrail", "status": "IDLE", "subtitle": "Waiting for updates",
        "lines": ["No active messages"], "footer": "papertrail" } },
      "poll_interval_s": 120,
      "low_batt_interval_s": 600
    }
  ],
  "tokens": [ /* see §4 */ ]
}
```

> The examples below target `kitchen-01` with one ingest token subscribed to every
> channel used here. In production you would split these across devices and scope
> ingest tokens per channel — see §5.

A device only resolves events on a channel it is **subscribed to**. An event on a
channel the device does not list is stored but never shown (and an ingest token
scoped to a different channel is rejected `403` before storage).

> **First-run only:** the bridge seeds when the DB is empty. To add or rotate a
> device/token later, edit `seed.json`, delete `data/papertrail.db` (only ephemeral
> TTL'd events live there), and restart to re-seed. The generator
> [`server/secrets.example.py`](../server/secrets.example.py) can build `seed.json`
> programmatically if you'd rather not hand-edit JSON.

---

## 4. Device + ingest tokens

Two token classes, both sent as `Authorization: Bearer <token>`:

| kind | used on | scope |
|------|---------|-------|
| **device** | `GET .../current`, `PATCH .../config`, `GET .../status` | one device (read + control) |
| **ingest** | `POST .../events` | one device, optionally channel-scoped (write) |

Tokens are **random secrets you generate**; the bridge stores only the `sha256`
digest and compares with `hmac.compare_digest` (constant-time). Mint them and add
entries to the `tokens` array of `seed.json` (plaintext there, hashed at seed time):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # run once per token
```

```json
"tokens": [
  { "token": "<device-token>", "kind": "device", "device_id": "kitchen-01", "rate_per_min": 60 },
  { "token": "<ingest-token>", "kind": "ingest", "device_id": "kitchen-01", "channels": null, "rate_per_min": 120 }
]
```

The **device** token goes into `firmware/secrets.py` (alongside WiFi creds and
`SERVER_URL`); each **ingest** token goes to whatever source produces events.
**Never commit either** — both `seed.json` and `secrets.py` are gitignored
(see [`security.md`](security.md)).

---

## 5. Channel subscriptions

Channels are how one device filters many sources. A device subscribes to a set of
channel names; an event carries exactly one `channel`. Resolution only considers
events whose `channel` is in `device.channels`.

Ingest tokens can be **channel-scoped** to enforce least privilege — a webhook
that should only ever post alerts gets a token that can only write `home.alerts`:

```json
// a channel-scoped ingest token entry in seed.json -> can only write home.alerts
{ "token": "<alerts-only>", "kind": "ingest", "device_id": "kitchen-01",
  "channels": ["home.alerts"], "rate_per_min": 60 }
```

If that token POSTs an event with `channel` outside its allowed set -> `403`,
before the event is stored.

---

## 6. Example curl for each layout

Set up once:

```bash
BASE=https://paper.example.com
ING="Authorization: Bearer $INGEST_TOKEN"
JSON="Content-Type: application/json"
POST="$BASE/api/devices/kitchen-01/events"
```

Each body matches the field names frozen in [`SCHEMA.md` §3](../SCHEMA.md) and the
geometry in [`layout-specs.md`](layout-specs.md). On success the bridge returns
`200 {"status":"stored","id":...}`; re-POSTing the same `id` returns
`200 {"status":"duplicate","id":...}` (idempotent, first write wins).

### status_card

```bash
curl -sS -X POST "$POST" -H "$ING" -H "$JSON" -d '{
  "schema":"pico-paper.v1","id":"evt_status_0001","device":"kitchen-01",
  "channel":"home.status","kind":"base","layout":"status_card",
  "content":{
    "title":"Home Server","status":"OK","subtitle":"All services nominal",
    "lines":["CPU      12%","RAM      41%","Disk     63%","Uptime   18d 4h"],
    "footer":"updated 14:02"
  }
}'
```

### alert (high severity — banner + full-screen frame on the RED plane; mono folds to inverted-black)

```bash
curl -sS -X POST "$POST" -H "$ING" -H "$JSON" -d '{
  "schema":"pico-paper.v1","id":"evt_alert_0001","device":"kitchen-01",
  "channel":"home.alerts","kind":"interrupt","ttl_seconds":600,"layout":"alert",
  "content":{
    "severity":"high","title":"Water Leak",
    "message":"Sensor under the sink detected moisture. Shut off the supply valve and check immediately.",
    "footer":"basement-sensor-3"
  }
}'
```

### list (decorative checkboxes, non-interactive)

```bash
curl -sS -X POST "$POST" -H "$ING" -H "$JSON" -d '{
  "schema":"pico-paper.v1","id":"evt_list_0001","device":"kitchen-01",
  "channel":"home.tasks","kind":"base","layout":"list",
  "content":{
    "title":"Shopping",
    "items":["Milk","Eggs","Coffee beans","Bread","Dish soap"],
    "footer":"5 items"
  }
}'
```

### metric (value is a string)

```bash
curl -sS -X POST "$POST" -H "$ING" -H "$JSON" -d '{
  "schema":"pico-paper.v1","id":"evt_metric_0001","device":"kitchen-01",
  "channel":"energy","kind":"base","layout":"metric",
  "content":{
    "label":"Solar output","value":"3.42","unit":"kW",
    "trend":"UP +0.4 kW vs 1h","footer":"inverter-A"
  }
}'
```

### qr (encoded on-device; only qr_data on the wire, <= 512 chars)

```bash
curl -sS -X POST "$POST" -H "$ING" -H "$JSON" -d '{
  "schema":"pico-paper.v1","id":"evt_qr_0001","device":"kitchen-01",
  "channel":"guest","kind":"base","layout":"qr",
  "content":{
    "title":"Guest WiFi",
    "qr_data":"WIFI:T:WPA;S:GuestNet;P:welcome123;;",
    "caption":"Scan to join GuestNet. Valid for 12 hours."
  }
}'
```

After posting several, resolution prefers the **newest live (non-expired)
`interrupt`** on a subscribed channel; with none live it shows the **newest `base`**
screen, and with neither the device's **`fallback`** (idle) screen. With the above,
the `alert` is an `interrupt` that overlays the base screens until its 600 s TTL
lapses, after which the newest `base` shows, and finally the device's `fallback`.

### Read the resolved screen (what the Pico sees)

```bash
DEV="Authorization: Bearer $DEVICE_TOKEN"
curl -sSi "$BASE/api/devices/kitchen-01/current" -H "$DEV"
# 200 OK
# ETag: "a1b2..."
# {"schema":"pico-paper.v1","device":"kitchen-01","layout":"alert","content":{...},
#  "control":{"poll_interval":120},
#  "source_event_id":"evt_alert_0001","kind":"interrupt","etag":"a1b2...","rendered_at":...}

# Conditional GET: pass the ETag you last rendered.
curl -sSi "$BASE/api/devices/kitchen-01/current" -H "$DEV" \
  -H 'If-None-Match: "a1b2..."'
# 304 Not Modified  (empty body) when unchanged -> the Pico skips the ePaper refresh.
```

The ETag is `sha256(canonical_json({content, device, layout, control}))` — only
those keys are hashed, so `rendered_at`, `source_event_id`, and `kind` never
churn it. The screen is stable across polls until the content (or `control`)
actually changes; a `poll_interval` change busts the `304` exactly once so the
Pico picks it up. The `"control"` key is **not** the device id — that is the
separate top-level `"device"` string.

---

## 7. Remote poll interval & telemetry

These two endpoints share the device's read path and use the **device** token
(the same one in the Pico's `secrets.py`). Both are additive to `pico-paper.v1`.

### Set the poll interval (`PATCH .../config`)

`poll_interval` is the Pico's deep-sleep interval in **seconds**. The server
**clamps** every write to `[30, 3600]` and persists it (default `120`). The new
value reaches the Pico in the `control` block of its next `current` poll.

```bash
DEV="Authorization: Bearer $DEVICE_TOKEN"

# Slow the device to a 5-minute cadence to save battery.
curl -sS -X PATCH "$BASE/api/devices/kitchen-01/config" \
  -H "$DEV" -H "$JSON" -d '{"poll_interval":300}'
# -> 200 {"id":"kitchen-01","poll_interval":300}

# Out-of-range is clamped, not rejected:
curl -sS -X PATCH "$BASE/api/devices/kitchen-01/config" \
  -H "$DEV" -H "$JSON" -d '{"poll_interval":5}'
# -> 200 {"id":"kitchen-01","poll_interval":30}      # clamped up to the floor

# Non-int / missing field is a 422:
curl -sS -X PATCH "$BASE/api/devices/kitchen-01/config" \
  -H "$DEV" -H "$JSON" -d '{"poll_interval":"fast"}'
# -> 422 (validation error)
```

The very next `current` poll returns `"control":{"poll_interval":300}`, and
because `control` is folded into the ETag, that poll is a `200` (one redraw) and
subsequent unchanged polls return `304` again.

### Telemetry on the poll

The Pico appends optional, **best-effort** query params to its `current` poll.
They are validated + clamped, malformed values are silently ignored, and they
**never** make the poll `4xx` (so a flaky sensor never bricks the screen):

```bash
# What the firmware actually requests on a wake (device token):
curl -sS "$BASE/api/devices/kitchen-01/current?batt=83&rssi=-61&fw=v1.2.0&up=43200" \
  -H "$DEV" >/dev/null
```

| param | meaning | accepted / clamp |
|-------|---------|------------------|
| `batt` | battery % | int, clamped `0..100` |
| `rssi` | WiFi RSSI dBm | int, clamped `-120..0` |
| `fw`   | firmware tag | str `<=16`, charset `[A-Za-z0-9._-]` |
| `up`   | uptime seconds | int `>=0` |

### Read telemetry (`GET .../status`)

The dashboard reads back the stored telemetry + last-seen timestamp:

```bash
curl -sS "$BASE/api/devices/kitchen-01/status" -H "$DEV"
# -> 200
# {"id":"kitchen-01","last_seen_at":1750000000,"last_batt":83,"last_rssi":-61,
#  "last_fw":"v1.2.0","last_uptime":43200,"poll_interval":300}
```

`last_*` fields are `null` until the device has reported them at least once.

---

## 8. OpenAPI / interactive docs

The bridge is a FastAPI app (`title="papertrail bridge"`) with tagged routes
(`ingest` / `device` / `telemetry` / `ops`), per-layout request examples, and
response examples. A running bridge exposes:

| path | what |
|------|------|
| `https://paper.example.com/openapi.json` | the live OpenAPI 3.x spec |
| `https://paper.example.com/docs`         | Swagger UI (try-it-out) |
| `https://paper.example.com/redoc`        | ReDoc reference view |

The spec also declares both `servers`: the Caddy **HTTPS** ingest URL and the LAN
**HTTP** poll URL.

The spec is committed at [`openapi.json`](openapi.json). Regenerate it after
changing routes or models:

```bash
python server/dump_openapi.py    # builds the app on a throwaway in-memory DB,
                                 # writes docs/openapi.json (indent=2), no real seed
```

---

## 9. Daily dashboard push

The "VPS dashboard" is just another ingest source: a cron job that builds one
summary event per day and POSTs it like any other webhook. Use a **date-stamped
`id`** so a retry or an overlapping run dedups instead of double-writing, and send
it as a **`base`** screen (persistent, no TTL needed) so it stays up until the next
day's push replaces it — while any live `interrupt` (e.g. a real-time alert) still
overlays it in the meantime.

```bash
#!/usr/bin/env bash
# /opt/papertrail/daily-push.sh  — run by cron on the VPS dashboard host
set -euo pipefail
BASE=https://paper.example.com
DAY=$(date -u +%F)                       # e.g. 2026-06-27  -> idempotent id

curl -sS -X POST "$BASE/api/devices/kitchen-01/events" \
  -H "Authorization: Bearer $PAPERTRAIL_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"schema\":\"pico-paper.v1\",
    \"id\":\"daily-$DAY\",
    \"device\":\"kitchen-01\",
    \"channel\":\"home.status\",
    \"kind\":\"base\",
    \"layout\":\"status_card\",
    \"content\":{
      \"title\":\"Today\",
      \"status\":\"$DAY\",
      \"subtitle\":\"Daily summary\",
      \"lines\":[\"Backups   OK\",\"Uptime    99.9%\",\"Alerts    0\"],
      \"footer\":\"dashboard\"
    }
  }"
```

```cron
# crontab on the dashboard host: push once a day at 07:00 UTC.
0 7 * * *  PAPERTRAIL_INGEST_TOKEN=xxxx /opt/papertrail/daily-push.sh >> /var/log/papertrail-daily.log 2>&1
```

Because `id="daily-<date>"` is unique per day, a re-run on the same day is a
no-op (`200 {"status":"duplicate"}`); the next day's id is new and supersedes the
old one as the newest `base` on the channel.

---

## 10. Operational notes

- **Backups:** the entire state is the SQLite file on `papertrail-data`. Snapshot
  it (`sqlite3 papertrail.db ".backup ..."`) — tokens are already hashed, so the
  backup contains no plaintext secrets.
- **Rotating a token:** edit `seed.json` (swap the token value), delete the SQLite
  db so the bridge re-seeds on restart (only ephemeral TTL'd events are lost). A
  leaked token is revoked the moment its old digest is no longer seeded.
- **Rate limit:** the per-token bucket is **in-memory** — it resets on restart and
  is not shared across workers. It is a courtesy ceiling, not a hard guarantee;
  back it with Redis/SQLite if you need enforcement (see [`security.md`](security.md)).
- **Clock:** TTLs are evaluated against the server's epoch-seconds clock at read
  time. Keep the host clock sane (NTP); there is no background sweeper.
