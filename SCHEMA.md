# Papertrail Wire Schema — `pico-paper.v1`

Canonical contract for the webhook -> server -> ePaper pipeline. **Firmware and
server MUST obey this file and `docs/layout-specs.md` exactly.** The **event
envelope field names** (§1) and the **enum of layouts** (§2) are frozen for `v1`:
mutating either ships as `pico-paper.v2` (never silently mutate `v1`).

The **resolved-screen response, control, and telemetry plane** (§4) may grow
**additively** within `v1` — new top-level *response* keys and new optional
`GET .../current` query params do **not** bump the schema string. The `schema`
value **STAYS `"pico-paper.v1"`** for these additions, and old firmware simply
ignores response keys it doesn't recognise (forward-compatible by construction).

- Display target: Waveshare Pico-ePaper-2.13, **250 x 122**, tri-color
  Black/White/**Red** (the mono 1-bit B/W panel is still supported). The wire
  contract is **panel-agnostic**: the only render difference is that `alert`
  high-severity draws on the **Red** plane (§3.2) — resolution, control,
  telemetry, and poll-interval behavior are identical on both panels.
- Transport: HTTP/JSON, UTF-8.
- Encoding for hashing/canonicalization: see [Canonical JSON](#canonical-json).

---

## 1. Event envelope

A source POSTs an **event**. Every event is a flat envelope plus a per-layout
`content` object. There are **no other top-level keys** in `v1`.

```jsonc
{
  "schema":      "pico-paper.v1",   // string, MUST equal "pico-paper.v1"
  "id":          "evt_2026...",     // string, globally unique; used for dedup
  "device":      "kitchen-01",      // string, target device id
  "channel":     "home.status",     // string, logical channel the device subscribes to
  "kind":        "base",            // "base" persistent screen, or "interrupt" temporary overlay
  "ttl_seconds": 900,                // interrupt TTL seconds; base ignores it; cap 604800 (7d)
  "layout":      "status_card",     // enum: status_card|alert|list|metric|qr
  "content":     { /* per-layout, see §3 */ }
}
```

### Field rules

| field         | type   | required | rule |
|---------------|--------|----------|------|
| `schema`      | string | yes | MUST be `"pico-paper.v1"`; else 422 |
| `id`          | string | yes | 1..128 chars `[A-Za-z0-9._:-]`; duplicate id is a no-op (dedup) |
| `device`      | string | yes | MUST be a known device; else 404 |
| `channel`     | string | yes | 1..64 chars; ingest token may be channel-scoped (else 403) |
| `kind`        | string | no  | `"base"` (default) = persistent screen that **ignores** `ttl_seconds`; `"interrupt"` = temporary overlay that **uses** `ttl_seconds`; any other value 422 |
| `ttl_seconds` | int    | no  | `>=0`, clamped to `<=604800` (7d); ignored for base; interrupt omitted/`0` => 300s default |
| `layout`      | string | yes | MUST be in the allowlist (§3); else 422 |
| `content`     | object | yes | MUST validate against the layout shape; else 422 |

### Server-stamped fields (added at ingest, never accepted from the wire)

The server **ignores** these if a client sends them and stamps its own:

| field         | type | meaning |
|---------------|------|---------|
| `received_at` | int  | epoch **seconds**, server UTC clock, set at successful ingest |
| `raw_size`    | int  | byte length of the raw request body as received |

Stored row = envelope (`schema,id,device,channel,kind,ttl_seconds,layout,content`)
+ `received_at` + `raw_size`.

---

## 2. Layout allowlist (frozen for v1)

```
status_card | alert | list | metric | qr
```

Anything else -> **422 Unprocessable**. No external image URLs, no embedded
code, no HTML. `qr.qr_data` is capped at **512 chars** (server-enforced).

---

## 3. Per-layout `content` shapes + full examples

Exact pixel geometry for each is in [`docs/layout-specs.md`](docs/layout-specs.md).
String length caps below are the **render caps** (server may accept longer and let
the firmware clip per the layout spec, but sources SHOULD pre-trim).

### 3.1 `status_card`

General-purpose card: heading, a status word, subtitle, a few body lines, footer.

```jsonc
content: {
  "title":    string,        // S2 16px header, render cap ~12 chars
  "status":   string,        // S1 8px right-badge, render cap 8 chars (e.g. "OK","DOWN")
  "subtitle": string,        // S1 one line, cap 30 chars
  "lines":    [string, ...], // S1 body, up to 5 shown, each cap 30 chars
  "footer":   string         // S1 bottom line, cap 30 chars
}
```

```json
{
  "schema": "pico-paper.v1",
  "id": "evt_status_0001",
  "device": "kitchen-01",
  "channel": "home.status",
  "kind": "base",
  "ttl_seconds": 900,
  "layout": "status_card",
  "content": {
    "title": "Home Server",
    "status": "OK",
    "subtitle": "All services nominal",
    "lines": [
      "CPU      12%",
      "RAM      41%",
      "Disk     63%",
      "Uptime   18d 4h"
    ],
    "footer": "updated 14:02"
  }
}
```

### 3.2 `alert`

Severity-driven notice. **`high` severity draws a full-bleed banner with white
text plus a 2px frame around the whole screen** (see layout spec). On a
**tri-color panel that banner and frame render on the RED plane** (`canvas.red`);
on a mono panel the red plane folds onto black — a solid-**black** block with
white text, exactly as before. `low`/`med` render a normal white banner with a
1px underline (black-only on either panel).

```jsonc
content: {
  "severity": "low" | "med" | "high",  // required enum; default render = "low"
  "title":    string,   // S2 16px, cap 15 chars
  "message":  string,   // S1, word-wrapped, up to 4 lines @ 30 chars
  "footer":   string    // S1, cap 30 chars
}
```

```json
{
  "schema": "pico-paper.v1",
  "id": "evt_alert_0001",
  "device": "kitchen-01",
  "channel": "home.alerts",
  "kind": "interrupt",
  "ttl_seconds": 600,
  "layout": "alert",
  "content": {
    "severity": "high",
    "title": "Water Leak",
    "message": "Sensor under the sink detected moisture. Shut off the supply valve and check immediately.",
    "footer": "basement-sensor-3"
  }
}
```

### 3.3 `list`

Title + checklist. Checkboxes are **decorative only** (`[ ]` glyphs). There are
**no buttons** and nothing is interactive — the Pico cannot toggle them.

```jsonc
content: {
  "title":  string,          // S2 16px, cap 15 chars
  "items":  [string, ...],   // up to 6 shown; each rendered as "[ ] " + text, text cap 26 chars
  "footer": string           // S1, cap 30 chars
}
```

```json
{
  "schema": "pico-paper.v1",
  "id": "evt_list_0001",
  "device": "kitchen-01",
  "channel": "home.tasks",
  "kind": "base",
  "ttl_seconds": 86400,
  "layout": "list",
  "content": {
    "title": "Shopping",
    "items": [
      "Milk",
      "Eggs",
      "Coffee beans",
      "Bread",
      "Dish soap"
    ],
    "footer": "5 items"
  }
}
```

### 3.4 `metric`

One big number with a label, unit, and trend line.

```jsonc
content: {
  "label":  string,   // S1 top label, cap 30 chars
  "value":  string,   // S4 32px big number, cap 7 chars (string, not number, to preserve formatting)
  "unit":   string,   // S2 16px unit beside value, cap 4 chars
  "trend":  string,   // S1 centered trend line, cap 30 chars (ASCII tokens: "UP","DN","FLAT")
  "footer": string    // S1, cap 30 chars
}
```

```json
{
  "schema": "pico-paper.v1",
  "id": "evt_metric_0001",
  "device": "office-01",
  "channel": "energy",
  "kind": "base",
  "ttl_seconds": 300,
  "layout": "metric",
  "content": {
    "label": "Solar output",
    "value": "3.42",
    "unit": "kW",
    "trend": "UP +0.4 kW vs 1h",
    "footer": "inverter-A"
  }
}
```

### 3.5 `qr`

Title + a QR code the Pico renders **locally** from `qr_data` (vendored
MicroPython QR generator `uQR`), plus a caption beside it.

```jsonc
content: {
  "title":   string,   // S2 16px, cap 15 chars
  "qr_data": string,   // 1..512 chars, server-enforced cap; encoded on-device
  "caption": string    // S1 wrapped beside the QR, up to 7 lines @ 17 chars
}
```

```json
{
  "schema": "pico-paper.v1",
  "id": "evt_qr_0001",
  "device": "hallway-01",
  "channel": "guest",
  "kind": "base",
  "ttl_seconds": 43200,
  "layout": "qr",
  "content": {
    "title": "Guest WiFi",
    "qr_data": "WIFI:T:WPA;S:GuestNet;P:welcome123;;",
    "caption": "Scan to join GuestNet. Valid for 12 hours."
  }
}
```

---

## 4. Resolution & storage (server)

### Dedup

On `POST .../events`: if `content`/envelope passes validation and `id` already
exists, the server **ignores** the new event (idempotent no-op) and returns
`200` with `{"status":"duplicate","id":...}`. First write wins; never overwrite.

### `current(device)` — the resolved screen

TTL is evaluated **lazily at read time** (no background sweeper required). With
`now = server epoch seconds`:

```
live_interrupts = [ e for e in events
                    if e.device == device
                    and e.channel in device.channels
                    and e.kind == "interrupt"
                    and now < (e.received_at + e.ttl_seconds) ]

