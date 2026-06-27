# Papertrail — Web Flashing (experimental)

Provision a Pico from the browser: write its `secrets.py` (WiFi, device token, server
URL, device id) over USB with **no MicroPico, no hand-editing**. Served by the bridge
at **`GET /flash`**.

> **Prototype / Layer A** (see [`roadmap.md`](roadmap.md)). It writes/updates
> `secrets.py` on a Pico that already runs MicroPython + the papertrail firmware. It
> does **not** flash the MicroPython `.uf2` runtime or upload the firmware `.py` set —
> that's the one-time manual step.

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
5. **Provision Pico** — one button does it all: creates the device if new, **mints a
   fresh device token automatically** (never shown/pasted), writes `secrets.py` over the
   raw REPL (base64, no escaping), and soft-resets. The Pico reconnects and polls.

The serial log shows the exchange. Nothing leaves the browser — it's a direct USB
write; the admin "mint" call is the only network request (same-origin, to your bridge).

## How it works

Web Serial opens the port at 115200, sends `Ctrl-C` to interrupt, `Ctrl-A` to enter the
raw REPL, then executes
`open('secrets.py','wb').write(ubinascii.a2b_base64(...))`, and `Ctrl-D` to soft-reset
— the same protocol mpremote/MicroPico use. The firmware reads `DEVICE_ID` and
`SERVER_URL` from `secrets.py` when present (overriding `config.py`).
