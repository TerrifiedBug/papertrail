"""Contract-delta tests: device control (poll_interval), the /current control
block + ETag busting, piggybacked telemetry, /status, and fallback validation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.resolve import _IDLE_FALLBACK, resolve_from_events
from server.store import DeviceRow, Store

from .conftest import DEVICE_TOKEN, INGEST_TOKEN, bearer

CONFIG = "/api/devices/kitchen-01/config"
CURRENT = "/api/devices/kitchen-01/current"
STATUS = "/api/devices/kitchen-01/status"


# --- PATCH /config: poll_interval clamp + validation ---------------------------


def test_poll_interval_clamped_low(ctx):
    r = ctx.client.patch(CONFIG, headers=bearer(DEVICE_TOKEN), json={"poll_interval": 5})
    assert r.status_code == 200
    assert r.json()["poll_interval"] == 30


def test_poll_interval_clamped_high(ctx):
    r = ctx.client.patch(CONFIG, headers=bearer(DEVICE_TOKEN), json={"poll_interval": 99999})
    assert r.status_code == 200
    assert r.json()["poll_interval"] == 3600


def test_poll_interval_in_range_passes_through(ctx):
    r = ctx.client.patch(CONFIG, headers=bearer(DEVICE_TOKEN), json={"poll_interval": 300})
    assert r.status_code == 200
    assert r.json() == {"id": "kitchen-01", "poll_interval": 300}


@pytest.mark.parametrize(
    "body",
    [{"poll_interval": "300"}, {"poll_interval": 1.5}, {"poll_interval": True}, {}],
)
def test_poll_interval_non_int_or_missing_422(ctx, body):
    r = ctx.client.patch(CONFIG, headers=bearer(DEVICE_TOKEN), json=body)
    assert r.status_code == 422


def test_config_requires_auth(ctx):
    assert ctx.client.patch(CONFIG, json={"poll_interval": 300}).status_code == 401


def test_config_wrong_kind_403(ctx):
    # ingest token may not drive device control.
    r = ctx.client.patch(CONFIG, headers=bearer(INGEST_TOKEN), json={"poll_interval": 300})
    assert r.status_code == 403


def test_config_oversize_body_413(ctx):
    # /config must enforce the same 8 KiB cap as /events (no unbounded buffering).
    big = b'{"poll_interval":300,"x":"' + b"A" * 9000 + b'"}'
    r = ctx.client.patch(
        CONFIG,
        headers={**bearer(DEVICE_TOKEN), "Content-Type": "application/json"},
        content=big,
    )
    assert r.status_code == 413


def test_config_unknown_keys_422(ctx):
    # Strict config surface: unknown top-level keys are rejected, not dropped.
    r = ctx.client.patch(
        CONFIG, headers=bearer(DEVICE_TOKEN), json={"poll_interval": 300, "fallback": {}}
    )
    assert r.status_code == 422


# --- control block in /current + ETag busting ----------------------------------


def test_current_has_control_block(ctx):
    body = ctx.client.get(CURRENT, headers=bearer(DEVICE_TOKEN)).json()
    # control carries poll_interval AND the additive OTA fw version (12-hex).
    assert body["control"]["poll_interval"] == 120
    assert isinstance(body["control"]["fw"], str) and len(body["control"]["fw"]) == 12


def test_poll_interval_change_busts_304(ctx):
    g1 = ctx.client.get(CURRENT, headers=bearer(DEVICE_TOKEN))
    etag1 = g1.headers["etag"]
    # Unchanged screen -> 304.
    same = ctx.client.get(CURRENT, headers={**bearer(DEVICE_TOKEN), "If-None-Match": etag1})
    assert same.status_code == 304

    ctx.client.patch(CONFIG, headers=bearer(DEVICE_TOKEN), json={"poll_interval": 300})

    after = ctx.client.get(CURRENT, headers={**bearer(DEVICE_TOKEN), "If-None-Match": etag1})
    assert after.status_code == 200                       # control change busts the 304
    assert after.headers["etag"] != etag1
    assert after.json()["control"]["poll_interval"] == 300


# --- telemetry: persistence, clamping, and never-4xx ---------------------------


def test_telemetry_persisted_and_returned_by_status(ctx):
    g = ctx.client.get(
        CURRENT,
        headers=bearer(DEVICE_TOKEN),
        params={"batt": "55", "rssi": "-70", "fw": "1.2.3", "up": "3600"},
    )
    assert g.status_code == 200
    s = ctx.client.get(STATUS, headers=bearer(DEVICE_TOKEN)).json()
    assert s["id"] == "kitchen-01"
    assert s["last_batt"] == 55
    assert s["last_rssi"] == -70
    assert s["last_fw"] == "1.2.3"
    assert s["last_uptime"] == 3600
    assert s["last_seen_at"] is not None
    assert s["poll_interval"] == 120


def test_telemetry_clamped(ctx):
    ctx.client.get(
        CURRENT, headers=bearer(DEVICE_TOKEN),
        params={"batt": "250", "rssi": "999", "up": "-5"},
    )
    s = ctx.client.get(STATUS, headers=bearer(DEVICE_TOKEN)).json()
    assert s["last_batt"] == 100
    assert s["last_rssi"] == 0
    assert s["last_uptime"] == 0


def test_malformed_telemetry_does_not_4xx_the_poll(ctx):
    g = ctx.client.get(
        CURRENT, headers=bearer(DEVICE_TOKEN),
        params={"batt": "abc", "rssi": "x", "fw": "bad chars!", "up": "nope"},
    )
    assert g.status_code == 200                            # poll MUST NOT 4xx
    s = ctx.client.get(STATUS, headers=bearer(DEVICE_TOKEN)).json()
    # Malformed values are silently dropped (stay None)...
    assert s["last_batt"] is None
    assert s["last_rssi"] is None
    assert s["last_fw"] is None
    assert s["last_uptime"] is None
    # ...but last_seen_at is still stamped every poll.
    assert s["last_seen_at"] is not None


def test_telemetry_does_not_affect_etag(ctx):
    a = ctx.client.get(CURRENT, headers=bearer(DEVICE_TOKEN), params={"batt": "10"})
    b = ctx.client.get(CURRENT, headers=bearer(DEVICE_TOKEN), params={"batt": "90"})
    assert a.headers["etag"] == b.headers["etag"]


# --- /status auth --------------------------------------------------------------


def test_status_requires_auth(ctx):
    assert ctx.client.get(STATUS).status_code == 401


# --- fallback validation -------------------------------------------------------

_BAD_DEVICE = {
    "id": "kitchen-01",
    "channels": ["home.status"],
    # metric.value must be a string, not a number -> invalid content.
    "fallback": {"layout": "metric", "content": {"value": 3.42}},
    "poll_interval_s": 120,
    "low_batt_interval_s": 600,
}


def test_bad_fallback_rejected_by_store_seed(tmp_path):
    store = Store(str(tmp_path / "bad.db"))
    store.init_db()
    with pytest.raises(ValueError):
        store.seed([_BAD_DEVICE], [])


def test_bad_fallback_fails_app_startup(tmp_path):
    app = create_app(db_path=str(tmp_path / "bad.db"), seed={"devices": [_BAD_DEVICE], "tokens": []})
    with pytest.raises(ValueError):
        with TestClient(app):
            pass


def test_resolve_swaps_bad_fallback_for_idle():
    # Defense in depth: if a bad fallback is somehow loaded at resolve time, ship
    # the hardcoded idle screen rather than unvalidated content.
    dev = DeviceRow(
        id="x",
        channels=[],
        fallback={"layout": "metric", "content": {"value": 1.0}},
        poll_interval_s=120,
        low_batt_interval_s=600,
    )
    res = resolve_from_events(dev, [], now=1000)
    assert res.layout == _IDLE_FALLBACK["layout"]
    assert res.content == _IDLE_FALLBACK["content"]
    assert res.control == {"poll_interval": 120}


def test_resolve_swaps_non_dict_fallback_for_idle():
    # A corrupt non-dict fallback (JSON null/array) must hit the idle screen, not
    # 500 on .get()/[] access.
    dev = DeviceRow(
        id="x",
        channels=[],
        fallback=None,
        poll_interval_s=120,
        low_batt_interval_s=600,
    )
    res = resolve_from_events(dev, [], now=1000)
    assert res.layout == _IDLE_FALLBACK["layout"]
    assert res.content == _IDLE_FALLBACK["content"]
