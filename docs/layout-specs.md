# Papertrail Layout Specs — 250x122 (tri-color B/W/Red)

**Single source of truth for pixel geometry.** Firmware renders these regions
exactly; the server validates `content` against the same field names. If a number
here disagrees with code, this file wins until this file is changed.

Display (shipped): Waveshare Pico-ePaper-2.13-**B V4** — tri-color
**Black/White/Red**, SSD1680, **250 px wide x 122 px tall**, landscape. Geometry is
identical to the original mono 2.13, so every coordinate below is unchanged. The mono
2.13 panel is still supported (`EPAPER_MODEL = "2.13"`); see §6 for both drivers.

---

## 0. Conventions

### Coordinate system

- Origin `(0,0)` = **top-left**. `x` -> right `0..249`. `y` -> down `0..121`.
- `W = 250`, `H = 122`, margin `PAD = 4`.
- Usable text band (both margins): `x 4..245` = **242 px**; `y 4..117` = **114 px**.
- **Top-margin shift:** all coordinates here are *logical*. The firmware adds
  `config.EPAPER_Y_OFFSET` (shipped = **6**) to every `y` before drawing, pushing
  content down past the panel's hidden top rows (128 px of RAM vs 122 px visible).
  Applied in `render.FrameCanvas` to **both planes** (`fill` is exempt — it is
  full-screen). It is a per-panel calibration knob — tune on hardware with
  `test_offset.py`. Coordinates below do **not** include it.

### Colors (1-bit planes)

- Semantic: **INK = black**, **PAPER = white**.
- Framebuf bytes via the vendored Waveshare driver: `fill(0xFF)` clears to PAPER;
  draw with color `0x00` for INK, `0xFF` for PAPER. (Pixel-level: `0`=ink, `1`=paper.)
- Clear screen each frame with `fill(0xFF)` (PAPER) before drawing.
- **RED plane (tri-color):** the shipped B V4 has a second 1-bit plane, `imagered`,
  wired in the renderer as `canvas.red` and cleared to PAPER each frame. Only `alert`
  high-severity draws on it (§2); every other layout is black-only. On a mono panel
  `canvas.red` folds onto the black canvas, so red draws degrade to black (unchanged).

### Fonts

There is exactly one glyph source: the **built-in MicroPython `framebuf` 8x8
font** (monospaced, ASCII 32..126). "Large" fonts are integer **scales** of it,
drawn by a `text_scaled(buf, s, x, y, color, n)` helper (blit each glyph pixel as
an `n x n` block). **No external font files are vendored** — this keeps firmware
small and every size pixel-deterministic.

| name | scale n | glyph WxH | advance/char | chars across 242px band |
|------|---------|-----------|--------------|--------------------------|
| **S1** (default) | 1 | 8 x 8   | 8  | **30** |
| **S2** (large)   | 2 | 16 x 16 | 16 | **15** |
| **S3**           | 3 | 24 x 24 | 24 | 10 |
| **S4** (huge)    | 4 | 32 x 32 | 32 | **7**  |

`text origin (x,y)` = top-left pixel of the first glyph. A glyph at S`n` occupies
`y .. y+8n-1` vertically. "default 8px framebuf font" = **S1**; anything needing a
"large font" uses **S2** (titles) or **S4** (the `metric` value).

### Text fitting helpers (deterministic, ASCII-only)

- `clip(s, N)` — fit to a single line of `N` chars. If `len(s) > N`: keep first
  `N` chars and overwrite the **last 3 kept chars with `"..."`** (when `N > 3`);
  if `N <= 3`, hard cut. ASCII dots only (the 8x8 font has no ellipsis glyph).
- `wrap(s, N, L)` — greedy word wrap to width `N`, max `L` lines. Split on spaces;
  a single word longer than `N` is hard-split at `N`. If content exceeds `L`
  lines, the last shown line is `clip(line, N)` with the trailing `"..."`.

### Separators

`hline(y)` = 1 px INK horizontal rule spanning `x 0..249` at row `y`.

### Battery badge (overlay, all 5 layouts)

