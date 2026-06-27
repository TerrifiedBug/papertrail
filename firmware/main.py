# papertrail boot loop (MicroPython, Pico W).
#
# Each wake:
#   1. read battery (cheap, no radio). If low -> low-battery screen + long sleep.
#   2. connect WiFi (timeout + retry). Failure -> offline screen.
#   3. GET /current with If-None-Match=<last_etag>, piggybacking best-effort
#      telemetry (?batt=&rssi=&fw=&up=) onto the poll.
#        304 / unchanged    -> DON'T touch the panel (ePaper retains the image).
#        200 changed        -> render the new screen, store the new ETag.
#        200 future schema   -> offline screen (never render v2 against v1 renderers).
#        error              -> offline screen.
#      The response control.poll_interval (clamped to [30,3600]) becomes the next
#      sleep cadence and is persisted to flash like the ETag.
#   4. rest the panel (zero power, image retained) and sleep the interval.
#
# WiFi connect + poll run inside ONE try/finally that ALWAYS calls wifi.disconnect()
# -- the radio powers down before we draw/sleep even after a Wi-Fi outage (no leak).
#
# Robust to WiFi/server failure: every step is wrapped; a bad cycle shows the
# offline screen and retries next interval. The last ETag AND the server-tuned
# poll interval are kept in RAM across lightsleep and written to flash so they
# survive a deepsleep reset.
#
# Hardware modules (wifi/poller/ina219/epaper2in13) are imported lazily inside
# functions so this file imports/compiles on a host too.

import config

_BOOT_COUNT_FILE = getattr(config, "BOOT_COUNT_FILE", "boot_count.txt")  # code const, not device-local
import render

try:
    import machine
    _HW = True
except ImportError:
    machine = None
    _HW = False

try:
    import utime as time
except ImportError:                  # host (py_compile / import) -- stdlib time
    import time

# Sentinels stored in place of a real ETag so we don't redraw an unchanged
# offline/low-battery screen every wake.
LOWBATT_SENTINEL = "__lowbatt__"
OFFLINE_SENTINEL = "__offline__"


def _secrets():
    import secrets
    return secrets


def _server_url(secrets):
    url = getattr(secrets, "SERVER_URL", None)
    return url if url else config.BASE_URL


def _device_id(secrets):
    # secrets.DEVICE_ID (written by the web flasher) overrides config.DEVICE_ID.
    return getattr(secrets, "DEVICE_ID", None) or config.DEVICE_ID


