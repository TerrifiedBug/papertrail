"""Control plane: ETag-busting one-shot actions, quiet-hours interval stretch,
per-event render hints."""

from __future__ import annotations

import time

from server.resolve import QUIET_POLL_INTERVAL_S, in_quiet_hours, resolve_from_events
from server.store import DeviceRow, EventRow


def _device(**over) -> DeviceRow:
    base = dict(id="d", channels=["c"],
                fallback={"layout": "status_card", "content": {"title": "idle"}},
                poll_interval_s=120, low_batt_interval_s=600)
    base.update(over)
    return DeviceRow(**base)


def _evt(id, **over) -> EventRow:
    base = dict(id=id, device="d", channel="c", ttl_seconds=0, layout="status_card",
                content={"title": id}, received_at=1000, raw_size=10, kind="base")
    base.update(over)
    return EventRow(**base)


def test_action_busts_etag_and_is_carried():
    d, evs = _device(), [_evt("e1")]
    plain = resolve_from_events(d, evs, 2000)
    acted = resolve_from_events(d, evs, 2000, action={"name": "force_full_refresh", "token": 3})
    assert acted.etag != plain.etag, "a queued action changes the ETag -> busts a 304"
    assert acted.control["action"] == {"name": "force_full_refresh", "token": 3}
    assert "action" not in plain.control


def test_quiet_hours_stretches_interval():
    now = 1000
    h = time.localtime(now).tm_hour
    inside = _device(quiet_start_h=h, quiet_end_h=(h + 1) % 24)
    outside = _device(quiet_start_h=(h + 2) % 24, quiet_end_h=(h + 3) % 24)
    assert in_quiet_hours(inside, now) is True
    assert in_quiet_hours(outside, now) is False
    assert in_quiet_hours(_device(), now) is False        # no window set
    r = resolve_from_events(inside, [_evt("e1")], now)
    assert r.control["poll_interval"] == QUIET_POLL_INTERVAL_S


def test_event_hints_in_response_and_etag():
    d = _device()
    plain = resolve_from_events(d, [_evt("e1")], 2000)
    inv = resolve_from_events(d, [_evt("e1", invert=1)], 2000)
    assert inv.etag != plain.etag, "an invert hint changes the ETag"
    assert inv.hints == {"invert": True, "full_refresh": False}
    assert plain.hints is None
    assert inv.to_response()["hints"]["invert"] is True
