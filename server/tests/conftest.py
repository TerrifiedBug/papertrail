"""Shared pytest fixtures + seed for the papertrail server tests."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any

import pytest

# Put the repo root (parent of `server/`) on sys.path so `import server.*` works
# regardless of the directory pytest is invoked from.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from server.app import create_app  # noqa: E402

DEVICE_ID = "kitchen-01"
CHANNELS = ["home.status", "home.alerts", "home.tasks"]
FALLBACK = {
    "layout": "status_card",
    "content": {
        "title": "Papertrail",
        "status": "IDLE",
        "subtitle": "Waiting for updates",
        "lines": ["No active messages"],
        "footer": "papertrail",
    },
}

DEVICE_TOKEN = "device-secret-kitchen"
INGEST_TOKEN = "ingest-secret-kitchen"
INGEST_SCOPED = "ingest-secret-scoped"        # ingest, only home.status
RATE_TOKEN = "device-secret-rate1"            # device, rate_per_min=1
GHOST_TOKEN = "device-secret-ghost"           # device token for a non-existent device

SEED: dict[str, Any] = {
    "devices": [
        {
            "id": DEVICE_ID,
            "channels": CHANNELS,
            "fallback": FALLBACK,
            "poll_interval_s": 120,
            "low_batt_interval_s": 600,
        }
    ],
    "tokens": [
        {"token": DEVICE_TOKEN, "kind": "device", "device_id": DEVICE_ID, "rate_per_min": 1000},
        {"token": INGEST_TOKEN, "kind": "ingest", "device_id": DEVICE_ID, "channels": None, "rate_per_min": 1000},
        {"token": INGEST_SCOPED, "kind": "ingest", "device_id": DEVICE_ID, "channels": ["home.status"], "rate_per_min": 1000},
        {"token": RATE_TOKEN, "kind": "device", "device_id": DEVICE_ID, "rate_per_min": 1},
        {"token": GHOST_TOKEN, "kind": "device", "device_id": "ghost-01", "rate_per_min": 1000},
    ],
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_event(
    *,
    id: str,
    layout: str = "status_card",
    channel: str = "home.status",
    priority: int = 50,
    ttl_seconds: int = 900,
    kind: str = "base",
    content: dict[str, Any] | None = None,
    device: str = DEVICE_ID,
) -> dict[str, Any]:
    if content is None:
        content = {"title": "T", "status": "OK", "subtitle": "s", "lines": [], "footer": "f"}
    return {
        "schema": "pico-paper.v1",
        "id": id,
        "device": device,
        "channel": channel,
        "kind": kind,
        "priority": priority,
        "ttl_seconds": ttl_seconds,
        "layout": layout,
        "content": content,
    }


@pytest.fixture
def ctx(tmp_path):
    db = str(tmp_path / "test.db")
    app = create_app(db_path=db, seed=SEED, max_body_bytes=8192)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            store=app.state.store,
            device_id=DEVICE_ID,
        )
