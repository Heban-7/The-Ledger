"""
tests/test_regulatory_package.py — Regulatory Package Generator Tests (Phase 6 Bonus)

Validates the generation and cryptographic integrity of self-contained regulatory packages.
"""
import json
import pytest
from datetime import datetime, timezone

from ledger.event_store import InMemoryEventStore
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
from ledger.regulatory.package import generate_regulatory_package


def _ev(event_type: str, **payload):
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_generate_regulatory_package_complete():
    """
    Generates a full regulatory audit package and verifies all 5 core requirements:
    1. Complete event stream.
    2. Projection states at examination date.
    3. Hash chain integrity verification.
    4. Plain-English narrative lifecycle.
    5. AI agent governance metadata.
    """
    store = InMemoryEventStore()
    app_id = "APP-REG-001"

    # Step 1: Submit application
    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-REG-CORP",
            requested_amount_usd=500_000,
            loan_purpose="equipment",
        ),
        store,
    )

    # Step 2: Document processing
    await store.append(
        f"loan-{app_id}",
        [
            _ev("DocumentUploaded", application_id=app_id, document_id="doc-1"),
            _ev("DocumentsProcessed", application_id=app_id, summary="all verified"),
            _ev("CreditAnalysisRequested", application_id=app_id),
        ],
        expected_version=1,
    )

    # Step 3: Credit Analysis
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_type="credit_analysis",
            session_id="sess-reg-credit",
            application_id=app_id,
            model_version="v2.1",
        ),
        store,
    )
    await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id,
            agent_type="credit_analysis",
            session_id="sess-reg-credit",
            model_version="v2.1",
            risk_tier="LOW",
            recommended_limit_usd=500_000,
            confidence=0.92,
            duration_ms=300,
            input_data_hash="hash-reg-credit",
        ),
        store,
    )

    # Step 4: Fraud Screening
    await store.append(
        f"loan-{app_id}",
        [_ev("FraudScreeningRequested", application_id=app_id)],
        expected_version=4,
    )
    await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_type="fraud_detection",
            session_id="sess-reg-fraud",
            application_id=app_id,
            model_version="v1.4",
        ),
        store,
    )
    await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=app_id,
            agent_type="fraud_detection",
            session_id="sess-reg-fraud",
            fraud_score=0.04,
            anomaly_flags=[],
            model_version="v1.4",
            input_data_hash="hash-reg-fraud",
        ),
        store,
    )

    # Step 5: Compliance Check
    await store.append(
        f"loan-{app_id}",
        [_ev("ComplianceCheckRequested", application_id=app_id)],
        expected_version=5,
    )
    await handle_compliance_check(
        ComplianceCheckCommand(application_id=app_id, rule_id="REG-001", passed=True),
        store,
    )

    # Step 6: Decision Generation (APPROVE)
    await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=app_id,
            recommendation="APPROVE",
            confidence=0.92,
            approved_amount_usd=500_000,
        ),
        store,
    )

    # Generate full examination package
    pkg = await generate_regulatory_package(store, app_id)

    # Verify package properties
    assert pkg.application_id == app_id
    assert pkg.total_events > 5
    assert len(pkg.event_stream) == pkg.total_events

    # Verify projection states
    proj_states = pkg.projection_states_at_examination_date
    assert "application_summary" in proj_states
    assert proj_states["application_summary"].get("state") == "APPROVED"
    assert proj_states["application_summary"].get("approved_amount_usd") == 500_000.0

    # Verify integrity verification
    assert pkg.integrity_verification["chain_valid"] is True
    assert len(pkg.integrity_verification["integrity_hash"]) == 64  # SHA-256 hex string

    # Verify plain-English narrative
    assert len(pkg.narrative_lifecycle) == pkg.total_events
    narrative_text = " ".join(pkg.narrative_lifecycle)
    assert "submitted application" in narrative_text
    assert "Credit analysis completed" in narrative_text
    assert "Fraud screening completed" in narrative_text
    assert "APPROVED" in narrative_text

    # Verify AI agent governance metadata
    assert len(pkg.agent_governance_metadata) >= 2
    credit_meta = [m for m in pkg.agent_governance_metadata if "credit" in m.get("session_id", "")]
    assert len(credit_meta) >= 1
    assert credit_meta[0]["model_version"] == "v2.1"
    assert credit_meta[0]["confidence_score"] == 0.92

    # Verify package serialization to valid JSON
    pkg_json = pkg.to_json()
    parsed = json.loads(pkg_json)
    assert parsed["application_id"] == app_id
    assert len(parsed["package_signature_sha256"]) == 64
