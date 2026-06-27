"""Screen resolution + ETag.

``current(device)``: among the device's events on a subscribed channel that are
not expired, pick the highest priority (tie-break newest ``received_at``). If
none, fall back to the device's configurable idle screen.

TTL is evaluated lazily here at read time — there is no background sweeper.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from .schema import SCHEMA_VERSION, validate_fallback
from .store import DeviceRow, EventRow, Store

# Last-resort idle screen if a device's configured fallback is somehow invalid at
# read time (it is validated at seed, so this should never fire in practice). We
# ship THIS rather than unvalidated content.
_IDLE_FALLBACK: dict[str, Any] = {
    "layout": "status_card",
    "content": {
        "title": "Papertrail",
        "status": "IDLE",
        "subtitle": "",
        "lines": [],
        "footer": "",
    },
}


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace, UTF-8.

    Same bytes on the server and any verifier.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_etag(
    device: str,
    layout: str,
    content: dict[str, Any],
    control: Optional[dict[str, Any]] = None,
) -> str:
    """sha256 hex of the render-relevant payload ONLY.

    Only these keys are hashed so the ETag is stable across requests while the
    screen is unchanged; ``rendered_at`` / ``source_event_id`` / ``priority`` are
    deliberately excluded (they would churn the ETag every request).

    ``control`` is included so a rare remote control change (e.g. poll_interval)
    busts the Pico's 304 and is applied on the next poll. PONYTAIL: that forces
    ONE extra ePaper redraw on a control change — acceptable; split etags only if
    it ever matters.
    """
    hash_input = {
        "content": content,
        "control": control,
        "device": device,
        "layout": layout,
    }
    return hashlib.sha256(canonical_json(hash_input)).hexdigest()


@dataclass(frozen=True)
class Resolution:
    device: str
    layout: str
    content: dict[str, Any]
    source_event_id: Optional[str]   # None when fallback
    priority: Optional[int]          # None when fallback
    etag: str
    control: Optional[dict[str, Any]] = None   # {"poll_interval": N}
    received_at: Optional[int] = None          # epoch the winning event was ingested

    def to_response(self, rendered_at: Optional[int] = None) -> dict[str, Any]:
        """The GET /current JSON body. ``rendered_at`` is informational and NOT
        part of the ETag hash. ``control`` is additive to pico-paper.v1 — old
        firmware ignores the new top-level field."""
        return {
            "schema": SCHEMA_VERSION,
            "device": self.device,
            "layout": self.layout,
            "content": self.content,
            "control": self.control,
            "source_event_id": self.source_event_id,
            "priority": self.priority,
            "received_at": self.received_at,
            "etag": self.etag,
            "rendered_at": int(time.time()) if rendered_at is None else rendered_at,
        }


def resolve_from_events(
    device: DeviceRow,
    events: list[EventRow],
    now: int,
    fw_version: Optional[str] = None,
) -> Resolution:
    """Pure resolution given a device + its candidate events.

    ``fw_version`` (the latest firmware manifest version) rides ALONGSIDE
    poll_interval in the ``control`` block so OTA piggybacks the existing poll.
    It is deliberately kept OUT of the ETag (only ``etag_control`` is hashed,
    like ``rendered_at``) so a firmware bump never churns a device's 304 cache —
    the device reads ``control.fw`` from the 200 body it would have fetched
    anyway and only runs an OTA check when it differs from its running version.
    """
    # Only poll_interval is hashed into the ETag; fw is additive + non-churning.
    etag_control = {"poll_interval": device.poll_interval_s}
    control = (
        etag_control if fw_version is None else {**etag_control, "fw": fw_version}
    )
    subscribed = set(device.channels)
    candidates = [
        e
        for e in events
        if e.channel in subscribed
        and (e.ttl_seconds is None or e.ttl_seconds <= 0 or now < e.received_at + e.ttl_seconds)
    ]

    if not candidates:
        # Defense in depth: a bad fallback (should be caught at seed) must never
        # ship unvalidated content — swap to a hardcoded valid idle screen. Validate
        # BEFORE reading layout/content so a non-dict fallback (JSON null/array) is
        # caught here too instead of 500-ing on .get()/[].
        try:
            validate_fallback(device.fallback)
            layout = device.fallback["layout"]
            content = device.fallback["content"]
        except (ValueError, TypeError, KeyError):
            layout = _IDLE_FALLBACK["layout"]
            content = _IDLE_FALLBACK["content"]
        return Resolution(
            device=device.id,
            layout=layout,
            content=content,
            source_event_id=None,
            priority=None,
            control=control,
            etag=compute_etag(device.id, layout, content, etag_control),
        )

    # Highest priority wins; tie-break NEWEST received_at.
    chosen = max(candidates, key=lambda e: (e.priority, e.received_at))
    return Resolution(
        device=device.id,
        layout=chosen.layout,
        content=chosen.content,
        source_event_id=chosen.id,
        priority=chosen.priority,
        control=control,
        received_at=chosen.received_at,
        etag=compute_etag(device.id, chosen.layout, chosen.content, etag_control),
    )


def current(
    store: Store,
    device: DeviceRow,
    now: Optional[int] = None,
    fw_version: Optional[str] = None,
) -> Resolution:
    """Resolve the current screen for a device from the store.

    ``fw_version`` is the latest firmware manifest version, injected by the
    caller (app.py) so the ``control`` block can advertise it without resolve
    needing to know how firmware is hashed.
    """
    if now is None:
        now = int(time.time())
    events = store.events_for_device(device.id)
    return resolve_from_events(device, events, now, fw_version=fw_version)
