"""SQLite store. No ORM, no Postgres — stdlib ``sqlite3`` only (ponytail: keep
it small and lazy).

Tables match SCHEMA.md sec 6 exactly:
  - tokens(id, token_sha256 UNIQUE, kind, device_id, channels JSON|NULL, rate_per_min, created_at)
  - devices(id, channels JSON, fallback JSON, poll_interval_s, low_batt_interval_s)
  - events(id PK, device, channel, kind, priority, ttl_seconds, layout, content JSON, received_at, raw_size)
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


@dataclass(frozen=True)
class EventRow:
    id: str
    device: str
    channel: str
    priority: int
    ttl_seconds: int
    layout: str
    content: dict[str, Any]
    received_at: int
    raw_size: int
    kind: str = "base"


_SCHEMA_SQL = """
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
  last_uptime         INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  id          TEXT PRIMARY KEY,
  device      TEXT NOT NULL,
  channel     TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'base',
  priority    INTEGER NOT NULL,
  ttl_seconds INTEGER NOT NULL,
  layout      TEXT NOT NULL,
  content     TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  raw_size    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_device ON events(device, channel, received_at);
"""


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

    # Columns added after v1; ALTER them onto pre-existing rows.
    _EVENT_COLUMNS = (("kind", "TEXT NOT NULL DEFAULT 'base'"),)

    # Telemetry columns added after v1; ALTER them onto pre-existing devices rows.
    _TELEMETRY_COLUMNS = (
        ("last_seen_at", "INTEGER"),
        ("last_batt", "INTEGER"),
        ("last_rssi", "INTEGER"),
        ("last_fw", "TEXT"),
        ("last_uptime", "INTEGER"),
    )

    def init_db(self) -> None:
        """Create tables + index if missing. Safe to call on every startup."""
        with self._conn() as conn:
            conn.executescript(_SCHEMA_SQL)
            # Migrate older DBs that predate the telemetry columns (names are
            # hardcoded constants, so the f-string ALTER is injection-safe).
            event_existing = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
            for name, decl in self._EVENT_COLUMNS:
                if name not in event_existing:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")

            existing = {r["name"] for r in conn.execute("PRAGMA table_info(devices)")}
            for name, decl in self._TELEMETRY_COLUMNS:
                if name not in existing:
                    conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {decl}")

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

    def get_device(self, device_id: str) -> Optional[DeviceRow]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, channels, fallback, poll_interval_s, low_batt_interval_s,"
                " last_seen_at, last_batt, last_rssi, last_fw, last_uptime"
                " FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        return DeviceRow(
            id=row["id"],
            channels=json.loads(row["channels"]),
            fallback=json.loads(row["fallback"]),
            poll_interval_s=row["poll_interval_s"],
            low_batt_interval_s=row["low_batt_interval_s"],
            last_seen_at=row["last_seen_at"],
            last_batt=row["last_batt"],
            last_rssi=row["last_rssi"],
            last_fw=row["last_fw"],
            last_uptime=row["last_uptime"],
        )

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

    # --- devices: admin mutations ----------------------------------------------

    def list_devices(self) -> list[DeviceRow]:
        """All devices (for the admin dashboard), ordered by id."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, channels, fallback, poll_interval_s, low_batt_interval_s,"
                " last_seen_at, last_batt, last_rssi, last_fw, last_uptime"
                " FROM devices ORDER BY id"
            ).fetchall()
        return [
            DeviceRow(
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
            )
            for r in rows
        ]

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
        """Dedup-safe insert. Returns True if stored, False if the id already
        existed (idempotent no-op; first write wins, never overwrite)."""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events"
                " (id, device, channel, kind, priority, ttl_seconds, layout, content,"
                "  received_at, raw_size)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.device,
                    event.channel,
                    event.kind,
                    event.priority,
                    event.ttl_seconds,
                    event.layout,
                    json.dumps(event.content),
                    event.received_at,
                    event.raw_size,
                ),
            )
            return cur.rowcount > 0

    def events_for_device(self, device_id: str) -> list[EventRow]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, device, channel, kind, priority, ttl_seconds, layout, content,"
                " received_at, raw_size FROM events WHERE device = ?",
                (device_id,),
            ).fetchall()
        return [
            EventRow(
                id=r["id"],
                device=r["device"],
                channel=r["channel"],
                kind=r["kind"],
                priority=r["priority"],
                ttl_seconds=r["ttl_seconds"],
                layout=r["layout"],
                content=json.loads(r["content"]),
                received_at=r["received_at"],
                raw_size=r["raw_size"],
            )
            for r in rows
        ]

    def events_for_device_recent(
        self, device_id: str, limit: int = 20
    ) -> list[EventRow]:
        """Recent events for a device, NEWEST FIRST (for the admin event log)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, device, channel, kind, priority, ttl_seconds, layout, content,"
                " received_at, raw_size FROM events WHERE device = ?"
                " ORDER BY received_at DESC, id DESC LIMIT ?",
                (device_id, int(limit)),
            ).fetchall()
        return [
            EventRow(
                id=r["id"],
                device=r["device"],
                channel=r["channel"],
                kind=r["kind"],
                priority=r["priority"],
                ttl_seconds=r["ttl_seconds"],
                layout=r["layout"],
                content=json.loads(r["content"]),
                received_at=r["received_at"],
                raw_size=r["raw_size"],
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
