# papertrail firmware (MicroPython, Raspberry Pi Pico W)

Polls the papertrail bridge for the device's current screen and renders it to a
Waveshare Pico-ePaper-2.13-B **V4** (250x122 landscape, tri-color
Black/White/**Red**, SSD1680). Same geometry as the original mono 2.13, so every
layout coordinate is unchanged; the mono panel is still supported via a config
switch. Powered by a Waveshare Pico-UPS-B (INA219) for battery monitoring. Auto
power-aware deep/light sleeps between polls; the ePaper retains the image at zero
power.

Implements the frozen `pico-paper.v1` contract — see `../SCHEMA.md` and
`../docs/layout-specs.md`. Those docs are the source of truth for every pixel
coordinate; the renderers here mirror them exactly.

## Hardware + verified pin map

Boards stack on the Pico W headers. The two pin sets are **disjoint — no
collision** (display uses `{8,9,10,11,12,13}`, UPS uses `{6,7}`).

### Display — Waveshare Pico-ePaper-2.13-B V4 (SPI1, mode 0)

| signal     | GPIO  |
|------------|-------|
| RST        | GP12  |
| DC         | GP8   |
| CS         | GP9   |
| CLK / SCK  | GP10  |
| DIN / MOSI | GP11  |
| BUSY       | GP13  |

Panel select: `config.EPAPER_MODEL` = `"2.13-B"` (tri-color B/W/Red, SSD1680 V4
— the shipped panel; driver `epaper2in13b.py`) or `"2.13"` (mono B/W; driver
`epaper2in13.py`, which then honours `config.EPAPER_REV` = `"V4"` common rev or
`"V3"`). `get_epd()` in the selected driver picks the class. **Verify your panel
model/revision against the Waveshare wiki before flashing** — the tri-color panel
and the mono V3/V4 all use different init/LUT.

The panel is write-only, but GP8 (DC) is also SPI1's default MISO. Both drivers
build SPI1 with `miso=None` so the peripheral never claims GP8.

**Tri-color refresh + the red plane.** The 2.13-B is **full-refresh only**
(~5-10 s, with a flashing/fade waveform — that is the normal tri-color update, not
a fault). There is no partial/fast refresh, and red-vs-no-red content makes no
speed difference: the full waveform runs regardless. The panel is refreshed only
when the resolved screen actually **changes** — an unchanged poll (`304`) skips
the panel entirely (zero flash, zero power, image retained). The `alert` layout
is wired to the red plane: a **high**-severity alert draws its banner +
full-screen frame on the red plane (`canvas.red`); every other layout is
black-only. On a mono panel `canvas.red` folds onto black, so the same banner
renders as inverted-black (unchanged from the mono design).

**Top margin.** `config.EPAPER_Y_OFFSET` (default `6`) shifts ALL rendered
content down to clear the panel's hidden top rows (128px of RAM vs 122px
visible); `render.FrameCanvas` applies it to both planes. It is a per-panel
calibration knob — tune it on hardware with `test_offset.py` (raise if the top is
still clipped, lower if the bottom starts to clip).

### UPS — Waveshare Pico-UPS-B (INA219, I2C1)

| signal | GPIO | note            |
|--------|------|-----------------|
| SDA    | GP6  | I2C1 SDA        |
| SCL    | GP7  | I2C1 SCL        |
| addr   | —    | INA219 at 0x43  |

Battery %: `pct = clamp(0,100, round((v_bus - v_min)/(v_max - v_min)*100))`,
defaults `v_min=3.0`, `v_max=4.2`, `low_pct=15`. These are rough-linear LiPo
constants — calibrate per cell via `config.BATTERY`.

The **single source of truth** for all pins/knobs is `config.py`. Both the
display driver and the INA219 read their pins from there.

## Files

| file                | role | imports `machine`/`framebuf`? |
|---------------------|------|-------------------------------|
| `config.py`         | pin map + all tunable knobs (pure) | no |
| `secrets.example.py`| wifi + device-token placeholders (copy -> `secrets.py`) | no |
| `wifi.py`           | connect with timeout/retry + country code | guarded |
| `epaper2in13b.py`   | **official vendored** Waveshare 2.13-**B** V4 tri-color landscape driver (the shipped panel) | yes (compiles; not host-imported) |
| `epaper2in13.py`    | **official vendored** Waveshare mono V3/V4 landscape driver | yes (compiles; not host-imported) |
| `ina219.py`         | minimal INA219 bus-voltage + shunt-direction read + pure battery curve | guarded |
| `lib/uQR.py`        | **vendored** uQR QR generator (JASchilz/uQR, BSD-2) | no |
| `qr.py`             | thin adapter over `lib/uQR.py` | no |
| `render.py`         | per-layout renderers + offline/low-batt screens (canvas API) | guarded |
| `poller.py`         | conditional GET + pure ETag no-op decision | guarded |
| `main.py`           | boot/wake loop | guarded/lazy |
| `test_logic.py`     | host (CPython) logic tests, no framework | no |
| `test_panel_b.py`   | dev tool: on-device 2.13-B smoke test (black border + red bar) | yes (device only) |
| `test_offset.py`    | dev tool: `EPAPER_Y_OFFSET` calibration loop (renders a test alert) | yes (device only) |

"Guarded" = the hardware import is wrapped in `try/except ImportError`, so the
module imports on a host for testing and the pure logic is reachable without a
Pico.

## Flashing

1. Flash MicroPython for **Pico W** (UF2 from micropython.org). The Waveshare
   drivers here use `machine` + `framebuf`, both in the stock build.
2. Create secrets — `secrets.py` MUST live in `firmware/`:
   ```
   cp secrets.example.py secrets.py   # then edit with REAL values
   ```
   `secrets.py` is gitignored — never commit it. Set `WIFI_SSID`,
   `WIFI_PASSWORD`, `DEVICE_TOKEN`, and optionally `SERVER_URL`.
3. Set `config.DEVICE_ID`, `config.BASE_URL` (or `secrets.SERVER_URL`), and
   `config.EPAPER_MODEL` (`"2.13-B"` for the shipped tri-color panel, or `"2.13"`
   + `config.EPAPER_REV` for mono).
4. Copy these to the Pico filesystem **root** (`/`):
   `main.py`, `config.py`, `wifi.py`, `epaper2in13b.py`, `epaper2in13.py`,
   `ina219.py`, `qr.py`, `render.py`, `poller.py`, and your filled-in `secrets.py`.
   Copy `lib/uQR.py` to the Pico's **`/lib/`** directory (MicroPython auto-adds
   `/lib` to `sys.path`, so `import uQR` resolves). `main.py` runs at boot.

### VS Code + MicroPico (how the device is flashed)

**Gotcha: the VS Code workspace folder MUST be `papertrail/firmware`, NOT the
repo root.** Otherwise "Upload project" nests the files under `/firmware/` on the
device and stale root copies shadow them. For a clean device: run MicroPico
**"Delete all files from Pico"**, then **"Upload project"**. `main.py` auto-runs
on boot.

`test_panel_b.py` (panel smoke test) and `test_offset.py` (`EPAPER_Y_OFFSET`
calibration) are dev/calibration tools — run them on-device with MicroPico's "Run
current file"; they are not part of normal operation.

### mpremote / Thonny (alternative)

```
mpremote cp config.py wifi.py epaper2in13b.py epaper2in13.py ina219.py qr.py render.py poller.py main.py secrets.py :
mpremote mkdir lib ; mpremote cp lib/uQR.py :lib/uQR.py
```
`main.py` runs automatically on boot.

## Power / polling behaviour

- Each wake: read battery -> connect WiFi -> `GET /api/devices/:id/current` with
  `If-None-Match: "<last_etag>"`, piggybacking telemetry on the query string
  (`?batt=&rssi=&fw=&up=`). The bridge stores these against `last_seen` and serves
  them at `GET /status`.
  - `304` (or 200 with the same ETag): **panel untouched**, just sleep again
    (zero flash, image retained).
  - `200` changed: render the new layout (full refresh), store the new ETag.
  - error / wifi-down: render the offline screen.
- Low battery (`pct <= low_pct`): the battery badge turns **red** (tri-color
  panels) over the normal content, and the cadence stretches to
  `LOW_BATT_INTERVAL_S` (default 600 s) instead of `POLL_INTERVAL_S` (default
  120 s). Crossing the threshold forces one redraw even on an unchanged screen.
- Critically low (`pct <= 1`, hardcoded `main.CRITICAL_PCT`): full-screen
  "Battery low" takeover that also **skips the radio** to preserve the last of
  the runtime — the only case where the screen is fully replaced.
- **Auto power-aware sleep** (`config.POWER_AUTO_SLEEP=True`, the default): each
  cycle reads the INA219 shunt-current DIRECTION and picks the sleep mode — on
  battery (discharging) -> `machine.deepsleep` (max runtime; the board resets each
  wake, so the last ETag and the server-tuned poll interval are persisted to
  flash); plugged/charging -> `machine.lightsleep` (RAM + REPL preserved,
  responsive at the desk). Knobs in `config.BATTERY`: `charge_sign` (`+1`/`-1` —
  flip if detection reads backwards, e.g. plugged shows `BATTERY`) and
  `power_threshold_mv` (near-zero noise gate). Each cycle prints e.g.
  `battery: 4.19V -> 99% | shunt -12.3mV -> BATTERY`.
- When `POWER_AUTO_SLEEP=False`, the fixed `config.USE_DEEPSLEEP` is used:
  `False` -> `machine.lightsleep` (RAM kept, loop resumes in place); `True` ->
  `machine.deepsleep` (board resets). Either way the last ETag (`last_etag.txt`)
  and the server-tuned interval (`poll_interval.txt`) are written to flash so they
  survive a deepsleep reset and the panel still isn't redrawn unnecessarily.
  (Tell-tale of deepsleep in the logs: the telemetry `up=` uptime resets to a
  small number every poll.)

## Self-check (host, no hardware)

```
python3 firmware/test_logic.py
```

Tests the pure logic with hardware stubbed: the battery voltage->% curve
(including clamping below `v_min` / above `v_max`), the ETag no-op decision
(render only when changed), and per-layout field selection + truncation/wrap
(via a recording canvas). The QR test exercises the real vendored `uQR` path.

Byte-compile everything:

```
cd firmware && python3 -m py_compile config.py wifi.py epaper2in13b.py \
  epaper2in13.py ina219.py qr.py render.py poller.py main.py test_logic.py \
  test_panel_b.py test_offset.py lib/uQR.py
```

## Attribution

- `epaper2in13b.py` — vendored from the official Waveshare driver
  (`waveshareteam/Pico_ePaper_Code`, `python/Pico_ePaper-2.13-B_V4.py`), MIT. The
  Landscape class only; init/LUT copied verbatim. Local changes: SPI1 built with
  `miso=None` (frees GP8/DC), and the upstream `buffer_balck` typo corrected.
- `epaper2in13.py` — vendored from the official Waveshare mono driver
  (`waveshareteam/Pico_ePaper_Code`, `python/Pico_ePaper-2.13_V4.py` and
  `..._V3.py`), MIT. Init/LUT sequences copied verbatim; only the pin map was
  parametrised to `config.py` and both revisions merged behind `get_epd()`.
- `lib/uQR.py` — vendored from `JASchilz/uQR` (a MicroPython port of
  `lincolnloop/python-qrcode`), BSD-2-Clause, (c) 2018 Joseph Schilz. Only the
  first import line was wrapped for CPython host-testability; the algorithm is
  untouched.
