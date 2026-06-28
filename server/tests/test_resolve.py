"""Unit tests for the pure resolver + ETag/canonical-json helpers."""

from __future__ import annotations

from server.resolve import (
    Resolution,
    canonical_json,
    compute_etag,
    resolve_from_events,
)
from server.store import DeviceRow, EventRow

DEVICE = DeviceRow(
    id="kitchen-01",
    channels=["home.status", "home.alerts"],
    fallback={"layout": "status_card", "content": {"title": "IDLE"}},
    poll_interval_s=120,
    low_batt_interval_s=600,
)


def _evt(id, *, priority, received_at, ttl=900, channel="home.status", kind="base", content=None):
    return EventRow(
        id=id,
        device="kitchen-01",
        channel=channel,
        priority=priority,
        ttl_seconds=ttl,
        layout="status_card",
        content=content or {"title": id},
        received_at=received_at,
        raw_size=10,
        kind=kind,
    )


def test_newest_base_wins_over_priority():
    now = 1000
    events = [
        _evt("high-old", priority=200, received_at=900),
        _evt("low-new", priority=10, received_at=950),
    ]
    res = resolve_from_events(DEVICE, events, now)
    assert res.source_event_id == "low-new"
    assert res.kind == "base"
    assert res.priority == 10



def test_live_interrupt_overlays_newer_base_then_expires():
    now = 1000
    events = [
        _evt("base", priority=1, received_at=990, kind="base"),
        _evt("interrupt", priority=1, received_at=900, ttl=200, kind="interrupt"),
    ]
    live = resolve_from_events(DEVICE, events, now)
    assert live.source_event_id == "interrupt"
    assert live.kind == "interrupt"

    expired = resolve_from_events(DEVICE, events, now=1201)
    assert expired.source_event_id == "base"
    assert expired.kind == "base"

def test_tie_break_newest_received_at():
    now = 1000
    events = [
        _evt("old", priority=50, received_at=910),
        _evt("new", priority=50, received_at=950),
    ]
    res = resolve_from_events(DEVICE, events, now)
    assert res.source_event_id == "new"


def test_ttl_expiry_falls_back():
    now = 2000
    # received_at 900 + ttl 900 = 1800 < now 2000 -> expired
    events = [_evt("stale", priority=99, received_at=900, ttl=900, kind="interrupt")]
    res = resolve_from_events(DEVICE, events, now)
    assert res.source_event_id is None
    assert res.priority is None
    assert res.layout == "status_card"
    assert res.content == {"title": "IDLE"}


def test_channel_not_subscribed_filtered():
    now = 1000
    events = [_evt("offchan", priority=99, received_at=950, channel="home.other")]
    res = resolve_from_events(DEVICE, events, now)
    assert res.source_event_id is None  # fallback


def test_fallback_when_no_events():
    res = resolve_from_events(DEVICE, [], now=1000)
    assert res.source_event_id is None
    assert res.content == {"title": "IDLE"}


def test_canonical_json_is_sorted_and_compact():
    out = canonical_json({"b": 1, "a": 2})
    assert out == b'{"a":2,"b":1}'


def test_etag_only_hashes_three_keys():
    content = {"title": "x"}
    e1 = compute_etag("kitchen-01", "status_card", content)
    e2 = compute_etag("kitchen-01", "status_card", content)
    assert e1 == e2
    # different content -> different etag
    assert compute_etag("kitchen-01", "status_card", {"title": "y"}) != e1


def test_to_response_excludes_rendered_at_from_etag():
    res = Resolution(
        device="kitchen-01",
        layout="status_card",
        content={"title": "x"},
        source_event_id="e1",
        priority=10,
        etag=compute_etag("kitchen-01", "status_card", {"title": "x"}),
    )
    r1 = res.to_response(rendered_at=1)
    r2 = res.to_response(rendered_at=999999)
    assert r1["etag"] == r2["etag"]  # rendered_at does not affect etag
    assert r1["rendered_at"] != r2["rendered_at"]
    assert r1["schema"] == "pico-paper.v1"
