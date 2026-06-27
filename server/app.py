"""FastAPI app: the always-on Pico <-> webhook bridge.

Endpoints
  POST  /api/devices/{id}/events   ingest token; validate + 8 KiB cap + store
  GET   /api/devices/{id}/current  device token; resolved screen + control + ETag (304);
                                   optional telemetry query params (batt/rssi/fw/up)
  PATCH /api/devices/{id}/config   device token; set poll_interval (clamped [30,3600])
  GET   /api/devices/{id}/status   device token; stored telemetry + last_seen_at
  GET   /healthz                   liveness, no auth

Reject matrix (SCHEMA.md sec 5):
  401 missing/malformed/unknown token
  403 valid token, wrong device / wrong kind / disallowed channel
  404 unknown :id device
  413 raw body > 8 KiB
  422 bad schema / unknown layout / invalid content / qr_data > 512
  429 rate limit
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import ValidationError

from .auth import AuthError, RateLimiter, authenticate, authorize_channel
from .resolve import current
from .schema import CONTENT_MODELS, SCHEMA_VERSION, validate_envelope
from .store import EventRow, Store

DEFAULT_MAX_BODY_BYTES = 8192        # 8 KiB hard cap -> 413

POLL_INTERVAL_MIN = 30               # seconds; remote deep-sleep clamp floor
POLL_INTERVAL_MAX = 3600             # seconds; remote deep-sleep clamp ceiling

_FW_RE = re.compile(r"^[A-Za-z0-9._-]+$")   # telemetry fw charset

# OpenAPI metadata (purely descriptive; does not change behavior).
_TAGS_METADATA = [
    {"name": "ingest", "description": "POST webhook events into the bridge (ingest token)."},
    {"name": "device", "description": "Pico-facing screen poll + remote device control (device token)."},
    {"name": "telemetry", "description": "Stored device telemetry for the dashboard (device token)."},
    {"name": "ops", "description": "Operational / liveness endpoints (no auth)."},
]
_SERVERS = [
    {
        "url": "https://papertrail.example.com",
        "description": "Caddy HTTPS ingest (internet-facing POST /events).",
    },
    {
        "url": "http://papertrail.lan:8000",
        "description": "LAN HTTP poll (Pico GET /current, direct, no TLS).",
    },
]
_DESCRIPTION = (
    "Always-on Pico <-> webhook bridge for the **pico-paper.v1** wire contract. "
    "Ingest validated events, resolve the current screen per device, and serve it "
    "to a Waveshare Pico ePaper over LAN HTTP with ETag / If-None-Match polling. "
    "The poll piggybacks optional telemetry and carries a `control` block for "
    "remote settings (e.g. poll_interval)."
)
def _ingest_examples() -> dict[str, Any]:
    """Per-layout POST /events request examples, sourced from each content
    model's ``json_schema_extra`` (single source of truth) so the OpenAPI doc
    stays in lock-step with the validators."""
    base = {
        "schema": SCHEMA_VERSION,
        "id": "evt_example_0001",
        "device": "kitchen-01",
        "channel": "home.status",
        "priority": 50,
        "ttl_seconds": 900,
    }
    out: dict[str, Any] = {}
    for layout, model in CONTENT_MODELS.items():
        example = (model.model_config.get("json_schema_extra") or {}).get("example")
        if example is None:
            continue
        out[layout] = {
            "summary": f"{layout} event",
            "value": {**base, "layout": layout, "content": example},
        }
    return out


_INGEST_OPENAPI_EXTRA = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"examples": _ingest_examples()}},
    }
}

# Example GET /current body (resolved fallback + additive control block).
_CURRENT_EXAMPLE = {
    "schema": SCHEMA_VERSION,
    "device": "kitchen-01",
    "layout": "status_card",
    "content": {
        "title": "Papertrail",
        "status": "IDLE",
        "subtitle": "Waiting for updates",
        "lines": ["No active messages"],
        "footer": "papertrail",
    },
    "control": {"poll_interval": 120},
    "source_event_id": None,
    "priority": None,
    "etag": "c0ffee...",
    "rendered_at": 1750000000,
}


# --- seeding --------------------------------------------------------------------


def _load_seed_file(path: str) -> Optional[dict[str, Any]]:
    """Load a JSON seed file: {"devices": [...], "tokens": [...]} or None."""
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _seed_store(store: Store, seed: Optional[dict[str, Any]]) -> None:
    if not seed:
        return
    if store.is_seeded():
        return
    store.seed(seed.get("devices", []), seed.get("tokens", []))


# --- app factory ----------------------------------------------------------------


def create_app(
    db_path: Optional[str] = None,
    seed: Optional[dict[str, Any]] = None,
    max_body_bytes: Optional[int] = None,
) -> FastAPI:
    db_path = db_path or os.environ.get("PAPERTRAIL_DB", "papertrail.db")
    max_body = int(
        max_body_bytes
        if max_body_bytes is not None
        else os.environ.get("PAPERTRAIL_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
    )
    seed_file = os.environ.get("PAPERTRAIL_SEED_FILE", "seed.json")

    store = Store(db_path)
    limiter = RateLimiter()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        store.init_db()
        _seed_store(store, seed if seed is not None else _load_seed_file(seed_file))
        yield

    app = FastAPI(
        title="papertrail bridge",
        version="1.0.0",
        description=_DESCRIPTION,
        openapi_tags=_TAGS_METADATA,
        servers=_SERVERS,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.limiter = limiter
    app.state.max_body_bytes = max_body

    # --- helpers ----------------------------------------------------------------

    def _auth(request: Request, *, kind: str, device_id: str):
        try:
            return authenticate(
                store,
                request.headers.get("authorization"),
                expected_kind=kind,
                device_id=device_id,
            )
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    def _rate_limit(token) -> None:
        if not limiter.allow(token.token_sha256, token.rate_per_min):
            raise HTTPException(status_code=429, detail="rate limit exceeded")

    # --- endpoints --------------------------------------------------------------

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/devices/{device_id}/events",
        tags=["ingest"],
        openapi_extra=_INGEST_OPENAPI_EXTRA,
    )
    async def ingest(device_id: str, request: Request):
        # 1. auth: 401 (missing/unknown) / 403 (wrong kind or device)
        token = _auth(request, kind="ingest", device_id=device_id)

        # 2. unknown device -> 404
        if store.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="unknown device")

        # 3. declared Content-Length over the cap -> 413 (free header check,
        #    rejected BEFORE we throttle or buffer anything).
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_body:
                    raise HTTPException(status_code=413, detail="payload too large")
            except ValueError:
                pass  # malformed header: fall through to streamed enforcement

        # 4. rate limit -> 429 (BEFORE the body read, so abusive tokens are
        #    throttled before we buffer their payload).
        _rate_limit(token)

        # 5. read the body with a hard streaming cap -> 413 (chunked / no CL).
        body = await _read_streamed_body(request, max_body)

        # 6. parse + validate -> 422 (schema, layout allowlist, content, qr_data>512)
        try:
            raw = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        try:
            envelope = validate_envelope(raw)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc

        # body device must match the path device
        if envelope.device != device_id:
            raise HTTPException(
                status_code=422, detail="envelope.device does not match path device"
            )

        # 7. channel scope (needs the parsed channel) -> 403
        try:
            authorize_channel(token, envelope.channel)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

        # 8. stamp + dedup insert. First write wins; duplicate id is a no-op.
        received_at = int(time.time())
        row = EventRow(
            id=envelope.id,
            device=envelope.device,
            channel=envelope.channel,
            priority=envelope.priority,
            ttl_seconds=envelope.ttl_seconds,
            layout=envelope.layout,
            content=envelope.content,
            received_at=received_at,
            raw_size=len(body),
        )
        stored = store.insert_event(row)
        if not stored:
            return Response(
                status_code=200,
                media_type="application/json",
                content=json.dumps({"status": "duplicate", "id": envelope.id}),
            )
        return Response(
            status_code=201,
            media_type="application/json",
            content=json.dumps({"status": "stored", "id": envelope.id}),
        )

    @app.get(
        "/api/devices/{device_id}/current",
        tags=["device"],
        responses={
            200: {
                "description": "Resolved screen + additive control block.",
                "content": {"application/json": {"example": _CURRENT_EXAMPLE}},
            },
            304: {"description": "Not Modified (If-None-Match matched the ETag)."},
        },
    )
    async def get_current(
        device_id: str,
        request: Request,
        batt: Optional[str] = Query(None, description="battery %, clamped 0..100"),
        rssi: Optional[str] = Query(None, description="wifi RSSI dBm, clamped -120..0"),
        fw: Optional[str] = Query(None, description="firmware id, <=16 chars [A-Za-z0-9._-]"),
        up: Optional[str] = Query(None, description="uptime seconds, >=0"),
    ):
        # 1. auth: 401 / 403
        token = _auth(request, kind="device", device_id=device_id)

        # 2. unknown device -> 404
        device = store.get_device(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="unknown device")

        # 3. rate limit -> 429
        _rate_limit(token)

        # 4. piggybacked telemetry: validate + clamp, persist, NEVER 4xx the poll.
        #    Malformed params are silently dropped; last_seen_at stamps every poll
        #    and telemetry does NOT affect resolution or the ETag.
        store.update_telemetry(
            device_id,
            last_seen_at=int(time.time()),
            last_batt=_qp_clamp_int(batt, 0, 100),
            last_rssi=_qp_clamp_int(rssi, -120, 0),
            last_fw=_qp_fw(fw),
            last_uptime=_qp_uptime(up),
        )

        # 5. resolve current screen (lazy TTL) + ETag (the hash covers control)
        resolution = current(store, device)
        etag = resolution.etag
        etag_header = f'"{etag}"'

        # 6. If-None-Match -> 304 (empty body) when unchanged
        if _if_none_match_hit(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers={"ETag": etag_header})

        return Response(
            status_code=200,
            media_type="application/json",
            content=json.dumps(resolution.to_response()),
            headers={"ETag": etag_header},
        )

    @app.patch("/api/devices/{device_id}/config", tags=["device"])
    async def set_config(device_id: str, request: Request):
        """Remote device control. Body: {"poll_interval": N}. The deep-sleep
        interval is CLAMPED to [30, 3600]s; non-int/missing -> 422. Authed with
        the device's OWN device token (same kind the Pico polls with)."""
        # 1. auth: 401 / 403 (device-scoped device token)
        token = _auth(request, kind="device", device_id=device_id)

        # 2. unknown device -> 404
        if store.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="unknown device")

        # 3. declared Content-Length over the cap -> 413 (before throttle/buffer)
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_body:
                    raise HTTPException(status_code=413, detail="payload too large")
            except ValueError:
                pass  # malformed header: fall through to streamed enforcement

        # 4. rate limit -> 429 (BEFORE the body read)
        _rate_limit(token)

        # 5. read the body with a hard streaming cap -> 413 (mirrors /events; a
        #    valid ingest/device token must not be able to OOM the process).
        body = await _read_streamed_body(request, max_body)

        # 6. parse + validate body -> 422
        try:
            raw = json.loads(body)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        # strict config surface: unknown keys -> 422 (matches the envelope's
        # extra='forbid' posture; no silent drops / mass-assignment surprises).
        unknown = sorted(set(raw) - {"poll_interval"})
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"unknown config keys: {unknown}"
            )
        value = raw.get("poll_interval")
        # bool is a subclass of int; a JSON true/false is NOT a valid interval.
        if not isinstance(value, int) or isinstance(value, bool):
            raise HTTPException(
                status_code=422, detail="poll_interval must be an integer"
            )

        clamped = max(POLL_INTERVAL_MIN, min(POLL_INTERVAL_MAX, value))
        store.set_poll_interval(device_id, clamped)
        return {"id": device_id, "poll_interval": clamped}

    @app.get("/api/devices/{device_id}/status", tags=["telemetry"])
    async def get_status(device_id: str, request: Request):
        """Stored telemetry + last_seen_at for the dashboard (device token)."""
        # 1. auth: 401 / 403
        token = _auth(request, kind="device", device_id=device_id)

        # 2. unknown device -> 404
        device = store.get_device(device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="unknown device")

        # 3. rate limit -> 429
        _rate_limit(token)

        return {
            "id": device_id,
            "last_seen_at": device.last_seen_at,
            "last_batt": device.last_batt,
            "last_rssi": device.last_rssi,
            "last_fw": device.last_fw,
            "last_uptime": device.last_uptime,
            "poll_interval": device.poll_interval_s,
        }

    return app


