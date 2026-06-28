#!/usr/bin/env python3
"""papertrail MCP server.

Exposes the papertrail webhook->e-paper bridge as MCP tools so an agent can push
screens to a device. Builds the frozen ``pico-paper.v1`` event envelope and POSTs
it to ``{PAPERTRAIL_URL}/api/devices/{device}/events`` with a Bearer ingest token.

Transport: stdio (FastMCP default).

Environment:
  PAPERTRAIL_URL          base URL of the bridge, e.g. http://192.168.1.50:8000
  PAPERTRAIL_TOKEN        an ingest token (may be channel-scoped)
  PAPERTRAIL_ADMIN_TOKEN  optional; required only for list_devices()

The display font is ASCII-only: non-ASCII (e.g. the degree sign) is dropped on
the device, so prefer plain ASCII in titles/messages.
"""

import os
import time
import uuid
from typing import Any, Optional, Union

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("papertrail")

SCHEMA = "pico-paper.v1"
LAYOUTS = ("status_card", "alert", "list", "metric", "qr")
KINDS = ("base", "interrupt")
SEVERITIES = ("low", "med", "high")
TTL_CAP = 604800  # 7 days, server-clamped
HTTP_TIMEOUT = 10.0


# --- helpers ---------------------------------------------------------------

def _base_url() -> str:
    url = os.environ.get("PAPERTRAIL_URL", "").strip()
    if not url:
        raise RuntimeError(
            "PAPERTRAIL_URL is not set (e.g. http://192.168.1.50:8000)."
        )
    return url.rstrip("/")


def _ingest_token() -> str:
    tok = os.environ.get("PAPERTRAIL_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("PAPERTRAIL_TOKEN is not set (an ingest token).")
    return tok


def _new_id(layout: str) -> str:
    """A fresh, unique id per distinct update (re-posting an id is a dedup no-op)."""
    return f"{layout}-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _post_event(
    device: str,
    channel: str,
    layout: str,
    content: dict,
    kind: str,
    ttl_seconds: Optional[int],
    id: Optional[str],
) -> str:
    """Validate, build the envelope, POST it, and return a short status line."""
    if layout not in LAYOUTS:
        raise ValueError(
            f"layout must be one of {', '.join(LAYOUTS)}; got {layout!r}"
        )
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}; got {kind!r}")
    if not isinstance(content, dict):
        raise ValueError("content must be a JSON object (dict).")

    event_id = id or _new_id(layout)
    body: dict = {
        "schema": SCHEMA,
        "id": event_id,
        "device": device,
        "channel": channel,
        "kind": kind,
        "layout": layout,
        "content": content,
    }
    # ttl_seconds is interrupt-only; base ignores it, so do not send it.
    if kind == "interrupt" and ttl_seconds is not None:
        body["ttl_seconds"] = max(0, min(int(ttl_seconds), TTL_CAP))

    url = f"{_base_url()}/api/devices/{device}/events"
    headers = {
        "Authorization": f"Bearer {_ingest_token()}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=HTTP_TIMEOUT)
    except httpx.RequestError as exc:
        raise RuntimeError(f"could not reach papertrail at {url}: {exc}") from exc

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"papertrail {resp.status_code}: {resp.text}")

    verb = "duplicate (no-op)" if resp.status_code == 200 else "stored"
    return f"{layout} -> {device}/{channel}: {verb} (id={event_id})"


# --- tools -----------------------------------------------------------------

@mcp.tool()
def send_screen(
    device: str,
    channel: str,
    layout: str,
    content: dict,
    kind: str = "base",
    ttl_seconds: Optional[int] = None,
    id: Optional[str] = None,
) -> str:
    """Send any of the 5 layouts to a device.

    Builds the pico-paper.v1 envelope (auto-generates a unique id if none given)
    and POSTs it. ``layout`` must be one of: status_card, alert, list, metric, qr.
    ``kind`` is "base" (persistent; sticks until replaced) or "interrupt"
    (temporary overlay that auto-clears after ttl_seconds). ``ttl_seconds`` is
    included only for interrupts. Returns a short status string; raises on a
    non-2xx response (with the bridge's error body, which explains validation
    failures). See SCHEMA.md for each layout's content shape.
    """
    return _post_event(device, channel, layout, content, kind, ttl_seconds, id)


@mcp.tool()
def send_status_card(
    device: str,
    channel: str,
    title: str,
    status: str = "",
    subtitle: str = "",
    lines: Optional[list] = None,
    footer: str = "",
    kind: str = "base",
    ttl_seconds: Optional[int] = None,
) -> str:
    """Convenience wrapper for the status_card layout.

    Heading + status word + a few body lines. Render caps: title ~12, status 8,
    subtitle/lines/footer 30 chars (lines: up to 5). Defaults to a persistent
    "base" screen; pass kind="interrupt" + ttl_seconds to auto-clear.
    """
    content = {
        "title": title,
        "status": status,
        "subtitle": subtitle,
        "lines": lines or [],
        "footer": footer,
    }
    return _post_event(device, channel, "status_card", content, kind, ttl_seconds, None)


@mcp.tool()
def send_alert(
    device: str,
    channel: str,
    title: str,
    message: str,
    severity: str = "high",
    footer: str = "",
    ttl_seconds: int = 600,
) -> str:
    """Send an alert as a temporary interrupt overlay (auto-clears after ttl_seconds).

    severity is "low" | "med" | "high"; "high" draws a red banner/frame on the
    tri-color panel. Render caps: title 15, message wraps ~4x30, footer 30.
    """
    if severity not in SEVERITIES:
        raise ValueError(
            f"severity must be one of {', '.join(SEVERITIES)}; got {severity!r}"
        )
    content = {
        "severity": severity,
        "title": title,
        "message": message,
        "footer": footer,
    }
    return _post_event(device, channel, "alert", content, "interrupt", ttl_seconds, None)


@mcp.tool()
def send_metric(
    device: str,
    channel: str,
    label: str,
    value: str,
    unit: str = "",
    trend: str = "",
    footer: str = "",
    kind: str = "base",
    ttl_seconds: Optional[int] = None,
) -> str:
    """Convenience wrapper for the metric layout (one big number).

    value is a string (preserves formatting). Render caps: label 30, value 7,
    unit 4, trend 30 (ASCII tokens like "UP +0.4 vs 1h"), footer 30.
    """
    content = {
        "label": label,
        "value": str(value),
        "unit": unit,
        "trend": trend,
        "footer": footer,
    }
    return _post_event(device, channel, "metric", content, kind, ttl_seconds, None)


@mcp.tool()
def list_devices() -> Union[list, str]:
    """List known devices (admin-gated).

    Requires PAPERTRAIL_ADMIN_TOKEN; ingest tokens cannot list devices. Returns
    the parsed JSON from GET {PAPERTRAIL_URL}/api/admin/devices, or a clear
    message if the admin token is not configured.
    """
    admin = os.environ.get("PAPERTRAIL_ADMIN_TOKEN", "").strip()
    if not admin:
        return (
            "PAPERTRAIL_ADMIN_TOKEN is not set. Device listing is admin-gated; "
            "an ingest token cannot list devices. Set PAPERTRAIL_ADMIN_TOKEN to "
            "use this tool."
        )
    url = f"{_base_url()}/api/admin/devices"
    headers = {"Authorization": f"Bearer {admin}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    except httpx.RequestError as exc:
        raise RuntimeError(f"could not reach papertrail at {url}: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"papertrail {resp.status_code}: {resp.text}")
    return resp.json()


if __name__ == "__main__":
    mcp.run()
