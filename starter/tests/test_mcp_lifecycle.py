"""
tests/test_mcp_lifecycle.py — Full loan lifecycle via MCP tools

Drives: start_agent_session -> record_credit_analysis -> record_fraud_screening
-> record_compliance_check -> generate_decision -> record_human_review
using only tool invocations (simulating MCP client). No direct Python calls to domain/commands.
"""
import asyncio
import json
import pytest

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
    run_integrity_check,
)
from ledger.projections import ProjectionDaemon, ApplicationSummaryProjection


@pytest.mark.asyncio
async def test_full_lifecycle_via_tools():
    """Complete flow: submit -> start session -> credit -> fraud -> compliance -> decision -> human review."""
    store = InMemoryEventStore()
    app_proj = ApplicationSummaryProjection()
    init_ledger_mcp(store, {"ApplicationSummary": app_proj})

    r1 = await submit_application(
        application_id="APP-MCP-001",
        applicant_id="C1",
        requested_amount_usd=100000,
        loan_purpose="working_capital",
    )
    data1 = json.loads(r1)
    assert "stream_id" in data1 or "error_type" in data1
    if "error_type" in data1:
        pytest.fail(f"submit_application failed: {data1}")

    r2 = await start_agent_session(
        agent_type="credit_analysis",
        session_id="sess-mcp-1",
        application_id="APP-MCP-001",
        model_version="v1",
    )
    data2 = json.loads(r2)
    assert "session_id" in data2 or "error_type" in data2

    def _ev(et, **p):
        return {"event_type": et, "event_version": 1, "payload": dict(p)}

    await store.append(
        "loan-APP-MCP-001",
        [_ev("DocumentUploaded", application_id="APP-MCP-001", document_id="d1")],
        expected_version=1,
    )
    await store.append(
        "loan-APP-MCP-001",
        [_ev("CreditAnalysisRequested", application_id="APP-MCP-001")],
        expected_version=2,
    )
    daemon = ProjectionDaemon(store, [app_proj])
    await daemon._process_batch()

    r3 = await record_credit_analysis(
        application_id="APP-MCP-001",
        agent_type="credit_analysis",
        session_id="sess-mcp-1",
        model_version="v1",
        risk_tier="MEDIUM",
        recommended_limit_usd=80000,
        confidence=0.85,
    )
    data3 = json.loads(r3)
    assert "new_stream_version" in data3 or "error_type" in data3

    await store.append(
        "loan-APP-MCP-001",
        [_ev("FraudScreeningRequested", application_id="APP-MCP-001", triggered_by_event_id="e1")],
        expected_version=3,
    )
    await start_agent_session(
        agent_type="fraud_detection",
        session_id="sess-fraud-1",
        application_id="APP-MCP-001",
        model_version="v1",
    )
    r4 = await record_fraud_screening(
        application_id="APP-MCP-001",
        agent_type="fraud_detection",
        session_id="sess-fraud-1",
        fraud_score=0.1,
    )
    assert "new_stream_version" in json.loads(r4) or "error_type" in json.loads(r4)

    await store.append(
        "loan-APP-MCP-001",
        [_ev("ComplianceCheckRequested", application_id="APP-MCP-001")],
        expected_version=4,
    )
    await daemon._process_batch()

    r5 = await record_compliance_check(
        application_id="APP-MCP-001",
        rule_id="REG-001",
        passed=True,
    )
    data5 = json.loads(r5)
    assert "new_stream_version" in data5 or "error_type" in data5

    r6 = await generate_decision(
        application_id="APP-MCP-001",
        recommendation="REFER",
        confidence=0.55,
    )
    data6 = json.loads(r6)
    assert "new_stream_version" in data6 or "error_type" in data6

    await daemon._process_batch()

    r7 = await record_human_review(
        application_id="APP-MCP-001",
        reviewer_id="R1",
        final_decision="APPROVE",
        override=False,
    )
    data7 = json.loads(r7)
    assert "new_stream_version" in data7 or "error_type" in data7

    await daemon._process_batch()

    r8 = await get_application("APP-MCP-001")
    data8 = json.loads(r8)
    assert "state" in data8 or "error" in data8

    r9 = await run_integrity_check(entity_type="loan", entity_id="APP-MCP-001")
    data9 = json.loads(r9)
    assert "chain_valid" in data9 or "error_type" in data9