After a layout renders, `render.draw_battery` overlays a badge in the **bottom-right**
corner (`x≈188..246, y≈109..121`): the charge `%` + a level-filled battery glyph, a `+`
prefix when wired/charging, drawn on the **red plane when low** (tri-color). It seats on a
cleared patch so it sits above a long footer's tail. Fed `(pct, on_battery, low)` from
`main.read_battery`; skipped on the offline / low-battery screens.

---

## 1. `status_card`

```
x:0                                               249
   +----------------------------------------------+  y0
   | Home Server                            OK     |  HEADER  y0..19
   +----------------------------------------------+  hline y20
     All services nominal                            SUBTITLE y25..32
                                                       (gap)
     CPU      12%                                    LINE 0   y40..47
     RAM      41%                                    LINE 1   y51..58
     Disk     63%                                    LINE 2   y62..69
     Uptime   18d 4h                                 LINE 3   y73..80
     ...                                            LINE 4   y84..91
   +----------------------------------------------+  hline y100
     updated 14:02                                  FOOTER   y110..117
   +----------------------------------------------+  y121
```

| region    | field      | font | origin (x,y)        | rule |
|-----------|------------|------|---------------------|------|
| title     | `title`    | S2   | (4, 2)              | max width `= 250 - 8 - 8*len(status) - 4`; practical `clip(title,12)` |
| status    | `status`   | S1   | right-aligned, baseline_y=6 | `x = 246 - 8*len(status)`; `clip(status,8)` |
| sep 1     | —          | —    | `hline(20)`         | |
| subtitle  | `subtitle` | S1   | (4, 25)             | `clip(subtitle,30)` |
| lines[i]  | `lines[i]` | S1   | (4, 40 + 11*i)      | i in 0..4 (rows y=40,51,62,73,84); extra lines dropped; `clip(line,30)` |
| sep 2     | —          | —    | `hline(100)`        | |
| footer    | `footer`   | S1   | (4, 110)            | `clip(footer,30)` |

Header height 20 px; status (8px) is vertically centered at `y=6`. No inversion.

---

## 2. `alert`

```
LOW / MED:                          HIGH (RED banner + frame; mono inverts):
   +----------------------------+      ##============================##  <- 2px frame
   | MED                        |      # [######## RED BANNER ######] #  banner y0..27
   +----------------------------+      # [ !! HIGH    (white text)  ] #  filled RED (INK on mono)
     Door Sensor                      #  Water Leak                  #  TITLE
     Garage door left open for         #  Sensor under the sink...   #  MESSAGE (wrap)
     12 minutes.                       #  ...                        #
   +----------------------------+      # --------------------------- #  hline y100
     garage-sensor-1                   #  basement-sensor-3          #  FOOTER
   +----------------------------+      ##============================##
```

| region    | field      | font | origin (x,y)       | rule |
|-----------|------------|------|--------------------|------|
| banner    | `severity` | S2   | text at (4, 6)     | label text: `low`->"LOW", `med`->"MED", `high`->"!! HIGH". Band = `y 0..27` (28 px) |
| title     | `title`    | S2   | (4, 34)            | `clip(title,15)` |
| message   | `message`  | S1   | (4, 58 + 11*i)     | `wrap(message,30,4)` -> rows y=58,69,80,91 |
| sep       | —          | —    | `hline(100)`       | always drawn |
| footer    | `footer`   | S1   | (4, 110)           | `clip(footer,30)` |

### Severity rendering (high = red plane; mono inverts)

- **`low` / `med`** — banner is PAPER; severity label drawn INK at (4,6); draw a
  separator `hline(28)` under the banner. No frame. Title/message/footer normal
  (INK on PAPER).
- **`high`** — **RED banner + frame**: on the shipped tri-color B V4 the banner and
  frame draw on the **RED plane** (`canvas.red`). Fill the banner rect `(0,0,250,28)`
  solid (red), draw the `"!! HIGH"` label punched **PAPER** at (4,6), then draw a
  **2 px frame** around the whole screen: `rect_outline(0,0,250,122)` +
  `rect_outline(1,1,248,120)`. Body text (title/message/footer) stays INK on the
  black plane (starts at x=4, clear of the 2 px frame). On a **mono** panel
  `canvas.red` folds onto black, so this degrades to the original **inverted-black
  banner + frame** (unchanged). This is the only layout that touches the red plane.

