"""
ledger/commands/handlers.py — Command handlers (load → validate → append pattern)

Version sourcing (intended pattern):
- When an aggregate is loaded for the target stream, use aggregate.version as expected_version.
- Streams without a dedicated aggregate (credit/fraud today): use store.stream_version(stream_id)
  immediately before append (after validation), minimizing the race window; callers may pass
  correlation_id to tie retries to one logical command.

Causal metadata (intended pattern):
- correlation_id: one id per logical command / user request; threaded to every append in that handler.
- causation_id: optional; points to a prior event or command (e.g. chained writes). For multi-append
  handlers, later appends may set causation_id to the prior append's last stream position (string).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import uuid

from ledger.domain.aggregates.loan_application import ApplicationState, LoanApplicationAggregate
from ledger.domain.aggregates.agent_session import AgentSessionAggregate
from ledger.domain.aggregates.compliance_record import ComplianceRecordAggregate
from ledger.domain.regulation import assert_valid_rule_id


def _ev(event_type: str, event_version: int = 1, **payload) -> dict:
    return {"event_type": event_type, "event_version": event_version, "payload": payload}


def _correlation_id(cmd) -> str:
    """One correlation id per command batch; caller may set cmd.correlation_id for tracing."""
    cid = getattr(cmd, "correlation_id", None)
    return cid if cid else str(uuid.uuid4())


def _causation_id(cmd) -> str | None:
    return getattr(cmd, "causation_id", None)


async def _append(
    store,
    stream_id: str,
    events: list,
    *,
    expected_version: int,
    correlation_id: str | None = None,
    causation_id: str | None = None,
):
    """Single append entry point so causal metadata is never dropped."""
    return await store.append(
        stream_id,
        events,
        expected_version=expected_version,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


# ─── Commands (input DTOs) ────────────────────────────────────────────────────


@dataclass
class SubmitApplicationCommand:
    application_id: str
    applicant_id: str
    requested_amount_usd: float
    loan_purpose: str
    submission_channel: str = "api"
    contact_email: str = ""
    contact_name: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class CreditAnalysisCompletedCommand:
    application_id: str
    agent_type: str
    session_id: str
    model_version: str
    risk_tier: str
    recommended_limit_usd: float
    confidence: float
    duration_ms: int
    input_data_hash: str
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class FraudScreeningCompletedCommand:
    application_id: str
    agent_type: str
    session_id: str
    fraud_score: float
    anomaly_flags: list
    model_version: str
    input_data_hash: str
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class StartAgentSessionCommand:
    agent_type: str
    session_id: str
    application_id: str
    model_version: str
    context_source: str = "fresh"
    context_token_count: int = 0
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class ComplianceCheckCommand:
    application_id: str
    rule_id: str
    passed: bool
    session_id: str = "mcp-session"
    failure_reason: str = ""
    is_hard_block: bool = False
    rule_version: str = "2026-Q1-v1"
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class GenerateDecisionCommand:
    application_id: str
    recommendation: str
    confidence: float
    approved_amount_usd: float | None = None
    orchestrator_session_id: str = "mcp-orch"
    agent_type: str = "decision_orchestrator"
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass
class HumanReviewCompletedCommand:
    application_id: str
    reviewer_id: str
    final_decision: str
    override: bool = False
    override_reason: str = ""
    correlation_id: str | None = None
    causation_id: str | None = None


# ─── Handlers ─────────────────────────────────────────────────────────────────


async def handle_submit_application(cmd: SubmitApplicationCommand, store) -> tuple[str, int]:
    """Append ApplicationSubmitted and DocumentUploadRequested to loan stream."""
    stream_id = f"loan-{cmd.application_id}"
    cid = _correlation_id(cmd)
    cause = _causation_id(cmd)
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_can_submit_new_application()

    now = datetime.now(timezone.utc)
    events = [
        _ev(
            "ApplicationSubmitted",
            application_id=cmd.application_id,
            applicant_id=cmd.applicant_id,
            requested_amount_usd=float(cmd.requested_amount_usd),
            loan_purpose=cmd.loan_purpose,
            loan_term_months=36,
            submission_channel=cmd.submission_channel,
            contact_email=cmd.contact_email or "noreply@apex.example",
            contact_name=cmd.contact_name or "Applicant",
            submitted_at=now.isoformat(),
            application_reference=cmd.application_id,
        ),
        _ev(
            "DocumentUploadRequested",
            application_id=cmd.application_id,
            required_document_types=["application_proposal", "income_statement", "balance_sheet"],
            deadline=(now + timedelta(days=7)).isoformat(),
            requested_by="system",
        ),
    ]
    await _append(
        store,
        stream_id,
        events,
        expected_version=app.version,
        correlation_id=cid,
        causation_id=cause,
    )
    return stream_id, 1


async def handle_credit_analysis_completed(cmd: CreditAnalysisCompletedCommand, store) -> tuple[str, int]:
    """Append CreditAnalysisCompleted to credit stream. Validates LoanApplication and AgentSession."""
    cid = _correlation_id(cmd)
    cause = _causation_id(cmd)
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_awaiting_credit_analysis()

    agent = await AgentSessionAggregate.load(store, cmd.agent_type, cmd.session_id)
    agent.assert_context_loaded()
    agent.assert_model_version_current(cmd.model_version)

    stream_id = f"credit-{cmd.application_id}"
    decision = {
        "risk_tier": cmd.risk_tier,
        "recommended_limit_usd": float(cmd.recommended_limit_usd),
        "confidence": cmd.confidence,
        "rationale": "",
        "key_concerns": [],
        "data_quality_caveats": [],
        "policy_overrides_applied": [],
    }
    new_event = _ev(
        "CreditAnalysisCompleted",
        event_version=2,
        application_id=cmd.application_id,
        session_id=cmd.session_id,
        decision=decision,
        model_version=cmd.model_version,
        model_deployment_id="default",
        input_data_hash=cmd.input_data_hash,
        analysis_duration_ms=cmd.duration_ms,
        regulatory_basis=[],
        completed_at=datetime.now(timezone.utc).isoformat(),
    )

    # No CreditRecord aggregate yet: expected_version = store.stream_version immediately before append.
    version = await store.stream_version(stream_id)
    positions = await _append(
        store,
        stream_id,
        [new_event],
        expected_version=version,
        correlation_id=cid,
        causation_id=cause,
    )
    return stream_id, positions[-1]


async def handle_fraud_screening_completed(cmd: FraudScreeningCompletedCommand, store) -> tuple[str, int]:
    """Append FraudScreeningCompleted to fraud stream."""
    cid = _correlation_id(cmd)
    cause = _causation_id(cmd)
    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_awaiting_fraud_screening()

    agent = await AgentSessionAggregate.load(store, cmd.agent_type, cmd.session_id)
    agent.assert_context_loaded()

    stream_id = f"fraud-{cmd.application_id}"
    new_event = _ev(
        "FraudScreeningCompleted",
        application_id=cmd.application_id,
        session_id=cmd.session_id,
        fraud_score=cmd.fraud_score,
        risk_level="HIGH" if cmd.fraud_score > 0.7 else "LOW",
        anomalies_found=len(cmd.anomaly_flags or []),
        recommendation="PASS" if cmd.fraud_score < 0.5 else "REVIEW",
        screening_model_version=cmd.model_version,
        input_data_hash=cmd.input_data_hash,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    version = await store.stream_version(stream_id)
    positions = await _append(
        store,
        stream_id,
        [new_event],
        expected_version=version,
        correlation_id=cid,
        causation_id=cause,
    )
    return stream_id, positions[-1]


async def handle_start_agent_session(cmd: StartAgentSessionCommand, store) -> tuple[str, int]:
    """Append AgentSessionStarted — Gas Town: required before any agent decision."""
    cid = _correlation_id(cmd)
    cause = _causation_id(cmd)
    stream_id = f"agent-{cmd.agent_type}-{cmd.session_id}"
    agent = await AgentSessionAggregate.load(store, cmd.agent_type, cmd.session_id)
    agent.assert_session_not_started()

    new_event = _ev(
        "AgentSessionStarted",
        session_id=cmd.session_id,
        agent_type=cmd.agent_type,
        agent_id=f"{cmd.agent_type}-agent",
        application_id=cmd.application_id,
        model_version=cmd.model_version,
        langgraph_graph_version="1.0",
        context_source=cmd.context_source,
        context_token_count=cmd.context_token_count,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    positions = await _append(
        store,
        stream_id,
        [new_event],
        expected_version=agent.version,
        correlation_id=cid,
        causation_id=cause,
    )
    return stream_id, positions[-1]


async def handle_compliance_check(cmd: ComplianceCheckCommand, store) -> tuple[str, int]:
    """Append ComplianceRulePassed or ComplianceRuleFailed to compliance stream."""
    cid = _correlation_id(cmd)
    cause = _causation_id(cmd)

    assert_valid_rule_id(cmd.rule_id)

    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_awaiting_compliance()

    stream_id = f"compliance-{cmd.application_id}"
    comp = await ComplianceRecordAggregate.load(store, cmd.application_id)
    version = comp.version

    events = []
    if version < 0:
        events.append(
            _ev(
                "ComplianceCheckInitiated",
                application_id=cmd.application_id,
                regulation_set_version=cmd.rule_version,
                rules_to_evaluate=[cmd.rule_id],
                initiated_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    if cmd.passed:
        events.append(
            _ev(
                "ComplianceRulePassed",
                application_id=cmd.application_id,
                session_id=cmd.session_id,
                rule_id=cmd.rule_id,
                rule_name=cmd.rule_id,
                rule_version=cmd.rule_version,
                evidence_hash="",
                evaluation_notes="",
                evaluated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    else:
        evt = _ev(
            "ComplianceRuleFailed",
            application_id=cmd.application_id,
            session_id=cmd.session_id,
            rule_id=cmd.rule_id,
            rule_name=cmd.rule_id,
            rule_version=cmd.rule_version,
            failure_reason=cmd.failure_reason or f"Rule {cmd.rule_id} failed",
            is_hard_block=cmd.is_hard_block,
            evidence_hash="",
            evaluated_at=datetime.now(timezone.utc).isoformat(),
        )
        events.append(evt)
        if cmd.is_hard_block:
            loan_stream = f"loan-{cmd.application_id}"
            loan_positions = await _append(
                store,
                loan_stream,
                [
                    _ev("ComplianceRuleFailed", application_id=cmd.application_id, rule_id=cmd.rule_id, is_hard_block=True),
                    _ev(
                        "ApplicationDeclined",
                        application_id=cmd.application_id,
                        decline_reasons=[f"Compliance hard block: {cmd.rule_id}"],
                        adverse_action_codes=["COMPLIANCE_BLOCK"],
                        declined_at=datetime.now(timezone.utc).isoformat(),
                    ),
                ],
                expected_version=app.version,
                correlation_id=cid,
                causation_id=cause,
            )
            cause = f"{loan_stream}:{loan_positions[-1]}"

    exp = -1 if version < 0 else version
    positions = await _append(
        store,
        stream_id,
        events,
        expected_version=exp,
        correlation_id=cid,
        causation_id=cause,
    )
    return stream_id, positions[-1]


async def handle_generate_decision(cmd: GenerateDecisionCommand, store) -> tuple[str, int]:
    """Append DecisionGenerated; optionally ApplicationApproved/Declined or HumanReviewRequested."""
    cid = _correlation_id(cmd)
    cause = _causation_id(cmd)

    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_states_allowed_for_generate_decision()
    comp = await ComplianceRecordAggregate.load(store, cmd.application_id)
    comp.assert_ready_for_decision_generation()
    app.assert_decision_recommendation_and_confidence(cmd.recommendation, cmd.confidence)

    rec = cmd.recommendation.strip().upper()

    stream_id = f"loan-{cmd.application_id}"
    version = app.version

    events = []
    if app.state == ApplicationState.COMPLIANCE_CHECK_REQUESTED:
        events.append(
            _ev(
                "DecisionRequested",
                application_id=cmd.application_id,
                requested_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    events.extend([
        _ev(
            "DecisionGenerated",
            event_version=2,
            application_id=cmd.application_id,
            orchestrator_session_id=cmd.orchestrator_session_id,
            recommendation=rec,
            confidence=cmd.confidence,
            approved_amount_usd=str(cmd.approved_amount_usd or 0) if cmd.approved_amount_usd else None,
            conditions=[],
            executive_summary="",
            key_risks=[],
            contributing_sessions=[],
            model_versions={"orchestrator": cmd.agent_type},
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
    ])

    if rec in ("APPROVE", "APPROVED"):
        amt = cmd.approved_amount_usd or app.requested_amount_usd or 0
        events.append(
            _ev(
                "ApplicationApproved",
                application_id=cmd.application_id,
                approved_amount_usd=float(amt),
                approved_by=cmd.orchestrator_session_id,
                approved_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    elif rec in ("DECLINE", "DECLINED"):
        events.append(
            _ev(
                "ApplicationDeclined",
                application_id=cmd.application_id,
                decline_reasons=[],
                adverse_action_codes=[],
                declined_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    else:
        events.append(
            _ev(
                "HumanReviewRequested",
                application_id=cmd.application_id,
                reason="REFER",
                requested_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    positions = await _append(
        store,
        stream_id,
        events,
        expected_version=version,
        correlation_id=cid,
        causation_id=cause,
    )
    return stream_id, positions[-1]


async def handle_human_review_completed(cmd: HumanReviewCompletedCommand, store) -> tuple[str, int]:
    """Append HumanReviewCompleted and ApplicationApproved/Declined."""
    cid = _correlation_id(cmd)
    cause = _causation_id(cmd)

    app = await LoanApplicationAggregate.load(store, cmd.application_id)
    app.assert_pending_human_review()
    app.assert_human_review_command(cmd.final_decision, cmd.override, cmd.override_reason or "")

    stream_id = f"loan-{cmd.application_id}"
    version = app.version

    dec = cmd.final_decision.strip().upper()

    events = [
        _ev(
            "HumanReviewCompleted",
            application_id=cmd.application_id,
            reviewer_id=cmd.reviewer_id,
            final_decision=dec,
            override=cmd.override,
            override_reason=cmd.override_reason if cmd.override else None,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    ]

    if dec in ("APPROVE", "APPROVED"):
        events.append(
            _ev(
                "ApplicationApproved",
                application_id=cmd.application_id,
                approved_amount_usd=float(app.approved_amount_usd or app.requested_amount_usd or 0),
                approved_by=cmd.reviewer_id,
                approved_at=datetime.now(timezone.utc).isoformat(),
            )
        )
    else:
        events.append(
            _ev(
                "ApplicationDeclined",
                application_id=cmd.application_id,
                decline_reasons=["Human review decline"],
                adverse_action_codes=[],
                declined_at=datetime.now(timezone.utc).isoformat(),
            )
        )

    positions = await _append(
        store,
        stream_id,
        events,
        expected_version=version,
        correlation_id=cid,
        causation_id=cause,
    )
    return stream_id, positions[-1]
