"""Persistence model: base screens are sticky (ignore ttl); interrupts ALWAYS
expire (a missing/zero ttl falls back to the default lifetime, never permanent).
received_at is surfaced in the response."""

from __future__ import annotations

from server.resolve import resolve_from_events
from server.schema import INTERRUPT_DEFAULT_TTL
from server.store import DeviceRow, EventRow

from .conftest import DEVICE_TOKEN, INGEST_TOKEN, bearer

_CARD = {"title": "x", "status": "", "subtitle": "", "lines": [], "footer": ""}


def _device():
    return DeviceRow(
        id="d", channels=["c"],
        fallback={"layout": "status_card", "content": _CARD},
        poll_interval_s=120, low_batt_interval_s=600,
    )


def _evt(kind, ttl, received_at=1000):
    return EventRow(
        id="e1", device="d", channel="c", ttl_seconds=ttl,
        layout="status_card", content=_CARD, received_at=received_at, raw_size=10,
        kind=kind,
    )


# --- pure resolution -----------------------------------------------------------

def test_base_is_sticky_ignores_ttl():
    # A base persists no matter how old, regardless of any ttl it carries.
    res = resolve_from_events(_device(), [_evt("base", 0)], now=1000 + 10**9)
    assert res.source_event_id == "e1", "base stays forever"
    assert res.kind == "base"
    assert res.received_at == 1000


def test_interrupt_zero_ttl_uses_default_then_expires():
    events = [_evt("interrupt", 0)]
    live = resolve_from_events(_device(), events, now=1000 + INTERRUPT_DEFAULT_TTL - 1)
    assert live.source_event_id == "e1", "within the default window -> live"
    gone = resolve_from_events(_device(), events, now=1000 + INTERRUPT_DEFAULT_TTL + 1)
    assert gone.source_event_id is None, "past the default window -> expired (never permanent)"


def test_interrupt_none_ttl_uses_default():
    events = [_evt("interrupt", None)]
    assert resolve_from_events(
        _device(), events, now=1000 + INTERRUPT_DEFAULT_TTL - 1
    ).source_event_id == "e1"
    assert resolve_from_events(
        _device(), events, now=1000 + INTERRUPT_DEFAULT_TTL + 1
    ).source_event_id is None


def test_interrupt_positive_ttl_still_expires():
    res = resolve_from_events(_device(), [_evt("interrupt", 60)], now=1000 + 61)
    assert res.source_event_id is None, "positive ttl expires -> fallback"
    assert res.received_at is None


# --- HTTP: omitting ttl/kind is valid (defaults to a sticky base) --------------

def test_omitted_ttl_accepted_and_received_at_present(ctx):
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN),
        json={
            "schema": "pico-paper.v1", "id": "evt_sticky", "device": "kitchen-01",
            "channel": "home.status", "layout": "status_card",
            "content": {"title": "STICKY", "status": "OK", "subtitle": "",
                        "lines": [], "footer": ""},
        },
    )
    assert r.status_code == 201, r.text                     # default kind=base, no ttl needed
    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN)).json()
    assert g["content"]["title"] == "STICKY"
    assert g["kind"] == "base"
    assert isinstance(g["received_at"], int)                # surfaced in the response


def test_negative_ttl_rejected(ctx):
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN),
        json={
            "schema": "pico-paper.v1", "id": "evt_neg", "device": "kitchen-01",
            "channel": "home.status", "ttl_seconds": -5,
            "layout": "status_card",
            "content": {"title": "x", "status": "", "subtitle": "", "lines": [], "footer": ""},
        },
    )
    assert r.status_code == 422, "negative ttl rejected by ge=0"
