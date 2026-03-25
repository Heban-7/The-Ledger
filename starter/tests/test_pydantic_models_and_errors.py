"""
Unit tests for typed Pydantic stored-row models and structured errors.
"""

import json

import pytest

from ledger.exceptions import DomainError, OptimisticConcurrencyError, PreconditionFailedError
from ledger.models import BaseEvent, StreamMetadata


def test_optimistic_concurrency_error_structured_shape_json_serializable():
    e = OptimisticConcurrencyError(stream_id="s", expected=3, actual=4)
    d = e.to_structured()
    assert d["error_type"] == "OptimisticConcurrencyError"
    assert d["stream_id"] == "s"
    assert d["expected_version"] == 3
    assert d["actual_version"] == 4
    assert d["suggested_action"] == "reload_stream_and_retry"
    json.dumps(d)  # stable for MCP response


def test_domain_error_structured_shape_json_serializable():
    e = DomainError("bad", rule="x")
    d = e.to_structured()
    assert d["error_type"] == "DomainError"
    assert d["rule"] == "x"
    json.dumps(d)


def test_precondition_failed_structured_shape_json_serializable():
    e = PreconditionFailedError("pre", precondition="active_session_required")
    d = e.to_structured()
    assert d["error_type"] == "PreconditionFailed"
    assert d["precondition"] == "active_session_required"
    json.dumps(d)


def test_stream_metadata_pydantic_from_row_and_model_dump_compat():
    md = StreamMetadata.from_row(
        {
            "stream_id": "loan-A",
            "aggregate_type": "loan",
            "current_version": 2,
            "created_at": "2026-01-01T00:00:00+00:00",
            "archived_at": None,
            "metadata": {"k": "v"},
        }
    )
    assert md.stream_id == "loan-A"
    assert md.metadata["k"] == "v"


def test_base_event_to_dict_stable_keys():
    ev = BaseEvent(
        event_id="e1",
        stream_id="s1",
        stream_position=1,
        global_position=10,
        event_type="E",
        event_version=1,
        payload={"a": 1},
        metadata={"m": 2},
        recorded_at=None,
    )
    d = ev.to_dict()
    assert d["event_type"] == "E"
    assert d["global_position"] == 10
    assert d["event_id"] == "e1"