# --- tiny flash-backed ETag store (survives deepsleep reset) ----------------
def load_etag():
    try:
        with open(config.ETAG_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def save_etag(etag):
    try:
        with open(config.ETAG_FILE, "w") as f:
            f.write(etag or "")
    except Exception:
        pass


# --- flash-backed poll interval (server-tuned; survives deepsleep reset) --------
def load_interval():
    """Return the persisted normal-cadence interval, or the config default."""
    try:
        with open(config.INTERVAL_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return config.POLL_INTERVAL_S


def save_interval(seconds):
    try:
        with open(config.INTERVAL_FILE, "w") as f:
            f.write(str(int(seconds)))
    except Exception:
        pass


# --- crash-loop counter (boot.py's recovery guard increments it each boot) ------
def load_boot_count():
    try:
        with open(_BOOT_COUNT_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def clear_boot_count():
    """Zero the crash-loop counter after ONE fully-successful cycle -> tells boot.py
    'this firmware works'. Only writes when non-zero (flash-wear: a deepsleep wake
    re-runs boot.py which sets it to 1, so we write 0 once per healthy wake)."""
    try:
        if load_boot_count() != 0:
            with open(_BOOT_COUNT_FILE, "w") as f:
                f.write("0")
    except Exception:
        pass


def _uptime_s():
    """Seconds since boot (best-effort telemetry). 0 if the clock can't be read.
    Resets on a deepsleep wake (board reset); monotonic across lightsleep."""
    if not _HW:
        return 0
    try:
        import utime
        return utime.ticks_ms() // 1000
    except Exception:
        return 0


def read_battery():
    """Return (pct, is_low, on_battery). (None, False, False) if unreadable."""
    try:
        import ina219
        sensor = ina219.INA219.from_config(config.BATTERY)
        v = sensor.bus_voltage()
        pct = ina219.voltage_to_pct(v, config.BATTERY["v_min"], config.BATTERY["v_max"])
        low = ina219.is_low(pct, config.BATTERY["low_pct"])
        sh = sensor.shunt_mv()
        on_batt = ina219.is_on_battery(sh, config.BATTERY.get("charge_sign", 1),
                                       config.BATTERY.get("power_threshold_mv", 2.0))
        print("battery: %.2fV -> %d%%%s | shunt %.1fmV -> %s" %
              (v, pct, " LOW" if low else "", sh, "BATTERY" if on_batt else "plugged"))
        return pct, low, on_batt
    except Exception as e:
        print("battery read failed:", e)
        return None, False, False


class Panel:
    """Wakes the ePaper only when something must be drawn; rests it otherwise."""

    def __init__(self, epd):
        self.epd = epd
        self.awake = True            # get_epd() already ran init()

    def draw(self, fn, *args):
        if not self.awake:
            self.epd.init()
            self.awake = True
        fn(self.epd, *args)          # fn pushes via epd.display() internally

    def rest(self):
        if self.awake:
            try:
                self.epd.sleep()
            except Exception:
                pass
            self.awake = False


def _use_deepsleep(on_battery):
    """Sleep mode for this cycle: auto -> deepsleep on battery, lightsleep when
    plugged; otherwise the fixed USE_DEEPSLEEP."""
    if getattr(config, "POWER_AUTO_SLEEP", False):
        return on_battery
    return config.USE_DEEPSLEEP


def cycle(panel, last_etag, interval_pref):
    """Run one poll/render cycle.

    Returns (new_etag, sleep_seconds, new_interval_pref, use_deepsleep). `interval_pref`
    is the persisted NORMAL cadence (server-tunable via the response control block); a
    low battery overrides it for one sleep without changing the stored preference. The
    sleep MODE is chosen per cycle from the power source (deepsleep on battery).
    """
    secrets = _secrets()

    # 1. Battery + power source (no radio needed).
    pct, low, on_battery = read_battery()
    use_deep = _use_deepsleep(on_battery)
    if low:
        if last_etag != LOWBATT_SENTINEL:
            panel.draw(render.draw_low_battery, pct if pct is not None else 0)
            save_etag(LOWBATT_SENTINEL)
        return LOWBATT_SENTINEL, config.LOW_BATT_INTERVAL_S, interval_pref, use_deep

    # Leaving a sentinel screen: force a fresh fetch+render by dropping the ETag.
    poll_etag = "" if last_etag in (LOWBATT_SENTINEL, OFFLINE_SENTINEL) else last_etag

    # 2/3. WiFi + poll wrapped in ONE try/finally so the CYW43 radio ALWAYS powers
    # down before we render/sleep -- even when connect fails. (The old early-return
    # on connect failure skipped disconnect and leaked current after a Wi-Fi outage.)
    import wifi
    wifi_ok = False
    result = None
    try:
        wifi_ok = wifi.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)
        if wifi_ok:
            import poller
            import ota
            local_fw = ota.local_version()      # local manifest version, else None
            telemetry = {
                "batt": pct,                    # None if the gauge couldn't be read
                "rssi": wifi.rssi(),            # None if unavailable -> omitted
                "fw": local_fw or config.FW_VERSION,   # report the running version
                "up": _uptime_s(),
            }
            result = poller.poll(_server_url(secrets), _device_id(secrets),
                                 secrets.DEVICE_TOKEN, poll_etag, telemetry)

            # OTA rides the existing poll (zero extra cost until an update waits):
            # only when the server advertises a DIFFERENT fw do we run ota.apply(),
            # and we do it HERE -- inside the wifi try, radio still up -- so the
            # download can happen. apply() verifies every file's sha and resets on
            # success (never returns); ANY failure is fully guarded so a bad OTA
            # logs + carries on to render/sleep rather than bricking the cycle.
            if result is not None and ota.should_update(result.get("control_fw"),
                                                        local_fw):
                try:
                    print("ota: update", local_fw, "->", result.get("control_fw"))
                    ota.apply(_server_url(secrets), secrets.DEVICE_TOKEN)
                except Exception as e:
                    print("ota: apply failed (continuing):", e)
    finally:
        wifi.disconnect()                       # radio off before any draw / sleep

    if not wifi_ok:
        if last_etag != OFFLINE_SENTINEL:
            panel.draw(render.draw_offline, "wifi failed")
        return OFFLINE_SENTINEL, interval_pref, interval_pref, use_deep

    # Apply any server-tuned cadence (already clamped to [30,3600] by the poller).
    new_pref = interval_pref
    if result.get("poll_interval") is not None:
        new_pref = result["poll_interval"]

    action = result["action"]
    if action == "render":
        screen = result["screen"] or {}
        panel.draw(render.draw_to_epd, screen.get("layout"), screen.get("content") or {})
        new = result["etag"] or ""
        save_etag(new)
        print("rendered:", screen.get("layout"), "etag", new[:12], "interval", new_pref)
        return new, new_pref, new_pref, use_deep

    if action == "offline":
        if last_etag != OFFLINE_SENTINEL:
            panel.draw(render.draw_offline, result.get("error") or "server error")
        return OFFLINE_SENTINEL, new_pref, new_pref, use_deep

    # action == "skip": unchanged (304) -> panel untouched.
    print("unchanged (304) -> skip")
    return last_etag, new_pref, new_pref, use_deep


def sleep(seconds, use_deep):
    if not _HW:
        return
    ms = int(seconds * 1000)
    if use_deep:
        machine.deepsleep(ms)        # board resets on wake (state persisted to flash)
    else:
        machine.lightsleep(ms)       # resumes in place, RAM + REPL preserved


def main():
    if not _HW:
        raise RuntimeError("main() requires MicroPython hardware")

    if getattr(config, "EPAPER_MODEL", "2.13") == "2.13-B":
        import epaper2in13b as epd_drv           # tri-color B/W/Red (SSD1680 V4)
    else:
        import epaper2in13 as epd_drv            # mono B/W (EPAPER_REV V3/V4)
    panel = Panel(epd_drv.get_epd())
    last_etag = load_etag()
    interval_pref = load_interval()      # server-tuned normal cadence (persisted)

    while True:
        prev_pref = interval_pref
        cycle_ok = False
        try:
            last_etag, sleep_s, interval_pref, use_deep = cycle(panel, last_etag, interval_pref)
            cycle_ok = True
        except Exception as e:
            # A cycle() crash must ADVANCE the recovery path, not retry forever in
            # place: draw the offline screen, then machine.reset() so boot.py's
            # boot-count climbs and the crash-loop guard can heal a bad OTA. A short
            # delay leaves a Ctrl-C window for a dev at the REPL (KeyboardInterrupt is
            # BaseException, so it escapes this `except Exception` and drops to REPL).
            print("cycle error:", e)
            try:
                panel.draw(render.draw_offline, "err: " + str(e))
            except Exception:
                pass
            panel.rest()                 # zero-power before the reset; image retained
            if _HW:
                time.sleep(3)            # ~3s Ctrl-C window for a dev at the REPL
                machine.reset()          # -> boot.py increments boot_count -> guard heals
            return                       # off-hardware only (device resets above)

        # One clean cycle -> the running firmware works: clear boot.py's crash-loop
        # counter. Only on the no-exception path, so a firmware that keeps throwing
        # lets the counter climb (across deepsleep resets) until the guard restores.
        if cycle_ok:
            clear_boot_count()

        # Persist the cadence only when the server actually changed it (flash wear).
        if interval_pref != prev_pref:
            save_interval(interval_pref)

        panel.rest()                     # zero-power; image is retained
        sleep(sleep_s, use_deep)
        # lightsleep resumes here and loops; deepsleep resets and re-enters main().


if __name__ == "__main__":
    main()
