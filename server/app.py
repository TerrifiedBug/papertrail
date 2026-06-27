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

import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .auth import AuthError, RateLimiter, authenticate, authorize_channel, parse_bearer
from .resolve import current
from .schema import CONTENT_MODELS, SCHEMA_VERSION, validate_envelope, validate_fallback
from .store import EventRow, Store, sha256_hex

DEFAULT_MAX_BODY_BYTES = 8192        # 8 KiB hard cap -> 413

POLL_INTERVAL_MIN = 30               # seconds; remote deep-sleep clamp floor
POLL_INTERVAL_MAX = 3600             # seconds; remote deep-sleep clamp ceiling

_FW_RE = re.compile(r"^[A-Za-z0-9._-]+$")   # telemetry fw charset

# Admin auth-failure throttle: failed admin-token attempts per client host per
# minute before a 429 (nice-to-have brute-force speed bump; per-process, see
# RateLimiter). Generous enough never to impede legitimate dashboard use.
ADMIN_FAIL_PER_MIN = 30

# The admin frontend lives here (the page is written by the frontend agent).
# Served WITHOUT auth (it carries no data); /static/* assets likewise.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# OpenAPI metadata (purely descriptive; does not change behavior).
_TAGS_METADATA = [
    {"name": "ingest", "description": "POST webhook events into the bridge (ingest token)."},
    {"name": "device", "description": "Pico-facing screen poll + remote device control (device token)."},
    {"name": "telemetry", "description": "Stored device telemetry for the dashboard (device token)."},
    {"name": "ops", "description": "Operational / liveness endpoints (no auth)."},
    {
        "name": "admin",
        "description": (
            "LAN-only admin backend. Every call needs the single "
            "`PAPERTRAIL_ADMIN_TOKEN` as a Bearer token (401 without it, 503 if "
            "the token is unset). NEVER expose `/admin` or `/api/admin/*` "
            "publicly — keep them behind the LAN (Caddy serves only `/events`)."
        ),
    },
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
    admin_token: Optional[str] = None,
) -> FastAPI:
    db_path = db_path or os.environ.get("PAPERTRAIL_DB", "papertrail.db")
    max_body = int(
        max_body_bytes
        if max_body_bytes is not None
        else os.environ.get("PAPERTRAIL_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
    )
    seed_file = os.environ.get("PAPERTRAIL_SEED_FILE", "seed.json")
    # The admin token is read ONCE at app construction. If it is unset/empty the
    # whole admin surface returns 503 (disabled) so it is never accidentally open.
    admin_token = (
        admin_token
        if admin_token is not None
        else os.environ.get("PAPERTRAIL_ADMIN_TOKEN")
    ) or None

    store = Store(db_path)
    limiter = RateLimiter()
    admin_fail = RateLimiter()   # keyed on client host; throttles bad admin auth

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
    app.state.admin_enabled = admin_token is not None

    # Serve the admin frontend + its assets WITHOUT auth (it carries no data;
    # the page prompts for the admin token and sends it on /api/admin/* fetches).
    os.makedirs(_STATIC_DIR, exist_ok=True)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

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

    def _require_admin(request: Request) -> None:
        """Gate every /api/admin/* call on the single admin token.

        503 if PAPERTRAIL_ADMIN_TOKEN is unset (admin disabled -> never open),
        401 on a missing/wrong token (constant-time compare), 429 after too many
        failed attempts from one client (brute-force speed bump).
        """
        if not admin_token:
            raise HTTPException(status_code=503, detail="admin disabled")
        presented = parse_bearer(request.headers.get("authorization"))
        # Compare sha256 hex of both sides: fixed-length ASCII, constant-time, and
        # can never raise on a non-ASCII (latin-1 header) bearer string.
        if presented is not None and hmac.compare_digest(
            sha256_hex(presented), sha256_hex(admin_token)
        ):
            return
        client = request.client.host if request.client else "unknown"
        if not admin_fail.allow(f"admin-fail:{client}", ADMIN_FAIL_PER_MIN):
            raise HTTPException(status_code=429, detail="too many admin auth failures")
        raise HTTPException(status_code=401, detail="invalid or missing admin token")

    async def _admin_json_body(request: Request) -> Any:
        """Read + parse an admin request body under the SAME hard size cap as the
        public surface (a valid admin token must not be able to OOM the process)."""
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_body:
                    raise HTTPException(status_code=413, detail="payload too large")
            except ValueError:
                pass
        body = await _read_streamed_body(request, max_body)
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc

    def _device_admin_dict(device) -> dict[str, Any]:
        """Full per-device admin view: config + fallback + telemetry + online +
        the resolved current screen (reuses resolve.current)."""
        resolution = current(store, device)
        now = int(time.time())
        online = (
            device.last_seen_at is not None
            and (now - device.last_seen_at) <= 2.5 * device.poll_interval_s
        )
        return {
            "id": device.id,
            "channels": device.channels,
            "poll_interval_s": device.poll_interval_s,
            "low_batt_interval_s": device.low_batt_interval_s,
            "fallback": device.fallback,
            "telemetry": {
                "last_seen_at": device.last_seen_at,
                "last_batt": device.last_batt,
                "last_rssi": device.last_rssi,
                "last_fw": device.last_fw,
                "last_uptime": device.last_uptime,
            },
            "online": online,
            "current": {
                "layout": resolution.layout,
                "content": resolution.content,
                "etag": resolution.etag,
                "source_event_id": resolution.source_event_id,
                "priority": resolution.priority,
            },
        }

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
            ttl_seconds=envelope.ttl_seconds or 0,   # None/omitted => 0 = no expiry
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

    # --- admin frontend ---------------------------------------------------------

    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        """Serve the admin dashboard HTML (no auth — it carries no data; the page
        prompts for the admin token and stores it client-side). The file is
        written by the frontend agent into server/static/."""
        path = os.path.join(_STATIC_DIR, "admin.html")
        if not os.path.exists(path):
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><title>papertrail admin</title>"
                "<p>admin.html not deployed yet.</p>",
                status_code=200,
            )
        return FileResponse(path, media_type="text/html")

    # --- admin API: devices -----------------------------------------------------

    @app.get("/api/admin/devices", tags=["admin"])
    async def admin_list_devices(request: Request):
        _require_admin(request)
        return {"devices": [_device_admin_dict(d) for d in store.list_devices()]}

    @app.post("/api/admin/devices", tags=["admin"], status_code=201)
    async def admin_create_device(request: Request):
        _require_admin(request)
        raw = await _admin_json_body(request)
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")

        device_id = raw.get("id")
        if not isinstance(device_id, str) or not device_id:
            raise HTTPException(status_code=422, detail="id (non-empty string) required")

        channels = raw.get("channels")
        if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
            raise HTTPException(status_code=422, detail="channels must be a list of strings")

        # Same per-layout fallback contract as wire events / seed (422 on bad shape).
        try:
            validate_fallback(raw.get("fallback"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid fallback: {exc}") from exc

        poll = raw.get("poll_interval_s", 120)
        low = raw.get("low_batt_interval_s", 600)
        if not _is_int(poll) or not _is_int(low):
            raise HTTPException(
                status_code=422,
                detail="poll_interval_s / low_batt_interval_s must be integers",
            )
        poll = max(POLL_INTERVAL_MIN, min(POLL_INTERVAL_MAX, poll))

        created = store.add_device(
            id=device_id,
            channels=channels,
            fallback=raw["fallback"],
            poll_interval_s=poll,
            low_batt_interval_s=low,
        )
        if not created:
            raise HTTPException(status_code=409, detail="device id already exists")
        return _device_admin_dict(store.get_device(device_id))

    @app.patch("/api/admin/devices/{device_id}", tags=["admin"])
    async def admin_patch_device(device_id: str, request: Request):
        _require_admin(request)
        if store.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="unknown device")
        raw = await _admin_json_body(request)
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")

        allowed = {"channels", "fallback", "poll_interval_s", "low_batt_interval_s"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown keys: {unknown}")

        kwargs: dict[str, Any] = {}
        if "channels" in raw:
            channels = raw["channels"]
            if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
                raise HTTPException(status_code=422, detail="channels must be a list of strings")
            kwargs["channels"] = channels
        if "fallback" in raw:
            try:
                validate_fallback(raw["fallback"])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"invalid fallback: {exc}") from exc
            kwargs["fallback"] = raw["fallback"]
        if "poll_interval_s" in raw:
            value = raw["poll_interval_s"]
            if not _is_int(value):
                raise HTTPException(status_code=422, detail="poll_interval_s must be an integer")
            kwargs["poll_interval_s"] = max(POLL_INTERVAL_MIN, min(POLL_INTERVAL_MAX, value))
        if "low_batt_interval_s" in raw:
            value = raw["low_batt_interval_s"]
            if not _is_int(value):
                raise HTTPException(status_code=422, detail="low_batt_interval_s must be an integer")
            kwargs["low_batt_interval_s"] = value

        store.update_device(device_id, **kwargs)
        return _device_admin_dict(store.get_device(device_id))

    @app.delete("/api/admin/devices/{device_id}", tags=["admin"])
    async def admin_delete_device(device_id: str, request: Request):
        _require_admin(request)
        # Cascade: removes the device's tokens + events too.
        if not store.delete_device(device_id):
            raise HTTPException(status_code=404, detail="unknown device")
        return {"id": device_id, "deleted": True}

    # --- admin API: events ------------------------------------------------------

    @app.get("/api/admin/devices/{device_id}/events", tags=["admin"])
    async def admin_device_events(
        device_id: str,
        request: Request,
        limit: int = Query(20, ge=1, le=200),
    ):
        _require_admin(request)
        if store.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="unknown device")
        now = int(time.time())
        out = []
        for e in store.events_for_device_recent(device_id, limit):
            permanent = e.ttl_seconds is None or e.ttl_seconds <= 0
            expires_at = None if permanent else e.received_at + e.ttl_seconds
            out.append(
                {
                    "id": e.id,
                    "channel": e.channel,
                    "priority": e.priority,
                    "ttl_seconds": e.ttl_seconds,
                    "layout": e.layout,
                    "received_at": e.received_at,
                    "expires_at": expires_at,
                    "expired": (False if permanent else now >= expires_at),
                }
            )
        return out

    @app.post("/api/admin/devices/{device_id}/events", tags=["admin"], status_code=201)
    async def admin_push_event(device_id: str, request: Request):
        _require_admin(request)
        if store.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="unknown device")
        raw = await _admin_json_body(request)
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")

        # Build a wire envelope and reuse the SAME validation as public ingest
        # (schema.validate_envelope -> Envelope + CONTENT_MODELS): layout
        # allowlist, content shape, ttl/priority bounds. device is forced to the
        # path device; id is auto-generated when omitted.
        envelope_in = {
            "schema": SCHEMA_VERSION,
            "id": raw.get("id") or _gen_event_id(),
            "device": device_id,
            "channel": raw.get("channel"),
            "priority": raw.get("priority", 0),
            "ttl_seconds": raw.get("ttl_seconds"),
            "layout": raw.get("layout"),
            "content": raw.get("content"),
        }
        try:
            envelope = validate_envelope(envelope_in)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc

        received_at = int(time.time())
        row = EventRow(
            id=envelope.id,
            device=envelope.device,
            channel=envelope.channel,
            priority=envelope.priority,
            ttl_seconds=envelope.ttl_seconds or 0,   # None/omitted => 0 = no expiry
            layout=envelope.layout,
            content=envelope.content,
            received_at=received_at,
            raw_size=len(json.dumps(envelope_in)),
        )
        stored = store.insert_event(row)
        status = "stored" if stored else "duplicate"
        return Response(
            status_code=201 if stored else 200,
            media_type="application/json",
            content=json.dumps({"status": status, "id": envelope.id}),
        )

    @app.delete("/api/admin/devices/{device_id}/events/{event_id}", tags=["admin"])
    async def admin_delete_event(device_id: str, event_id: str, request: Request):
        _require_admin(request)
        if not store.delete_event(device_id, event_id):
            raise HTTPException(status_code=404, detail="unknown event")
        return {"id": event_id, "deleted": True}

    @app.patch("/api/admin/devices/{device_id}/config", tags=["admin"])
    async def admin_set_config(device_id: str, request: Request):
        _require_admin(request)
        if store.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="unknown device")
        raw = await _admin_json_body(request)
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        unknown = sorted(set(raw) - {"poll_interval"})
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown config keys: {unknown}")
        value = raw.get("poll_interval")
        if not _is_int(value):
            raise HTTPException(status_code=422, detail="poll_interval must be an integer")
        clamped = max(POLL_INTERVAL_MIN, min(POLL_INTERVAL_MAX, value))
        store.set_poll_interval(device_id, clamped)
        return {"id": device_id, "poll_interval": clamped}

    # --- admin API: tokens ------------------------------------------------------

    @app.get("/api/admin/tokens", tags=["admin"])
    async def admin_list_tokens(request: Request):
        _require_admin(request)
        # NEVER the plaintext: only a non-secret preview derived from the hash.
        return [
            {
                "id": t.id,
                "kind": t.kind,
                "device_id": t.device_id,
                "channels": t.channels,
                "rate_per_min": t.rate_per_min,
                "token_preview": _token_preview(t.kind, t.token_sha256),
            }
            for t in store.list_tokens()
        ]

    @app.post("/api/admin/tokens", tags=["admin"], status_code=201)
    async def admin_mint_token(request: Request):
        _require_admin(request)
        raw = await _admin_json_body(request)
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")

        kind = raw.get("kind")
        if kind not in ("device", "ingest"):
            raise HTTPException(status_code=422, detail="kind must be 'device' or 'ingest'")
        device_id = raw.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise HTTPException(status_code=422, detail="device_id (non-empty string) required")

        # Channels are an ingest-only scope; null/absent => all channels. A device
        # token has no channel scope, so any channels are ignored for kind=device.
        channels = raw.get("channels")
        if kind == "device":
            channels = None
        elif channels is not None and (
            not isinstance(channels, list) or not all(isinstance(c, str) for c in channels)
        ):
            raise HTTPException(status_code=422, detail="channels must be a list of strings or null")

        rate = raw.get("rate_per_min", 60)
        if not _is_int(rate) or rate <= 0:
            raise HTTPException(status_code=422, detail="rate_per_min must be a positive integer")

        # GENERATE a strong random secret; store ONLY its sha256; return the
        # plaintext exactly once (the UI shows it once and warns it won't recur).
        plaintext = _gen_token(kind)
        token_id = store.add_token(
            token_sha256=sha256_hex(plaintext),
            kind=kind,
            device_id=device_id,
            channels=channels,
            rate_per_min=rate,
        )
        return Response(
            status_code=201,
            media_type="application/json",
            content=json.dumps({"id": token_id, "token": plaintext}),
        )

    @app.delete("/api/admin/tokens/{token_id}", tags=["admin"])
    async def admin_delete_token(token_id: str, request: Request):
        _require_admin(request)
        if not store.delete_token(token_id):
            raise HTTPException(status_code=404, detail="unknown token")
        return {"id": token_id, "revoked": True}

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


# --- admin helpers --------------------------------------------------------------


def _is_int(value: Any) -> bool:
    """True for a real int (a JSON bool is NOT a valid integer here)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _gen_event_id() -> str:
    """Auto-generate a wire-legal event id (matches schema.ID_PATTERN)."""
    return f"evt_admin_{secrets.token_hex(8)}"


def _gen_token(kind: str) -> str:
    """Mint a strong random bearer token with a kind-tagged prefix. The plaintext
    is returned to the admin exactly once; only its sha256 is ever stored."""
    short = "dev" if kind == "device" else "ing"
    return f"pt_{short}_{secrets.token_urlsafe(32)}"


def _token_preview(kind: str, token_sha256: str) -> str:
    """A NON-secret display handle: kind prefix + last 4 of the stored hash.
    Never reveals any part of the plaintext token."""
    short = "dev" if kind == "device" else "ing"
    return f"pt_{short}_…{token_sha256[-4:]}"


# Module-level app for `uvicorn server.app:app` (reads config from env).
app = create_app()