base_screens = [ e for e in events
                 if e.device == device
                 and e.channel in device.channels
                 and e.kind == "base" ]

if live_interrupts:
    chosen = newest(live_interrupts)     # temporary overlay
elif base_screens:
    chosen = newest(base_screens)        # persistent until replaced/deleted
else:
    screen = device.fallback             # ambient/idle screen, per-device configurable
```

The fallback is a complete `{layout, content}` using any of the 5 layouts
(default: a `status_card` idle screen). See `docs/payloads/device-config.json`.

### Resolved-screen response (`GET /api/devices/:id/current`)

```jsonc
200 OK
ETag: "<sha256-hex>"
{
  "schema":          "pico-paper.v1",
  "device":          "kitchen-01",            // the device-id STRING (not the control block)
  "layout":          "status_card",          // or the fallback's layout
  "content":         { ... },                 // the chosen/fallback content
  "control":         { "poll_interval": 120 }, // server->Pico control plane; see "Remote poll interval" below
  "source_event_id": "evt_status_0001",       // null when fallback
  "kind":            "base",                    // null when fallback
  "etag":            "<sha256-hex>",
  "rendered_at":     1750000000                // epoch s; informational, NOT hashed
}
```

The top-level `"control"` block carries server->Pico settings (currently just
`poll_interval`, in **seconds**). It is **not** named `"device"` — that key is
already the device-id string. The block is **additive**: old firmware that
predates it ignores it and keeps using its compiled-in poll interval.

### ETag / `If-None-Match`

The ETag is the **content hash of the render-relevant payload only**, so it is
stable across requests when the screen is unchanged:

```
hash_input = { "content": <content>, "device": <id>, "layout": <layout>, "control": <control> }
etag       = sha256( canonical_json(hash_input) ).hexdigest()
```

`rendered_at`, `source_event_id`, and `kind` are **excluded** from the hash
(they would otherwise churn the ETag every request). `control` **is** hashed: a
`poll_interval` change busts the `304` so the Pico picks up the new interval on
its next poll.

> A rare `control` change forces **one** ePaper redraw even when the on-screen
> pixels are identical — accepted as cheap and infrequent. (Split the ETag into
> a render-hash and a control-hash only if control changes ever get noisy.)

`GET .../current` honors `If-None-Match: "<etag>"`:
- match -> **`304 Not Modified`**, empty body (saves Pico bytes + a full ePaper refresh).
- no match / no header -> `200` with body + fresh `ETag`.

### Canonical JSON

```
canonical_json(obj) = json.dumps(obj, sort_keys=True,
                                 separators=(",", ":"),
                                 ensure_ascii=False).encode("utf-8")
