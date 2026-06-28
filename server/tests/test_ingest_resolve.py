"""Integration tests: ingest -> resolve via the HTTP app."""

from __future__ import annotations

import time

from server.store import EventRow

from .conftest import DEVICE_TOKEN, INGEST_TOKEN, bearer, make_event


def test_post_then_current(ctx):
    r = ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_TOKEN),
        json=make_event(id="evt_a", priority=50, content={"title": "Hi"}),
    )
    assert r.status_code == 201
    assert r.json()["status"] == "stored"

    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    assert g.status_code == 200
    body = g.json()
    assert body["layout"] == "status_card"
    assert body["content"]["title"] == "Hi"
    assert body["source_event_id"] == "evt_a"
    assert body["priority"] == 50
    assert body["schema"] == "pico-paper.v1"
    assert g.headers["etag"].strip('"') == body["etag"]


def test_newest_base_wins_over_http(ctx):
    ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_TOKEN),
        json=make_event(id="evt_old_high", priority=200, content={"title": "old high"}),
    )
    ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_TOKEN),
        json=make_event(id="evt_new_low", priority=10, content={"title": "new low"}),
    )
    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    assert g.json()["content"]["title"] == "new low"
    assert g.json()["kind"] == "base"
    assert g.json()["priority"] == 10


def test_dedup_first_write_wins(ctx):
    first = ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_TOKEN),
        json=make_event(id="dup1", content={"title": "first"}),
    )
    assert first.status_code == 201

    dup = ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_TOKEN),
        json=make_event(id="dup1", content={"title": "second"}),
    )
    assert dup.status_code == 200
    assert dup.json()["status"] == "duplicate"

    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    assert g.json()["content"]["title"] == "first"  # never overwritten


def test_channel_not_subscribed_falls_back(ctx):
    # token allows all channels, but device does not subscribe to home.other
    ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_TOKEN),
        json=make_event(id="offchan", channel="home.other", content={"title": "ignored"}),
    )
    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    assert g.json()["source_event_id"] is None  # fallback
    assert g.json()["content"]["status"] == "IDLE"


def test_ttl_expiry_falls_back_over_http(ctx):
    # Insert directly with a past received_at so it is already expired.
    past = int(time.time()) - 10_000
    ctx.store.insert_event(
        EventRow(
            id="stale",
            device="kitchen-01",
            channel="home.status",
            priority=99,
            ttl_seconds=60,
            layout="status_card",
            content={"title": "old"},
            received_at=past,
            raw_size=10,
            kind="interrupt",
        )
    )
    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    assert g.json()["source_event_id"] is None  # expired -> fallback


def test_empty_store_returns_fallback(ctx):
    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    assert g.status_code == 200
    assert g.json()["source_event_id"] is None
    assert g.json()["priority"] is None
    assert g.json()["layout"] == "status_card"


def test_healthz(ctx):
    assert ctx.client.get("/healthz").json() == {"status": "ok"}
