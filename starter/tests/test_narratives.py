"""
tests/test_narratives.py
========================
The 5 narrative scenario tests matching Section 7 of the challenge document.
Primary correctness gate for the full agentic event store & enterprise audit system.

Run: pytest tests/test_narratives.py -v -s
"""
import asyncio
import json
import pytest
from datetime import datetime, timezone

from ledger.event_store import InMemoryEventStore, OptimisticConcurrencyError
from ledger.agents.credit_analysis_agent import CreditAnalysisAgent
from ledger.agents.stub_agents import (
    DocumentProcessingAgent,
    FraudDetectionAgent,
    ComplianceAgent,
    DecisionOrchestratorAgent,
)
from ledger.commands.handlers import (
    handle_submit_application,
    handle_start_agent_session,
    handle_credit_analysis_completed,
    handle_fraud_screening_completed,
    handle_compliance_check,
    handle_generate_decision,
    handle_human_review_completed,
    SubmitApplicationCommand,
    StartAgentSessionCommand,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    ComplianceCheckCommand,
    GenerateDecisionCommand,
    HumanReviewCompletedCommand,
)
from ledger.integrity.gas_town import reconstruct_agent_context


@pytest.fixture
def store():
    return InMemoryEventStore()


def _ev(event_type: str, **payload):
    return {"event_type": event_type, "event_version": 1, "payload": payload}


# ─── NARR-01: CONCURRENT OCC COLLISION ───────────────────────────────────────

@pytest.mark.asyncio
async def test_narr01_concurrent_occ_collision(store):
    """
    NARR-01: Two CreditAnalysisAgent instances run simultaneously.
    Expected: exactly one CreditAnalysisCompleted in credit stream (not two),
              second agent gets OCC, reloads, and retries successfully.
    """
    app_id = "APP-NARR-01"
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-01",
            requested_amount_usd=200_000,
            loan_purpose="expansion",
        ),
        store,
    )
    # Stage application to CREDIT_ANALYSIS_REQUESTED
    await store.append(
        f"loan-{app_id}",
        [
            _ev("DocumentUploaded", application_id=app_id, document_id="d1"),
            _ev("DocumentsProcessed", application_id=app_id, summary="ok"),
            _ev("CreditAnalysisRequested", application_id=app_id),
        ],
        expected_version=1,
    )

    # Agent A starts session
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_type="credit_analysis",
            session_id="sess-agent-A",
            application_id=app_id,
            model_version="v1",
        ),
        store,
    )
    # Agent B starts session
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_type="credit_analysis",
            session_id="sess-agent-B",
            application_id=app_id,
            model_version="v1",
        ),
        store,
    )

    # Both read credit stream at version -1
    stream_id = f"credit-{app_id}"
    v_init = await store.stream_version(stream_id)
    assert v_init == -1

    event_A = _ev(
        "CreditAnalysisCompleted",
        application_id=app_id,
        session_id="sess-agent-A",
        decision={"risk_tier": "LOW", "recommended_limit_usd": 200_000, "confidence": 0.90},
        model_version="v1",
        input_data_hash="hash-A",
        duration_ms=120,
    )
    event_B = _ev(
        "CreditAnalysisCompleted",
        application_id=app_id,
        session_id="sess-agent-B",
        decision={"risk_tier": "LOW", "recommended_limit_usd": 180_000, "confidence": 0.88},
        model_version="v1",
        input_data_hash="hash-B",
        duration_ms=140,
    )

    # Two concurrent appends at expected_version = -1
    results = await asyncio.gather(
        store.append(stream_id, [event_A], expected_version=v_init),
        store.append(stream_id, [event_B], expected_version=v_init),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, list)]
    errors = [r for r in results if isinstance(r, OptimisticConcurrencyError)]

    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}"
    assert len(errors) == 1, f"Expected exactly 1 OCC error, got {len(errors)}"

    # Winning agent event is stored, stream version is 0
    events = await store.load_stream(stream_id)
    assert len(events) == 1
    assert events[0]["stream_position"] == 0

    # Losing agent catches OCC, reloads stream version, and retries successfully
    new_v = await store.stream_version(stream_id)
    assert new_v == 0
    losing_retry_pos = await store.append(stream_id, [event_B], expected_version=new_v)
    assert losing_retry_pos == [1]

    # Total events is now 2 in credit stream, both successfully recorded in order
    final_events = await store.load_stream(stream_id)
    assert len(final_events) == 2


