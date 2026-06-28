"""Unit tests for the pydantic envelope/content models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.schema import Envelope, validate_envelope


def _env(**over):
    base = {
        "schema": "pico-paper.v1",
        "id": "evt_1",
        "device": "kitchen-01",
        "channel": "home.status",
        "ttl_seconds": 900,
        "layout": "status_card",
        "content": {"title": "T", "status": "OK"},
    }
    base.update(over)
    return base


def test_valid_envelope():
    env = validate_envelope(_env())
    assert env.layout == "status_card"
    assert env.content["title"] == "T"
    # normalized: defaults filled
    assert env.content["lines"] == []


def test_kind_defaults_base():
    raw = _env()
    raw.pop("kind", None)
    assert validate_envelope(raw).kind == "base"


def test_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        validate_envelope(_env(kind="banner"))


def test_ttl_cap_sticky_and_reject_negative():
    # high values cap at 7 days
    assert validate_envelope(_env(ttl_seconds=10_000_000)).ttl_seconds == 604_800
    # 0 => sticky (no expiry), now accepted
    assert validate_envelope(_env(ttl_seconds=0)).ttl_seconds == 0
    # omitted => sticky (None)
    raw = _env()
    del raw["ttl_seconds"]
    assert validate_envelope(raw).ttl_seconds is None
    # negative is still rejected (ge=0)
    with pytest.raises(ValidationError):
        validate_envelope(_env(ttl_seconds=-1))


def test_bad_schema_rejected():
    with pytest.raises(ValidationError):
        validate_envelope(_env(schema="pico-paper.v2"))


def test_unknown_layout_rejected():
    with pytest.raises(ValidationError):
        validate_envelope(_env(layout="banner"))


def test_id_pattern_rejects_bad_chars():
    with pytest.raises(ValidationError):
        validate_envelope(_env(id="bad id!"))


def test_unknown_top_level_field_rejected():
    with pytest.raises(ValidationError):
        validate_envelope(_env(extra="nope"))


def test_unknown_content_field_rejected():
    with pytest.raises(ValidationError):
        validate_envelope(_env(content={"title": "T", "bogus": 1}))


def test_metric_value_must_be_string_not_number():
    raw = _env(layout="metric", content={"label": "x", "value": 3.42, "unit": "kW"})
    with pytest.raises(ValidationError):
        validate_envelope(raw)
    # string value is fine
    ok = _env(layout="metric", content={"label": "x", "value": "3.42", "unit": "kW"})
    assert validate_envelope(ok).content["value"] == "3.42"


def test_alert_severity_enum():
    with pytest.raises(ValidationError):
        validate_envelope(_env(layout="alert", content={"severity": "boom", "title": "x"}))
    assert (
        validate_envelope(_env(layout="alert", content={"severity": "high", "title": "x"})).content[
            "severity"
        ]
        == "high"
    )


def test_qr_data_required_and_capped():
    # missing qr_data
    with pytest.raises(ValidationError):
        validate_envelope(_env(layout="qr", content={"title": "x"}))
    # over 512
    with pytest.raises(ValidationError):
        validate_envelope(_env(layout="qr", content={"qr_data": "x" * 513}))
    # exactly 512 ok
    assert (
        len(validate_envelope(_env(layout="qr", content={"qr_data": "x" * 512})).content["qr_data"])
        == 512
    )


def test_server_stamps_stripped_from_wire():
    raw = _env(received_at=123, raw_size=999)
    env = validate_envelope(raw)
    assert env.id == "evt_1"  # no error despite server-stamp keys present


def test_schema_alias_roundtrip():
    env = validate_envelope(_env())
    dumped = env.model_dump(by_alias=True)
    assert dumped["schema"] == "pico-paper.v1"
    assert "schema_" not in dumped


def test_image_layout_validates_bitmap():
    import base64
    good = base64.b64encode(bytes([0x80] + [0] * 7)).decode()   # 8x8 = ceil(8/8)*8 = 8 bytes
    env = validate_envelope(_env(layout="image", content={"w": 8, "h": 8, "data": good}))
    assert env.layout == "image"
    # wrong data length for the declared dims -> 422
    with pytest.raises(ValidationError):
        validate_envelope(_env(layout="image",
                               content={"w": 8, "h": 8, "data": base64.b64encode(b"\x00").decode()}))


def test_render_hints_default_false():
    env = validate_envelope(_env())
    assert env.invert is False and env.full_refresh is False
    assert validate_envelope(_env(invert=True)).invert is True
