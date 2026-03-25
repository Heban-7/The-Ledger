"""
ledger/integrity/gas_town.py — Gas Town Agent Memory Reconstruction

Replays agent-{agent_type}-{session_id} after crash. Last N events kept verbatim;
older events summarized. Detects incomplete work (decision / tool in flight).
"""
from __future__ import annotations
from dataclasses import dataclass, field


# Terminal / completion-like event types for session health
_SESSION_COMPLETE_TYPES = frozenset(
    {
        "AgentSessionCompleted",
        "AgentSessionFailed",
        "AgentOutputWritten",
    }
)
_IN_PROGRESS_TYPES = frozenset(
    {
        "AgentNodeExecuted",
        "AgentToolCalled",
        "AgentReasoningStep",
    }
)


@dataclass
class AgentContext:
    """LLM-consumable bundle for resuming work after restart."""

    context_text: str
    last_event_position: int
    pending_work: list[str]
    session_health_status: str
    model_version: str | None
    verbatim_tail: list[dict] = field(default_factory=list)
    gas_town_notes: str = ""


async def reconstruct_agent_context(
    store,
    agent_id: str,
    session_id: str,
    agent_type: str | None = None,
    token_budget: int = 8000,
    verbatim_tail_count: int = 3,
) -> AgentContext:
    """
    Load AgentSession stream; summarize for token budget; preserve last K events verbatim.
    NEEDS_RECONCILIATION if stream ends on an in-progress event without completion.
    """
    atype = agent_type or (agent_id.split("-")[0] if "-" in agent_id else "credit_analysis")
    stream_id = f"agent-{atype}-{session_id}"
    events = await store.load_stream(stream_id)

    if not events:
        return AgentContext(
            context_text="",
            last_event_position=-1,
            pending_work=[],
            session_health_status="EMPTY",
            model_version=None,
            verbatim_tail=[],
            gas_town_notes="No events in session stream; call start_agent_session first.",
        )

    last_pos = events[-1].get("stream_position", len(events) - 1)
    last_type = events[-1].get("event_type", "")
    mv = None
    for ev in events:
        if ev.get("event_type") == "AgentSessionStarted":
            p = ev.get("payload", {}) or {}
            mv = p.get("model_version") or mv

    pending: list[str] = []
    needs_reconciliation = False
    if last_type in _IN_PROGRESS_TYPES:
        needs_reconciliation = True
        pending.append(f"Last event {last_type} has no following completion — replay or reconcile.")
    if last_type not in _SESSION_COMPLETE_TYPES and last_type not in ("AgentSessionStarted",):
        if any(ev.get("event_type") in _IN_PROGRESS_TYPES for ev in events[-3:]):
            needs_reconciliation = needs_reconciliation or True

    summary_parts: list[str] = []
    head = events[:-verbatim_tail_count] if len(events) > verbatim_tail_count else []
    for i, ev in enumerate(head):
        et = ev.get("event_type", "")
        summary_parts.append(f"pos={ev.get('stream_position', i)}:{et}")

    verbatim = []
    for e in events[-verbatim_tail_count:]:
        verbatim.append(
            {
                "stream_position": e.get("stream_position"),
                "event_type": e.get("event_type"),
                "payload": e.get("payload"),
                "metadata": e.get("metadata"),
            }
        )

    notes = (
        "Gas Town: replay this stream before executing new side effects. "
        "Correlate outbound work via metadata.correlation_id on store appends."
    )
    context_text = (
        "[session replay summary]\n"
        + "; ".join(summary_parts)
        + "\n[verbatim tail]\n"
        + repr(verbatim)
    )
    if len(context_text) > token_budget:
        context_text = context_text[:token_budget] + "\n... [truncated to token_budget]"

    return AgentContext(
        context_text=context_text,
        last_event_position=last_pos,
        pending_work=pending,
        session_health_status="NEEDS_RECONCILIATION" if needs_reconciliation else "OK",
        model_version=mv,
        verbatim_tail=verbatim,
        gas_town_notes=notes,
    )
