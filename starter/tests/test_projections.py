"""
tests/test_projections.py — Projection daemon tests
"""
import pytest

from ledger.event_store import InMemoryEventStore
from ledger.projections import ProjectionDaemon, ApplicationSummaryProjection


def _ev(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_projection_daemon_processes_events():
    store = InMemoryEventStore()
    proj = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(store, [proj])

    await store.append("loan-APP-001", [_ev("ApplicationSubmitted", application_id="APP-001", applicant_id="C1", requested_amount_usd=100000)], expected_version=-1)
    await daemon._process_batch()

    row = proj.get("APP-001")
    assert row is not None
    assert row["state"] == "SUBMITTED"
    assert row["applicant_id"] == "C1"
