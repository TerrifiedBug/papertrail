# Papertrail — Roadmap

Shipped: the FastAPI bridge + MicroPython firmware (see [`../README.md`](../README.md)).
In progress: the admin dashboard ([`dashboard.md`](dashboard.md)). This tracks what's next.

## Shipped

- **OTA firmware updates** — the bridge serves a hashed-at-startup manifest + files; devices
  pull only changed files on the next poll (`control.fw`), verify each `sha256`, write
  atomically, keep one known-good `/backup/`, and roll back on a boot crash-loop. Pull-only,
  delta, hash-verified, atomic, recoverable. Full contract: [`ota.md`](ota.md).
  - **Brick-fixes applied** — `boot.py` (the recovery guard) is now **immutable to OTA** (laid
    down only at flash time, never in the manifest); the pull is **protected** (only manifest
    keys are fetched; device-local + guard files are never pruned or overwritten); a boot
    **crash-loop resets and rolls back** to `/backup/`; and the rolled-back **version is
    quarantined** (`pending_version` → `bad_version`) so a bad update is never re-pulled in a loop.
- **Layer A.2 — flasher also uploads firmware.** The `/flash` page can write the full firmware
  `.py` set over USB alongside `secrets.py` + `config.DEVICE_ID`, seeding `manifest.json` so the
  first OTA check is a no-op — a USB provision lands latest code + config in one go. See
  [`flashing.md`](flashing.md#layer-a2--also-upload-firmware).
- **On-screen battery badge + calibrated curve + richer event history.** A bottom-right battery
  badge (charge %, `+` when charging, **red** when low) on all 5 layouts; a piecewise-linear LiPo
  discharge curve (`config.BATTERY["curve"]` — a tunable calibration knob) replacing the
  rough-linear voltage→% map; and the admin events drawer now expands each event to its raw
  payload + a rendered ePaper preview.

## Web-based device provisioning / flashing

Configure + flash a Pico from the browser — no MicroPico, no hand-edited `secrets.py`.

- **Layer A — config push + firmware upload (shipped).** A `/flash` page writes `secrets.py`
  (WiFi, device token, server URL, device id) over USB and — with **Layer A.2** (shipped) —
  uploads the firmware `.py` set over the same channel and seeds `manifest.json` — see
  [`flashing.md`](flashing.md). Still to do: host the page at an HTTPS origin so it works off
  `localhost`. The browser [Web Serial API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Serial_API)
  opens the Pico's USB serial port → drops MicroPython into **raw REPL** → writes files
  (~100 lines of JS, same as mpremote/MicroPico). The dashboard already holds the device
  token + `SERVER_URL`, so one **Provision** click writes `secrets.py` (wifi creds you type
  + token + URL auto-filled) and `config.py` knobs (`DEVICE_ID`, `EPAPER_MODEL`,
  `EPAPER_Y_OFFSET`, interval), optionally uploads the firmware `.py` set, and soft-resets.
  Onboarding a new display becomes: plug in USB → pick the device → Provision.
- **Layer B — MicroPython `.uf2` runtime (later).** RP2040 BOOTSEL via WebUSB/PICOBOOT.
  Less mature than ESP web-flashing; keep the one-time `.uf2` drag manual for now.
- **Constraint — secure context.** Web Serial/USB require HTTPS or `http://localhost`, and
  Chromium (Chrome/Edge/Opera). The LAN-HTTP dashboard is not a secure context, so ship the
  flasher as a **standalone HTTPS static page** (the ESP-Web-Tools pattern) that runs serial
  locally and calls the admin API over CORS. Prior art: ViperIDE, `micropython/webrepl`.

> **OTA firmware updates and Layer A.2 shipped** — moved to [Shipped](#shipped) above.
> The full contract (manifest + files, `control.fw`, delta/atomic/backup/recovery, the
> rollback guarantee) is documented in [`ota.md`](ota.md).

## Event resolution — base / interrupt (shipped)

Events carry a `kind` that decides persistence; `priority` was removed (this supersedes it):

- **`base`** — a persistent screen. Ignores `ttl_seconds`; stays until a newer base on a
  subscribed channel replaces it, or it's deleted. (wifi-QR, ambient status.)
- **`interrupt`** — a temporary overlay with a TTL. `ttl_seconds` omitted/`0` → a default
  `300s`; positive values cap at 7 days. **Always expires — never permanent.** (alerts,
  transient notices.)

`GET /current` resolves in layers: **newest live interrupt → newest base → device fallback**
(idle). TTL is evaluated lazily at read time (no sweeper). `received_at` (epoch of first
ingest) is in the response but **not** in the ETag, so it never churns the `304`; the
dashboard renders relative age from it.

Footgun the UI surfaces: a `base` with no replacement sticks until deleted — the dashboard
offers event-delete to clear a stuck screen.

## Planned (curated 2026-06-28)

Prioritised after the live-debugging session. **Dropped:** firmware **signing** (OTA is
LAN-only + device-token-gated + `sha256`-verified for integrity; authenticity only matters
against a LAN MITM — out of scope for a trusted home network); **wiring more webhook sources**
(added ad-hoc as needed); an **HTTPS flasher origin** (Caddy already fronts the UI with HTTPS,
so the flasher gets its secure context for free).

### Reliability & control

- **`control.force_full_refresh`** — a bridge→device one-shot that forces a full redraw **even
  on a `304`** (clears ghosting / un-sticks a frozen screen — today's incident in one click, not
  a hand-inserted event). Rides the `control` block like `poll_interval`/`fw`, kept OUT of the
  ETag; carry a monotonic token so the device acts once and the bridge clears it on ack.
  Dashboard "Force refresh" button.
- **One-shot device actions** — `reboot` / `clear` (wipe to fallback) / `force_full_refresh`
  via `control`, with an **ack**: the device echoes the action token in its next telemetry and
  the bridge clears it, so the action never repeats every poll. (Sketched in `security.md`.)
- **Schema-version table + honest inserts** — a `meta(key,value)` row holding `schema_version`;
  `init_db` runs **ordered, versioned migrations** instead of column-sniffing (both prod
  footguns — `add kind`, `drop priority` — came from sniffing). And `insert_event` must
  distinguish **dedup from constraint failure**: catch `IntegrityError`, treat ONLY a PK(`id`)
  conflict as a dedup no-op, re-raise/log anything else — `INSERT OR IGNORE` silently masking a
  NOT-NULL error as a "200 duplicate" cost real debugging today.
- **Diagnostics — `GET /api/admin/diag` + a dashboard card** — schema_version, table row counts,
  per-device last_seen + reported `fw` vs the bridge manifest (flag drift / a stuck device),
  resolve sanity. Today's root cause would've been one glance.
- **OTA residual hardening — VALIDATE ON-DEVICE before trusting remote (no-USB) OTA.** The
  core brick-guarantees hold (immutable `boot.py`, protected files never pulled/deleted,
  reset-on-crash, atomic + sha-verified writes, manifest-committed-last, pending-version
  quarantine, rollback from `/backup`). Adversarial re-verify flagged residuals that need a
  real device to settle, not more agent rounds:
  - **Hung (not crashed) bad OTA** isn't caught by the exception-based guard. A naive
    `machine.WDT` won't do — RP2040's watchdog maxes ~8.3s but a tri-color render is ~15s, so
    a cycle-spanning WDT resets mid-render. Needs a designed hang-guard: feed the WDT at safe
    points + bound the display `ReadBusy` with a timeout (so a stuck BUSY can't hang forever).
  - **Interrupted-apply reconcile:** on a power cut mid-rename, the on-disk bytes no longer
    match the local manifest; the next delta plan is wrong. Detect a leftover `pending_fw.txt`
    at `apply()` start and reconcile (restore `/backup` first) before planning.
  - **Stream the OTA download (memory):** `ota.py` buffers each file via `resp.content`
    (whole file in RAM) before hashing/writing — a ~36KB file (`uQR.py`) can MemoryError on
    the Pico's fragmented ~256KB heap (the web flasher already hit this and now chunks at 4KB).
    Fix `ota.apply()` to stream the `urequests` body in chunks: incremental `sha256.update()`
    + chunked `f.write()`, never holding the whole file. (Rare in practice — deltas pull 1–2
    files on a freer fresh-boot heap — but the same bug class.)
  - **Heal a latched crash-counter:** the flasher should zero `boot_count.txt` when laying
    down firmware (a no-backup crash-loop currently latches the counter high). Clear
    `pending_fw.txt` on the recovery paths; only zero the counter when a restore actually wrote.
  - On-device smoke tests to run first: (a) interrupt `apply()` before commit → confirm the NEW
    version is quarantined; (b) crash-loop with empty `/backup` → confirm long-idle, not reset-loop;
    (c) a `cycle()` throw → confirm `boot_count` increments and the guard heals.
- **Per-event render hints** — optional `invert` / `full_refresh` flags on an event that the
  renderer honors (per-screen inversion; force a full panel refresh to clear ghosting).

### Features

- **Image / icon layout** — a new `image` layout: a 1-bit (+ optional red plane) **dithered
  bitmap** (base64 `content.data` + `w`/`h`), drawn via `framebuf.blit`; server size-cap + a JS
  preview decode. Weather icons, logos, glyphs — the biggest step beyond text.
- **Battery graph + runtime estimate** — persist a battery time-series (`battery_samples(device,
  at, pct)`; the bridge already records `last_batt`); dashboard sparkline + a linear-fit
  "≈ N days left".
- **Quiet hours / adaptive cadence** — per-device `quiet_start`/`quiet_end` (skip the refresh +
  long overnight deepsleep) and a poll interval that stretches as the battery drops. Real LiPo
  runtime gains.
- **papertrail MCP server** — wrap the ingest API as an MCP `send_screen` tool (+ `list_devices`
  / `clear`) so any agent (your TARS/Hermes) pushes to the display natively, reading `BASE_URL` +
  an ingest token from env. Builds on [`for-agents.md`](for-agents.md).

### Done / resolved (record)

- **Text-fit guardrails — DONE.** Every field is bounded: single-line fields `clip()` with an
  ellipsis, scale-2 headers are clipped to their box (`status_card`/`alert` titles), and
  `status_card` `lines` + `alert` messages **word-wrap** into their row budget — a long line
  spills onto the next row instead of clipping (firmware `render.py` + the admin preview kept in
  sync). Render-side fit is the guarantee; a per-field `422` in `schema.py` for early sender
  feedback remains an optional nicety, not needed for correctness.
- **Disable the UPS-B power-on LED — investigated: not software-controllable.** The Pico-UPS-B's
  green PWR LED is wired to the boost-converter output through a resistor, not to any Pico GPIO
  (the INA219 is the only device on the I2C bus), so firmware can't switch it off. To kill its
  ~1–2 mA draw, physically remove the LED or its series resistor. The Pico's *onboard* LED is the
  only SW-controllable one and is already dark in deepsleep. No code change possible.
