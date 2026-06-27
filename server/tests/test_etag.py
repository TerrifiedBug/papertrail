"""ETag / If-None-Match behavior."""

from __future__ import annotations

from .conftest import DEVICE_TOKEN, INGEST_TOKEN, bearer, make_event


def test_304_on_matching_etag(ctx):
    g = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    assert g.status_code == 200
    etag = g.headers["etag"]

    again = ctx.client.get(
        "/api/devices/kitchen-01/current",
        headers={**bearer(DEVICE_TOKEN), "If-None-Match": etag},
    )
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag


def test_stale_if_none_match_returns_200(ctx):
    g = ctx.client.get(
        "/api/devices/kitchen-01/current",
        headers={**bearer(DEVICE_TOKEN), "If-None-Match": '"deadbeef"'},
    )
    assert g.status_code == 200
    assert g.json()["etag"]


def test_etag_changes_when_screen_changes(ctx):
    fallback = ctx.client.get(
        "/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN)
    )
    fallback_etag = fallback.headers["etag"]

    ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_TOKEN),
        json=make_event(id="evt_new", content={"title": "fresh"}),
    )

    after = ctx.client.get(
        "/api/devices/kitchen-01/current",
        headers={**bearer(DEVICE_TOKEN), "If-None-Match": fallback_etag},
    )
    # screen changed -> not 304
    assert after.status_code == 200
    assert after.headers["etag"] != fallback_etag


def test_etag_stable_across_identical_requests(ctx):
    a = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    b = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(DEVICE_TOKEN))
    assert a.headers["etag"] == b.headers["etag"]