async def _read_streamed_body(request: Request, max_bytes: int) -> bytes:
    """Stream the request body, enforcing a hard running cap WITHOUT buffering
    the whole body up front. Raises 413 the moment the running total exceeds the
    cap (handles chunked transfer / absent Content-Length)."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _qp_clamp_int(raw: Optional[str], lo: int, hi: int) -> Optional[int]:
    """Parse a query-param int and clamp to [lo, hi]; None if absent/malformed."""
    if raw is None:
        return None
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return None


def _qp_uptime(raw: Optional[str]) -> Optional[int]:
    """Parse the uptime query param (int >= 0); None if absent/malformed."""
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _qp_fw(raw: Optional[str]) -> Optional[str]:
    """Validate the fw query param (<= 16 chars, [A-Za-z0-9._-]); None if bad."""
    if raw is None or len(raw) > 16 or not _FW_RE.match(raw):
        return None
    return raw


def _validation_detail(exc: ValidationError) -> list[dict[str, Any]]:
    """JSON-safe 422 detail. Drops pydantic's ``ctx`` (which can carry raw,
    non-serializable exception objects) and the echoed input."""
    detail: list[dict[str, Any]] = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        detail.append(
            {
                "loc": [str(p) for p in err.get("loc", ())],
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
        )
    return detail


def _if_none_match_hit(header: Optional[str], etag: str) -> bool:
    """True if an If-None-Match header matches the current etag (or is '*')."""
    if not header:
        return False
    for tag in header.split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        if len(tag) >= 2 and tag[0] == '"' and tag[-1] == '"':
            tag = tag[1:-1]
        if tag == etag:
            return True
    return False


# Module-level app for `uvicorn server.app:app` (reads config from env).
app = create_app()
