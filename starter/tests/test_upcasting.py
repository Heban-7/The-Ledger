"""
tests/test_upcasting.py — Immutability test

1. Store a v1 event
2. Load via store.load_stream() — verify upcasted to v2
3. Raw stored payload must be UNCHANGED
"""
import pytest

from ledger.event_store import InMemoryEventStore
from ledger.upcasters import UpcasterRegistry


def _ev_v1(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_upcaster_does_not_modify_stored_events():
    """Immutability: upcasted event has new fields, but raw DB payload is unchanged."""
    registry = UpcasterRegistry()
    store = InMemoryEventStore(upcaster_registry=registry)

    v1_payload = {"application_id": "APP-001", "session_id": "s1", "risk_tier": "MEDIUM"}
    events = [_ev_v1("CreditAnalysisCompleted", **v1_payload)]
    await store.append("credit-APP-001", events, expected_version=-1)

    raw_events = await store.load_stream("credit-APP-001")
    raw_first = raw_events[0]

    assert raw_first["event_version"] == 2, "Upcaster should upgrade to v2"
    assert "model_version" in raw_first["payload"], "v2 should have model_version"
    assert raw_first["payload"].get("model_version") == "legacy-pre-2026"
    assert "confidence_score" in raw_first["payload"]

    raw_stored = store._streams["credit-APP-001"][0]
    assert raw_stored["event_version"] == 1, "Stored event must remain v1"
    assert "model_version" not in raw_stored["payload"], "Stored payload must NOT have model_version"
