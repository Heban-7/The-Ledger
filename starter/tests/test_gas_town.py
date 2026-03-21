"""
tests/test_gas_town.py — Gas Town recovery test

Append 5 events, call reconstruct_agent_context without in-memory agent.
Verify reconstructed context is sufficient to continue.
"""
import pytest

from ledger.event_store import InMemoryEventStore
from ledger.integrity.gas_town import reconstruct_agent_context


def _ev(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_reconstruct_agent_context_after_events():
    store = InMemoryEventStore()
    stream_id = "agent-credit_analysis-sess-123"
    events = [
        _ev("AgentSessionStarted", session_id="sess-123", agent_type="credit_analysis", model_version="v1"),
        _ev("AgentNodeExecuted", session_id="sess-123", node_name="analyze", node_sequence=1),
        _ev("AgentNodeExecuted", session_id="sess-123", node_name="score", node_sequence=2),
        _ev("AgentOutputWritten", session_id="sess-123", application_id="APP-001"),
        _ev("AgentSessionCompleted", session_id="sess-123", application_id="APP-001"),
    ]
    for i, ev in enumerate(events):
        ver = await store.stream_version(stream_id)
        await store.append(stream_id, [ev], expected_version=ver)

    ctx = await reconstruct_agent_context(store, agent_id="credit_analysis", session_id="sess-123", agent_type="credit_analysis")

    assert ctx.last_event_position >= 0
    assert "AgentSessionStarted" in ctx.context_text or "AgentSessionCompleted" in ctx.context_text
    assert ctx.session_health_status in ("OK", "NEEDS_RECONCILIATION")
