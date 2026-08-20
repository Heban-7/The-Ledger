"""
Video demo — Step 5: Gas Town — reconstruct_agent_context after simulated work on a session.

From repository root:
  cd starter
  python scripts/video_demo_gas_town.py

Narration: a new worker reloads the same session stream from the store (in production: same Postgres);
in-memory here proves the reconstruction API.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.event_store import InMemoryEventStore
from ledger.integrity.gas_town import reconstruct_agent_context
from ledger.mcp_server import init_ledger_mcp, start_agent_session
from ledger.projections import ApplicationSummaryProjection, ComplianceAuditProjection


async def main():
    store = InMemoryEventStore()
    init_ledger_mcp(
        store,
        {"ApplicationSummary": ApplicationSummaryProjection(), "ComplianceAuditView": ComplianceAuditProjection()},
    )
    app_id = "APP-GAS-001"
    session_id = "sess-gas-demo"
    agent_type = "credit_analysis"

    await start_agent_session(
        agent_type=agent_type,
        session_id=session_id,
        application_id=app_id,
        model_version="v1",
    )
    ver = await store.stream_version(f"agent-{agent_type}-{session_id}")
    await store.append(
        f"agent-{agent_type}-{session_id}",
        [
            {
                "event_type": "AgentToolCalled",
                "event_version": 1,
                "payload": {"tool_name": "credit_bureau_pull", "application_id": app_id},
            }
        ],
        expected_version=ver,
    )

    ctx = await reconstruct_agent_context(
        store,
        agent_id=f"{agent_type}-agent",
        session_id=session_id,
        agent_type=agent_type,
    )
    out = {
        "session_stream_id": f"agent-{agent_type}-{session_id}",
        "session_health_status": ctx.session_health_status,
        "model_version": ctx.model_version,
        "last_event_position": ctx.last_event_position,
        "pending_work": ctx.pending_work,
        "gas_town_notes": ctx.gas_town_notes,
        "verbatim_tail_preview": ctx.verbatim_tail[-2:] if ctx.verbatim_tail else [],
        "context_text_head": (ctx.context_text[:500] + "…") if len(ctx.context_text) > 500 else ctx.context_text,
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
