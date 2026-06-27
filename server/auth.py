"""Auth + rate limiting.

Tokens are stored only as their sha256 hex digest (plaintext never persisted)
and compared with ``hmac.compare_digest`` (constant-time). Two kinds:

  - device token -> GET  /api/devices/:id/current  (scoped to one device)
  - ingest token -> POST /api/devices/:id/events   (scoped to a device,
                                                     optionally channel-scoped)

This module is framework-agnostic: it returns results / raises ``AuthError``
with an HTTP-ish status, and app.py maps that onto FastAPI responses.
"""

from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .store import Store, TokenRow, sha256_hex


class AuthError(Exception):
    """An auth/scope failure carrying the HTTP status to return."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def parse_bearer(authorization: Optional[str]) -> Optional[str]:
    """Extract the token from an ``Authorization: Bearer <token>`` header.

    Returns None if the header is missing or malformed (caller -> 401).
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def lookup_token(store: Store, presented_token: str) -> Optional[TokenRow]:
    """Look up a token by the sha256 of the presented secret.

    The stored value is itself a hash, so the index lookup does not leak the
    secret; we still re-verify with ``hmac.compare_digest`` to honor the
    constant-time comparison contract.
    """
    digest = sha256_hex(presented_token)
    row = store.get_token_by_hash(digest)
    if row is None:
        return None
    if not hmac.compare_digest(row.token_sha256, digest):
        return None
    return row


def authenticate(
    store: Store,
    authorization: Optional[str],
    *,
    expected_kind: str,
    device_id: str,
) -> TokenRow:
    """Authenticate + authorize the device scope (NOT the channel scope, which
    needs the parsed body and is checked by the caller).

    Raises AuthError:
      - 401 missing / malformed / unknown token
      - 403 valid token of the wrong kind, or scoped to a different device
    """
    presented = parse_bearer(authorization)
    if presented is None:
        raise AuthError(401, "missing or malformed bearer token")

    token = lookup_token(store, presented)
    if token is None:
        raise AuthError(401, "unknown token")

    if token.kind != expected_kind:
        raise AuthError(403, f"token is not a {expected_kind} token")

    if token.device_id != device_id:
        raise AuthError(403, "token not scoped to this device")

    return token


def authorize_channel(token: TokenRow, channel: str) -> None:
    """Channel-scope check for ingest tokens. ``channels=None`` means all.

    Raises AuthError(403) if the token may not write to ``channel``.
    """
    if token.channels is not None and channel not in token.channels:
        raise AuthError(403, "token not scoped to this channel")


# --- rate limiting --------------------------------------------------------------


@dataclass
class _Bucket:
    tokens: float
    last: float


class RateLimiter:
    """Per-token in-memory token bucket.

    PONYTAIL: this bucket lives in process memory. It RESETS on process restart
    and is NOT shared across workers/replicas, so the rate it enforces is a
    best-effort ceiling, NOT a security boundary. If you need hard guarantees,
    back it with Redis or a SQLite counter table. For a single-process Pico
    bridge this is deliberately the lazy, good-enough choice.

    RUNBOOK: because the bucket is per-process, run a SINGLE worker
    (``uvicorn --workers 1``). Horizontal scale-out (multiple workers/replicas)
    needs a shared store (Redis/SQLite) or the ceiling multiplies per worker.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, rate_per_min: int, *, now: Optional[float] = None) -> bool:
        """Consume one token for ``key``. Returns False if over the per-minute rate."""
        if rate_per_min <= 0:
            return False
        if now is None:
            now = time.monotonic()
        capacity = float(rate_per_min)
        refill_per_sec = rate_per_min / 60.0
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # Start full, then immediately spend one token.
                self._buckets[key] = _Bucket(tokens=capacity - 1.0, last=now)
                return True
            elapsed = max(0.0, now - bucket.last)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_sec)
            bucket.last = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False
