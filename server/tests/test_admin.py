"""Admin backend coverage.

Auth posture (503 disabled / 401 unauth), device CRUD, event push/delete (reusing
the public ingest validation), config clamp, and token minting/listing/revocation
(plaintext returned ONCE, only the sha256 stored, preview never leaks the secret).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.store import EventRow, sha256_hex

from .conftest import DEVICE_ID, DEVICE_TOKEN, SEED, bearer, make_event

ADMIN_TOKEN = "admin-secret-xyz"

DEVICES = "/api/admin/devices"
TOKENS = "/api/admin/tokens"


def admin_bearer(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_ctx(tmp_path):
    """An app with the admin token ENABLED (fresh per test, so the auth-failure
    throttle never bleeds across tests)."""
    db = str(tmp_path / "admin.db")
    app = create_app(db_path=db, seed=SEED, max_body_bytes=8192, admin_token=ADMIN_TOKEN)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=app.state.store)


@pytest.fixture
def disabled_ctx(tmp_path):
    """An app with NO admin token -> the whole admin surface is 503 (disabled)."""
    db = str(tmp_path / "disabled.db")
    app = create_app(db_path=db, seed=SEED, max_body_bytes=8192, admin_token=None)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=app.state.store)


def _good_fallback(title: str = "Idle") -> dict:
    return {
        "layout": "status_card",
        "content": {"title": title, "status": "IDLE", "subtitle": "", "lines": [], "footer": ""},
    }


# --- auth posture ---------------------------------------------------------------


def test_admin_missing_token_401(admin_ctx):
    assert admin_ctx.client.get(DEVICES).status_code == 401


def test_admin_wrong_token_401(admin_ctx):
    r = admin_ctx.client.get(DEVICES, headers=admin_bearer("nope"))
    assert r.status_code == 401


def test_admin_disabled_503_even_with_token(disabled_ctx):
    # Unset PAPERTRAIL_ADMIN_TOKEN must 503 (never silently open), regardless of
    # what bearer the caller sends.
    assert disabled_ctx.client.get(DEVICES).status_code == 503
    assert disabled_ctx.client.get(DEVICES, headers=admin_bearer()).status_code == 503


def test_admin_page_served_without_auth(admin_ctx):
    # GET /admin carries no data and needs no token.
    r = admin_ctx.client.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_admin_page_served_even_when_disabled(disabled_ctx):
    assert disabled_ctx.client.get("/admin").status_code == 200


# --- devices: list (online + current) ------------------------------------------


def test_list_devices_shape_online_and_current(admin_ctx):
    body = admin_ctx.client.get(DEVICES, headers=admin_bearer()).json()
    devices = body["devices"]
    assert len(devices) == 1
    d = devices[0]
    assert d["id"] == DEVICE_ID
    assert d["poll_interval_s"] == 120
    assert d["fallback"]["layout"] == "status_card"
    # Never polled yet -> offline; current resolves to the fallback idle screen.
    assert d["online"] is False
    assert d["telemetry"]["last_seen_at"] is None
    assert d["current"]["layout"] == "status_card"
    assert d["current"]["source_event_id"] is None
    assert "etag" in d["current"]


def test_list_devices_online_after_poll(admin_ctx):
    # A real device poll stamps last_seen_at -> the dashboard reports it online.
    admin_ctx.client.get(f"/api/devices/{DEVICE_ID}/current", headers=bearer(DEVICE_TOKEN))
    d = admin_ctx.client.get(DEVICES, headers=admin_bearer()).json()["devices"][0]
    assert d["online"] is True
    assert d["telemetry"]["last_seen_at"] is not None


# --- devices: create -----------------------------------------------------------


def test_create_device_201(admin_ctx):
    r = admin_ctx.client.post(
        DEVICES,
        headers=admin_bearer(),
        json={"id": "office-01", "channels": ["work.status"], "fallback": _good_fallback()},
    )
    assert r.status_code == 201
    assert r.json()["id"] == "office-01"
    ids = [d["id"] for d in admin_ctx.client.get(DEVICES, headers=admin_bearer()).json()["devices"]]
    assert "office-01" in ids


def test_create_device_duplicate_409(admin_ctx):
    r = admin_ctx.client.post(
        DEVICES,
        headers=admin_bearer(),
        json={"id": DEVICE_ID, "channels": [], "fallback": _good_fallback()},
    )
    assert r.status_code == 409


def test_create_device_bad_fallback_422(admin_ctx):
    # metric.value must be a string, not a number -> invalid content -> 422.
    r = admin_ctx.client.post(
        DEVICES,
        headers=admin_bearer(),
        json={
            "id": "bad-01",
            "channels": [],
            "fallback": {"layout": "metric", "content": {"value": 3.42}},
        },
    )
    assert r.status_code == 422


def test_create_device_bad_shape_422(admin_ctx):
    # channels must be a list of strings.
    r = admin_ctx.client.post(
        DEVICES,
        headers=admin_bearer(),
        json={"id": "x", "channels": "not-a-list", "fallback": _good_fallback()},
    )
    assert r.status_code == 422


# --- devices: patch + delete ----------------------------------------------------


def test_patch_device_channels_and_clamp(admin_ctx):
    r = admin_ctx.client.patch(
        f"{DEVICES}/{DEVICE_ID}",
        headers=admin_bearer(),
        json={"channels": ["only.this"], "poll_interval_s": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["channels"] == ["only.this"]
    assert body["poll_interval_s"] == 30  # clamped up to the floor


def test_patch_unknown_device_404(admin_ctx):
    r = admin_ctx.client.patch(f"{DEVICES}/ghost", headers=admin_bearer(), json={"poll_interval_s": 60})
    assert r.status_code == 404


def test_delete_device_cascades_tokens_and_events(admin_ctx):
    # Mint a token + push an event, then delete the device and confirm the cascade.
    mint = admin_ctx.client.post(
        TOKENS,
        headers=admin_bearer(),
        json={"kind": "ingest", "device_id": DEVICE_ID},
    ).json()
    admin_ctx.client.post(
        f"{DEVICES}/{DEVICE_ID}/events",
        headers=admin_bearer(),
        json=make_event(id="evt_cascade"),
    )

    d = admin_ctx.client.delete(f"{DEVICES}/{DEVICE_ID}", headers=admin_bearer())
    assert d.status_code == 200

    # Device gone.
    assert admin_ctx.client.get(DEVICES, headers=admin_bearer()).json()["devices"] == []
    # Its events gone (device unknown now -> 404 on the event log).
    assert admin_ctx.client.get(f"{DEVICES}/{DEVICE_ID}/events", headers=admin_bearer()).status_code == 404
    # Its tokens gone (the minted id no longer lists).
    token_ids = [t["id"] for t in admin_ctx.client.get(TOKENS, headers=admin_bearer()).json()]
    assert mint["id"] not in token_ids


# --- events: push (reuses ingest validation) + delete + list -------------------


def test_push_event_bad_layout_422(admin_ctx):
    r = admin_ctx.client.post(
        f"{DEVICES}/{DEVICE_ID}/events",
        headers=admin_bearer(),
        json={"channel": "home.status", "ttl_seconds": 60, "layout": "nope", "content": {}},
    )
    assert r.status_code == 422


def test_push_event_201_and_resolves(admin_ctx):
    r = admin_ctx.client.post(
        f"{DEVICES}/{DEVICE_ID}/events",
        headers=admin_bearer(),
        json={
            "channel": "home.status",
            "kind": "interrupt",
            "ttl_seconds": 600,
            "layout": "status_card",
            "content": {"title": "Pushed"},
        },
    )
    assert r.status_code == 201
    evt_id = r.json()["id"]
    assert evt_id  # auto-generated id present

    # Shows in the admin event log, newest first, not expired.
    events = admin_ctx.client.get(f"{DEVICES}/{DEVICE_ID}/events", headers=admin_bearer()).json()
    assert events[0]["id"] == evt_id
    assert events[0]["expired"] is False
    assert events[0]["expires_at"] == events[0]["received_at"] + events[0]["ttl_seconds"]

    # And the device's resolved screen now reflects it.
    cur = admin_ctx.client.get(DEVICES, headers=admin_bearer()).json()["devices"][0]["current"]
    assert cur["content"]["title"] == "Pushed"
    assert cur["source_event_id"] == evt_id


def test_delete_event_clears_screen(admin_ctx):
    r = admin_ctx.client.post(
        f"{DEVICES}/{DEVICE_ID}/events",
        headers=admin_bearer(),
        json={"channel": "home.status", "kind": "interrupt", "ttl_seconds": 600,
              "layout": "status_card", "content": {"title": "Temp"}},
    )
    evt_id = r.json()["id"]
    d = admin_ctx.client.delete(f"{DEVICES}/{DEVICE_ID}/events/{evt_id}", headers=admin_bearer())
    assert d.status_code == 200
    # Back to fallback.
    cur = admin_ctx.client.get(DEVICES, headers=admin_bearer()).json()["devices"][0]["current"]
    assert cur["source_event_id"] is None


def test_delete_unknown_event_404(admin_ctx):
    r = admin_ctx.client.delete(f"{DEVICES}/{DEVICE_ID}/events/missing", headers=admin_bearer())
    assert r.status_code == 404


def test_events_newest_first_and_expired_flag(admin_ctx):
    now = int(time.time())
    # Insert directly with controlled timestamps: one stale, one fresh.
    admin_ctx.store.insert_event(EventRow(
        id="old", device=DEVICE_ID, channel="home.status", ttl_seconds=60,
        layout="status_card", content={"title": "old"}, received_at=now - 10_000, raw_size=10, kind="interrupt",
    ))
    admin_ctx.store.insert_event(EventRow(
        id="new", device=DEVICE_ID, channel="home.status", ttl_seconds=600,
        layout="status_card", content={"title": "new"}, received_at=now, raw_size=10, kind="interrupt",
    ))
    events = admin_ctx.client.get(f"{DEVICES}/{DEVICE_ID}/events", headers=admin_bearer()).json()
    assert [e["id"] for e in events] == ["new", "old"]   # newest first
    assert events[0]["expired"] is False
    assert events[1]["expired"] is True


# --- config --------------------------------------------------------------------


def test_admin_config_clamps_and_persists(admin_ctx):
    r = admin_ctx.client.patch(
        f"{DEVICES}/{DEVICE_ID}/config", headers=admin_bearer(), json={"poll_interval": 99999}
    )
    assert r.status_code == 200
    assert r.json()["poll_interval"] == 3600
    # Persisted: the dashboard view reflects it.
    d = admin_ctx.client.get(DEVICES, headers=admin_bearer()).json()["devices"][0]
    assert d["poll_interval_s"] == 3600


def test_admin_config_unknown_key_422(admin_ctx):
    r = admin_ctx.client.patch(
        f"{DEVICES}/{DEVICE_ID}/config", headers=admin_bearer(),
        json={"poll_interval": 60, "evil": 1},
    )
    assert r.status_code == 422


# --- tokens: mint (plaintext once, hash only) + list + revoke ------------------


def test_mint_ingest_token_returns_plaintext_once_and_authenticates(admin_ctx):
    r = admin_ctx.client.post(
        TOKENS, headers=admin_bearer(), json={"kind": "ingest", "device_id": DEVICE_ID}
    )
    assert r.status_code == 201
    minted = r.json()
    assert set(minted) == {"id", "token"}
    plaintext = minted["token"]
    assert plaintext.startswith("pt_ing_")

    # ONLY the sha256 is stored (the plaintext is recoverable nowhere; its hash is).
    assert admin_ctx.store.get_token_by_hash(sha256_hex(plaintext)) is not None

    # The minted token actually authenticates as an ingest token for the device.
    ingest = admin_ctx.client.post(
        f"/api/devices/{DEVICE_ID}/events",
        headers=bearer(plaintext),
        json=make_event(id="evt_minted"),
    )
    assert ingest.status_code == 201


def test_mint_device_token_authenticates_on_current(admin_ctx):
    r = admin_ctx.client.post(
        TOKENS, headers=admin_bearer(), json={"kind": "device", "device_id": DEVICE_ID}
    )
    plaintext = r.json()["token"]
    assert plaintext.startswith("pt_dev_")
    g = admin_ctx.client.get(f"/api/devices/{DEVICE_ID}/current", headers=bearer(plaintext))
    assert g.status_code == 200


def test_mint_bad_kind_422(admin_ctx):
    r = admin_ctx.client.post(
        TOKENS, headers=admin_bearer(), json={"kind": "root", "device_id": DEVICE_ID}
    )
    assert r.status_code == 422


def test_list_tokens_preview_only_never_full(admin_ctx):
    mint = admin_ctx.client.post(
        TOKENS, headers=admin_bearer(), json={"kind": "ingest", "device_id": DEVICE_ID}
    ).json()
    plaintext = mint["token"]

    listed = admin_ctx.client.get(TOKENS, headers=admin_bearer()).json()
    assert len(listed) == len(SEED["tokens"]) + 1
    for t in listed:
        # Never the secret: no plaintext field, and the preview is a short handle.
        assert "token" not in t
        assert t["token_preview"].startswith("pt_")
        assert "…" in t["token_preview"]
        assert plaintext not in t.values()
        assert plaintext not in t["token_preview"]
    # The freshly minted id is present.
    assert mint["id"] in [t["id"] for t in listed]


def test_revoke_token(admin_ctx):
    mint = admin_ctx.client.post(
        TOKENS, headers=admin_bearer(), json={"kind": "device", "device_id": DEVICE_ID}
    ).json()
    plaintext, token_id = mint["token"], mint["id"]

    # It works before revocation...
    assert admin_ctx.client.get(
        f"/api/devices/{DEVICE_ID}/current", headers=bearer(plaintext)
    ).status_code == 200

    d = admin_ctx.client.delete(f"{TOKENS}/{token_id}", headers=admin_bearer())
    assert d.status_code == 200

    # ...and stops working after (and drops out of the listing).
    assert admin_ctx.client.get(
        f"/api/devices/{DEVICE_ID}/current", headers=bearer(plaintext)
    ).status_code == 401
    assert token_id not in [t["id"] for t in admin_ctx.client.get(TOKENS, headers=admin_bearer()).json()]


def test_revoke_unknown_token_404(admin_ctx):
    assert admin_ctx.client.delete(f"{TOKENS}/deadbeefdeadbeef", headers=admin_bearer()).status_code == 404