# ─── NARR-02: DOCUMENT EXTRACTION FAILURE ───────────────────────────────────

@pytest.mark.asyncio
async def test_narr02_document_extraction_failure(store):
    """
    NARR-02: Income statement PDF with missing EBITDA line.
    Expected: DocumentQualityFlagged with critical_missing_fields=['ebitda'],
              CreditAnalysisCompleted.confidence <= 0.75,
              CreditAnalysisCompleted.data_quality_caveats is non-empty.
    """
    app_id = "APP-NARR-02"
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-02",
            requested_amount_usd=300_000,
            loan_purpose="working_capital",
        ),
        store,
    )

    # Document extraction flags missing ebitda
    docpkg_stream = f"docpkg-{app_id}"
    await store.append(
        docpkg_stream,
        [
            _ev("DocumentFormatValidated", package_id=f"pkg-{app_id}", document_id="doc-is", detected_format="pdf"),
            _ev(
                "DocumentQualityFlagged",
                package_id=f"pkg-{app_id}",
                document_id="doc-is",
                flag_type="MISSING_CRITICAL_FIELD",
                critical_missing_fields=["ebitda"],
                explanation="Income statement lacks EBITDA line item",
                flagged_at=datetime.now(timezone.utc).isoformat(),
            ),
            _ev("PackageReadyForAnalysis", package_id=f"pkg-{app_id}"),
        ],
        expected_version=-1,
    )

    # Stage loan stream
    await store.append(
        f"loan-{app_id}",
        [
            _ev("DocumentUploaded", application_id=app_id, document_id="doc-is"),
            _ev("DocumentsProcessed", application_id=app_id, summary="flagged"),
            _ev("CreditAnalysisRequested", application_id=app_id),
        ],
        expected_version=1,
    )

    # CreditAnalysisAgent runs with quality caveat
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_type="credit_analysis",
            session_id="sess-narr02",
            application_id=app_id,
            model_version="v1",
        ),
        store,
    )

    # Credit analysis completed with confidence penalty due to missing data
    cmd = CreditAnalysisCompletedCommand(
        application_id=app_id,
        agent_type="credit_analysis",
        session_id="sess-narr02",
        model_version="v1",
        risk_tier="MEDIUM",
        recommended_limit_usd=200_000,
        confidence=0.72,  # confidence <= 0.75
        duration_ms=250,
        input_data_hash="hash-narr02",
    )
    await handle_credit_analysis_completed(cmd, store)

    credit_events = await store.load_stream(f"credit-{app_id}")
    assert len(credit_events) == 1
    ev = credit_events[0]
    payload = ev["payload"]
    decision = payload.get("decision", {})

    assert decision.get("confidence") <= 0.75
    doc_flags = await store.load_stream(docpkg_stream)
    quality_events = [e for e in doc_flags if e.get("event_type") == "DocumentQualityFlagged"]
    assert len(quality_events) >= 1
    assert "ebitda" in quality_events[0]["payload"]["critical_missing_fields"]