```

Deterministic: sorted keys, no whitespace, UTF-8. Same bytes on server and any
verifier.

### Remote poll interval — `control.poll_interval` + `PATCH /api/devices/:id/config`

`poll_interval` is a per-device deep-sleep interval, in **seconds**, persisted on
the device row (stored column `poll_interval_s`, **default `120`**). The server
**clamps** every write to **`[30, 3600]`**. It is surfaced to the Pico in the
`control` block of the resolved-screen response (above) so the firmware can apply
it on the next poll without a reflash.

Set it with the device's own **device** token (the same kind the Pico polls with,
scoped to that one device):

```jsonc
PATCH /api/devices/:id/config
Authorization: Bearer <device-token>
Content-Type: application/json
{ "poll_interval": 300 }            // int seconds; server clamps to [30, 3600]

200 OK
{ "id": "kitchen-01", "poll_interval": 300 }   // echoes the clamped value
```

| input | result |
|-------|--------|
| `poll_interval` < 30 or > 3600 | accepted, **clamped** into `[30, 3600]` |
| `poll_interval` non-int, or body missing it | **`422`** (validation error) |

### Telemetry — Pico->bridge, piggybacked on the poll (best-effort)

`GET /api/devices/:id/current` accepts **optional** query params the Pico tacks
onto its poll. They are **best-effort and MUST NOT `4xx` the poll** — malformed
values are silently ignored, never rejected. They **do not** affect resolution or
the ETag:

| param | type | handling |
|-------|------|----------|
| `batt` | int  | battery %, **clamped `0..100`** |
| `rssi` | int  | WiFi RSSI dBm, **clamped `-120..0`** |
| `fw`   | str  | firmware tag, **`<=16` chars, charset `[A-Za-z0-9._-]`**; else ignored |
| `up`   | int  | uptime seconds, **`>=0`** |

Valid/clamped values are persisted on the device row alongside a server-stamped
`last_seen_at`:

```
GET /api/devices/kitchen-01/current?batt=83&rssi=-61&fw=v1.2.0&up=43200
```

### Device telemetry — `GET /api/devices/:id/status`

Returns the stored telemetry + last-seen timestamp for the dashboard. Authed with
the **device** token:

```jsonc
GET /api/devices/:id/status
Authorization: Bearer <device-token>

