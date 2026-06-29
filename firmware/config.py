# papertrail firmware config -- the ONE place for the pin map + tunable knobs.
#
# Pure module: NO hardware imports here, so it is importable on a host (CPython)
# for the logic tests. Secrets (wifi creds, device bearer token, and optionally a
# server_url override) live in secrets.py -- see secrets.example.py. NEVER put real
# tokens/creds in this file.
#
# Geometry + the contract live in ../SCHEMA.md and ../docs/layout-specs.md.

# --------------------------------------------------------------------------
# Identity / network
# --------------------------------------------------------------------------
DEVICE_ID = "kitchen-01"          # must match a known device on the server

# Firmware version tag sent as the `fw` telemetry param on each poll. Keep it to
# <=16 chars of [A-Za-z0-9._-] (the server's accepted charset) or it is ignored.
FW_VERSION = "pt-1.0.0"

# LAN base URL of the papertrail bridge (no trailing slash). secrets.SERVER_URL
# overrides this if defined, so you can keep the real address out of git.
BASE_URL = "http://192.168.1.50:8000"

# Endpoint template the poller hits: GET {BASE_URL}/api/devices/{id}/current
CURRENT_PATH = "/api/devices/{id}/current"

HTTP_TIMEOUT_S = 15               # per-request socket timeout

# --------------------------------------------------------------------------
# WiFi
# --------------------------------------------------------------------------
WIFI_COUNTRY = "GB"               # 2-letter regulatory domain knob (e.g. GB, US, DE)
WIFI_CONNECT_TIMEOUT_S = 20       # per-attempt association timeout
WIFI_RETRIES = 3                  # connect attempts before giving up this wake

# --------------------------------------------------------------------------
# Polling / power
# --------------------------------------------------------------------------
POLL_INTERVAL_S = 120             # normal cadence default; the server can retune
                                  # this remotely via the response control block
                                  # (clamped locally to [30,3600] -- see poller).
LOW_BATT_INTERVAL_S = 600         # extended cadence when battery is low (default 600)

# machine.deepsleep() resets the RP2040 (RAM lost) -- the last ETag AND the server-
# tuned poll interval are persisted to flash so both survive a deepsleep reset.
# machine.lightsleep() keeps RAM and resumes in place (simpler, slightly higher
# current). True => deepsleep, False => lightsleep.
USE_DEEPSLEEP = False

# Auto power mode: when True, pick the sleep mode each cycle from the INA219 -- on
# battery (discharging) -> deepsleep (max runtime); plugged/charging -> lightsleep
# (REPL stays alive, responsive at the desk). When False, USE_DEEPSLEEP is fixed.
POWER_AUTO_SLEEP = True

ETAG_FILE = "last_etag.txt"          # tiny flash backstop for the last seen ETag
INTERVAL_FILE = "poll_interval.txt"  # flash backstop for the server-tuned cadence
BATT_SETTINGS_FILE = "batt_settings.json"  # backstop for server-tuned low_pct + low_batt_interval

# --------------------------------------------------------------------------
# OTA (over-the-air firmware update) -- see ota.py + boot.py.
# Updates are PULL/delta/sha-verified/atomic with a /backup rollback guard. The
# bridge serves the manifest + files at the two paths below (auth = this device's
# bearer token). NONE of these files are ever OTA'd themselves except where noted;
# config.py + secrets.py stay device-local so DEVICE_ID/pins are never clobbered.
# --------------------------------------------------------------------------
FIRMWARE_MANIFEST_PATH = "/api/firmware/manifest"  # GET -> {version, files:{path:sha}}
FIRMWARE_FILE_PATH = "/api/firmware/file"          # GET ?path=<path> -> raw bytes

MANIFEST_FILE = "manifest.json"      # last-applied {version, files:{path:sha}}
BACKUP_DIR = "backup"                # ONE known-good snapshot of changed files
BOOT_COUNT_FILE = "boot_count.txt"   # crash-loop counter (boot.py increments)
BAD_VERSION_FILE = "bad_fw.txt"      # a version boot.py quarantined after a crash loop
PENDING_VERSION_FILE = "pending_fw.txt"  # version of an in-flight apply: set BEFORE the
                                     # rename loop, cleared AFTER the manifest commit. If
                                     # an apply is interrupted before commit, boot.py
                                     # quarantines THIS (not the still-current good one).
