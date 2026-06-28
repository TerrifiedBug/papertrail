"""Regression: init_db must migrate an EXISTING database by ALTERing in columns
introduced after a table's first release. A deployed bridge keeps its SQLite
volume, so `CREATE TABLE IF NOT EXISTS` alone never adds them -- which is exactly
how dropping the migration produced the prod break `no such column: kind`."""

from __future__ import annotations

import sqlite3

from server.store import Store


def _make_pre_kind_db(path: str) -> None:
    """An events/devices schema from before the `kind` + telemetry columns existed."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE events ("
        " id TEXT PRIMARY KEY, device TEXT NOT NULL, channel TEXT NOT NULL,"
        " priority INTEGER NOT NULL, ttl_seconds INTEGER NOT NULL, layout TEXT NOT NULL,"
        " content TEXT NOT NULL, received_at INTEGER NOT NULL, raw_size INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE devices ("
        " id TEXT PRIMARY KEY, channels TEXT NOT NULL, fallback TEXT NOT NULL,"
        " poll_interval_s INTEGER NOT NULL DEFAULT 120,"
        " low_batt_interval_s INTEGER NOT NULL DEFAULT 600)"
    )
    conn.execute(
        "INSERT INTO events VALUES"
        " ('e1','kitchen-01','home.status',50,0,'status_card','{\"title\":\"hi\"}',1000,10)"
    )
    conn.commit()
    conn.close()


def test_init_db_migrates_existing_db(tmp_path):
    db = str(tmp_path / "old.db")
    _make_pre_kind_db(db)

    store = Store(db)
    store.init_db()                         # must ALTER missing columns in, not raise

    # the read path that 500'd in prod (SELECT ... kind ...) now works, and the
    # ALTER's DEFAULT backfills the pre-existing row.
    rows = store.events_for_device("kitchen-01")
    assert len(rows) == 1
    assert rows[0].kind == "base"

    # telemetry columns were added to the old devices table too
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(devices)")}
    assert {"last_seen_at", "last_batt", "last_rssi", "last_fw", "last_uptime"} <= cols

    # the obsolete NOT NULL `priority` column is DROPPED, so a current-shape INSERT
    # (which omits priority) succeeds instead of silently failing the constraint and
    # being swallowed by INSERT OR IGNORE -- the prod "events don't store" bug.
    ev_cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(events)")}
    assert "priority" not in ev_cols, "obsolete priority column dropped"
    from server.store import EventRow
    stored = store.insert_event(EventRow(
        id="new1", device="kitchen-01", channel="home.status", ttl_seconds=0,
        layout="status_card", content={"title": "x"}, received_at=2000, raw_size=10, kind="base",
    ))
    assert stored is True, "insert succeeds after the obsolete column is dropped"
    assert len(store.events_for_device("kitchen-01")) == 2

    # idempotent: a second init_db is a no-op (columns already migrated)
    store.init_db()
    assert len(store.events_for_device("kitchen-01")) == 2