---

## 3. `list`

```
   +----------------------------------------------+  y0
   | Shopping                                     |  TITLE   y2..17
   +----------------------------------------------+  hline y20
     [ ] Milk                                        ITEM 0  y26..33
     [ ] Eggs                                        ITEM 1  y38..45
     [ ] Coffee beans                                ITEM 2  y50..57
     [ ] Bread                                       ITEM 3  y62..69
     [ ] Dish soap                                   ITEM 4  y74..81
     [ ] ...                                         ITEM 5  y86..93
   +----------------------------------------------+  hline y100
     5 items                                         FOOTER  y110..117
   +----------------------------------------------+  y121
```

| region    | field      | font | origin (x,y)     | rule |
|-----------|------------|------|------------------|------|
| title     | `title`    | S2   | (4, 2)           | `clip(title,15)` |
| sep 1     | —          | —    | `hline(20)`      | |
| checkbox  | (literal)  | S1   | (4, 26 + 12*i)   | literal text `"[ ] "` (4 chars = 32 px). **Decorative, non-interactive** |
| items[i]  | `items[i]` | S1   | (36, 26 + 12*i)  | i in 0..5 (rows y=26,38,50,62,74,86); items beyond index 5 dropped; `clip(text,26)` |
| sep 2     | —          | —    | `hline(100)`     | |
| footer    | `footer`   | S1   | (4, 110)         | `clip(footer,30)` |

Row pitch 12 px. The `[ ]` is rendered as plain glyphs; there are no buttons and
the Pico never toggles them.

---

## 4. `metric`

```
   +----------------------------------------------+  y0
     Solar output                                    LABEL   y6..13
   +----------------------------------------------+  hline y18
                                                       (gap)
              3.42  kW                               VALUE (S4) y34..65 + UNIT (S2) y50..65
                                                       (gap)
            UP +0.4 kW vs 1h                         TREND   y82..89
   +----------------------------------------------+  hline y100
     inverter-A                                      FOOTER  y110..117
   +----------------------------------------------+  y121
```

| region | field   | font | origin (x,y) | rule |
|--------|---------|------|--------------|------|
| label  | `label` | S1   | (4, 6)       | `clip(label,30)` |
| sep 1  | —       | —    | `hline(18)`  | |
| value  | `value` | S4   | (x_v, 34)    | `clip(value,7)`; centered as a group with unit (below) |
| unit   | `unit`  | S2   | (x_u, 50)    | `clip(unit,4)`; bottom-aligned to value baseline |
| trend  | `trend` | S1   | (x_t, 82)    | `clip(trend,30)`; centered |
| sep 2  | —       | —    | `hline(100)` | |
| footer | `footer`| S1   | (4, 110)     | `clip(footer,30)` |

### Centering math (value + unit form one centered group)

```
value_px = 32 * len(clip(value,7))
unit_px  = 16 * len(clip(unit,4))           # 0 if unit empty
gap      = 6 if unit else 0
group_px = value_px + gap + unit_px
x_v = max(4, (250 - group_px) // 2)         # value top-left
x_u = x_v + value_px + gap                  # unit top-left, y_u = 50 (= 34 + 32 - 16)
x_t = max(4, (250 - 8 * len(clip(trend,30))) // 2)   # trend centered, y_t = 82
```

Recommend ASCII trend tokens (no arrow glyph in the 8x8 font): `UP`, `DN`, `FLAT`.

---

## 5. `qr`

```
   +----------------------------------------------+  y0
   | Guest WiFi                                   |  TITLE   y2..17
   +----------------------------------------------+  hline y20
     #############        Scan to join             QR box: x8..98, y26..116 (90x90)
     ## ### ## ##         GuestNet. Valid          CAPTION: x104..246, wrapped
     ## ### ## ##         for 12 hours.            (right of the QR)
     #############
   +----------------------------------------------+  y121
```

