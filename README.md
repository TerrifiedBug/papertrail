# Papertrail

Push short, glanceable messages to a battery-powered e-paper tag on your desk or
wall. A webhook hits a small bridge; the bridge keeps the **current** screen per
device; a Raspberry Pi Pico W with a Waveshare 2.13" e-paper HAT wakes every two
minutes, asks "what should I show?", and goes back to sleep. The display holds the
last image at **zero power**, so the tag can run for weeks on a small LiPo.

- **Wire contract:** [`SCHEMA.md`](SCHEMA.md) — the frozen `pico-paper.v1` envelope.
- **Pixel geometry:** [`docs/layout-specs.md`](docs/layout-specs.md) — exact regions for the 250x122 panel.
- **Operations:** [`docs/deploy.md`](docs/deploy.md) — run the bridge, mint tokens, push events.
- **Security:** [`docs/security.md`](docs/security.md) — threat model + what's implemented vs. out of scope.
- **Dashboard:** [`docs/dashboard.md`](docs/dashboard.md) — the LAN-only admin web UI (devices, tokens, live preview).
- **Flashing:** [`docs/flashing.md`](docs/flashing.md) — provision a Pico over USB from the browser (config + firmware upload).
- **OTA:** [`docs/ota.md`](docs/ota.md) — over-the-air firmware updates: manifest, delta pull, atomic write, rollback.
- **Roadmap:** [`docs/roadmap.md`](docs/roadmap.md) — sticky events, what's next.

---

## What it is

A one-way notification surface. Sources POST **events**; each event names a
`device`, a `channel`, a `kind`, and a `layout` + `content`. `kind=base` is the
normal persistent screen; `kind=interrupt` is a temporary overlay with a TTL.
The bridge stores events, then resolves a single **current screen** per device:
the newest live interrupt wins, otherwise the newest base screen on a subscribed
channel wins. If nothing exists, the device shows its configured **fallback**
(idle) screen.