BOOT_MAX_ATTEMPTS = 3                # > this many boots w/o a clean cycle -> restore /backup

# When the crash-loop guard trips but there is NO usable /backup to restore, a
# reset-loop would just thrash a headless, battery-powered board. Idle in a long
# deepsleep instead (preserve battery; recovers on a power-cycle / re-flash).
RECOVERY_IDLE_S = 3600               # 1h long-idle deepsleep on unrecoverable crash loop

# --------------------------------------------------------------------------
# e-Paper display -- Waveshare Pico-ePaper-2.13-B V4 (SPI1, 250x122 landscape, tri-color)
# Pins {8,9,10,11,12,13} don't overlap the UPS I2C set {6,7}. BUT note GP8 (DC) is
# ALSO SPI1's default MISO -- the panel is write-only, so epaper2in13._make_spi()
# passes miso=None to stop the SPI peripheral claiming GP8, leaving it free for DC.
# --------------------------------------------------------------------------
# Panel MODEL select:
#   "2.13-B" = tri-color Black/White/Red (SSD1680 V4, driver epaper2in13b). Full
#              refresh only (~5-10s); uses the RED plane for the `alert` layout.
#   "2.13"   = mono Black/White (driver epaper2in13, honours EPAPER_REV below).
# Same 250x122 geometry either way, so all layouts are shared.
EPAPER_MODEL = "2.13-B"

EPAPER_REV = "V4"                 # mono-only revision knob: "V4" (common) or "V3"
EPAPER = {
    "spi_bus":  1,                # SPI1
    "baudrate": 4000000,          # 4 MHz
    "rst":      12,               # RST  -> GP12
    "dc":       8,                # DC   -> GP8
    "cs":       9,                # CS   -> GP9
    "sck":      10,               # CLK/SCK  -> GP10
    "mosi":     11,               # DIN/MOSI -> GP11
    "busy":     13,               # BUSY -> GP13
}
DISPLAY_W = 250                   # logical width  (x 0..249)
DISPLAY_H = 122                   # logical height (y 0..121); panel RAM is 128 tall

# Panel RAM is 128px tall but only 122px show, hiding a few rows at the TOP.
# Shift ALL rendered content down by this many px to pull it into view. TUNE ON
# HARDWARE: raise if the top is still clipped, lower if the bottom starts to clip.
# Set 0 for panels that don't need it.
EPAPER_Y_OFFSET = 6

# --------------------------------------------------------------------------
# Battery -- Waveshare Pico-UPS-B (INA219 fuel gauge, I2C1)
# {6,7} don't overlap the display pins {8..13}. The only sharing subtlety is on the
# display side (GP8=DC vs SPI1's default MISO, freed via miso=None; see above).
# --------------------------------------------------------------------------
BATTERY = {
    "i2c_bus":  1,                # I2C1
    "sda":      6,                # SDA -> GP6
    "scl":      7,                # SCL -> GP7
    "freq":     100000,           # 100 kHz
    "addr":     0x43,             # INA219 address on the UPS-B
    "v_min":    3.0,              # volts at 0%  (LiPo rough-linear; calibrate per cell)
    "v_max":    4.2,              # volts at 100%
    "low_pct":  15,               # <= this -> red battery badge + LOW_BATT_INTERVAL_S.
                                  # DEFAULT only -- the server can override per-device via
                                  # the control block. (<=1% is a hardcoded critical takeover.)
    # Voltage->% discharge curve: ascending (volts, pct) points, piecewise-linear.
    # The calibration knob -- a real LiPo curve is flat in the middle, so the linear
    # v_min..v_max map reads badly there. Measure your pack's resting voltage at known
    # charge levels and tune. Remove this key to fall back to the linear map.
    "curve": [
        [3.30, 0], [3.45, 5], [3.60, 10], [3.70, 20], [3.75, 30], [3.79, 40],
        [3.83, 50], [3.87, 60], [3.92, 70], [3.97, 80], [4.05, 90], [4.20, 100],
    ],
    # Power-source detection (auto deepsleep). The shunt sign tells charge vs
    # discharge; flip charge_sign to -1 if detection reads backwards on your board
    # (plugged shows "BATTERY"). threshold ignores near-zero noise.
    "charge_sign":       1,
    "power_threshold_mv": 2.0,
}
