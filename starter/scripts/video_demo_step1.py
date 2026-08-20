"""
Video demo — Step 1: full decision history in one timed run (target: under 60 seconds).

From repository root:
  cd starter
  python scripts/video_demo_step1.py

Prints: loan event stream, audit tool output, agent sessions, compliance snapshot,
integrity check, and highlights correlation / causation metadata where present.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.event_store import InMemoryEventStore
from ledger.mcp_server import (
    init_ledger_mcp,
    submit_application,
    start_agent_session,
    record_credit_analysis,
    record_fraud_screening,
    record_compliance_check,
    generate_decision,
    record_human_review,
    get_application,
    get_audit_trail,
    get_agent_session,
    get_compliance,
    run_integrity_check,
)
from ledger.projections import ProjectionDaemon, ApplicationSummaryProjection, ComplianceAuditProjection


APP_ID = "APP-VIDEO-001"


def _ev(et: str, **p):
    return {"event_type": et, "event_version": 1, "payload": dict(p)}


async def main():
    t0 = time.perf_counter()
    store = InMemoryEventStore()
    app_proj = ApplicationSummaryProjection()
    comp_proj = ComplianceAuditProjection()
    init_ledger_mcp(store, {"ApplicationSummary": app_proj, "ComplianceAuditView": comp_proj})
    daemon = ProjectionDaemon(store, [app_proj, comp_proj])

    await submit_application(
        application_id=APP_ID,
        applicant_id="demo-applicant",
        requested_amount_usd=250_000,
        loan_purpose="equipment",
    )
    await store.append(
        f"loan-{APP_ID}",
        [_ev("DocumentUploaded", application_id=APP_ID, document_id="doc-demo-1")],
        expected_version=1,
    )
    await store.append(
        f"loan-{APP_ID}",
        [_ev("DocumentsProcessed", application_id=APP_ID, summary="demo")],
        expected_version=2,
    )
    await store.append(
        f"loan-{APP_ID}",
        [_ev("CreditAnalysisRequested", application_id=APP_ID)],
        expected_version=3,
    )
    await daemon._process_batch()

    await start_agent_session(
        agent_type="credit_analysis",
        session_id="sess-video-credit",
        application_id=APP_ID,
        model_version="v1",
    )
    await record_credit_analysis(
        application_id=APP_ID,
        agent_type="credit_analysis",
        session_id="sess-video-credit",
        model_version="v1",
        risk_tier="MEDIUM",
        recommended_limit_usd=200_000,
        confidence=0.88,
    )
    await store.append(
        f"loan-{APP_ID}",
        [
            _ev(
                "FraudScreeningRequested",
                application_id=APP_ID,
                triggered_by_event_id="credit-complete",
            )
        ],
        expected_version=4,
    )
    await start_agent_session(
        agent_type="fraud_detection",
        session_id="sess-video-fraud",
        application_id=APP_ID,
        model_version="v1",
    )
    await record_fraud_screening(
        application_id=APP_ID,
        agent_type="fraud_detection",
        session_id="sess-video-fraud",
        fraud_score=0.12,
    )
    await store.append(
        f"loan-{APP_ID}",
        [_ev("ComplianceCheckRequested", application_id=APP_ID)],
        expected_version=5,
    )
    await daemon._process_batch()

    compliance_snapshot_mid = json.loads(await get_compliance(APP_ID))
    mid_ts = None
    if compliance_snapshot_mid.get("events"):
        mid_ts = compliance_snapshot_mid["events"][len(compliance_snapshot_mid["events"]) // 2].get(
            "recorded_at"
        )

    await record_compliance_check(
        application_id=APP_ID,
        rule_id="REG-001",
        passed=True,
    )
    await generate_decision(
        application_id=APP_ID,
        recommendation="REFER",
        confidence=0.55,
    )
    await daemon._process_batch()
    await record_human_review(
        application_id=APP_ID,
        reviewer_id="reviewer-demo",
        final_decision="APPROVE",
        override=False,
    )
    await daemon._process_batch()

    loan_stream = await store.load_stream(f"loan-{APP_ID}")
    audit = json.loads(await get_audit_trail("loan", APP_ID))
    sess_credit = json.loads(await get_agent_session("credit_analysis", "sess-video-credit"))
    sess_fraud = json.loads(await get_agent_session("fraud_detection", "sess-video-fraud"))
    summary = json.loads(await get_application(APP_ID))
    compliance_now = json.loads(await get_compliance(APP_ID))
    compliance_as_of_mid = (
        json.loads(await get_compliance(APP_ID, as_of=mid_ts))
        if mid_ts
        else {"error": "no_mid_ts"}
    )
    integrity = json.loads(await run_integrity_check(entity_type="loan", entity_id=APP_ID))

    elapsed = time.perf_counter() - t0

    def _compact_stream(events: list[dict]) -> list[dict]:
        out = []
        for e in events:
            out.append(
                {
                    "stream_position": e.get("stream_position"),
                    "event_type": e.get("event_type"),
                    "event_version": e.get("event_version"),
                    "payload_keys": sorted((e.get("payload") or {}).keys()),
                    "metadata": e.get("metadata"),
                }
            )
        return out

    report = {
        "application_id": APP_ID,
        "elapsed_seconds": round(elapsed, 3),
        "under_60s_target": elapsed < 60.0,
        "loan_stream_compact": _compact_stream(loan_stream),
        "audit_trail_via_get_audit_trail": audit,
        "agent_session_credit_analysis": sess_credit,
        "agent_session_fraud_detection": sess_fraud,
        "application_summary_projection": summary,
        "compliance_projection_current": compliance_now,
        "compliance_at_as_of_mid_timestamp": compliance_as_of_mid,
        "temporal_compliance_hint": {
            "message": "Compare compliance_at_as_of_mid_timestamp (fewer events) vs compliance_projection_current",
            "example_as_of_iso": mid_ts,
            "resource_uri": f"ledger://applications/{APP_ID}/compliance?as_of={mid_ts}",
        },
        "integrity_check_loan_entity": integrity,
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
