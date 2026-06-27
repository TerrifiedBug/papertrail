"""Pydantic v2 models for the ``pico-paper.v1`` wire envelope + per-layout content.

The whole point of validating here is to *reject garbage before the Pico ever
sees it*. Models are strict: unknown layouts and unknown fields are 422.

Contract source of truth: ../SCHEMA.md. Field names and the layout enum are
frozen for v1; any additive change ships as ``pico-paper.v2``.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# --- limits / constants (frozen) ------------------------------------------------

SCHEMA_VERSION = "pico-paper.v1"
TTL_CAP = 604_800          # 7 days, seconds
PRIORITY_MIN = 0
PRIORITY_MAX = 255
QR_DATA_MAX = 512
ID_PATTERN = r"^[A-Za-z0-9._:-]+$"

# Frozen layout allowlist. Anything else -> 422.
LAYOUTS = ("status_card", "alert", "list", "metric", "qr")
Layout = Literal["status_card", "alert", "list", "metric", "qr"]


# --- per-layout content models --------------------------------------------------
#
# extra="forbid" => unknown content fields are rejected (422).
# str fields are strict per pydantic v2: a JSON number is NOT coerced to str,
# which is exactly why metric.value must be a quoted string, not a number.


class _Content(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StatusCardContent(_Content):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "title": "Home Server",
                "status": "OK",
                "subtitle": "All services nominal",
                "lines": ["CPU      12%", "RAM      41%", "Disk     63%"],
                "footer": "updated 14:02",
            }
        },
    )
    title: str = ""
    status: str = ""
    subtitle: str = ""
    lines: list[str] = Field(default_factory=list)
    footer: str = ""


class AlertContent(_Content):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "severity": "high",
                "title": "Water Leak",
                "message": "Sensor under the sink detected moisture.",
                "footer": "basement-sensor-3",
            }
        },
    )
    severity: Literal["low", "med", "high"]
    title: str = ""
    message: str = ""
    footer: str = ""


class ListContent(_Content):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "title": "Shopping",
                "items": ["Milk", "Eggs", "Coffee beans"],
                "footer": "3 items",
            }
        },
    )
    title: str = ""
    items: list[str] = Field(default_factory=list)
    footer: str = ""


class MetricContent(_Content):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "label": "Solar output",
                "value": "3.42",
                "unit": "kW",
                "trend": "UP +0.4 kW vs 1h",
                "footer": "inverter-A",
            }
        },
    )
    label: str = ""
    value: str = ""          # string, NOT a number, to preserve formatting
    unit: str = ""
    trend: str = ""
    footer: str = ""


class QrContent(_Content):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "title": "Guest WiFi",
                "qr_data": "WIFI:T:WPA;S:GuestNet;P:welcome123;;",
                "caption": "Scan to join GuestNet. Valid for 12 hours.",
            }
        },
    )
    title: str = ""
    qr_data: str = Field(min_length=1, max_length=QR_DATA_MAX)
    caption: str = ""


CONTENT_MODELS: dict[str, type[_Content]] = {
    "status_card": StatusCardContent,
    "alert": AlertContent,
    "list": ListContent,
    "metric": MetricContent,
    "qr": QrContent,
}

# A union type alias, handy for callers/tests that want the concrete classes.
AnyContent = Union[StatusCardContent, AlertContent, ListContent, MetricContent, QrContent]


# --- envelope -------------------------------------------------------------------


class Envelope(BaseModel):
    """A wire event. Flat envelope + per-layout ``content`` object.

    There are NO other top-level keys in v1. The two server-stamped fields
    (``received_at`` / ``raw_size``) are never accepted from the wire — strip
    them before validating (see app.py); ``extra="forbid"`` would otherwise 422.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # `schema` is aliased to avoid shadowing pydantic's deprecated BaseModel.schema().
    schema_: Literal["pico-paper.v1"] = Field(alias="schema")
    id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    device: str = Field(min_length=1)
    channel: str = Field(min_length=1, max_length=64)
    priority: int = 0
    # Optional: omitted or 0 => NO expiry (sticky until replaced/deleted). A
    # positive value expires after N seconds (capped at TTL_CAP = 7 days).
    ttl_seconds: Optional[int] = Field(default=None, ge=0)
    layout: Layout
    content: dict[str, Any]

    @field_validator("priority")
    @classmethod
    def _clamp_priority(cls, v: int) -> int:
        # priority is clamped (not rejected) to 0..255; higher wins.
        return max(PRIORITY_MIN, min(PRIORITY_MAX, v))

    @field_validator("ttl_seconds")
    @classmethod
    def _cap_ttl(cls, v):
        # None (omitted) or 0 => no expiry (sticky); positive values cap at
        # TTL_CAP. Negative is already rejected by the ge=0 field constraint.
        if v is None or v <= 0:
            return v
        return min(v, TTL_CAP)

    @model_validator(mode="after")
    def _validate_content(self) -> "Envelope":
        # `layout` is already a valid enum here (field validation ran first),
        # so an unknown layout never reaches this point — it 422s earlier.
        model_cls = CONTENT_MODELS[self.layout]
        try:
            validated = model_cls.model_validate(self.content)
        except ValidationError as exc:
            # Re-raise as a normalized validation error against the layout shape.
            # Static message ONLY — never reflect the caller's raw content back.
            raise ValueError(
                f"content invalid for layout '{self.layout}'"
            ) from exc
        # Store the normalized content (defaults filled, extras already rejected)
        # so storage + ETag are deterministic.
        self.content = validated.model_dump()
        return self

    def content_model(self) -> _Content:
        """Return the validated, layout-specific content model instance."""
        return CONTENT_MODELS[self.layout].model_validate(self.content)


def validate_envelope(raw: dict[str, Any]) -> Envelope:
    """Validate a wire dict into an Envelope.

    Server-stamped fields are dropped first (ignored from the wire). Raises
    pydantic ``ValidationError`` on any failure (caller maps to HTTP 422).
    """
    clean = {k: v for k, v in raw.items() if k not in ("received_at", "raw_size")}
    return Envelope.model_validate(clean)


def validate_fallback(fallback: Any) -> None:
    """Validate a device fallback screen against the SAME per-layout contract as
    wire events. The fallback ships straight to the Pico when no event is live,
    so it must never bypass the layout/content checks.

    Raises ``ValueError`` on a bad layout or content. Callers fail fast at seed
    time; resolve.py also guards at read time (defense in depth).
    """
    if not isinstance(fallback, dict):
        raise ValueError("fallback must be an object with 'layout' and 'content'")
    layout = fallback.get("layout")
    if layout not in LAYOUTS:
        raise ValueError(f"fallback layout '{layout}' not in {LAYOUTS}")
    try:
        CONTENT_MODELS[layout].model_validate(fallback.get("content"))
    except ValidationError as exc:
        raise ValueError(f"fallback content invalid for layout '{layout}'") from exc
