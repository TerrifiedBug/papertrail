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
        "priority": 50,
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


def test_priority_clamped_both_ends():
    assert validate_envelope(_env(priority=300)).priority == 255
    assert validate_envelope(_env(priority=-5)).priority == 0


def test_priority_default_zero():
    raw = _env()
    del raw["priority"]
    assert validate_envelope(raw).priority == 0


def test_ttl_capped_high_and_rejected_low():
    assert validate_envelope(_env(ttl_seconds=10_000_000)).ttl_seconds == 604_800
    with pytest.raises(ValidationError):
        validate_envelope(_env(ttl_seconds=0))


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
