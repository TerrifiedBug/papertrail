# Polls GET /api/devices/:id/current with a device bearer token and conditional
# If-None-Match, then decides whether the panel needs a redraw.
#
# The render/no-render decision (`decide_action`) is PURE and host-testable. The
# network call guards the `urequests` import so this module imports on a host.
#
# ETag handling: the contract puts the canonical ETag in the JSON body's `etag`
# field, so we never need response headers (stock urequests doesn't expose them).
# We send  If-None-Match: "<last_etag>"  (quoted, per the contract).

try:
    import urequests
    _HW = True
except ImportError:
    urequests = None
    _HW = False

import config

# The wire schema this firmware's renderers implement. A 200 body carrying any
# other schema (e.g. a future "pico-paper.v2") MUST NOT be rendered against v1
# renderers -- we treat it as offline instead. (Additive v1 response keys keep the
# string "pico-paper.v1", so they pass this guard untouched.)
SCHEMA_VERSION = "pico-paper.v1"

# Safe band for the server-supplied control.poll_interval. The server clamps too,
# but we re-clamp locally: it is a calibration knob, never trusted blindly.
POLL_INTERVAL_MIN_S = 30
POLL_INTERVAL_MAX_S = 3600

# Telemetry `fw` charset (mirrors the server's [A-Za-z0-9._-]); anything else is
# stripped so a stray char can't malform the query string.
_FW_OK = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"


def clamp_interval(value, lo=POLL_INTERVAL_MIN_S, hi=POLL_INTERVAL_MAX_S):
    """Clamp a server-supplied poll_interval into [lo, hi]. PURE.

    Returns None for a missing / non-int value so the caller can keep its own
    configured default rather than apply garbage.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _clean_fw(fw):
    """Keep only the server-accepted charset, capped at 16 chars. PURE."""
    return "".join(ch for ch in str(fw) if ch in _FW_OK)[:16]


def build_query(telemetry):
    """Build the optional telemetry query string from a dict. PURE.

    Keys (any subset): batt, rssi, fw, up. A None value (or absent key) is
    omitted -- e.g. rssi is dropped when the radio can't report it. Best-effort:
    the server validates/clamps and silently ignores anything malformed, so we
    keep this lean and deterministic (fixed key order: batt, rssi, fw, up).
    Returns "" when there is nothing to send.
    """
    if not telemetry:
        return ""
    parts = []
    batt = telemetry.get("batt")
    if batt is not None:
        parts.append("batt=" + str(int(batt)))
    rssi = telemetry.get("rssi")
    if rssi is not None:
        parts.append("rssi=" + str(int(rssi)))
    fw = telemetry.get("fw")
    if fw is not None:
        cleaned = _clean_fw(fw)
        if cleaned:
            parts.append("fw=" + cleaned)
    up = telemetry.get("up")
    if up is not None:
        parts.append("up=" + str(int(up)))
    if not parts:
        return ""
    return "?" + "&".join(parts)


def decide_action(status_code, body_etag, last_etag, body_schema=None):
    """Map an HTTP outcome to (action, etag). PURE.

      304                  -> ("skip",    last_etag)   # unchanged: leave the panel
      200 schema mismatch  -> ("offline", last_etag)   # future schema: don't render
      200 same etag        -> ("skip",    last_etag)   # belt-and-braces de-dupe
      200 new etag         -> ("render",  body_etag)   # changed: redraw + store etag
      anything else        -> ("offline", last_etag)   # error: show offline screen
    """
    if status_code == 304:
        return ("skip", last_etag)
    if status_code == 200:
        if body_schema is not None and body_schema != SCHEMA_VERSION:
            # Honor the schema-version guard: a v1 renderer must not draw a v2 body.
            return ("offline", last_etag)
        if body_etag is not None and body_etag == last_etag:
            return ("skip", last_etag)
        return ("render", body_etag)
    return ("offline", last_etag)


def poll(base_url, device_id, token, last_etag, telemetry=None):
    """Fetch the current screen. Returns a dict:
        {"action": "skip"|"render"|"offline", "etag": <str|None>,
         "screen": <body dict|None>, "error": <str|None>,
         "poll_interval": <int|None>}
    `screen` carries the {schema,device,layout,content,control,...} body only on
    render. `poll_interval` is the server's control.poll_interval, already clamped
    to [30,3600] (None when absent / unparseable -> caller keeps its default).
    `telemetry` (optional dict: batt/rssi/fw/up) is piggybacked as query params.
    """
    if not _HW:
        raise RuntimeError("poller.poll requires MicroPython (urequests)")

    url = base_url + config.CURRENT_PATH.format(id=device_id) + build_query(telemetry)
    headers = {"Authorization": "Bearer " + token}
    if last_etag:
        headers["If-None-Match"] = '"' + last_etag + '"'

    try:
        resp = urequests.get(url, headers=headers)
    except Exception as e:
        # Uniform return shape: every poll() exit carries control_fw + poll_interval
        # (both None here -- no body to read) so callers never KeyError on a path.
        return {"action": "offline", "etag": last_etag, "screen": None,
                "error": "request failed: " + str(e),
                "poll_interval": None, "control_fw": None,
                "control_action": None, "hints": None}

    body = None
    error = None
    try:
        status = resp.status_code
        if status == 200:
            try:
                body = resp.json()
            except Exception as e:
                error = "bad json: " + str(e)
    except Exception as e:
        status = -1
        error = str(e)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    body_etag = body.get("etag") if isinstance(body, dict) else None
    body_schema = body.get("schema") if isinstance(body, dict) else None
    action, etag = decide_action(status, body_etag, last_etag, body_schema)

    # Surface the server-tuned cadence + the latest firmware version. Both come from
    # control on any parsed 200 body -- including a 200-same-etag "skip" -- so a
    # control-only change (new cadence OR a waiting OTA) still rides the poll. Both
    # are None when there's no body (304) or no control block; on a 304 the device
    # learns the new fw on the next 200 (the bridge folds fw into the ETag so an fw
    # bump forces a 200). control.fw drives the OTA trigger in main.cycle().
    interval = None
    control_fw = None
    control_action = None
    hints = body.get("hints") if isinstance(body, dict) else None
    control = body.get("control") if isinstance(body, dict) else None
    if isinstance(control, dict):
        interval = clamp_interval(control.get("poll_interval"))
        fw = control.get("fw")
        if fw is not None:
            control_fw = str(fw)
        ca = control.get("action")
        if isinstance(ca, dict) and ca.get("name"):
            control_action = ca

    if action == "render" and body is None:
        # 200 but unparseable body -> treat as offline rather than render garbage.
        return {"action": "offline", "etag": last_etag, "screen": None,
                "error": error or "empty body", "poll_interval": interval,
                "control_fw": control_fw, "control_action": control_action,
                "hints": hints}

    return {"action": action, "etag": etag,
            "screen": body if action == "render" else None,
            "error": error if action == "offline" else None,
            "poll_interval": interval, "control_fw": control_fw,
            "control_action": control_action, "hints": hints}