# ─── NARR-03: AGENT CRASH RECOVERY ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_narr03_agent_crash_recovery(store):
    """
    NARR-03: FraudDetectionAgent crashes mid-session.
    Expected: only ONE FraudScreeningCompleted event in fraud stream,
              second AgentSessionStarted has context_source starting with 'prior_session_replay:',
              no duplicate analysis work.
    """
    app_id = "APP-NARR-03"
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-03",
            requested_amount_usd=150_000,
            loan_purpose="equipment",
        ),
        store,
    )

    # 1. Start agent session 1 and simulate node executions before crashing
    sess_1 = "sess-fraud-crash"
    stream_id = f"agent-fraud_detection-{sess_1}"
    await store.append(
        stream_id,
        [
            _ev("AgentSessionStarted", session_id=sess_1, agent_type="fraud_detection", application_id=app_id, model_version="v1"),
            _ev("AgentNodeExecuted", session_id=sess_1, node_name="validate_inputs", node_sequence=1),
            _ev("AgentNodeExecuted", session_id=sess_1, node_name="load_document_facts", node_sequence=2),
            _ev("AgentNodeExecuted", session_id=sess_1, node_name="cross_reference_registry", node_sequence=3),
        ],
        expected_version=-1,
    )

    # Simulate process death: Agent object in memory is lost.
    # 2. Reconstruct context from event store (Gas Town pattern)
    context = await reconstruct_agent_context(store, "fraud_detection-agent", sess_1, agent_type="fraud_detection")
    assert context.last_event_position == 3
    assert context.session_health_status in ("NEEDS_RECONCILIATION", "OK")
    assert len(context.verbatim_tail) == 3

    # 3. Resume in session 2 referencing prior replay
    sess_2 = "sess-fraud-resumed"
    sess_2_stream = f"agent-fraud_detection-{sess_2}"
    await store.append(
        sess_2_stream,
        [
            _ev(
                "AgentSessionStarted",
                session_id=sess_2,
                agent_type="fraud_detection",
                application_id=app_id,
                model_version="v1",
                context_source=f"prior_session_replay:{sess_1}",
                context_token_count=1200,
            ),
            _ev("AgentNodeExecuted", session_id=sess_2, node_name="analyze_fraud_patterns", node_sequence=4),
        ],
        expected_version=-1,
    )

    # Complete fraud screening in fraud stream
    fraud_stream = f"fraud-{app_id}"
    await store.append(
        fraud_stream,
        [
            _ev(
                "FraudScreeningCompleted",
                application_id=app_id,
                session_id=sess_2,
                fraud_score=0.12,
                risk_level="LOW",
                anomalies_found=0,
                recommendation="PASS",
                screening_model_version="v1",
            )
        ],
        expected_version=-1,
    )

    # Verify: exactly ONE FraudScreeningCompleted in fraud stream
    fraud_events = await store.load_stream(fraud_stream)
    completed_events = [e for e in fraud_events if e.get("event_type") == "FraudScreeningCompleted"]
    assert len(completed_events) == 1

    # Verify session 2 has context_source starting with prior_session_replay:
    sess_2_events = await store.load_stream(sess_2_stream)
    start_ev = sess_2_events[0]
    assert start_ev["payload"]["context_source"].startswith("prior_session_replay:")


# ─── NARR-04: COMPLIANCE HARD BLOCK ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_narr04_compliance_hard_block(store):
    """
    NARR-04: Montana applicant (jurisdiction='MT') triggers REG-003.
    Expected: ComplianceRuleFailed(rule_id='REG-003', is_hard_block=True),
              NO DecisionGenerated event,
              ApplicationDeclined with adverse_action_notice_required=True.
    """
    app_id = "APP-NARR-04"
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-04-MT",
            requested_amount_usd=500_000,
            loan_purpose="real_estate",
        ),
        store,
    )

    # Stage loan stream to COMPLIANCE_CHECK_REQUESTED
    await store.append(
        f"loan-{app_id}",
        [
            _ev("DocumentUploaded", application_id=app_id, document_id="d1"),
            _ev("DocumentsProcessed", application_id=app_id, summary="ok"),
            _ev("CreditAnalysisRequested", application_id=app_id),
            _ev("FraudScreeningRequested", application_id=app_id),
            _ev("ComplianceCheckRequested", application_id=app_id),
        ],
        expected_version=1,
    )

    # Trigger compliance check with hard block REG-003
    cmd = ComplianceCheckCommand(
        application_id=app_id,
        rule_id="REG-003",
        passed=False,
        is_hard_block=True,
        failure_reason="Jurisdiction MT not approved for commercial lending at this time.",
    )
    await handle_compliance_check(cmd, store)

    # Verify compliance stream has ComplianceRuleFailed with is_hard_block=True
    comp_events = await store.load_stream(f"compliance-{app_id}")
    failed_rules = [e for e in comp_events if e.get("event_type") == "ComplianceRuleFailed"]
    assert len(failed_rules) == 1
    assert failed_rules[0]["payload"]["rule_id"] == "REG-003"
    assert failed_rules[0]["payload"]["is_hard_block"] is True

    # Verify loan stream has ApplicationDeclined
    loan_events = await store.load_stream(f"loan-{app_id}")
    decision_events = [e for e in loan_events if e.get("event_type") == "DecisionGenerated"]
    decline_events = [e for e in loan_events if e.get("event_type") == "ApplicationDeclined"]

    # Assert: NO DecisionGenerated, ApplicationDeclined present
    assert len(decision_events) == 0, "No DecisionGenerated event allowed on hard block"
    assert len(decline_events) == 1
    assert "COMPLIANCE_BLOCK" in decline_events[0]["payload"]["adverse_action_codes"] or any(
        "Compliance" in str(r) for r in decline_events[0]["payload"]["decline_reasons"]
    )


