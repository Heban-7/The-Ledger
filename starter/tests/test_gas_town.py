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


@pytest.mark.asyncio
async def test_reconstruct_agent_context_flags_in_flight_work_after_crash():
    store = InMemoryEventStore()
    stream_id = "agent-credit_analysis-sess-crash-1"
    events = [
        _ev("AgentSessionStarted", session_id="sess-crash-1", agent_type="credit_analysis", model_version="gpt-5"),
        _ev("AgentNodeExecuted", session_id="sess-crash-1", node_name="collect_docs", node_sequence=1),
        _ev("AgentToolCalled", session_id="sess-crash-1", tool_name="credit_bureau.pull"),
    ]
    for ev in events:
        ver = await store.stream_version(stream_id)
        await store.append(stream_id, [ev], expected_version=ver)

    ctx = await reconstruct_agent_context(
        store,
        agent_id="credit_analysis",
        session_id="sess-crash-1",
        agent_type="credit_analysis",
    )

    assert ctx.session_health_status == "NEEDS_RECONCILIATION"
    assert ctx.pending_work
    assert "no following completion" in ctx.pending_work[0]
    assert ctx.model_version == "gpt-5"
    assert ctx.verbatim_tail[-1]["event_type"] == "AgentToolCalled"
    assert "correlation_id" in ctx.gas_town_notes


@pytest.mark.asyncio
async def test_reconstruct_agent_context_respects_token_budget_and_tail_count():
    store = InMemoryEventStore()
    stream_id = "agent-credit_analysis-sess-budget-1"

    await store.append(
        stream_id,
        [_ev("AgentSessionStarted", session_id="sess-budget-1", agent_type="credit_analysis", model_version="v2")],
        expected_version=-1,
    )
    for i in range(20):
        ver = await store.stream_version(stream_id)
        await store.append(
            stream_id,
            [_ev("AgentReasoningStep", session_id="sess-budget-1", step=i, rationale=("x" * 50))],
            expected_version=ver,
        )
    ver = await store.stream_version(stream_id)
    await store.append(
        stream_id,
        [_ev("AgentSessionCompleted", session_id="sess-budget-1", application_id="APP-42")],
        expected_version=ver,
    )

    ctx = await reconstruct_agent_context(
        store,
        agent_id="credit_analysis",
        session_id="sess-budget-1",
        agent_type="credit_analysis",
        token_budget=180,
        verbatim_tail_count=4,
    )

    assert ctx.session_health_status == "OK"
    assert len(ctx.verbatim_tail) == 4
    assert ctx.verbatim_tail[-1]["event_type"] == "AgentSessionCompleted"
    assert len(ctx.context_text) <= 220
    assert "[truncated to token_budget]" in ctx.context_text