| region  | field     | font | placement | rule |
|---------|-----------|------|-----------|------|
| title   | `title`   | S2   | (4, 2)    | `clip(title,15)` |
| sep 1   | —         | —    | `hline(20)` | |
| qr      | `qr_data` | —    | box `x 8..98`, `y 26..116` (90x90) | generated on-device from `qr_data`; cap 512 chars |
| caption | `caption` | S1   | (104, 30 + 11*i) | `wrap(caption,17,7)` -> rows y=30,41,52,63,74,85,96 |

### QR generation (on-device)

- The Pico encodes `qr_data` locally with the vendored **`uQR`** MicroPython QR
  generator (`firmware/lib/uQR.py`, the pure-python QR port). **Do not** fetch an
  image and **do not** put pixels on the wire — only `qr_data` is transmitted.
- Module pixel size: `module_px = max(2, 90 // qr_modules)` where `qr_modules` is
  the side length in modules of the chosen QR version. Keep `qr_data` short so the
  version stays small (cap 512 enforces an upper bound on memory on RP2040).
- Centre the rendered QR inside the 90x90 box:
  `rendered = module_px * qr_modules`,
  `x_off = 8 + (90 - rendered)//2`, `y_off = 26 + (90 - rendered)//2`.

---

## 6. Hardware (verified against Waveshare wiki + Zephyr shield)

Boards stack via the Pico headers; **pin sets are disjoint — no collision.**

### Display — Waveshare Pico-ePaper-2.13-B V4 (SPI1, 250x122 tri-color B/W/Red)

| signal     | GPIO  |
|------------|-------|
| RST        | GP12  |
| DC         | GP8   |
| CS         | GP9   |
| CLK / SCK  | GP10  |
| DIN / MOSI | GP11  |
| BUSY       | GP13  |

SPI mode 0 on **SPI1**. The shipped panel is the **B V4** (tri-color, SSD1680): use
the vendored official Waveshare *Landscape* driver `firmware/epaper2in13b.py`,
selected by **`config.EPAPER_MODEL = "2.13-B"`**. It carries a `miso=None` fix so
SPI1 never claims **GP8** (the DC pin). The mono 2.13 is still supported via
`firmware/epaper2in13.py` + the **`EPAPER_REV`** knob (`"V4"`/`"V3"`, default `"V4"`)
when `EPAPER_MODEL = "2.13"`. **Do not hand-roll the init sequence**; verify the
panel model/revision against the wiki before flashing.

### UPS — Waveshare Pico-UPS-B (INA219 fuel gauge, I2C1)

| signal | GPIO | note |
|--------|------|------|
| SDA    | GP6  | I2C1 SDA |
| SCL    | GP7  | I2C1 SCL |
| addr   | —    | INA219 at **0x43** |

`{6,7}` is disjoint from the display set `{8,9,10,11,12,13}` — **confirmed no
conflict.** `I2C(1, sda=Pin(6), scl=Pin(7), freq=100000)`.

### Battery % from INA219 bus voltage (config knobs — real cells drift)

```
pct = clamp(0, 100, round( (v_bus - V_MIN) / (V_MAX - V_MIN) * 100 ))
# defaults (LiPo, rough linear): V_MIN = 3.0 V (0%), V_MAX = 4.2 V (100%)
# LOW_PCT = 15  -> render a low-battery screen and switch to LOW_BATT_INTERVAL_S
```

`V_MIN`, `V_MAX`, `LOW_PCT` are config knobs — calibrate per cell.

### Power / polling

- **Refresh — tri-color is FULL refresh ONLY.** The B V4 has no partial/fast mode:
  every update runs the full waveform (**~5-10 s** with a flashing/fade sweep — this
  is **normal, not a fault**), and red-vs-no-red makes no speed difference. The panel
  refreshes **only when content actually changes** (see the `304` path below): no
  change -> no flash, no power, image retained.
- Deep sleep between polls (`machine.lightsleep`/`deepsleep`); ePaper **retains the
  image at zero power**, so nothing needs to be redrawn on wake unless the ETag
  changed.
- Default poll interval **120 s** (`poll_interval_s`); low-battery extends to
  **600 s** (`low_batt_interval_s`). Reconnect WiFi on each wake.
- Each poll: `GET /api/devices/:id/current` with `If-None-Match: "<last_etag>"`.
  `304` -> sleep without touching the panel. `200` -> render + store new ETag.