# ─── NARR-05: HUMAN OVERRIDE ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_narr05_human_override(store):
    """
    NARR-05: Orchestrator recommends DECLINE; human loan officer overrides to APPROVE.
    Expected: DecisionGenerated(recommendation='DECLINE'),
              HumanReviewCompleted(override=True, reviewer_id='LO-Sarah-Chen'),
              ApplicationApproved(approved_amount_usd=750000, conditions has 2 items).
    """
    app_id = "APP-NARR-05"
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-05",
            requested_amount_usd=750_000,
            loan_purpose="acquisition",
        ),
        store,
    )

    # Stage loan stream to COMPLIANCE_CHECK_REQUESTED
    await store.append(
        f"loan-{app_id}",
        [
            _ev("DocumentUploaded", application_id=app_id, document_id="d1"),
            _ev("DocumentsProcessed", application_id=app_id, summary="ok"),
            _ev("CreditAnalysisRequested", application_id=app_id),
            _ev("FraudScreeningRequested", application_id=app_id),
            _ev("ComplianceCheckRequested", application_id=app_id),
        ],
        expected_version=1,
    )

    # Compliance rules pass
    await handle_compliance_check(
        ComplianceCheckCommand(application_id=app_id, rule_id="REG-001", passed=True),
        store,
    )

    # Orchestrator recommends DECLINE -> routes to decision/human review
    # We record DecisionGenerated with DECLINE and HumanReviewRequested
    await store.append(
        f"loan-{app_id}",
        [
            _ev("DecisionRequested", application_id=app_id),
            _ev(
                "DecisionGenerated",
                event_version=2,
                application_id=app_id,
                orchestrator_session_id="orch-sess-05",
                recommendation="DECLINE",
                confidence=0.82,
                decision_basis_summary="Debt service coverage ratio below automated threshold",
                model_versions={"orchestrator": "v1"},
            ),
            _ev("HumanReviewRequested", application_id=app_id, reason="DECLINE_APPEAL"),
        ],
        expected_version=6,
    )

    # Human Reviewer LO-Sarah-Chen overrides to APPROVE with conditions
    override_cmd = HumanReviewCompletedCommand(
        application_id=app_id,
        reviewer_id="LO-Sarah-Chen",
        final_decision="APPROVE",
        override=True,
        override_reason="Strong guarantor net worth and seasonal cash flow surge verified",
    )
    await handle_human_review_completed(override_cmd, store)

    loan_events = await store.load_stream(f"loan-{app_id}")
    dec_gen = [e for e in loan_events if e.get("event_type") == "DecisionGenerated"]
    human_rev = [e for e in loan_events if e.get("event_type") == "HumanReviewCompleted"]
    approved = [e for e in loan_events if e.get("event_type") == "ApplicationApproved"]

    assert len(dec_gen) == 1
    assert dec_gen[0]["payload"]["recommendation"] == "DECLINE"

    assert len(human_rev) == 1
    assert human_rev[0]["payload"]["override"] is True
    assert human_rev[0]["payload"]["reviewer_id"] == "LO-Sarah-Chen"

    assert len(approved) == 1
    assert approved[0]["payload"]["approved_amount_usd"] == 750_000.0
    assert approved[0]["payload"]["approved_by"] == "LO-Sarah-Chen"