200 OK
{
  "id":            "kitchen-01",
  "last_seen_at":  1750000000,   // epoch s, server clock at the last poll
  "last_batt":     83,           // null until first reported
  "last_rssi":     -61,          // null until first reported
  "last_fw":       "v1.2.0",     // null until first reported
  "last_uptime":   43200,        // null until first reported
  "poll_interval": 300           // current effective interval (seconds)
}
```

---

## 5. Auth, limits, and rejects (server)

Two token classes. Each token is stored only as its **`sha256` hex digest** in
SQLite and compared with `hmac.compare_digest` (constant-time). Plaintext tokens
are never persisted. Never commit real tokens or WiFi creds — see
`.env.example` / `secrets.example.py`.

| token kind | used on | scope |
|------------|---------|-------|
| **device** | `GET /api/devices/:id/current`, `GET /api/devices/:id/status`, `PATCH /api/devices/:id/config` | exactly one device |
| **ingest** | `POST /api/devices/:id/events` | a device, optionally channel-scoped |

Both sent as `Authorization: Bearer <token>`.

### Reject matrix

| condition | status |
|-----------|--------|
| missing / malformed / unknown bearer token | `401` |
| valid token, wrong device or disallowed channel scope | `403` |
| unknown `:id` device | `404` |
| raw body > **8 KiB (8192 bytes)** | `413` |
| `layout` not in allowlist | `422` |
| `schema` mismatch or content fails layout validation | `422` |
| `qr_data` length > 512 | `422` |
| `PATCH .../config` `poll_interval` non-int or missing | `422` |
| rate limit exceeded | `429` |

Note: telemetry query params on `GET .../current` are **never** a reject reason —
malformed `batt`/`rssi`/`fw`/`up` are silently ignored so a poll never `4xx`s on
telemetry (§4).

### Rate limit

Per-token in-memory token bucket (default `rate_per_min` per token). See the
ponytail note in the server code: an in-memory bucket **resets on process
restart and is not shared across workers**, so its ceiling is best-effort, not a
security boundary — back it with Redis/SQLite if you need hard guarantees.

---

## 6. Token & storage tables (SQLite, reference shape)

```sql
CREATE TABLE tokens (
  id           INTEGER PRIMARY KEY,
  token_sha256 TEXT NOT NULL UNIQUE,   -- hex sha256 of the bearer token
  kind         TEXT NOT NULL,          -- 'device' | 'ingest'
  device_id    TEXT NOT NULL,          -- scope: the one device this token may touch
  channels     TEXT,                   -- JSON array; NULL = all channels (ingest only)
  rate_per_min INTEGER NOT NULL DEFAULT 60,
  created_at   INTEGER NOT NULL
);

