"""
ledger/integrity/gas_town.py — Gas Town Agent Memory Pattern

Reconstruct agent context from event stream after crash.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class AgentContext:
    context_text: str
    last_event_position: int
    pending_work: list
    session_health_status: str


async def reconstruct_agent_context(
    store,
    agent_id: str,
    session_id: str,
    agent_type: str | None = None,
    token_budget: int = 8000,
) -> AgentContext:
    """
    Load full AgentSession stream, summarize for token budget.
    Preserve last 3 events verbatim. Flag NEEDS_RECONCILIATION if partial decision.
    """
    atype = agent_type or agent_id.split("-")[0] if "-" in agent_id else "credit_analysis"
    stream_id = f"agent-{atype}-{session_id}"
    events = await store.load_stream(stream_id)

    if not events:
        return AgentContext(
            context_text="",
            last_event_position=-1,
            pending_work=[],
            session_health_status="EMPTY",
        )

    last_pos = events[-1].get("stream_position", len(events) - 1)
    last_type = events[-1].get("event_type", "")
    pending = []

    needs_reconciliation = False
    if last_type in ("AgentNodeExecuted", "AgentToolCalled") and len(events) >= 2:
        prev = events[-2].get("event_type", "")
        if prev in ("AgentOutputWritten", "AgentSessionCompleted"):
            pass
        else:
            needs_reconciliation = True

    summary_parts = []
    for i, ev in enumerate(events[:-3]):
        et = ev.get("event_type", "")
        summary_parts.append(f"{i}: {et}")

    verbatim = [{"event_type": e.get("event_type"), "payload": e.get("payload")} for e in events[-3:]]
    context_text = "Summary: " + "; ".join(summary_parts) + "\nLast 3: " + str(verbatim)
    if len(context_text) > token_budget:
        context_text = context_text[:token_budget] + "..."

    return AgentContext(
        context_text=context_text,
        last_event_position=last_pos,
        pending_work=pending,
        session_health_status="NEEDS_RECONCILIATION" if needs_reconciliation else "OK",
    )
