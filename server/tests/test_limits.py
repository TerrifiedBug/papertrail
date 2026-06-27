"""Body-size, validation, and rate-limit reject tests."""

from __future__ import annotations

from .conftest import INGEST_TOKEN, RATE_TOKEN, bearer, make_event


def test_oversize_body_413(ctx):
    big = make_event(id="big", content={"title": "T", "lines": ["x" * 9000]})
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN), json=big
    )
    assert r.status_code == 413


def test_unknown_layout_422(ctx):
    ev = make_event(id="badlayout")
    ev["layout"] = "banner"
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN), json=ev
    )
    assert r.status_code == 422


def test_bad_schema_422(ctx):
    ev = make_event(id="badschema")
    ev["schema"] = "pico-paper.v2"
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN), json=ev
    )
    assert r.status_code == 422


def test_invalid_content_422(ctx):
    ev = make_event(id="badcontent", layout="metric", content={"value": 3.42})
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN), json=ev
    )
    assert r.status_code == 422


def test_qr_data_too_long_422(ctx):
    ev = make_event(id="bigqr", layout="qr", content={"qr_data": "x" * 513})
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN), json=ev
    )
    assert r.status_code == 422


def test_device_mismatch_422(ctx):
    ev = make_event(id="mism", device="someone-else")
    r = ctx.client.post(
        "/api/devices/kitchen-01/events", headers=bearer(INGEST_TOKEN), json=ev
    )
    assert r.status_code == 422


def test_malformed_json_422(ctx):
    r = ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers={**bearer(INGEST_TOKEN), "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert r.status_code == 422


def test_rate_limit_429(ctx):
    # RATE_TOKEN is a device token with rate_per_min=1 -> bucket holds one token.
    first = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(RATE_TOKEN))
    assert first.status_code == 200
    second = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(RATE_TOKEN))
    assert second.status_code == 429
