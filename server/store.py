"""SQLite store. No ORM, no Postgres — stdlib ``sqlite3`` only (ponytail: keep
it small and lazy).

Tables match SCHEMA.md sec 6 exactly:
  - tokens(id, token_sha256 UNIQUE, kind, device_id, channels JSON|NULL, rate_per_min, created_at)
  - devices(id, channels JSON, fallback JSON, poll_interval_s, low_batt_interval_s)
  - events(id PK, device, channel, kind, ttl_seconds, layout, content JSON, received_at, raw_size)
  - INDEX idx_events_device(device, channel, received_at)

A fresh sqlite3 connection is opened per call. Connections are cheap, this is a
Pico-scale bridge (one display, low QPS), and per-call connections sidestep
SQLite's cross-thread restrictions under uvicorn's threadpool.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from .schema import validate_fallback


def sha256_hex(text: str) -> str:
    """Hex SHA-256 of a UTF-8 string (used to hash bearer tokens)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- row dataclasses ------------------------------------------------------------


@dataclass(frozen=True)
class TokenRow:
    token_sha256: str
    kind: str                       # 'device' | 'ingest'
    device_id: str
    channels: Optional[list[str]]   # None => all channels (ingest only)
    rate_per_min: int


@dataclass(frozen=True)
class AdminTokenRow:
    """A token as listed by the admin backend. Carries a stable ``id`` handle
    (the sha256 prefix) but NEVER the plaintext — only the stored hash, from
    which the admin layer derives a non-secret preview."""

    id: str                         # stable handle: token_sha256[:16]
    token_sha256: str
    kind: str
    device_id: str
    channels: Optional[list[str]]
    rate_per_min: int


@dataclass(frozen=True)
class DeviceRow:
    id: str
    channels: list[str]
    fallback: dict[str, Any]        # {layout, content}
    poll_interval_s: int
    low_batt_interval_s: int
    # Telemetry piggybacked on the poll (best-effort, all nullable until first seen).
    last_seen_at: Optional[int] = None
    last_batt: Optional[int] = None
    last_rssi: Optional[int] = None
    last_fw: Optional[str] = None
    last_uptime: Optional[int] = None
    # Quiet hours (server-evaluated wall-clock window; the bridge stretches the
    # device's poll_interval inside it, so the clock-less Pico needs no change).
    quiet_start_h: Optional[int] = None
    quiet_end_h: Optional[int] = None
    # One-shot control action (reboot / clear / force_full_refresh) + a monotonic
    # token the device echoes back to ack, so the bridge fires it exactly once.
    pending_action: Optional[str] = None
    action_token: int = 0


@dataclass(frozen=True)
class EventRow:
    id: str
    device: str
    channel: str
    ttl_seconds: int
    layout: str
    content: dict[str, Any]
    received_at: int
    raw_size: int
    kind: str = "base"
    invert: int = 0           # per-event render hint: draw inverted
    full_refresh: int = 0     # per-event render hint: force a full panel refresh


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
  id           INTEGER PRIMARY KEY,
  token_sha256 TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL,
  device_id    TEXT NOT NULL,
  channels     TEXT,
  rate_per_min INTEGER NOT NULL DEFAULT 60,
  created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id                  TEXT PRIMARY KEY,
  channels            TEXT NOT NULL,
  fallback            TEXT NOT NULL,
  poll_interval_s     INTEGER NOT NULL DEFAULT 120,
  low_batt_interval_s INTEGER NOT NULL DEFAULT 600,
  last_seen_at        INTEGER,
  last_batt           INTEGER,
  last_rssi           INTEGER,
  last_fw             TEXT,
  last_uptime         INTEGER,
  quiet_start_h       INTEGER,
  quiet_end_h         INTEGER,
  pending_action      TEXT,
  action_token        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  id          TEXT PRIMARY KEY,
  device      TEXT NOT NULL,
  channel     TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'base',
  ttl_seconds INTEGER NOT NULL,
  layout      TEXT NOT NULL,
  content     TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  raw_size    INTEGER NOT NULL,
  invert       INTEGER NOT NULL DEFAULT 0,
  full_refresh INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_device ON events(device, channel, received_at);

