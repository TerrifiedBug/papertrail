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

- **Hardening — firmware signing (deferred).** OTA today trusts the manifest by `sha256`
  (integrity, not authenticity); the v1 threat model is **LAN + device-token-gated**. A future
  revision **signs the manifest** — HMAC with a flash-baked key, or Ed25519 with the bridge
  holding the private key and the device only the public key — and verifies the signature
  **before** trusting any `sha`, closing the LAN-MITM gap. See
  [`ota.md`](ota.md#hardening--firmware-signing-deferred).
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
- One-shot device actions — `reboot` / `clear` / `force_full_refresh` — with the
  ack-handshake (sketched in [`security.md`](security.md)).
- Per-event render hints (`invert`, `full_refresh`).
- Productionize: deploy the bridge on the homelab (Docker/GHCR + Caddy), wire real webhook
  sources (Home Assistant, CI, cron, the daily dashboard push).
- Battery discharge-curve calibration (the LiPo voltage→% curve is rough-linear today).
- **On-screen battery indicator** — draw a small battery glyph + `%` in the **bottom-right**
  corner of the panel (currently free space on every layout). Best as a global overlay in
  `render.draw_to_epd` after the layout renders, fed the `pct` already read in `main.read_battery`;
  on the tri-color panel, render it **red** when low. Appears on all 5 layouts.
