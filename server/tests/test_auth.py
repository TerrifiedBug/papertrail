"""Auth + scope reject-matrix tests."""

from __future__ import annotations

from .conftest import (
    DEVICE_TOKEN,
    GHOST_TOKEN,
    INGEST_SCOPED,
    INGEST_TOKEN,
    bearer,
    make_event,
)


def test_missing_token_401(ctx):
    r = ctx.client.get("/api/devices/kitchen-01/current")
    assert r.status_code == 401


def test_malformed_header_401(ctx):
    r = ctx.client.get(
        "/api/devices/kitchen-01/current", headers={"Authorization": "Token abc"}
    )
    assert r.status_code == 401


def test_unknown_token_401(ctx):
    r = ctx.client.get(
        "/api/devices/kitchen-01/current", headers=bearer("not-a-real-token")
    )
    assert r.status_code == 401


def test_wrong_kind_device_token_on_ingest_403(ctx):
    r = ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(DEVICE_TOKEN),
        json=make_event(id="x"),
    )
    assert r.status_code == 403


def test_wrong_kind_ingest_token_on_current_403(ctx):
    r = ctx.client.get("/api/devices/kitchen-01/current", headers=bearer(INGEST_TOKEN))
    assert r.status_code == 403


def test_wrong_device_403(ctx):
    # DEVICE_TOKEN is scoped to kitchen-01, but we ask for a different device id.
    r = ctx.client.get("/api/devices/bathroom-09/current", headers=bearer(DEVICE_TOKEN))
    assert r.status_code == 403


def test_unknown_device_404(ctx):
    # GHOST_TOKEN is scoped to ghost-01 which is NOT a seeded device.
    r = ctx.client.get("/api/devices/ghost-01/current", headers=bearer(GHOST_TOKEN))
    assert r.status_code == 404


def test_channel_scope_denied_403(ctx):
    # INGEST_SCOPED may only write home.status; home.alerts is denied.
    r = ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_SCOPED),
        json=make_event(id="scoped_bad", channel="home.alerts"),
    )
    assert r.status_code == 403


def test_channel_scope_allowed_201(ctx):
    r = ctx.client.post(
        "/api/devices/kitchen-01/events",
        headers=bearer(INGEST_SCOPED),
        json=make_event(id="scoped_ok", channel="home.status"),
    )
    assert r.status_code == 201