CREATE TABLE IF NOT EXISTS battery_samples (
  device     TEXT NOT NULL,
  at         INTEGER NOT NULL,
  pct        INTEGER NOT NULL,
  on_battery INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_battery_device_at ON battery_samples(device, at);
"""

# Bumped when the schema changes; stamped into meta(schema_version) by init_db so
# diagnostics + future ordered migrations can read it. v1 = original; v2 = events.kind
# added + obsolete priority dropped; v3 = meta + battery_samples + device quiet-hours.
SCHEMA_VERSION = 3


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- lifecycle --------------------------------------------------------------

    # Columns added to a table AFTER its first release. A deployed bridge keeps a
    # persistent SQLite volume, so `CREATE TABLE IF NOT EXISTS` never adds them to
    # an already-existing table -- we ALTER each missing one in on startup
    # (idempotent, PRAGMA-guarded). Names are hardcoded constants, so the ALTER
    # string is injection-safe.
    _EVENT_COLUMNS = (
        ("kind", "TEXT NOT NULL DEFAULT 'base'"),
        ("invert", "INTEGER NOT NULL DEFAULT 0"),
        ("full_refresh", "INTEGER NOT NULL DEFAULT 0"),
    )
    _TELEMETRY_COLUMNS = (
        ("last_seen_at", "INTEGER"),
        ("last_batt", "INTEGER"),
        ("last_rssi", "INTEGER"),
        ("last_fw", "TEXT"),
        ("last_uptime", "INTEGER"),
    )
    # Columns REMOVED after a table's first release -- drop them from an existing DB
    # so the live schema matches the code. `priority` was `NOT NULL` and the current
    # INSERT omits it, so leaving it makes every insert fail the NOT NULL constraint
    # (silently, via INSERT OR IGNORE -> looks like a dedup). SQLite 3.35+ (the 3.11
    # image ships it) supports DROP COLUMN; on an older engine we leave it + log.
    _DEVICE_V3_COLUMNS = (
        ("quiet_start_h", "INTEGER"),
        ("quiet_end_h", "INTEGER"),
        ("pending_action", "TEXT"),
        ("action_token", "INTEGER NOT NULL DEFAULT 0"),
    )
    _OBSOLETE_COLUMNS = (("events", "priority"),)

    def init_db(self) -> None:
        """Create tables + index if missing, then migrate an existing DB: add columns
        introduced after a table's first release, drop columns since removed, and stamp
        the schema version into meta. Safe on every startup."""
        with self._conn() as conn:
            conn.executescript(_SCHEMA_SQL)
            self._add_columns(conn, "events", self._EVENT_COLUMNS)
            self._add_columns(conn, "devices", self._TELEMETRY_COLUMNS)
            self._add_columns(conn, "devices", self._DEVICE_V3_COLUMNS)
            self._drop_columns(conn, self._OBSOLETE_COLUMNS)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _add_columns(conn, table, columns):
        have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        for name, decl in columns:
            if name not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))

    @staticmethod
    def _drop_columns(conn, table_columns):
        for table, name in table_columns:
            have = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            if name in have:
                try:
                    conn.execute("ALTER TABLE %s DROP COLUMN %s" % (table, name))
                except sqlite3.OperationalError as e:
                    print("init_db: could not drop obsolete %s.%s (%s)" % (table, name, e))

    # --- meta (schema version + small key/value state) --------------------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def is_seeded(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()
            return row["n"] > 0

    def seed(
        self,
        devices: list[dict[str, Any]],
        tokens: list[dict[str, Any]],
    ) -> None:
        """Idempotently seed devices + tokens (INSERT OR IGNORE; first write wins).

        ``tokens`` entries carry a PLAINTEXT ``token`` which is hashed here; the
        plaintext is never persisted.

        Each device's ``fallback`` is validated against the same per-layout
        contract as wire events; a bad fallback FAILS FAST here (raises) so a
        misconfigured idle screen can never reach a Pico.
        """
        for d in devices:
            validate_fallback(d.get("fallback"))
        now = int(time.time())
        with self._conn() as conn:
            for d in devices:
                conn.execute(
                    "INSERT OR IGNORE INTO devices"
                    " (id, channels, fallback, poll_interval_s, low_batt_interval_s)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        d["id"],
                        json.dumps(d.get("channels", [])),
                        json.dumps(d["fallback"]),
                        int(d.get("poll_interval_s", 120)),
                        int(d.get("low_batt_interval_s", 600)),
                    ),
                )
            for t in tokens:
                channels = t.get("channels")
                conn.execute(
                    "INSERT OR IGNORE INTO tokens"
                    " (token_sha256, kind, device_id, channels, rate_per_min, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        sha256_hex(t["token"]),
                        t["kind"],
                        t["device_id"],
                        json.dumps(channels) if channels is not None else None,
                        int(t.get("rate_per_min", 60)),
                        now,
                    ),
                )

    # --- tokens -----------------------------------------------------------------

    def get_token_by_hash(self, token_sha256: str) -> Optional[TokenRow]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT token_sha256, kind, device_id, channels, rate_per_min"
                " FROM tokens WHERE token_sha256 = ?",
                (token_sha256,),
            ).fetchone()
        if row is None:
            return None
        channels = json.loads(row["channels"]) if row["channels"] is not None else None
        return TokenRow(
            token_sha256=row["token_sha256"],
            kind=row["kind"],
            device_id=row["device_id"],
            channels=channels,
            rate_per_min=row["rate_per_min"],
        )

    # --- devices ----------------------------------------------------------------

    _DEVICE_COLS = (
        "id, channels, fallback, poll_interval_s, low_batt_interval_s,"
        " last_seen_at, last_batt, last_rssi, last_fw, last_uptime,"
        " quiet_start_h, quiet_end_h, pending_action, action_token"
    )

    @staticmethod
    def _device_from_row(r) -> DeviceRow:
        return DeviceRow(
            id=r["id"],
            channels=json.loads(r["channels"]),
            fallback=json.loads(r["fallback"]),
            poll_interval_s=r["poll_interval_s"],
            low_batt_interval_s=r["low_batt_interval_s"],
            last_seen_at=r["last_seen_at"],
            last_batt=r["last_batt"],
            last_rssi=r["last_rssi"],
            last_fw=r["last_fw"],
            last_uptime=r["last_uptime"],
            quiet_start_h=r["quiet_start_h"],
            quiet_end_h=r["quiet_end_h"],
            pending_action=r["pending_action"],
            action_token=r["action_token"] if r["action_token"] is not None else 0,
        )

    def get_device(self, device_id: str) -> Optional[DeviceRow]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT " + self._DEVICE_COLS + " FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
        return self._device_from_row(row) if row else None

    def set_poll_interval(self, device_id: str, poll_interval_s: int) -> None:
        """Persist a remote deep-sleep interval change (already clamped by caller)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET poll_interval_s = ? WHERE id = ?",
                (poll_interval_s, device_id),
            )

    def update_telemetry(
        self,
        device_id: str,
        *,
        last_seen_at: int,
        last_batt: Optional[int] = None,
        last_rssi: Optional[int] = None,
        last_fw: Optional[str] = None,
        last_uptime: Optional[int] = None,
    ) -> None:
        """Stamp ``last_seen_at`` every poll; COALESCE keeps the prior value for
        any telemetry field that was absent/malformed (passed as None)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET last_seen_at = ?,"
                " last_batt   = COALESCE(?, last_batt),"
                " last_rssi   = COALESCE(?, last_rssi),"
                " last_fw     = COALESCE(?, last_fw),"
                " last_uptime = COALESCE(?, last_uptime)"
                " WHERE id = ?",
                (last_seen_at, last_batt, last_rssi, last_fw, last_uptime, device_id),
            )

    # --- battery history, quiet hours, one-shot actions, diagnostics -----------

    def record_battery_sample(self, device_id: str, at: int, pct: Optional[int],
                              on_battery: bool = True, keep: int = 1000) -> None:
        """Append a battery reading + prune to the newest ``keep`` per device."""
        if pct is None:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO battery_samples(device, at, pct, on_battery) VALUES(?,?,?,?)",
                (device_id, int(at), int(pct), 1 if on_battery else 0),
            )
            conn.execute(
                "DELETE FROM battery_samples WHERE device = ? AND rowid NOT IN ("
                " SELECT rowid FROM battery_samples WHERE device = ? ORDER BY at DESC LIMIT ?)",
                (device_id, device_id, int(keep)),
            )

    def battery_series(self, device_id: str, limit: int = 200) -> list[tuple]:
        """Recent (at, pct, on_battery) samples, OLDEST first (for a sparkline)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT at, pct, on_battery FROM battery_samples WHERE device = ?"
                " ORDER BY at DESC LIMIT ?",
                (device_id, int(limit)),
            ).fetchall()
        return [(r["at"], r["pct"], bool(r["on_battery"])) for r in reversed(rows)]

    def set_quiet_hours(self, device_id: str, start_h: Optional[int],
                        end_h: Optional[int]) -> None:
        """Set/clear the quiet-hours window (None, None clears). Hours are 0..23."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET quiet_start_h = ?, quiet_end_h = ? WHERE id = ?",
                (start_h, end_h, device_id),
            )

    def set_pending_action(self, device_id: str, action: str) -> int:
        """Queue a one-shot action; bumps action_token so the device runs it once.
        Returns the new token."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET pending_action = ?, action_token = action_token + 1"
                " WHERE id = ?",
                (action, device_id),
            )
            row = conn.execute(
                "SELECT action_token FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
            return row["action_token"] if row else 0

    def clear_action(self, device_id: str, token: int) -> None:
        """Clear the pending action iff the device acked the matching token."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE devices SET pending_action = NULL"
                " WHERE id = ? AND action_token = ?",
                (device_id, int(token)),
            )

    def counts(self) -> dict[str, int]:
        """Row counts per table, for diagnostics."""
        with self._conn() as conn:
            return {
                t: conn.execute("SELECT COUNT(*) AS n FROM %s" % t).fetchone()["n"]
                for t in ("devices", "events", "tokens", "battery_samples")
            }

    # --- devices: admin mutations ----------------------------------------------

    def list_devices(self) -> list[DeviceRow]:
        """All devices (for the admin dashboard), ordered by id."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT " + self._DEVICE_COLS + " FROM devices ORDER BY id"
            ).fetchall()
        return [self._device_from_row(r) for r in rows]

    def add_device(
        self,
        *,
        id: str,
        channels: list[str],
        fallback: dict[str, Any],
        poll_interval_s: int = 120,
        low_batt_interval_s: int = 600,
    ) -> bool:
        """Insert a new device. Returns False if the id already exists (caller
        maps that to 409). The caller is responsible for validating ``fallback``
        (via schema.validate_fallback) BEFORE calling so a bad shape 422s first."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO devices"
                " (id, channels, fallback, poll_interval_s, low_batt_interval_s)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    id,
                    json.dumps(channels),
                    json.dumps(fallback),
                    int(poll_interval_s),
                    int(low_batt_interval_s),
                ),
            )
            return cur.rowcount > 0

    def update_device(
        self,
        device_id: str,
        *,
        channels: Optional[list[str]] = None,
        fallback: Optional[dict[str, Any]] = None,
        poll_interval_s: Optional[int] = None,
        low_batt_interval_s: Optional[int] = None,
    ) -> bool:
        """Partial update (PATCH). Only the fields explicitly passed (not None)
        are written. Returns False if the device does not exist."""
        sets: list[str] = []
        vals: list[Any] = []
        if channels is not None:
            sets.append("channels = ?")
            vals.append(json.dumps(channels))
        if fallback is not None:
            sets.append("fallback = ?")
            vals.append(json.dumps(fallback))
        if poll_interval_s is not None:
            sets.append("poll_interval_s = ?")
            vals.append(int(poll_interval_s))
        if low_batt_interval_s is not None:
            sets.append("low_batt_interval_s = ?")
            vals.append(int(low_batt_interval_s))
        if not sets:
            # Nothing to change: succeed iff the device exists.
            return self.get_device(device_id) is not None
        vals.append(device_id)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", vals
            )
            return cur.rowcount > 0

    def delete_device(self, device_id: str) -> bool:
        """Delete a device AND cascade-delete its tokens + events. Returns False
        if the device did not exist."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            conn.execute("DELETE FROM tokens WHERE device_id = ?", (device_id,))
            conn.execute("DELETE FROM events WHERE device = ?", (device_id,))
            return cur.rowcount > 0

    # --- tokens: admin mutations -----------------------------------------------

    def list_tokens(self) -> list[AdminTokenRow]:
        """All tokens for the admin backend. Returns the stored hash + a stable
        id handle; the plaintext is NEVER recoverable (only its sha256 is kept)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT token_sha256, kind, device_id, channels, rate_per_min"
                " FROM tokens ORDER BY created_at, id"
            ).fetchall()
        return [
            AdminTokenRow(
                id=r["token_sha256"][:16],
                token_sha256=r["token_sha256"],
                kind=r["kind"],
                device_id=r["device_id"],
                channels=json.loads(r["channels"]) if r["channels"] is not None else None,
                rate_per_min=r["rate_per_min"],
            )
            for r in rows
        ]

    def add_token(
        self,
        *,
        token_sha256: str,
        kind: str,
        device_id: str,
        channels: Optional[list[str]] = None,
        rate_per_min: int = 60,
    ) -> str:
        """Persist a token by its sha256 ONLY (the plaintext is never stored).
        Returns the stable id handle (the first 16 hex chars of the hash)."""
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tokens"
                " (token_sha256, kind, device_id, channels, rate_per_min, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    token_sha256,
                    kind,
                    device_id,
                    json.dumps(channels) if channels is not None else None,
                    int(rate_per_min),
                    now,
                ),
            )
        return token_sha256[:16]

    def delete_token(self, token_id: str) -> bool:
        """Revoke a token by its id handle (a sha256 prefix; full hash also
        accepted). Returns False if no row matched."""
        with self._conn() as conn:
            # Match ONLY the canonical 16-hex id handle or the full 64-hex hash --
            # never an open-ended prefix (a short id must not delete many tokens).
            cur = conn.execute(
                "DELETE FROM tokens WHERE token_sha256 = ? OR substr(token_sha256, 1, 16) = ?",
                (token_id, token_id),
            )
            return cur.rowcount > 0

    # --- events -----------------------------------------------------------------

    def insert_event(self, event: EventRow) -> bool:
        """Dedup-safe insert. Returns True if stored, False ONLY when the id already
        exists (idempotent no-op; first write wins). Any OTHER integrity failure
        (e.g. a NOT NULL violation from schema drift) is re-raised, not silently
        swallowed -- a bare INSERT OR IGNORE once hid exactly that as a fake dedup."""
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO events"
                    " (id, device, channel, kind, ttl_seconds, layout, content,"
                    "  received_at, raw_size, invert, full_refresh)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.id,
                        event.device,
                        event.channel,
                        event.kind,
                        event.ttl_seconds,
                        event.layout,
                        json.dumps(event.content),
                        event.received_at,
                        event.raw_size,
                        event.invert,
                        event.full_refresh,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                exists = conn.execute(
                    "SELECT 1 FROM events WHERE id = ?", (event.id,)
                ).fetchone()
                if exists:
                    return False        # genuine dedup: id already present
                raise                   # NOT a dedup -> surface the real constraint error

    def events_for_device(self, device_id: str) -> list[EventRow]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, device, channel, kind, ttl_seconds, layout, content,"
                " received_at, raw_size, invert, full_refresh FROM events WHERE device = ?"
                " ORDER BY received_at, rowid",
                (device_id,),
            ).fetchall()
        return [
            EventRow(
                id=r["id"],
                device=r["device"],
                channel=r["channel"],
                kind=r["kind"],
                ttl_seconds=r["ttl_seconds"],
                layout=r["layout"],
                content=json.loads(r["content"]),
                received_at=r["received_at"],
                raw_size=r["raw_size"],
                invert=r["invert"],
                full_refresh=r["full_refresh"],
            )
            for r in rows
        ]

    def events_for_device_recent(
        self, device_id: str, limit: int = 20
    ) -> list[EventRow]:
        """Recent events for a device, NEWEST FIRST (for the admin event log)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, device, channel, kind, ttl_seconds, layout, content,"
                " received_at, raw_size, invert, full_refresh FROM events WHERE device = ?"
                " ORDER BY received_at DESC, id DESC LIMIT ?",
                (device_id, int(limit)),
            ).fetchall()
        return [
            EventRow(
                id=r["id"],
                device=r["device"],
                channel=r["channel"],
                kind=r["kind"],
                ttl_seconds=r["ttl_seconds"],
                layout=r["layout"],
                content=json.loads(r["content"]),
                received_at=r["received_at"],
                raw_size=r["raw_size"],
                invert=r["invert"],
                full_refresh=r["full_refresh"],
            )
            for r in rows
        ]

    def delete_event(self, device_id: str, event_id: str) -> bool:
        """Delete one event (scoped to its device, so an admin can clear the
        screen). Returns False if no matching event existed."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM events WHERE id = ? AND device = ?",
                (event_id, device_id),
            )
            return cur.rowcount > 0