CREATE TABLE devices (
  id                  TEXT PRIMARY KEY,
  channels            TEXT NOT NULL,    -- JSON array of subscribed channels
  fallback            TEXT NOT NULL,    -- JSON {layout, content} idle screen
  poll_interval_s     INTEGER NOT NULL DEFAULT 120,  -- wire field "poll_interval"; PATCH-clamped to [30,3600]
  low_batt_interval_s INTEGER NOT NULL DEFAULT 600,
  -- telemetry, last-write-wins from the Pico's poll query params (all nullable)
  last_seen_at        INTEGER,          -- epoch s, server clock at the last poll
  last_batt           INTEGER,          -- battery %, 0..100
  last_rssi           INTEGER,          -- WiFi RSSI dBm, -120..0
  last_fw             TEXT,             -- firmware tag, <=16 chars [A-Za-z0-9._-]
  last_uptime         INTEGER           -- uptime seconds, >=0
);

CREATE TABLE events (
  id          TEXT PRIMARY KEY,         -- event id (dedup key)
  device      TEXT NOT NULL,
  channel     TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'base',
  ttl_seconds INTEGER NOT NULL,
  layout      TEXT NOT NULL,
  content     TEXT NOT NULL,            -- JSON string
  received_at INTEGER NOT NULL,         -- epoch seconds
  raw_size    INTEGER NOT NULL
);
CREATE INDEX idx_events_device ON events(device, channel, received_at);
```

---

## 7. Device config / firmware knobs

See `docs/payloads/device-config.json` for the canonical example. Server owns
`channels`, `fallback`, `poll_interval_s`, `low_batt_interval_s`. The Pico owns
local-only knobs (server URL + token from secrets, `epaper_rev`, battery
calibration). Hardware pin map and battery math are in `docs/layout-specs.md` §6.

`poll_interval_s` (default `120`) is now also settable **remotely** via
`PATCH /api/devices/:id/config` with body `{"poll_interval": N}` — note the wire
field is `poll_interval` (seconds), persisted as the `poll_interval_s` column and
clamped to `[30, 3600]` (§4). `low_batt_interval_s` (default `600`) remains a
seed-time knob; it is not yet exposed over the control plane.

Future growth of the server->Pico control plane (one-shot actions, render hints)
is sketched as deferred work in [`docs/security.md`](docs/security.md#deferred--future).

---

## 8. OpenAPI / interactive docs

The bridge is a FastAPI app titled **"papertrail bridge"** with tagged routes
(`ingest` / `device` / `telemetry` / `ops`), per-layout request examples, and
response examples. The generated spec is committed at
[`docs/openapi.json`](docs/openapi.json) (rebuild it with `server/dump_openapi.py`,
which builds the app against a throwaway in-memory DB and writes the file).

A running bridge also serves the spec and interactive explorers:

| path | what |
|------|------|
| `/openapi.json` | the live OpenAPI 3.x spec |
| `/docs`         | Swagger UI (try-it-out) |
| `/redoc`        | ReDoc reference view |

The spec lists both `servers`: the Caddy **HTTPS** ingest URL and the LAN **HTTP**
poll URL.
