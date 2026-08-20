"""
tests/test_what_if.py — What-If Counterfactual Projection Tests (Phase 6 Bonus)

Validates counterfactual scenario replay:
1. Counterfactual event injection produces materially divergent outcomes.
2. Causal dependency filtering skips dependent downstream events.
3. Event store immutability guarantee: real event store is NEVER mutated.
"""
import pytest
from ledger.event_store import InMemoryEventStore
from ledger.commands.handlers import handle_submit_application, SubmitApplicationCommand
from ledger.what_if.projector import run_what_if


def _ev(event_type: str, **payload):
    return {"event_type": event_type, "event_version": 1, "payload": payload}


@pytest.mark.asyncio
async def test_what_if_high_risk_tier_produces_decline():
    """
    Scenario: Real application was approved under MEDIUM risk tier.
    Counterfactual: Substitute HIGH risk tier for CreditAnalysisCompleted.
    Expected: Counterfactual outcome produces DECLINE; real store is untouched.
    """
    store = InMemoryEventStore()
    app_id = "APP-WHATIF-001"

    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-WHATIF",
            requested_amount_usd=250_000,
            loan_purpose="working_capital",
        ),
        store,
    )

    # Append real stream events leading to approval
    await store.append(
        f"loan-{app_id}",
        [
            _ev("DocumentUploaded", application_id=app_id, document_id="d1"),
            _ev("DocumentsProcessed", application_id=app_id, summary="ok"),
            _ev("CreditAnalysisRequested", application_id=app_id),
        ],
        expected_version=1,
    )

    # Real credit analysis: MEDIUM risk, confidence 0.85
    real_credit_ev = _ev(
        "CreditAnalysisCompleted",
        application_id=app_id,
        session_id="sess-real-credit",
        decision={"risk_tier": "MEDIUM", "recommended_limit_usd": 250_000, "confidence": 0.85},
        model_version="v1",
    )
    await store.append(f"credit-{app_id}", [real_credit_ev], expected_version=-1)

    # Real downstream events: DecisionGenerated (APPROVE) and ApplicationApproved
    await store.append(
        f"loan-{app_id}",
        [
            _ev(
                "DecisionGenerated",
                event_version=2,
                application_id=app_id,
                orchestrator_session_id="orch-real",
                recommendation="APPROVE",
                confidence=0.85,
                approved_amount_usd="250000",
            ),
            _ev(
                "ApplicationApproved",
                application_id=app_id,
                approved_amount_usd=250_000.0,
                approved_by="orch-real",
            ),
        ],
        expected_version=4,
    )

    # Snapshot real store event count
    real_loan_events_before = await store.load_stream(f"loan-{app_id}")
    real_credit_events_before = await store.load_stream(f"credit-{app_id}")
    total_real_before = len(real_loan_events_before) + len(real_credit_events_before)

    # Counterfactual event: Substitute HIGH risk tier
    cf_event = {
        "event_type": "CreditAnalysisCompleted",
        "event_version": 2,
        "payload": {
            "application_id": app_id,
            "session_id": "sess-cf-high-risk",
            "decision": {
                "risk_tier": "HIGH",
                "recommended_limit_usd": 0,
                "confidence": 0.90,
                "rationale": "High leverage and declining gross margins",
            },
            "model_version": "v2.3-march-risk",
        },
        "metadata": {"correlation_id": "whatif-test"},
    }

    # Run what-if scenario
    result = await run_what_if(
        store=store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[cf_event],
    )

    # 1. Assert divergence was detected
    assert result.divergence_detected is True
    assert result.real_outcome["decision"] == "APPROVE"
    assert result.real_outcome["state"] == "APPROVED"

    # 2. Assert counterfactual produced DECLINE
    assert result.counterfactual_outcome["decision"] == "DECLINE"
    assert result.counterfactual_outcome["state"] == "DECLINED"
    assert result.counterfactual_outcome["risk_tier"] == "HIGH"

    # 3. Assert causal dependency filtering skipped the real ApplicationApproved event
    assert any(
        e.get("event_type") == "ApplicationApproved"
        for e in result.causally_dependent_events_skipped
    )

    # 4. Assert real event store was NEVER mutated
    real_loan_events_after = await store.load_stream(f"loan-{app_id}")
    real_credit_events_after = await store.load_stream(f"credit-{app_id}")
    total_real_after = len(real_loan_events_after) + len(real_credit_events_after)

    assert total_real_before == total_real_after
    assert len(real_credit_events_after) == 1
    assert real_credit_events_after[0]["payload"]["decision"]["risk_tier"] == "MEDIUM"


@pytest.mark.asyncio
async def test_what_if_low_confidence_forces_refer():
    """
    Scenario: Counterfactual event has low confidence (<0.60).
    Expected: Business rule forces recommendation=REFER in counterfactual outcome.
    """
    store = InMemoryEventStore()
    app_id = "APP-WHATIF-002"

    await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="COMP-WHATIF-2",
            requested_amount_usd=100_000,
            loan_purpose="equipment",
        ),
        store,
    )

    cf_event = {
        "event_type": "CreditAnalysisCompleted",
        "event_version": 2,
        "payload": {
            "application_id": app_id,
            "session_id": "sess-cf-low-conf",
            "decision": {
                "risk_tier": "LOW",
                "recommended_limit_usd": 100_000,
                "confidence": 0.52,  # Low confidence < 0.60
            },
            "model_version": "v1",
        },
    }

    result = await run_what_if(
        store=store,
        application_id=app_id,
        branch_at_event_type="CreditAnalysisCompleted",
        counterfactual_events=[cf_event],
    )

    assert result.counterfactual_outcome["decision"] == "REFER"
    assert result.counterfactual_outcome["state"] == "PENDING_HUMAN_REVIEW"
