"""Store layer for the v3 features: schema_version stamping, honest inserts,
battery history, quiet hours, one-shot actions, diagnostics counts."""

from __future__ import annotations

from server.store import SCHEMA_VERSION, EventRow, Store

_FALLBACK = {"layout": "status_card",
             "content": {"title": "idle", "status": "", "subtitle": "", "lines": [], "footer": ""}}


def _seeded(tmp_path) -> Store:
    s = Store(str(tmp_path / "s.db"))
    s.init_db()
    s.seed(devices=[{"id": "d1", "channels": ["c"], "fallback": _FALLBACK}], tokens=[])
    return s


def _evt(id, **over) -> EventRow:
    base = dict(id=id, device="d1", channel="c", ttl_seconds=0, layout="status_card",
                content={"title": id}, received_at=1000, raw_size=10, kind="base")
    base.update(over)
    return EventRow(**base)


def test_schema_version_stamped(tmp_path):
    s = _seeded(tmp_path)
    assert s.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert s.get_meta("nope", "x") == "x"


def test_insert_dedup_vs_store(tmp_path):
    s = _seeded(tmp_path)
    assert s.insert_event(_evt("e1")) is True       # stored
    assert s.insert_event(_evt("e1")) is False      # genuine dedup (same id)
    assert len(s.events_for_device("d1")) == 1


def test_battery_series_and_prune(tmp_path):
    s = _seeded(tmp_path)
    for i in range(5):
        s.record_battery_sample("d1", 1000 + i, 90 - i, on_battery=True)
    s.record_battery_sample("d1", 2000, None)        # None -> ignored
    series = s.battery_series("d1")
    assert len(series) == 5
    assert series[0][0] < series[-1][0]              # oldest first
    assert series[-1][1] == 86
    for i in range(10):
        s.record_battery_sample("d1", 3000 + i, 50, keep=3)
    assert len(s.battery_series("d1")) == 3          # pruned to newest `keep`


def test_quiet_hours_and_actions(tmp_path):
    s = _seeded(tmp_path)
    s.set_quiet_hours("d1", 23, 7)
    d = s.get_device("d1")
    assert (d.quiet_start_h, d.quiet_end_h) == (23, 7)

    t1 = s.set_pending_action("d1", "reboot")
    assert t1 == 1
    assert s.get_device("d1").pending_action == "reboot"
    s.clear_action("d1", 999)                        # wrong token -> no-op
    assert s.get_device("d1").pending_action == "reboot"
    s.clear_action("d1", t1)                         # correct token -> cleared
    assert s.get_device("d1").pending_action is None


def test_counts(tmp_path):
    s = _seeded(tmp_path)
    s.insert_event(_evt("e1"))
    c = s.counts()
    assert c["devices"] == 1 and c["events"] == 1 and "battery_samples" in c
