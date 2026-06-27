# Papertrail — Roadmap

Shipped: the FastAPI bridge + MicroPython firmware (see [`../README.md`](../README.md)).
In progress: the admin dashboard ([`dashboard.md`](dashboard.md)). This tracks what's next.

## Web-based device provisioning / flashing

Configure + flash a Pico from the browser — no MicroPico, no hand-edited `secrets.py`.

- **Layer A — config push (PROTOTYPE shipped).** A `/flash` page writes `secrets.py`
  (WiFi, device token, server URL, device id) over USB — see [`flashing.md`](flashing.md).
  Still to do: upload the firmware `.py` set over the same channel; host it at an HTTPS
  origin so it works off `localhost`. The browser [Web Serial API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Serial_API)
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

## OTA firmware updates (next big build)

Keep deployed Picos current without USB. The web flasher only writes config, so a
device runs whatever firmware was last uploaded — it drifts from the repo. OTA closes
that gap from the bridge.

- **Bridge serves firmware:** `GET /api/firmware/manifest` → `{version, files:[{path, sha256}]}`;
  `GET /api/firmware/files/<path>` → content (device-token auth, LAN). Firmware bundled
  into the image (CI builds from the repo). [Packaging: `COPY firmware/` into the image +
  a small serve route.]
- **Device:** on boot / every N polls, fetch the manifest, diff `sha256` vs a local
  `version.txt`. For each changed file: download → write `<path>.new` → verify hash →
  atomic rename → bump version → reset. The poll's `control` block can carry `fw_latest`
  so the device knows an update is waiting (it already reports `fw` via telemetry).
- **Safety (OTA can brick a device):** atomic writes + per-file hash; a **recovery boot
  guard** (crash-loop → roll back to the known-good copy — the old project's `recovery.py`
  pattern); staged rollout (one device first); never half-write the updater itself.
- **Dashboard:** already receives each device's `fw` → show version spread + a "push
  update" action.
- Prior art: the user's earlier `pico/ota_updater.py` + `recovery.py` (GitHub-sourced OTA).

Companion: **Layer A.2** — have the web flasher also upload the firmware `.py` set over
serial, so a USB-in-hand provision lands the latest code + config in one go.

## Sticky-by-default events (DECIDED — build next)

Change the expiry model so a display keeps the **last webhook until it's replaced**, rather
than reverting to the idle fallback after a fixed TTL:

- **`ttl_seconds` becomes optional. Omitted → no expiry (sticky)** — permanent until a
  newer/higher-priority event supersedes it or it's deleted. Provided → auto-clears after N
  seconds (≤ `604800` = 7 days) as today. Backward-compatible (omitting was a `422` before;
  schema stays `pico-paper.v1`). Net: no-TTL = wifi-QR/ambient; TTL = alerts/transient;
  fallback = true empty state only.
- **Add `received_at` to the `GET /current` response** (epoch of first ingest — "when it
  showed up"). Additive, NOT in the ETag (won't churn the 304). Dashboard renders relative
  age; a footer/renderer can show absolute "Updated 14:32".
- **Footgun to surface in the UI:** resolution is priority-first, so a no-TTL *high-priority*
  event sticks until superseded `≥` its priority or deleted. The dashboard warns on
  "high-priority + no TTL" and offers event-delete to clear a stuck screen.

Implementation is small: `ttl_seconds` optional in `schema.py`; `resolve.py` skips the
expiry check when ttl is null; add `received_at` to the response; one test. Lands in the
same commit as the admin GUI.

## Deferred (designed, not yet built)

- One-shot device actions — `reboot` / `clear` / `force_full_refresh` — with the
  ack-handshake (sketched in [`security.md`](security.md)).
- Per-event render hints (`invert`, `full_refresh`).
- Productionize: deploy the bridge on the homelab (Docker/GHCR + Caddy), wire real webhook
  sources (Home Assistant, CI, cron, the daily dashboard push).
- Battery discharge-curve calibration (the LiPo voltage→% curve is rough-linear today).