The Pico is deliberately dumb: it polls `GET /api/devices/:id/current`, sends the
ETag of whatever it's currently showing in `If-None-Match`, and either gets a
`304 Not Modified` (sleep, don't touch the panel) or a `200` with a fresh screen to
render. Five fixed layouts cover most "tell me one thing" use cases:
`status_card | alert | list | metric | qr`.

There are no buttons and nothing is interactive — checkboxes in the `list` layout
are decorative. QR codes are generated **on-device** from a short string; no images
ever cross the wire.

---

## Architecture

```
        INGEST  (POST /api/devices/:id/events, Bearer <ingest-token>)
   +-----------------+        +----------------------+
   | Webhook sources |        |    VPS dashboard     |
   | CI / sensors /  |        |  cron: 1x/day push   |
   | home-assistant  |        |  (daily summary)     |
   +--------+--------+        +-----------+----------+
            |  HTTPS POST                 |  HTTPS POST
            +--------------+--------------+
                           v
                 +-------------------+   TLS termination + reverse proxy
                 |   Caddy  :443     |   auto-HTTPS (Let's Encrypt)
                 +---------+---------+
                           |  proxy -> bridge:8000
                           v
                 +-------------------+   validate -> dedup -> store (first write wins)
                 |  FastAPI bridge   |   resolve current(device): interrupt, then base
                 |  (Docker)         |   ETag = sha256(canonical_json{content,device,layout,control})
                 |  SQLite volume    |   TTL evaluated lazily at read time
                 +---------+---------+
                           ^
                           |  GET /api/devices/:id/current?batt=&rssi=&fw=&up=  (Bearer <device-token>)
                           |  If-None-Match: "<last_etag>"     telemetry: best-effort, never 4xx
                           |    304 -> sleep, leave panel alone
                           |    200 -> render new screen, store new ETag,
                           |          apply control.poll_interval
                 +---------+---------+
                 |  Pico W + ePaper  |   WiFi over LAN, auto deep/light-sleep between polls
                 |  poll 120s        |   (remotely set via PATCH .../config; 600s low-batt)
                 |  ePaper holds     |   image retained at 0 power
                 |  image at 0 power |
                 +-------------------+
```

The bridge + Caddy run on one host (a home server or a small VPS). External
**sources** reach Caddy over HTTPS to ingest events. The **Pico** sits on the same
WiFi/LAN and polls the `current` endpoint. A separate **VPS dashboard** is just
another source: a daily cron that POSTs a summary event (see
[`docs/deploy.md`](docs/deploy.md#daily-dashboard-push)).

---

## Remote control & telemetry

Two small additions on the device's own read path (both **additive** to
`pico-paper.v1`; the `schema` string is unchanged and old firmware ignores them):

- **Remote poll interval.** `GET .../current` now returns a top-level
  `"control": {"poll_interval": N}` block (seconds). Change it with
  `PATCH /api/devices/:id/config` (body `{"poll_interval": N}`, **device** token,
  server-clamped to `[30, 3600]`); the Pico applies it on its next poll — no
  reflash. `poll_interval` is folded into the ETag, so a change busts the `304`
  exactly once.
- **Telemetry piggybacked on the poll.** The Pico tacks optional
  `?batt=&rssi=&fw=&up=` query params onto its `current` poll. They are
  **best-effort** (validated + clamped, malformed values silently ignored) and
  **never** make a poll fail. The bridge stores them with a `last_seen_at`
  timestamp; read them back from `GET /api/devices/:id/status` (device token) for
  a dashboard.

Full field rules and curl examples: [`SCHEMA.md` §4](SCHEMA.md) and
[`docs/deploy.md`](docs/deploy.md#remote-poll-interval--telemetry).

### Interactive API docs

A running bridge serves `/openapi.json`, Swagger UI at `/docs`, and ReDoc at
`/redoc`. The committed spec is [`docs/openapi.json`](docs/openapi.json).

---

## Hardware

| part | role | notes |
|------|------|-------|
| **Raspberry Pi Pico W** (RP2040 + WiFi) | controller | runs MicroPython; reconnects WiFi on each wake |
| **Waveshare Pico-ePaper-2.13-B V4** | display | tri-color Black/White/**Red**, SSD1680, 250x122 landscape, SPI1; HAT stacks on the Pico headers |
| **Waveshare Pico-UPS-B** (INA219) | battery + fuel gauge | I2C1, INA219 at `0x43`; reports bus voltage -> battery % |
| **LiPo cell** | power | rough-linear gauge: `V_MIN 3.0V` = 0%, `V_MAX 4.2V` = 100% |

Pin map (verified against the Waveshare wiki; the two boards use **disjoint** GPIO
sets, so they stack without collision):

| signal | GPIO | bus |
|--------|------|-----|
| ePaper RST | GP12 | SPI1 |
| ePaper DC | GP8 | SPI1 |
| ePaper CS | GP9 | SPI1 |
| ePaper CLK/SCK | GP10 | SPI1 |
| ePaper DIN/MOSI | GP11 | SPI1 |
| ePaper BUSY | GP13 | SPI1 |
| UPS SDA | GP6 | I2C1 |
| UPS SCL | GP7 | I2C1 |

Display set `{8,9,10,11,12,13}` is disjoint from UPS set `{6,7}` — confirmed no
conflict. The shipped panel uses the **vendored official Waveshare Landscape
driver** (`firmware/epaper2in13b.py`, with a `miso=None` fix so SPI1 never claims
GP8/DC); do not hand-roll the panel init. Select it with `EPAPER_MODEL = "2.13-B"`.
The mono 2.13" panel is still supported via `firmware/epaper2in13.py` + the
`EPAPER_REV` knob (`"V4"`/`"V3"`) when `EPAPER_MODEL = "2.13"`. Full hardware +
battery math in
[`docs/layout-specs.md` §6](docs/layout-specs.md#6-hardware-verified-against-waveshare-wiki--zephyr-shield).

### Panel + power

The tri-color panel and battery behavior are driven by a few knobs in
[`firmware/config.py`](firmware/config.py):

- **`EPAPER_MODEL`** — `"2.13-B"` selects the tri-color B V4 driver (red plane
  enabled); `"2.13"` selects the mono driver, where `EPAPER_REV` then picks
  `"V4"`/`"V3"`.
- **`EPAPER_Y_OFFSET`** — shifts **all** rendered content down to clear the panel's
  hidden top rows (128px RAM vs 122px visible). Shipped at `6`; applied to both
  planes in `render.FrameCanvas`. It is a per-panel calibration knob — tune it on
  hardware (see `firmware/test_offset.py`).
- **`POWER_AUTO_SLEEP`** — when `True`, the firmware reads the INA219 shunt-current
  **direction** each cycle and picks the sleep mode: on battery (discharging) ->
  `machine.deepsleep` (max runtime; the board resets each wake, so the last ETag +
  poll interval are persisted to flash); plugged/charging -> `machine.lightsleep`
  (RAM + REPL preserved). Flip `BATTERY["charge_sign"]` if detection reads backwards.
  Each cycle logs e.g. `battery: 4.19V -> 99% | shunt -12.3mV -> BATTERY` (a
  deepsleep tell-tale: the `up=` uptime resets to a small number every poll). When
  `False`, the fixed `USE_DEEPSLEEP` is used.

**Tri-color refresh is full-refresh only** (~5-10s, with a flashing/fade waveform —
this is NORMAL, not a fault). There is no partial/fast refresh for tri-color, and
red-vs-no-red makes no speed difference (the full waveform runs regardless). The
panel only refreshes when content actually **changes** — a `304` poll skips the
panel entirely (zero flash, zero power, image retained).

---

## Quickstart

> Full operations, Caddy config, and per-layout examples are in
> [`docs/deploy.md`](docs/deploy.md). This is the 5-minute path.

### 1. Provision devices + tokens (`seed.json`)

Devices and tokens live in a `seed.json` the bridge reads on first run — it stores
only each token's `sha256`. Mint strong tokens and drop them in (start from
[`server/seed.example.json`](server/seed.example.json)):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # run once per token
```

```json
{
  "devices": [
    { "id": "kitchen-01",
      "channels": ["home.status", "home.alerts", "home.tasks"],
      "fallback": { "layout": "status_card", "content": {
        "title": "Papertrail", "status": "IDLE", "subtitle": "Waiting for updates",
        "lines": ["No active messages"], "footer": "papertrail" } },
      "poll_interval_s": 120, "low_batt_interval_s": 600 }
  ],
  "tokens": [
    { "token": "<device-token>", "kind": "device", "device_id": "kitchen-01", "rate_per_min": 60 },
    { "token": "<ingest-token>", "kind": "ingest", "device_id": "kitchen-01", "channels": null, "rate_per_min": 120 }
  ]
}
```

The **device** token goes into the Pico's `secrets.py`; each **ingest** token goes to
your webhook sources. After first run, manage devices + tokens live from the
[**dashboard**](docs/dashboard.md) instead of editing this file.

### 2. Run the bridge

```bash
mkdir -p data && cp seed.json data/seed.json   # real tokens, gitignored
docker compose up -d                           # pulls ghcr.io/<owner>/papertrail
```

Front it with your own Caddy for the public `/events` ingest (see
[`docs/deploy.md`](docs/deploy.md)); the Pico polls `http://<homelab-ip>:8000/...`
directly on the LAN.

### 3. Send an example webhook

```bash
curl -sS -X POST https://paper.example.com/api/devices/kitchen-01/events \
  -H "Authorization: Bearer $INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  --data @docs/payloads/status_card.json
# -> 200 {"status":"stored","id":"evt_status_0001"}   (re-POST -> {"status":"duplicate"})
```

### 4. See what the device would render

```bash
curl -sS https://paper.example.com/api/devices/kitchen-01/current \
  -H "Authorization: Bearer $DEVICE_TOKEN"
# -> 200 + ETag header + {schema,device,layout,content,control,source_event_id,kind,etag,rendered_at}
```

### 5. Point the Pico at it

1. Flash MicroPython to the Pico W, copy the `firmware/` tree (driver +
   `lib/uQR.py` + app).
2. Create `firmware/secrets.py` from `secrets.example.py` with your WiFi creds,
   `server_url = "https://paper.example.com"`, and the **device token** from step 2.
3. Reset. The poll loop: connect WiFi -> `GET current` with `If-None-Match` ->
   `304` sleep / `200` render -> sleep `poll_interval_s` (deepsleep on battery,
   lightsleep when plugged; see [Panel + power](#panel--power)).

---

## Layout gallery

Five frozen layouts, drawn to the 250x122 panel (black-only except `alert`'s red
banner — see below). Geometry is exact in
[`docs/layout-specs.md`](docs/layout-specs.md); ready-to-POST bodies live in
[`docs/payloads/`](docs/payloads/). ASCII mocks below (boxes ~ the 250x122 frame):

### `status_card` — heading, status badge, body lines, footer

```
+------------------------------------------------+
| Home Server                            OK      |  title S2 + status S1 badge
+------------------------------------------------+  hline(20)
  All services nominal                              subtitle S1
                                                    (gap)
  CPU      12%                                       line 0  \
  RAM      41%                                       line 1   } up to 5 lines, S1
  Disk     63%                                       line 2   } y=40,51,62,73,84
  Uptime   18d 4h                                    line 3  /
+------------------------------------------------+  hline(100)
  updated 14:02                                     footer S1
+------------------------------------------------+
```

### `alert` — severity banner; `high` inverts + frames the whole screen

```
   low / med (normal banner)              high (inverted + 2px frame)
+------------------------------------+  ###################################
| MED                                |  # [#### !! HIGH  (white on red) ##] #
+------------------------------------+  #                                  #
  Door Sensor                            #  Water Leak                      #
  Garage door left open for 12           #  Sensor under the sink detected  #
  minutes.                               #  moisture. Shut off the supply.  #
                                         #                                  #
+------------------------------------+  #  ------------------------------  #
  garage-sensor-1                        #  basement-sensor-3               #
+------------------------------------+  ###################################
```
`severity` maps `low`->"LOW", `med`->"MED", `high`->"!! HIGH". Only `high` inverts
the banner to solid ink + draws the 2px border. On the tri-color B V4 panel that
banner + full-screen frame render on the **red plane** (`canvas.red`), so a
high-severity alert is the one screen that shows red; on a mono panel `canvas.red`
folds onto black (an inverted-black banner, unchanged). All other layouts are
black-only. Message wraps to 4 lines @ 30 chars.

### `list` — title + decorative checklist (non-interactive)

```
+------------------------------------------------+
| Shopping                                       |  title S2
+------------------------------------------------+  hline(20)
  [ ] Milk                                          item 0  \
  [ ] Eggs                                          item 1   } up to 6 items, S1
  [ ] Coffee beans                                  item 2   } "[ ] " is a glyph,
  [ ] Bread                                         item 3   } NOT a button
  [ ] Dish soap                                     item 4  /
+------------------------------------------------+  hline(100)
  5 items                                           footer S1
+------------------------------------------------+
```

### `metric` — one big number, unit, trend

```
+------------------------------------------------+
  Solar output                                      label S1
+------------------------------------------------+  hline(18)

              3.42  kW                              value S4 (huge) + unit S2

            UP +0.4 kW vs 1h                        trend S1, centered (UP/DN/FLAT)
+------------------------------------------------+  hline(100)
  inverter-A                                        footer S1
+------------------------------------------------+
```
`value` is a **string** (e.g. `"3.42"`) so formatting is preserved; value+unit are
centered as one group.

### `qr` — on-device QR + caption

```
+------------------------------------------------+
| Guest WiFi                                     |  title S2
+------------------------------------------------+  hline(20)
  ###############     Scan to join                  90x90 QR at x8..98
  ## ### ### ## #     GuestNet. Valid               (rendered on-device
  ## ### ### ## #     for 12 hours.                  from qr_data via uQR)
  ###############                                   caption S1 wrapped, x>=104
+------------------------------------------------+
```
Only `qr_data` (<= 512 chars) is transmitted; the Pico encodes it locally with the
vendored `uQR` MicroPython library. No image fetch, ever.

---

## Repo layout

```
SCHEMA.md                  wire contract: envelope, resolution, auth, SQLite shapes
docs/layout-specs.md       pixel geometry for all 5 layouts + hardware/battery
docs/deploy.md             run the bridge, Caddy, tokens, channels, curl, daily push
docs/security.md           threat model + implemented controls + out-of-scope + deferred
docs/openapi.json          generated OpenAPI spec (server/dump_openapi.py writes it)
docs/payloads/             canonical example bodies (one per layout) + device-config

firmware/config.py         pin map + knobs (EPAPER_MODEL, EPAPER_Y_OFFSET, POWER_AUTO_SLEEP)
firmware/main.py           boot entry: battery -> WiFi -> poll -> render -> sleep
firmware/epaper2in13b.py   vendored Waveshare tri-color 2.13-B V4 driver (default panel)
firmware/epaper2in13.py    vendored mono 2.13 driver (EPAPER_MODEL="2.13" + EPAPER_REV)
firmware/lib/uQR.py        vendored QR generator for the on-device `qr` layout
firmware/test_panel_b.py   dev tool: on-device B V4 smoke test (black border + red bar)
firmware/test_offset.py    dev tool: EPAPER_Y_OFFSET calibration loop
```
