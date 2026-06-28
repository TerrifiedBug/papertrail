"""Sticky events: omitted/zero ttl_seconds = no expiry; received_at in response."""

from __future__ import annotations

from server.resolve import resolve_from_events
from server.store import DeviceRow, EventRow

from .conftest import DEVICE_TOKEN, INGEST_TOKEN, bearer

_CARD = {"title": "x", "status": "", "subtitle": "", "lines": [], "footer": ""}


def _device():
    return DeviceRow(
        id="d", channels=["c"],
        fallback={"layout": "status_card", "content": _CARD},
        poll_interval_s=120, low_batt_interval_s=600,
    )


def _event(ttl, received_at=1000):
    return EventRow(
        id="e1", device="d", channel="c", priority=50, ttl_seconds=ttl,
        layout="status_card", content=_CARD, received_at=received_at, raw_size=10,
        kind="interrupt",
    )


# --- pure resolution -----------------------------------------------------------

def test_zero_ttl_never_expires():
    res = resolve_from_events(_device(), [_event(0)], now=1000 + 10**9)
    assert res.source_event_id == "e1", "ttl=0 stays forever"
    assert res.received_at == 1000


def test_none_ttl_never_expires():
    res = resolve_from_events(_device(), [_event(None)], now=1000 + 10**9)
    assert res.source_event_id == "e1", "ttl=None stays forever"


def test_positive_ttl_still_expires():
    res = resolve_from_events(_device(), [_event(60)], now=1000 + 61)
    assert res.source_event_id is None, "positive ttl expires -> fallback"
    assert res.received_at is None


# --- HTTP: omitting ttl is now valid + received_at surfaced --------------------

def test_omitted_ttl_accepted_and_received_at_present(ctx):
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN),
        json={
            "schema": "pico-paper.v1", "id": "evt_sticky", "device": "kitchen-01",
            "channel": "home.status", "priority": 50, "layout": "status_card",
            "content": {"title": "STICKY", "status": "OK", "subtitle": "",
                        "lines": [], "footer": ""},
        },
    )
    assert r.status_code == 201, r.text                     # was 422 (ttl required) before
    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN)).json()
    assert g["content"]["title"] == "STICKY"
    assert isinstance(g["received_at"], int)                # surfaced in the response


def test_negative_ttl_rejected(ctx):
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN),
        json={
            "schema": "pico-paper.v1", "id": "evt_neg", "device": "kitchen-01",
            "channel": "home.status", "priority": 50, "ttl_seconds": -5,
            "layout": "status_card",
            "content": {"title": "x", "status": "", "subtitle": "", "lines": [], "footer": ""},
        },
    )
    assert r.status_code == 422, "negative ttl rejected by ge=0"
