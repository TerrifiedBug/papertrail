# WiFi connect/disconnect for the Pico W, with timeout + retry and a country knob.
# Hardware-only (needs `network`); guarded so the module still imports on a host.

try:
    import network
    import rp2
    import utime as time
    _HW = True
except ImportError:                         # running on a host (tests/py_compile)
    network = None
    rp2 = None
    import time
    _HW = False

import config


def _wlan():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    return wlan


def connect(ssid, password,
            country=config.WIFI_COUNTRY,
            timeout_s=config.WIFI_CONNECT_TIMEOUT_S,
            retries=config.WIFI_RETRIES):
    """Bring up STA mode and associate. Returns True on success, False otherwise.

    Tries up to `retries` times, each with a `timeout_s` association window.
    """
    if not _HW:
        raise RuntimeError("wifi.connect requires MicroPython hardware")

    # Regulatory domain must be set before the interface is used.
    try:
        rp2.country(country)
    except Exception:
        pass

    wlan = _wlan()
    for attempt in range(1, retries + 1):
        if wlan.isconnected():
            return True
        try:
            wlan.disconnect()
        except Exception:
            pass
        wlan.connect(ssid, password)

        deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
        while not wlan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
            time.sleep_ms(200)

        if wlan.isconnected():
            print("wifi: connected", wlan.ifconfig()[0], "(attempt %d)" % attempt)
            return True
        print("wifi: attempt %d/%d timed out" % (attempt, retries))

    return False


def is_connected():
    if not _HW:
        return False
    try:
        return network.WLAN(network.STA_IF).isconnected()
    except Exception:
        return False


def rssi():
    """Current STA RSSI in dBm, or None if unavailable (best-effort telemetry).

    Read while still associated -- call before disconnect(). Not all ports expose
    WLAN.status('rssi'); a failure just omits the value from the poll.
    """
    if not _HW:
        return None
    try:
        return int(network.WLAN(network.STA_IF).status("rssi"))
    except Exception:
        return None


def disconnect():
    """Drop the link and power the radio down to save current before sleep."""
    if not _HW:
        return
    try:
        wlan = network.WLAN(network.STA_IF)
        wlan.disconnect()
        wlan.active(False)
    except Exception:
        pass
