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

## Use it

1. Open `http://localhost:8000/flash`.
2. **Connect Pico** → pick the serial port.
3. Fill WiFi SSID/password, **Server URL** (the bridge's **LAN IP**, e.g.
   `http://192.168.1.50:8000` — *not* localhost; the Pico isn't on localhost), device
   id, and a device token.
   - If you're signed into the [dashboard](dashboard.md) (admin token in this browser),
     pick a device and **Mint token** to auto-fill a fresh device token.
4. **Write secrets.py + reboot.** The page enters the MicroPython raw REPL, writes
   `secrets.py` (base64, so no escaping issues), and soft-resets. The Pico reconnects
   and starts polling.

The serial log shows the exchange. Nothing leaves the browser — it's a direct USB
write; the admin "mint" call is the only network request (same-origin, to your bridge).

## How it works

Web Serial opens the port at 115200, sends `Ctrl-C` to interrupt, `Ctrl-A` to enter the
raw REPL, then executes
`open('secrets.py','wb').write(ubinascii.a2b_base64(...))`, and `Ctrl-D` to soft-reset
— the same protocol mpremote/MicroPico use. The firmware reads `DEVICE_ID` and
`SERVER_URL` from `secrets.py` when present (overriding `config.py`).
