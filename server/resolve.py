"""Screen resolution + ETag.

``current(device)`` resolves in layers: newest live interrupt, else newest base,
else the device fallback. TTL is evaluated lazily here at read time — there is
no background sweeper.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from .schema import INTERRUPT_DEFAULT_TTL, SCHEMA_VERSION, validate_fallback
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


# During a device's quiet-hours window the bridge stretches its poll_interval to
# this (the clock-less Pico just honors the interval). A FIXED value (not a
# countdown) so the ETag is stable across polls inside the window — no churn.
QUIET_POLL_INTERVAL_S = 3600

# One-shot device actions the bridge can queue (delivered via the control block;
# the action rides INSIDE the ETag so it busts a 304 and reaches a stuck device).
DEVICE_ACTIONS = ("reboot", "clear", "force_full_refresh")


def in_quiet_hours(device: DeviceRow, now: int) -> bool:
    """True when server-local `now` is inside the device's quiet window. PURE.
    The window may wrap midnight (e.g. start 23, end 7)."""
    s, e = device.quiet_start_h, device.quiet_end_h
    if s is None or e is None or s == e:
        return False
    h = time.localtime(now).tm_hour
    return (s <= h < e) if s < e else (h >= s or h < e)


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
    hints: Optional[dict[str, Any]] = None,
) -> str:
    """sha256 hex of the render-relevant payload ONLY.

    Only these keys are hashed so the ETag is stable across requests while the
    screen is unchanged; ``rendered_at`` / ``source_event_id`` / ``kind`` are
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
    # Only added when present, so screens without hints keep their existing ETag.
    if hints:
        hash_input["hints"] = hints
    return hashlib.sha256(canonical_json(hash_input)).hexdigest()


@dataclass(frozen=True)
class Resolution:
    device: str
    layout: str
    content: dict[str, Any]
    source_event_id: Optional[str]   # None when fallback
    etag: str
    control: Optional[dict[str, Any]] = None   # {"poll_interval": N, "fw"?, "action"?}
    received_at: Optional[int] = None          # epoch the winning event was ingested
    kind: Optional[str] = None        # None when fallback
    hints: Optional[dict[str, Any]] = None     # {"invert", "full_refresh"} or None

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
            "kind": self.kind,
            "hints": self.hints,
            "received_at": self.received_at,
            "etag": self.etag,
            "rendered_at": int(time.time()) if rendered_at is None else rendered_at,
        }


def _event_hints(e: EventRow) -> Optional[dict[str, bool]]:
    """Per-event render hints, or None when neither is set (keeps the ETag stable
    for the common no-hint case)."""
    inv = bool(getattr(e, "invert", 0))
    full = bool(getattr(e, "full_refresh", 0))
    return {"invert": inv, "full_refresh": full} if (inv or full) else None


def resolve_from_events(
    device: DeviceRow,
    events: list[EventRow],
    now: int,
    fw_version: Optional[str] = None,
    action: Optional[dict[str, Any]] = None,
) -> Resolution:
    """Pure resolution given a device + its candidate events.

    ``fw_version`` (the latest firmware manifest version) rides ALONGSIDE
    poll_interval in the ``control`` block so OTA piggybacks the existing poll.
    It is deliberately kept OUT of the ETag (only ``etag_control`` is hashed,
    like ``rendered_at``) so a firmware bump never churns a device's 304 cache —
    the device reads ``control.fw`` from the 200 body it would have fetched
    anyway and only runs an OTA check when it differs from its running version.
    """
    # Effective poll interval: stretched to QUIET_POLL_INTERVAL_S during the
    # device's quiet-hours window (the clock-less Pico just honors what we send).
    interval = device.poll_interval_s
    if in_quiet_hours(device, now):
        interval = max(interval, QUIET_POLL_INTERVAL_S)
    # etag_control is hashed (a change busts the 304); a queued one-shot `action`
    # is hashed too so it reaches a device otherwise stuck on 304. fw rides
    # ALONGSIDE but is NOT hashed (a firmware bump must not churn the 304 cache).
    etag_control: dict[str, Any] = {"poll_interval": interval}
    if action:
        etag_control["action"] = action
    control = etag_control if fw_version is None else {**etag_control, "fw": fw_version}
    subscribed = set(device.channels)

    def live_interrupt(e: EventRow) -> bool:
        # An interrupt is ALWAYS temporary: a missing/<=0 ttl falls back to the
        # default lifetime, never "permanent" (permanence is base's job). Ingest
        # already coerces an interrupt's omitted/0 ttl -> INTERRUPT_DEFAULT_TTL;
        # this mirrors it so a directly-built row can't be permanent either.
        if e.channel not in subscribed or e.kind != "interrupt":
            return False
        ttl = e.ttl_seconds if (e.ttl_seconds and e.ttl_seconds > 0) else INTERRUPT_DEFAULT_TTL
        return now < e.received_at + ttl

    interrupts = [e for e in events if live_interrupt(e)]
    bases = [e for e in events if e.channel in subscribed and e.kind == "base"]

    candidates = interrupts or bases

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
            control=control,
            etag=compute_etag(device.id, layout, content, etag_control),
        )

    # Newest interrupt wins; when no interrupt is live, newest base wins. If two
    # events share the same timestamp (common in tests and bursty writes), prefer
    # the later row from storage so "last write wins" remains true.
    chosen = max(enumerate(candidates), key=lambda item: (item[1].received_at, item[0]))[1]
    hints = _event_hints(chosen)
    return Resolution(
        device=device.id,
        layout=chosen.layout,
        content=chosen.content,
        source_event_id=chosen.id,
        kind=chosen.kind,
        control=control,
        received_at=chosen.received_at,
        hints=hints,
        etag=compute_etag(device.id, chosen.layout, chosen.content, etag_control, hints),
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
    action = None
    if device.pending_action:
        action = {"name": device.pending_action, "token": device.action_token}
    return resolve_from_events(
        device, events, now, fw_version=fw_version, action=action
    )
