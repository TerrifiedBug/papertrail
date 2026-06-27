# Papertrail — Web Flashing (experimental)

Provision a Pico from the browser: write its `secrets.py` (WiFi, device token, server
URL, device id) over USB with **no MicroPico, no hand-editing**. Served by the bridge
at **`GET /flash`**.

> **Layer A** (see [`roadmap.md`](roadmap.md)). It writes/updates `secrets.py` on a Pico
> that already runs MicroPython, and — with the **Layer A.2** option below — uploads the
> full firmware `.py` set over the same USB channel. It does **not** flash the MicroPython
> `.uf2` runtime itself; dropping the `.uf2` via BOOTSEL stays the one-time manual step.

## Requirements

- **Chromium browser** (Chrome / Edge / Opera) — the [Web Serial API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Serial_API)
  is Chromium-only.
- A **secure context**: open the page over **`http://localhost:8000/flash`** or HTTPS.
  A bare LAN IP (`http://192.168.1.x:8000`) is *not* a secure context and the browser
  blocks Web Serial. For provisioning, run/SSH-forward the bridge so the browser hits
  `localhost`, or front `/flash` with HTTPS.
- The Pico plugged into **this computer's** USB, already running MicroPython + firmware.

## Use it — one screen, one button

1. Open `http://localhost:8000/flash` (Chromium, secure context).
2. Paste your `PAPERTRAIL_ADMIN_TOKEN` once — the page uses it to create the device +
   mint a token for you (stored in this browser only).
3. **Connect Pico** → pick the serial port (disconnect MicroPico/Thonny first — one app
   owns the port).
4. **Name the device** (a new name is created, an existing one is reused), enter WiFi
   SSID/password, and the **Server URL** = the bridge's **LAN IP** (e.g.
   `http://192.168.1.50:8000` — *not* localhost; the Pico isn't on localhost).
5. *(Optional)* tick **Also upload firmware** to land the latest code alongside config —
   see [Layer A.2](#layer-a2--also-upload-firmware) below.
6. **Provision Pico** — one button does it all: creates the device if new, **mints a
   fresh device token automatically** (never shown/pasted), writes `secrets.py` over the
   raw REPL (base64, no escaping), and soft-resets. The Pico reconnects and polls.

The serial log shows the exchange. Nothing leaves the browser — it's a direct USB
write; the admin "mint" call (and, with Layer A.2, the firmware GETs) are the only
network requests (same-origin, to your bridge).

## Layer A.2 — also upload firmware

Tick **Also upload firmware** and the page lands the *latest* firmware code together with
config in a single USB provision — so a fresh Pico (MicroPython + nothing else) comes up
fully provisioned, on the current code, with no second step.

With the freshly-minted device token, the page:

1. `GET /api/firmware/manifest` — the bridge's current firmware version + per-file `sha256`
   (auth = the device token it just minted).
2. For each path in the manifest, `GET /api/firmware/file?path=<path>` and write it to the
   Pico over the raw REPL — **base64 per file** (`ubinascii.a2b_base64`, no escaping), and
   `os.mkdir('lib')` first so `lib/uQR.py` lands.
3. **Then** writes `secrets.py` and patches `config.DEVICE_ID` (so the chosen name works on
   any firmware version), and resets.
4. **Seeds `manifest.json`** on the Pico to match what it just wrote — so the device's
   **first OTA check is a no-op** (its local version already equals `control.fw`) instead of
   a full re-pull on first boot.

This is the USB counterpart to OTA: provision lays down the code once over serial; from
then on the device keeps itself current over the network. See [`ota.md`](ota.md) for the
pull/delta/verify/rollback contract. `config.py` and `secrets.py` are device-local and are
**never** part of the OTA file set, so an OTA can never clobber the identity this step
writes.

## How it works

Web Serial opens the port at 115200, sends `Ctrl-C` to interrupt, `Ctrl-A` to enter the
raw REPL, then executes
`open('secrets.py','wb').write(ubinascii.a2b_base64(...))`, and `Ctrl-D` to soft-reset
— the same protocol mpremote/MicroPico use. The firmware reads `DEVICE_ID` and
`SERVER_URL` from `secrets.py` when present (overriding `config.py`). With **Layer A.2**
enabled the same raw-REPL channel writes every firmware file (base64 per file, `mkdir 'lib'`)
plus a matching `manifest.json` *before* the `secrets.py` write and reset.
