"""
tests/test_concurrency.py — Double-decision concurrency test

The critical OCC test: Two AI agents simultaneously append to the same stream at expected_version=3.
Exactly one must succeed; the other must receive OptimisticConcurrencyError.
"""
import asyncio
import pytest

from ledger.event_store import InMemoryEventStore, OptimisticConcurrencyError


def _ev(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_double_decision_exactly_one_succeeds():
    """
    Two agents read stream at version 3 (4 events). Both call append with expected_version=3.
    (a) Total events == 5 (not 6)
    (b) Winner's event has stream_position=4
    (c) Loser raises OptimisticConcurrencyError
    """
    store = InMemoryEventStore()

    # Seed stream with 4 events (positions 0,1,2,3), version=3
    for i in range(4):
        ver = await store.stream_version("loan-APEX-001")
        await store.append("loan-APEX-001", [_ev("BaseEvent", seq=i)], expected_version=ver)

    assert await store.stream_version("loan-APEX-001") == 3

    results = []
    winner_positions = []

    async def agent_attempt():
        try:
            positions = await store.append(
                "loan-APEX-001",
                [_ev("CreditAnalysisCompleted", application_id="APEX-001", risk_tier="MEDIUM")],
                expected_version=3,
            )
            results.append("success")
            winner_positions.extend(positions)
        except OptimisticConcurrencyError as e:
            results.append(("occ", e.stream_id, e.expected, e.actual, str(e)))

    await asyncio.gather(agent_attempt(), agent_attempt())

    assert results.count("success") == 1, "Exactly one concurrent append must succeed"
    occ_results = [r for r in results if isinstance(r, tuple) and r[0] == "occ"]
    assert len(occ_results) == 1, "Exactly one must raise OptimisticConcurrencyError"

    # (a) Total events == 5
    assert await store.stream_version("loan-APEX-001") == 4
    events = await store.load_stream("loan-APEX-001")
    assert len(events) == 5

    # (b) Winner's event has stream_position=4
    assert winner_positions == [4]

    # (c) Loser received OptimisticConcurrencyError
    occ_result = next(r for r in results if isinstance(r, tuple))
    assert occ_result[0] == "occ"
    assert occ_result[2] == 3  # expected
    assert occ_result[3] == 4  # actual (winner advanced it)
    assert occ_result[4] == "OCC on 'loan-APEX-001': expected v3, actual v4"
